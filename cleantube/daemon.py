import logging
import signal
import subprocess
import threading

from .config import Config
from .db import Database
from .subscriptions import channel_videos_url, extract_handle, read_subscriptions
from .ytdlp import (
    build_output_path,
    download_video,
    fetch_channel_video_ids,
    fetch_video_metadata,
)

log = logging.getLogger("cleantube")

# Newest-first feed entries to scan per channel per cycle. Bounds the work on
# huge channels; anything beyond this within one poll interval is beyond a
# realistic upload rate.
_FEED_SCAN_LIMIT = 50


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
        try:
            while not self._shutdown.is_set():
                try:
                    self._run_cycle()
                except Exception as e:
                    log.exception("cycle_error", extra={"error": str(e)})
                if self._shutdown.is_set():
                    break
                log.info(
                    "cycle_complete",
                    extra={"sleep_seconds": self.config.poll_interval_seconds},
                )
                if self._interruptible_sleep(self.config.poll_interval_seconds):
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
        handle = extract_handle(channel_url)
        if handle:
            self.db.upsert_channel(handle, channel_url)

        videos_url = channel_videos_url(channel_url)
        log.info("channel_fetch_ids", extra={"channel_url": videos_url})
        try:
            ids = fetch_channel_video_ids(
                videos_url, limit=max(_FEED_SCAN_LIMIT, self.config.backfill_count)
            )
        except Exception as e:
            log.error(
                "channel_fetch_failed",
                extra={"channel_url": videos_url, "error": str(e)},
            )
            return

        log.info(
            "channel_ids_fetched",
            extra={"channel": handle, "channel_url": channel_url, "id_count": len(ids)},
        )

        if not ids:
            if handle:
                self.db.mark_channel_checked(handle)
            return

        if handle is None:
            # Learn the handle from the most recent video's metadata.
            try:
                meta = fetch_video_metadata(ids[0])
            except Exception as e:
                log.error(
                    "handle_discovery_failed",
                    extra={"channel_url": channel_url, "error": str(e)},
                )
                return
            handle = meta.channel_handle
            if handle is None:
                log.warning(
                    "handle_unresolved", extra={"channel_url": channel_url}
                )
                return
            self.db.upsert_channel(handle, channel_url)

        candidates: list[str] = []
        first_time = not self.db.channel_has_any_videos(handle)

        if first_time:
            for vid in ids[: self.config.backfill_count]:
                if self._shutdown.is_set():
                    return
                try:
                    meta = fetch_video_metadata(vid)
                except Exception as e:
                    log.error(
                        "video_meta_failed",
                        extra={"channel": handle, "video_id": vid, "error": str(e)},
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
                )
                candidates.append(meta.video_id)
            log.info(
                "channel_first_seen",
                extra={"channel": handle, "backfill_count": len(candidates)},
            )
        else:
            most_recent = self.db.most_recent_upload_date(handle) or ""
            # Channel /videos feeds are newest-first. We walk it from the top,
            # enqueueing every unknown ID uploaded on or after most_recent, and
            # stop at the first unknown ID that is strictly older (everything
            # after it is older still). Upload dates only have day resolution,
            # so "on or after" rather than "strictly newer": a second video
            # published the same day as the last download would otherwise be
            # missed forever.
            for vid in ids:
                if self._shutdown.is_set():
                    return
                status = self.db.video_status(vid)
                if status in ("downloaded", "permanently_failed"):
                    continue
                if status == "pending":
                    candidates.append(vid)
                    continue
                try:
                    meta = fetch_video_metadata(vid)
                except Exception as e:
                    log.error(
                        "video_meta_failed",
                        extra={"channel": handle, "video_id": vid, "error": str(e)},
                    )
                    continue
                if not meta.upload_date:
                    continue
                if most_recent and meta.upload_date < most_recent:
                    break
                self.db.insert_pending_video(
                    video_id=meta.video_id,
                    channel_handle=handle,
                    title=meta.title,
                    upload_date=meta.upload_date,
                    duration_seconds=meta.duration_seconds,
                )
                candidates.append(meta.video_id)
            log.info(
                "channel_candidates",
                extra={"channel": handle, "candidate_count": len(candidates)},
            )

        self.db.mark_channel_checked(handle)

        for vid in candidates:
            if self._shutdown.is_set():
                return
            status = self.db.video_status(vid)
            if status in ("downloaded", "permanently_failed"):
                continue
            self._download_one(handle, vid)

    def _download_one(self, channel_handle: str, video_id: str) -> None:
        row = self.db.get_video(video_id)
        if row is None:
            log.warning(
                "download_skipped_missing_row",
                extra={"channel": channel_handle, "video_id": video_id},
            )
            return

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
            attempts = self.db.record_failure(video_id, str(e))
            log.error(
                "download_failed",
                extra={
                    "channel": channel_handle,
                    "video_id": video_id,
                    "attempt_count": attempts,
                    "error": str(e),
                },
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
            return  # No cooldown after failure.

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

        if self._shutdown.is_set():
            return
        log.info(
            "download_cooldown",
            extra={"sleep_seconds": self.config.post_download_cooldown_seconds},
        )
        self._interruptible_sleep(self.config.post_download_cooldown_seconds)
