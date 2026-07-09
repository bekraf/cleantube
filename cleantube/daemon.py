import logging
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import Database
from .subscriptions import channel_videos_url, extract_handle, read_subscriptions
from .ytdlp import (
    UnavailableError,
    VideoMeta,
    build_output_path,
    download_video,
    fetch_channel_video_ids,
    fetch_video_metadata,
)

log = logging.getLogger("cleantube")


def _log_ytdlp_failure(event: str, e: Exception, **fields) -> None:
    """Expected unavailability (members-only, age-restricted or deleted
    videos, upcoming premieres, channels without a videos tab) is logged as a
    WARNING with a `reason` field; everything else is a real ERROR."""
    extra = {**fields, "error": str(e)}
    if isinstance(e, UnavailableError):
        extra["reason"] = e.reason
        log.warning(event, extra=extra)
    else:
        log.error(event, extra=extra)

# Newest-first feed entries to scan per channel per cycle. Bounds the work on
# huge channels; anything beyond this within one poll interval is beyond a
# realistic upload rate.
_FEED_SCAN_LIMIT = 50

# Seconds between queue checks while it yields nothing. New videos arrive via
# channel scans, but a video held back after a failed attempt becomes eligible
# again on its own (see Database.next_pending_video), so the queue must be
# re-polled between scans too.
_QUEUE_RECHECK_SECONDS = 60.0

# When a download hits a premiere that has not aired yet (rescheduled, or the
# computed availability was off), push it back this far instead of burning a
# retry attempt.
_PREMIERE_DEFER_SECONDS = 3600


