from pathlib import Path

import pytest

from cleantube.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def _add_pending(db, video_id, upload_date, handle="@chan"):
    db.insert_pending_video(
        video_id=video_id,
        channel_handle=handle,
        title=f"title {video_id}",
        upload_date=upload_date,
        duration_seconds=60,
    )


def test_channel_lifecycle(db):
    assert not db.channel_has_any_videos("@chan")
    db.upsert_channel("@chan", "https://youtube.com/@chan")
    db.upsert_channel("@chan", "https://youtube.com/@chan/videos")  # url update, no dupe
    _add_pending(db, "v1", "2026-01-01")
    assert db.channel_has_any_videos("@chan")


def test_video_status_transitions(db):
    db.upsert_channel("@chan", "url")
    assert db.video_status("v1") is None
    _add_pending(db, "v1", "2026-01-01")
    assert db.video_status("v1") == "pending"

    db.mark_downloaded(
        video_id="v1",
        filepath=Path("/dl/v1.mp4"),
        file_size_bytes=123,
        sponsorblock_cuts=2,
    )
    row = db.get_video("v1")
    assert row["status"] == "downloaded"
    assert row["filepath"] == "/dl/v1.mp4"
    assert row["sponsorblock_cuts"] == 2
    assert row["downloaded_at"] is not None


def test_insert_pending_is_idempotent(db):
    db.upsert_channel("@chan", "url")
    _add_pending(db, "v1", "2026-01-01")
    db.mark_downloaded(
        video_id="v1", filepath=Path("/dl/v1.mp4"), file_size_bytes=1, sponsorblock_cuts=0
    )
    _add_pending(db, "v1", "2026-01-01")  # must not reset status
    assert db.video_status("v1") == "downloaded"


def test_failure_counting(db):
    db.upsert_channel("@chan", "url")
    _add_pending(db, "v1", "2026-01-01")
    assert db.record_failure("v1", "boom") == 1
    assert db.record_failure("v1", "boom again") == 2
    row = db.get_video("v1")
    assert row["last_error"] == "boom again"
    assert row["status"] == "pending"

    db.mark_permanently_failed("v1")
    assert db.video_status("v1") == "permanently_failed"


def test_most_recent_upload_date(db):
    db.upsert_channel("@chan", "url")
    assert db.most_recent_upload_date("@chan") is None
    _add_pending(db, "v1", "2026-01-01")
    _add_pending(db, "v2", "2026-03-15")
    _add_pending(db, "v3", "2026-02-01")
    assert db.most_recent_upload_date("@chan") == "2026-03-15"
