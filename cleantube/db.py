import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    handle              TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_checked_at     TEXT
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
    status              TEXT NOT NULL,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_attempt_at     TEXT
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

    def mark_channel_checked(self, handle: str) -> None:
        self.conn.execute(
            "UPDATE channels SET last_checked_at = ? WHERE handle = ?",
            (_now(), handle),
        )

    def channel_has_any_videos(self, handle: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM videos WHERE channel_handle = ? LIMIT 1", (handle,)
        ).fetchone()
        return row is not None

    def most_recent_upload_date(self, handle: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(upload_date) AS d FROM videos WHERE channel_handle = ?",
            (handle,),
        ).fetchone()
        return row["d"] if row else None

    def video_status(self, video_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return row["status"] if row else None

    def get_video(self, video_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()

    def insert_pending_video(
        self,
        *,
        video_id: str,
        channel_handle: str,
        title: str,
        upload_date: str,
        duration_seconds: int | None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO videos
               (video_id, channel_handle, title, upload_date,
                duration_seconds, status)
               VALUES (?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(video_id) DO NOTHING""",
            (video_id, channel_handle, title, upload_date, duration_seconds),
        )

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
