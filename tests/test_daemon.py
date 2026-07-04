from pathlib import Path

import pytest

import cleantube.daemon as daemon_mod
from cleantube.config import Config
from cleantube.daemon import Daemon
from cleantube.db import Database
from cleantube.ytdlp import DownloadResult, VideoMeta

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
    )
    values.update(overrides)
    return Config(**values)


def make_meta(video_id: str, upload_date: str) -> VideoMeta:
    return VideoMeta(
        video_id=video_id,
        title=f"title {video_id}",
        upload_date=upload_date,
        duration_seconds=60,
        channel_handle="@chan",
    )


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
    assert downloaded == ["v1", "v2", "v3"]
    assert db.video_status("v1") == "downloaded"
    assert db.video_status("v4") is None


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

    feed.extend(["new", "old", "ancient"])
    metas["new"] = make_meta("new", "2026-07-01")
    metas["ancient"] = make_meta("ancient", "2026-01-01")

    daemon._process_channel(CHANNEL_URL)
    assert downloaded == ["new"]
    # Walk stops at the first unknown-and-older video; "old" is known so it
    # never needs a metadata fetch.
    assert meta_fetches == ["new", "ancient"]


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

    feed.extend(["b", "a"])
    metas["b"] = make_meta("b", "2026-07-01")  # same day as "a"

    daemon._process_channel(CHANNEL_URL)
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
    feed.append("stuck")

    daemon._process_channel(CHANNEL_URL)
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
    feed.append("dead")

    daemon._process_channel(CHANNEL_URL)
    assert downloaded == []
    assert meta_fetches == []


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