class Daemon:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._shutdown = threading.Event()
        self._current_proc: subprocess.Popen | None = None

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum: int, _frame) -> None:
        # First signal: graceful — let the in-flight download finish.
        # Second signal: the user insists; abandon the download.
        if self._shutdown.is_set():
            proc = self._current_proc
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._shutdown.set()

    def _register_process(self, proc: subprocess.Popen | None) -> None:
        self._current_proc = proc

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep up to `seconds`. Returns True if shutdown was requested."""
        return self._shutdown.wait(seconds)

    def run(self) -> None:
        self.install_signal_handlers()
        log.info("daemon_start")
        # Scanning and downloading run interleaved on independent deadlines:
        # scans enqueue pending videos, the queue is drained one video at a
        # time with post_download_cooldown_seconds of spacing. Deadlines are
        # on the monotonic clock; 0.0 means "due now", so the first scan
        # happens immediately on startup.
        next_scan = 0.0
        next_download = 0.0
        try:
            while not self._shutdown.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    try:
                        self._run_cycle()
                    except Exception as e:
                        log.exception("cycle_error", extra={"error": str(e)})
                    next_scan = time.monotonic() + self.config.poll_interval_seconds
                    log.info(
                        "cycle_complete",
                        extra={
                            "sleep_seconds": self.config.poll_interval_seconds,
                            "queue_size": self.db.pending_count(),
                        },
                    )
                    continue
                if now >= next_download:
                    success = self._download_next()
                    if self._shutdown.is_set():
                        break
                    if success is None:
                        # Queue empty (or everything is in retry hold-back).
                        next_download = time.monotonic() + _QUEUE_RECHECK_SECONDS
                    elif success:
                        log.info(
                            "download_cooldown",
                            extra={
                                "sleep_seconds":
                                    self.config.post_download_cooldown_seconds
                            },
                        )
                        next_download = (
                            time.monotonic()
                            + self.config.post_download_cooldown_seconds
                        )
                    # No cooldown after a failure: the failed video is held
                    # back by the queue itself, the next one is tried at once.
                    continue
                wait = min(next_scan, next_download) - time.monotonic()
                if wait > 0 and self._interruptible_sleep(wait):
                    break
        finally:
            log.info("daemon_stop")

    def _run_cycle(self) -> None:
        urls = read_subscriptions(self.config.subscriptions_path)
        log.info("cycle_start", extra={"channel_count": len(urls)})
        for channel_url in urls:
            if self._shutdown.is_set():
                return
            try:
                self._process_channel(channel_url)
            except Exception as e:
                log.exception(
                    "channel_error",
                    extra={"channel_url": channel_url, "error": str(e)},
                )

    def _process_channel(self, channel_url: str) -> None:
        # /channel/UC... URLs carry no handle; check the DB before falling
        # back to metadata discovery so the yt-dlp round-trip happens only
        # the first time a channel is seen.
        handle = extract_handle(channel_url) or self.db.handle_for_url(channel_url)
        if handle:
            self.db.upsert_channel(handle, channel_url)

        videos_url = channel_videos_url(channel_url)
        log.info("channel_fetch_ids", extra={"channel_url": videos_url})
        try:
            ids = fetch_channel_video_ids(
                videos_url, limit=max(_FEED_SCAN_LIMIT, self.config.backfill_count)
            )
        except Exception as e:
            _log_ytdlp_failure("channel_fetch_failed", e, channel_url=videos_url)
            return

        log.info(
            "channel_ids_fetched",
            extra={"channel": handle, "channel_url": channel_url, "id_count": len(ids)},
        )

        if not ids:
            if handle:
                self.db.mark_channel_checked(handle)
            return

        newest_meta: VideoMeta | None = None
        if handle is None:
            # Learn the handle from the most recent video's metadata.
            try:
                newest_meta = fetch_video_metadata(ids[0])
            except Exception as e:
                _log_ytdlp_failure(
                    "handle_discovery_failed", e, channel_url=channel_url
                )
                return
            handle = newest_meta.channel_handle
            if handle is None:
                log.warning(
                    "handle_unresolved", extra={"channel_url": channel_url}
                )
                return
            self.db.upsert_channel(handle, channel_url)

        def fetch_meta(vid: str) -> VideoMeta:
            if newest_meta is not None and newest_meta.video_id == vid:
                return newest_meta
            return fetch_video_metadata(vid)

        watermark = self.db.watermark(handle)

        if watermark is None and self.config.backfill_count == 0:
            self._set_baseline(handle, ids[0], fetch_meta)
            return

        candidates: list[str] = []
        newest_seen = ""

        if watermark is None:
            # First time seeing this channel: backfill the newest uploads.
            for vid in ids[: self.config.backfill_count]:
                if self._shutdown.is_set():
                    return
                try:
                    meta = fetch_meta(vid)
                except Exception as e:
                    _log_ytdlp_failure(
                        "video_meta_failed", e, channel=handle, video_id=vid
                    )
                    continue
                if not meta.upload_date:
                    log.warning(
                        "video_missing_upload_date",
                        extra={"channel": handle, "video_id": vid},
                    )
                    continue
                self.db.insert_pending_video(
                    video_id=meta.video_id,
                    channel_handle=handle,
                    title=meta.title,
                    upload_date=meta.upload_date,
                    duration_seconds=meta.duration_seconds,
                    available_at=meta.available_at,
                )
                if meta.available_at is None:
                    newest_seen = max(newest_seen, meta.upload_date)
                candidates.append(meta.video_id)
            log.info(
                "channel_first_seen",
                extra={"channel": handle, "backfill_count": len(candidates)},
            )
        else:
            # Channel /videos feeds are newest-first. We walk it from the top,
            # enqueueing every unknown ID uploaded on or after the watermark,
            # and stop at the first unknown ID that is strictly older
            # (everything after it is older still). Upload dates only have day
            # resolution, so "on or after" rather than "strictly newer": a
            # second video published the same day as the watermark would
            # otherwise be missed forever.
            for vid in ids:
                if self._shutdown.is_set():
                    return
                status = self.db.video_status(vid)
                if status in ("downloaded", "permanently_failed", "skipped"):
                    continue
                if status == "pending":
                    candidates.append(vid)
                    continue
                try:
                    meta = fetch_meta(vid)
                except Exception as e:
                    _log_ytdlp_failure(
                        "video_meta_failed", e, channel=handle, video_id=vid
                    )
                    continue
                if not meta.upload_date:
                    continue
                if meta.upload_date < watermark:
                    break
                self.db.insert_pending_video(
                    video_id=meta.video_id,
                    channel_handle=handle,
                    title=meta.title,
                    upload_date=meta.upload_date,
                    duration_seconds=meta.duration_seconds,
                    available_at=meta.available_at,
                )
                # An upcoming premiere must not advance the watermark: its
                # scheduled date lies ahead and would wall off normal uploads
                # published before it airs.
                if meta.available_at is None:
                    newest_seen = max(newest_seen, meta.upload_date)
                candidates.append(meta.video_id)
            log.info(
                "channel_candidates",
                extra={"channel": handle, "candidate_count": len(candidates)},
            )

        if newest_seen:
            self.db.advance_watermark(handle, newest_seen)
        self.db.mark_channel_checked(handle)

    def _set_baseline(self, handle: str, newest_id: str, fetch_meta) -> None:
        """Follow-only mode (backfill_count = 0): record the newest upload as
        the watermark and download nothing that exists today. The video also
        gets a 'skipped' row so the on-or-after date rule can never pull the
        baseline video itself in later."""
        try:
            meta = fetch_meta(newest_id)
        except Exception as e:
            _log_ytdlp_failure(
                "video_meta_failed", e, channel=handle, video_id=newest_id
            )
            return
        if not meta.upload_date:
            log.warning(
                "video_missing_upload_date",
                extra={"channel": handle, "video_id": meta.video_id},
            )
            return
        if meta.available_at is not None:
            # The newest video is an unaired premiere; using it as baseline
            # would put the watermark in the future. Try again next cycle.
            log.info(
                "channel_baseline_deferred",
                extra={"channel": handle, "video_id": meta.video_id},
            )
            return
        self.db.insert_skipped_video(
            video_id=meta.video_id,
            channel_handle=handle,
            title=meta.title,
            upload_date=meta.upload_date,
            duration_seconds=meta.duration_seconds,
        )
        self.db.advance_watermark(handle, meta.upload_date)
        self.db.mark_channel_checked(handle)
        log.info(
            "channel_baseline_set",
            extra={"channel": handle, "baseline_date": meta.upload_date},
        )

    def _download_next(self) -> bool | None:
        """Download the next eligible queued video. Returns True on success,
        False on failure, None when the queue yields nothing. Videos that
        failed recently are held back for one poll interval, preserving the
        old retry-once-per-cycle cadence."""
        row = self.db.next_pending_video(
            retry_delay_seconds=self.config.poll_interval_seconds
        )
        if row is None:
            return None
        return self._download_one(row["channel_handle"], row["video_id"])

    def _download_one(self, channel_handle: str, video_id: str) -> bool:
        row = self.db.get_video(video_id)
        if row is None:
            log.warning(
                "download_skipped_missing_row",
                extra={"channel": channel_handle, "video_id": video_id},
            )
            return False

        title = row["title"]
        upload_date = row["upload_date"]
        output_path = build_output_path(
            self.config.download_dir, channel_handle, upload_date, title, video_id
        )

        log.info(
            "download_start",
            extra={
                "channel": channel_handle,
                "video_id": video_id,
                "title": title,
                "output_path": str(output_path),
            },
        )

        try:
            result = download_video(
                video_id=video_id,
                output_path=output_path,
                video_format=self.config.video_format,
                register_process=self._register_process,
            )
        except Exception as e:
            if isinstance(e, UnavailableError) and e.reason == "premiere":
                # Not aired yet after all (rescheduled, or the computed
                # availability was off): defer instead of burning an attempt.
                available_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=_PREMIERE_DEFER_SECONDS)
                ).isoformat()
                self.db.set_available_at(video_id, available_at)
                log.warning(
                    "download_deferred",
                    extra={
                        "channel": channel_handle,
                        "video_id": video_id,
                        "reason": e.reason,
                        "available_at": available_at,
                    },
                )
                return False
            attempts = self.db.record_failure(video_id, str(e))
            _log_ytdlp_failure(
                "download_failed", e,
                channel=channel_handle,
                video_id=video_id,
                attempt_count=attempts,
            )
            if attempts >= self.config.max_download_attempts:
                self.db.mark_permanently_failed(video_id)
                log.warning(
                    "video_permanently_failed",
                    extra={
                        "channel": channel_handle,
                        "video_id": video_id,
                        "attempt_count": attempts,
                    },
                )
            return False

        self.db.mark_downloaded(
            video_id=video_id,
            filepath=result.filepath,
            file_size_bytes=result.file_size_bytes,
            sponsorblock_cuts=result.sponsorblock_cuts,
        )
        log.info(
            "download_success",
            extra={
                "channel": channel_handle,
                "video_id": video_id,
                "filepath": str(result.filepath),
                "file_size_bytes": result.file_size_bytes,
                "sponsorblock_cuts": result.sponsorblock_cuts,
            },
        )
        return True
