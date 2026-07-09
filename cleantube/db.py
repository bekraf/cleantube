import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    handle              TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_checked_at     TEXT,
    watermark_date      TEXT     -- newest upload_date seen; uploads on or
                                 -- after this date are candidates
);

CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_handle      TEXT NOT NULL REFERENCES channels(handle),
    title               TEXT NOT NULL,
    upload_date         TEXT NOT NULL,
    duration_seconds    INTEGER,
    filepath            TEXT,
    file_size_bytes     INTEGER,
    sponsorblock_cuts   INTEGER,
    downloaded_at       TEXT,
    status              TEXT NOT NULL,   -- 'pending' | 'downloaded'
                                         -- | 'permanently_failed' | 'skipped'
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_attempt_at     TEXT,
    available_at        TEXT             -- queue offers the video only from
                                         -- this moment (upcoming premieres)
);

CREATE INDEX IF NOT EXISTS idx_videos_channel_date
    ON videos(channel_handle, upload_date DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(channels)")}
        if "watermark_date" not in cols:
            self.conn.execute("ALTER TABLE channels ADD COLUMN watermark_date TEXT")
            # Seed from the previous implicit tracker (max stored upload_date).
            self.conn.execute(
                """UPDATE channels SET watermark_date =
                   (SELECT MAX(upload_date) FROM videos
                    WHERE videos.channel_handle = channels.handle)"""
            )
        video_cols = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(videos)")
        }
        if "available_at" not in video_cols:
            self.conn.execute("ALTER TABLE videos ADD COLUMN available_at TEXT")

    def close(self) -> None:
        self.conn.close()

    def upsert_channel(self, handle: str, url: str) -> None:
        row = self.conn.execute(
            "SELECT handle FROM channels WHERE handle = ?", (handle,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO channels (handle, url, first_seen_at) VALUES (?, ?, ?)",
                (handle, url, _now()),
            )
        else:
            self.conn.execute(
                "UPDATE channels SET url = ? WHERE handle = ?", (url, handle)
            )

    def handle_for_url(self, url: str) -> str | None:
        row = self.conn.execute(
            "SELECT handle FROM channels WHERE url = ?", (url,)
        ).fetchone()
        return row["handle"] if row else None

    def mark_channel_checked(self, handle: str) -> None:
        self.conn.execute(
            "UPDATE channels SET last_checked_at = ? WHERE handle = ?",
            (_now(), handle),
        )

    def watermark(self, handle: str) -> str | None:
        row = self.conn.execute(
            "SELECT watermark_date FROM channels WHERE handle = ?", (handle,)
        ).fetchone()
        return row["watermark_date"] if row else None

    def advance_watermark(self, handle: str, upload_date: str) -> None:
        """Move the channel watermark forward; older dates are a no-op."""
        self.conn.execute(
            """UPDATE channels SET watermark_date = ?
               WHERE handle = ?
                 AND (watermark_date IS NULL OR watermark_date < ?)""",
            (upload_date, handle, upload_date),
        )

    def video_status(self, video_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return row["status"] if row else None

    def get_video(self, video_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE status = 'pending'"
        ).fetchone()
        return int(row["n"])

    def next_pending_video(self, retry_delay_seconds: float) -> sqlite3.Row | None:
        """Oldest queued video that is eligible for download. A video
        attempted within the last `retry_delay_seconds` is held back, so a
        failing download is retried at the old once-per-cycle cadence instead
        of in a tight loop. Videos with an `available_at` in the future
        (upcoming premieres) are not offered yet."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=retry_delay_seconds)).isoformat()
        return self.conn.execute(
            """SELECT * FROM videos
               WHERE status = 'pending'
                 AND (last_attempt_at IS NULL OR last_attempt_at <= ?)
                 AND (available_at IS NULL OR available_at <= ?)
               ORDER BY upload_date ASC, rowid ASC
               LIMIT 1""",
            (cutoff, now.isoformat()),
        ).fetchone()

    def set_available_at(self, video_id: str, available_at: str) -> None:
        """Defer a queued video: the queue will not offer it before this
        moment. Does not touch status or attempt counters."""
        self.conn.execute(
            "UPDATE videos SET available_at = ? WHERE video_id = ?",
            (available_at, video_id),
        )

    def _insert_video(
        self,
        *,
        video_id: str,
        channel_handle: str,
        title: str,
        upload_date: str,
        duration_seconds: int | None,
        status: str,
        available_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO videos
               (video_id, channel_handle, title, upload_date,
                duration_seconds, status, available_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO NOTHING""",
            (video_id, channel_handle, title, upload_date, duration_seconds,
             status, available_at),
        )

    def insert_pending_video(self, **kwargs) -> None:
        self._insert_video(status="pending", **kwargs)

    def insert_skipped_video(self, **kwargs) -> None:
        """Record a video as the follow-only baseline; never downloaded."""
        self._insert_video(status="skipped", **kwargs)

    def mark_downloaded(
        self,
        *,
        video_id: str,
        filepath: Path,
        file_size_bytes: int,
        sponsorblock_cuts: int,
    ) -> None:
        now = _now()
        self.conn.execute(
            """UPDATE videos
               SET status = 'downloaded',
                   filepath = ?,
                   file_size_bytes = ?,
                   sponsorblock_cuts = ?,
                   downloaded_at = ?,
                   last_attempt_at = ?,
                   last_error = NULL
               WHERE video_id = ?""",
            (str(filepath), file_size_bytes, sponsorblock_cuts, now, now, video_id),
        )

    def record_failure(self, video_id: str, error: str) -> int:
        self.conn.execute(
            """UPDATE videos
               SET attempt_count = attempt_count + 1,
                   last_error = ?,
                   last_attempt_at = ?
               WHERE video_id = ?""",
            (error, _now(), video_id),
        )
        row = self.conn.execute(
            "SELECT attempt_count FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return int(row["attempt_count"]) if row else 0

    def mark_permanently_failed(self, video_id: str) -> None:
        self.conn.execute(
            "UPDATE videos SET status = 'permanently_failed' WHERE video_id = ?",
            (video_id,),
        )
