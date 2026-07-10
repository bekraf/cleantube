import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cleantube.daemon as daemon_mod
from cleantube.config import Config
from cleantube.daemon import Daemon
from cleantube.db import Database
from cleantube.ytdlp import DownloadResult, UnavailableError, VideoMeta

CHANNEL_URL = "https://www.youtube.com/@chan"


def make_config(tmp_path, **overrides) -> Config:
    values = dict(
        backfill_count=3,
        poll_interval_seconds=3600,
        post_download_cooldown_seconds=0,
        download_dir=tmp_path / "downloaded",
        video_format="best",
        max_download_attempts=3,
        db_path=tmp_path / "test.db",
        subscriptions_path=tmp_path / "subscriptions.txt",
        web_enabled=False,
        web_host="127.0.0.1",
        web_port=0,
    )
    values.update(overrides)
    return Config(**values)


def make_meta(
    video_id: str, upload_date: str, available_at: str | None = None
) -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        title=f"title {video_id}",
        upload_date=upload_date,
        duration_seconds=60,
        channel_handle="@chan",
        available_at=available_at,
    )


def drain_queue(daemon: Daemon) -> None:
    """Work the download queue until it yields nothing, like the daemon's
    run loop would (minus the spacing between downloads)."""
    while daemon._download_next() is not None:
        pass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Daemon wired to a real temp DB with the yt-dlp layer faked out."""
    config = make_config(tmp_path)
    db = Database(config.db_path)
    daemon = Daemon(config, db)

    feed: list[str] = []
    metas: dict[str, VideoMeta] = {}
    downloaded: list[str] = []
    meta_fetches: list[str] = []

    monkeypatch.setattr(
        daemon_mod, "fetch_channel_video_ids", lambda url, limit: feed[:limit]
    )

    def fake_fetch_meta(video_id):
        meta_fetches.append(video_id)
        return metas[video_id]

    monkeypatch.setattr(daemon_mod, "fetch_video_metadata", fake_fetch_meta)

    def fake_download(*, video_id, output_path, video_format, register_process):
        downloaded.append(video_id)
        return DownloadResult(
            filepath=output_path, file_size_bytes=123, sponsorblock_cuts=1
        )

    monkeypatch.setattr(daemon_mod, "download_video", fake_download)

    yield daemon, db, feed, metas, downloaded, meta_fetches
    db.close()


def test_first_sight_backfills_most_recent(harness):
    daemon, db, feed, metas, downloaded, _ = harness
    feed.extend(["v1", "v2", "v3", "v4", "v5"])
    metas.update(
        {
            "v1": make_meta("v1", "2026-07-01"),
            "v2": make_meta("v2", "2026-06-20"),
            "v3": make_meta("v3", "2026-06-10"),
        }
    )
    daemon._process_channel(CHANNEL_URL)
    # Scanning only enqueues; nothing is downloaded until the queue runs.
    assert downloaded == []
    assert db.video_status("v1") == "pending"
    drain_queue(daemon)
    # The queue downloads oldest-first.
    assert downloaded == ["v3", "v2", "v1"]
    assert db.video_status("v1") == "downloaded"
    assert db.video_status("v4") is None
    assert db.watermark("@chan") == "2026-07-01"


def test_new_upload_detected_on_subsequent_run(harness):
    daemon, db, feed, metas, downloaded, meta_fetches = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="old",
        channel_handle="@chan",
        title="old",
        upload_date="2026-06-01",
        duration_seconds=60,
    )
    db.mark_downloaded(
        video_id="old", filepath=Path("/dl/old.mp4"), file_size_bytes=1,
        sponsorblock_cuts=0,
    )
    db.advance_watermark("@chan", "2026-06-01")

    feed.extend(["new", "old", "ancient"])
    metas["new"] = make_meta("new", "2026-07-01")
    metas["ancient"] = make_meta("ancient", "2026-01-01")

    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == ["new"]
    # Walk stops at the first unknown-and-older video; "old" is known so it
    # never needs a metadata fetch.
    assert meta_fetches == ["new", "ancient"]
    assert db.watermark("@chan") == "2026-07-01"


def test_same_day_upload_is_not_missed(harness):
    # Upload dates have day resolution: a second video published on the same
    # day as the newest downloaded one must still be picked up.
    daemon, db, feed, metas, downloaded, _ = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="a",
        channel_handle="@chan",
        title="a",
        upload_date="2026-07-01",
        duration_seconds=60,
    )
    db.mark_downloaded(
        video_id="a", filepath=Path("/dl/a.mp4"), file_size_bytes=1,
        sponsorblock_cuts=0,
    )
    db.advance_watermark("@chan", "2026-07-01")

    feed.extend(["b", "a"])
    metas["b"] = make_meta("b", "2026-07-01")  # same day as "a"

    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == ["b"]


def test_pending_video_is_retried(harness):
    daemon, db, feed, metas, downloaded, _ = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="stuck",
        channel_handle="@chan",
        title="stuck",
        upload_date="2026-06-01",
        duration_seconds=60,
    )
    db.advance_watermark("@chan", "2026-06-01")
    feed.append("stuck")

    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == ["stuck"]
    assert db.video_status("stuck") == "downloaded"


def test_permanently_failed_is_never_retried(harness):
    daemon, db, feed, metas, downloaded, meta_fetches = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="dead",
        channel_handle="@chan",
        title="dead",
        upload_date="2026-06-01",
        duration_seconds=60,
    )
    db.mark_permanently_failed("dead")
    db.advance_watermark("@chan", "2026-06-01")
    feed.append("dead")

    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == []
    assert meta_fetches == []


def test_backfill_zero_sets_baseline_and_follows(tmp_path, monkeypatch):
    # Follow-only mode: nothing that exists at subscribe time is downloaded;
    # only uploads that appear after the baseline are.
    config = make_config(tmp_path, backfill_count=0)
    db = Database(config.db_path)
    daemon = Daemon(config, db)

    feed = ["v1", "v0"]
    metas = {
        "v1": make_meta("v1", "2026-07-01"),
        "v0": make_meta("v0", "2026-06-01"),
    }
    downloaded: list[str] = []
    monkeypatch.setattr(
        daemon_mod, "fetch_channel_video_ids", lambda url, limit: feed[:limit]
    )
    monkeypatch.setattr(daemon_mod, "fetch_video_metadata", lambda vid: metas[vid])

    def fake_download(*, video_id, output_path, video_format, register_process):
        downloaded.append(video_id)
        return DownloadResult(
            filepath=output_path, file_size_bytes=1, sponsorblock_cuts=0
        )

    monkeypatch.setattr(daemon_mod, "download_video", fake_download)

    # First sight: baseline recorded, nothing downloaded.
    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == []
    assert db.watermark("@chan") == "2026-07-01"
    assert db.video_status("v1") == "skipped"

    # Second cycle, nothing new: the baseline video must not be pulled in by
    # the on-or-after date rule.
    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == []

    # A new upload appears: only that one is downloaded.
    feed.insert(0, "v2")
    metas["v2"] = make_meta("v2", "2026-07-02")
    daemon._process_channel(CHANNEL_URL)
    drain_queue(daemon)
    assert downloaded == ["v2"]
    assert db.watermark("@chan") == "2026-07-02"
    db.close()


def test_channel_url_handle_cached_after_discovery(harness):
    # /channel/UC... URLs carry no handle. The first cycle discovers it via a
    # metadata fetch; later cycles must reuse the handle stored in the DB.
    daemon, db, feed, metas, downloaded, meta_fetches = harness
    url = "https://www.youtube.com/channel/UCabc123"
    feed.append("v1")
    metas["v1"] = make_meta("v1", "2026-07-01")

    daemon._process_channel(url)
    drain_queue(daemon)
    assert downloaded == ["v1"]
    assert db.handle_for_url(url) == "@chan"
    fetches_after_first = len(meta_fetches)

    daemon._process_channel(url)
    assert len(meta_fetches) == fetches_after_first


def test_download_failure_marks_permanent_after_max_attempts(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path, max_download_attempts=2)
    db = Database(config.db_path)
    daemon = Daemon(config, db)
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="flaky",
        channel_handle="@chan",
        title="flaky",
        upload_date="2026-06-01",
        duration_seconds=60,
    )

    def fail_download(**kwargs):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(daemon_mod, "download_video", fail_download)

    daemon._download_one("@chan", "flaky")
    row = db.get_video("flaky")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert "exploded" in row["last_error"]

    daemon._download_one("@chan", "flaky")
    assert db.video_status("flaky") == "permanently_failed"
    db.close()


def test_download_next_empty_queue_returns_none(harness):
    daemon, db, feed, metas, downloaded, _ = harness
    assert daemon._download_next() is None
    assert downloaded == []


def test_failed_download_is_held_back_from_queue(tmp_path, monkeypatch):
    # A failure re-enters the queue only after a poll interval, so a flaky
    # video is retried once per cycle instead of hammering yt-dlp in a loop.
    config = make_config(tmp_path)
    db = Database(config.db_path)
    daemon = Daemon(config, db)
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="flaky",
        channel_handle="@chan",
        title="flaky",
        upload_date="2026-06-01",
        duration_seconds=60,
    )

    def fail_download(**kwargs):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(daemon_mod, "download_video", fail_download)

    assert daemon._download_next() is False
    assert db.video_status("flaky") == "pending"
    assert daemon._download_next() is None
    db.close()


def test_premiere_is_gated_until_available(harness):
    daemon, db, feed, metas, downloaded, _ = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.advance_watermark("@chan", "2026-06-01")
    feed.append("prem")
    airs_at = (datetime.now(timezone.utc) + timedelta(hours=9)).isoformat()
    metas["prem"] = make_meta("prem", "2026-07-10", available_at=airs_at)

    daemon._process_channel(CHANNEL_URL)
    row = db.get_video("prem")
    assert row["status"] == "pending"
    assert row["available_at"] == airs_at
    # An unaired premiere must not advance the watermark: its future date
    # would wall off normal uploads published before it airs.
    assert db.watermark("@chan") == "2026-06-01"

    # The queue does not offer it before its availability moment.
    drain_queue(daemon)
    assert downloaded == []

    db.set_available_at("prem", "2026-01-01T00:00:00+00:00")
    drain_queue(daemon)
    assert downloaded == ["prem"]


def test_premiere_download_is_deferred_not_failed(tmp_path, monkeypatch):
    # A download that still hits "Premieres in N hours" (rescheduled, or the
    # computed availability was off) is pushed back instead of burning one of
    # the max_download_attempts.
    config = make_config(tmp_path)
    db = Database(config.db_path)
    daemon = Daemon(config, db)
    db.upsert_channel("@chan", CHANNEL_URL)
    db.insert_pending_video(
        video_id="prem",
        channel_handle="@chan",
        title="prem",
        upload_date="2026-07-09",
        duration_seconds=60,
    )

    def premiere_download(**kwargs):
        raise UnavailableError("Premieres in 2 hours", reason="premiere")

    monkeypatch.setattr(daemon_mod, "download_video", premiere_download)

    assert daemon._download_next() is False
    row = db.get_video("prem")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["available_at"] > datetime.now(timezone.utc).isoformat()
    assert daemon._download_next() is None
    db.close()


def test_members_only_video_logs_warning_not_error(harness, caplog, monkeypatch):
    daemon, db, feed, metas, downloaded, _ = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.advance_watermark("@chan", "2026-06-01")
    feed.append("locked")

    def fail_meta(video_id):
        raise UnavailableError("members only", reason="members_only")

    monkeypatch.setattr(daemon_mod, "fetch_video_metadata", fail_meta)

    with caplog.at_level(logging.INFO, logger="cleantube"):
        daemon._process_channel(CHANNEL_URL)

    records = [r for r in caplog.records if r.getMessage() == "video_meta_failed"]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].reason == "members_only"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert downloaded == []


def test_unexpected_meta_failure_stays_error(harness, caplog, monkeypatch):
    daemon, db, feed, metas, downloaded, _ = harness
    db.upsert_channel("@chan", CHANNEL_URL)
    db.advance_watermark("@chan", "2026-06-01")
    feed.append("broken")

    def fail_meta(video_id):
        raise RuntimeError("yt-dlp metadata failed (rc=1): something odd")

    monkeypatch.setattr(daemon_mod, "fetch_video_metadata", fail_meta)

    with caplog.at_level(logging.INFO, logger="cleantube"):
        daemon._process_channel(CHANNEL_URL)

    records = [r for r in caplog.records if r.getMessage() == "video_meta_failed"]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"


def test_channel_without_videos_tab_logs_warning(harness, caplog, monkeypatch):
    daemon, db, feed, metas, downloaded, _ = harness

    def fail_ids(url, limit):
        raise UnavailableError(
            "This channel does not have a videos tab", reason="no_videos_tab"
        )

    monkeypatch.setattr(daemon_mod, "fetch_channel_video_ids", fail_ids)

    with caplog.at_level(logging.INFO, logger="cleantube"):
        daemon._process_channel(CHANNEL_URL)

    records = [
        r for r in caplog.records if r.getMessage() == "channel_fetch_failed"
    ]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].reason == "no_videos_tab"
