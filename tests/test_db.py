import sqlite3
from datetime import datetime, timedelta, timezone
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
    assert db.handle_for_url("https://youtube.com/@chan") is None
    db.upsert_channel("@chan", "https://youtube.com/@chan")
    assert db.handle_for_url("https://youtube.com/@chan") == "@chan"
    db.upsert_channel("@chan", "https://youtube.com/@chan/videos")  # url update, no dupe
    assert db.handle_for_url("https://youtube.com/@chan/videos") == "@chan"


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


def test_watermark_advances_only_forward(db):
    db.upsert_channel("@chan", "url")
    assert db.watermark("@chan") is None
    db.advance_watermark("@chan", "2026-06-01")
    assert db.watermark("@chan") == "2026-06-01"
    db.advance_watermark("@chan", "2026-05-01")  # older date: no-op
    assert db.watermark("@chan") == "2026-06-01"
    db.advance_watermark("@chan", "2026-07-01")
    assert db.watermark("@chan") == "2026-07-01"


def test_next_pending_video_pops_oldest_first(db):
    db.upsert_channel("@chan", "url")
    assert db.pending_count() == 0
    _add_pending(db, "newer", "2026-07-01")
    _add_pending(db, "older", "2026-06-01")
    assert db.pending_count() == 2

    row = db.next_pending_video(retry_delay_seconds=3600)
    assert row["video_id"] == "older"
    db.mark_downloaded(
        video_id="older", filepath=Path("/dl/older.mp4"), file_size_bytes=1,
        sponsorblock_cuts=0,
    )
    assert db.next_pending_video(retry_delay_seconds=3600)["video_id"] == "newer"
    assert db.pending_count() == 1


def test_next_pending_video_respects_retry_holdback(db):
    db.upsert_channel("@chan", "url")
    _add_pending(db, "v1", "2026-06-01")
    db.record_failure("v1", "boom")
    # Attempted just now: held back for the retry delay, eligible once it
    # has passed (delay 0 puts the cutoff at "now").
    assert db.next_pending_video(retry_delay_seconds=3600) is None
    assert db.next_pending_video(retry_delay_seconds=0)["video_id"] == "v1"


def test_next_pending_video_gates_on_available_at(db):
    db.upsert_channel("@chan", "url")
    airs_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db.insert_pending_video(
        video_id="prem",
        channel_handle="@chan",
        title="premiere",
        upload_date="2026-07-10",
        duration_seconds=60,
        available_at=airs_at,
    )
    assert db.next_pending_video(retry_delay_seconds=3600) is None

    aired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.set_available_at("prem", aired)
    assert db.next_pending_video(retry_delay_seconds=3600)["video_id"] == "prem"


def test_skipped_video_status(db):
    db.upsert_channel("@chan", "url")
    db.insert_skipped_video(
        video_id="base",
        channel_handle="@chan",
        title="baseline",
        upload_date="2026-06-01",
        duration_seconds=None,
    )
    assert db.video_status("base") == "skipped"


def test_migration_seeds_watermark_from_existing_rows(tmp_path):
    # Simulate a database created before the watermark column existed.
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE channels (
            handle TEXT PRIMARY KEY, url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_checked_at TEXT);
        CREATE TABLE videos (
            video_id TEXT PRIMARY KEY, channel_handle TEXT NOT NULL,
            title TEXT NOT NULL, upload_date TEXT NOT NULL,
            duration_seconds INTEGER, filepath TEXT, file_size_bytes INTEGER,
            sponsorblock_cuts INTEGER, downloaded_at TEXT, status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            last_attempt_at TEXT);
        INSERT INTO channels VALUES ('@chan', 'url', '2026-01-01', NULL);
        INSERT INTO channels VALUES ('@empty', 'url2', '2026-01-01', NULL);
        INSERT INTO videos (video_id, channel_handle, title, upload_date, status)
            VALUES ('v1', '@chan', 't', '2026-06-15', 'downloaded'),
                   ('v2', '@chan', 't', '2026-03-01', 'downloaded');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.watermark("@chan") == "2026-06-15"
    assert db.watermark("@empty") is None
    # The available_at column is added on the fly too.
    db.set_available_at("v1", "2026-07-01T00:00:00+00:00")
    assert db.get_video("v1")["available_at"] == "2026-07-01T00:00:00+00:00"
    db.close()
