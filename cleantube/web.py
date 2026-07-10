import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .status import DaemonStatus
from .subscriptions import read_subscriptions

log = logging.getLogger("cleantube")

_WEBUI_DIR = Path(__file__).parent / "webui"

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

_TIMELINE_PAGE_LIMIT = 200


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _video_event(row: sqlite3.Row, kind: str, at: str) -> dict:
    return {
        "type": kind,
        "at": at,
        "video_id": row["video_id"],
        "title": row["title"],
        "channel_handle": row["channel_handle"],
        "upload_date": row["upload_date"],
        "duration_seconds": row["duration_seconds"],
        "file_size_bytes": row["file_size_bytes"],
        "sponsorblock_cuts": row["sponsorblock_cuts"],
        "attempt_count": row["attempt_count"],
        "error": row["last_error"],
    }


def estimate_queue(
    rows,
    *,
    now: datetime,
    next_download_at: str | None,
    cooldown_seconds: float,
    retry_delay_seconds: float,
) -> list[dict]:
    """Approximate when each pending video will be downloaded. Rows must be
    in queue order (upload_date ASC, rowid ASC). Regular videos are given
    sequential slots spaced by the post-download cooldown; videos held back
    after a failure also wait out their retry delay; unaired premieres wait
    for their availability moment and do not block the videos behind them."""
    base = now
    parsed_next = _parse_iso(next_download_at)
    if parsed_next and parsed_next > base:
        base = parsed_next
    cursor = base
    out = []
    for row in rows:
        kind = "scheduled"
        available = _parse_iso(row["available_at"])
        held_until = None
        if row["attempt_count"] > 0:
            held_until = _parse_iso(row["last_attempt_at"])
            if held_until:
                held_until += timedelta(seconds=retry_delay_seconds)
        if available and available > now:
            # Premiere not aired yet: its slot depends on availability, not
            # on queue position, and it does not delay the rest.
            eta = available
            kind = "premiere"
        elif held_until and held_until > cursor:
            # Held back after a failure: the queue skips it until the retry
            # delay has passed, so it does not delay the videos behind it.
            eta = held_until
            kind = "retry"
        else:
            eta = cursor
            cursor = eta + timedelta(seconds=cooldown_seconds)
        event = _video_event(row, "queued", eta.isoformat())
        event["eta"] = eta.isoformat()
        event["kind"] = kind
        event["available_at"] = row["available_at"]
        out.append(event)
    out.sort(key=lambda e: e["eta"])
    return out


def _pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM videos WHERE status = 'pending'
           ORDER BY upload_date ASC, rowid ASC"""
    ).fetchall()


def timeline_past(
    conn: sqlite3.Connection, before: str | None, limit: int
) -> list[dict]:
    """Download history, newest first: completed downloads, failed attempts
    that will be retried, and permanently failed videos."""
    before = before or "9999"
    events: list[dict] = []
    for row in conn.execute(
        """SELECT * FROM videos
           WHERE status = 'downloaded' AND downloaded_at IS NOT NULL
             AND downloaded_at < ?
           ORDER BY downloaded_at DESC LIMIT ?""",
        (before, limit),
    ):
        events.append(_video_event(row, "downloaded", row["downloaded_at"]))
    for row in conn.execute(
        """SELECT * FROM videos
           WHERE status = 'permanently_failed' AND last_attempt_at IS NOT NULL
             AND last_attempt_at < ?
           ORDER BY last_attempt_at DESC LIMIT ?""",
        (before, limit),
    ):
        events.append(
            _video_event(row, "permanently_failed", row["last_attempt_at"])
        )
    for row in conn.execute(
        """SELECT * FROM videos
           WHERE status = 'pending' AND attempt_count > 0
             AND last_attempt_at IS NOT NULL AND last_attempt_at < ?
           ORDER BY last_attempt_at DESC LIMIT ?""",
        (before, limit),
    ):
        events.append(_video_event(row, "failed_attempt", row["last_attempt_at"]))
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


def video_detail(conn: sqlite3.Connection, video_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()}
    channel = conn.execute(
        "SELECT * FROM channels WHERE handle = ?", (row["channel_handle"],)
    ).fetchone()
    data["channel"] = (
        {key: channel[key] for key in channel.keys()} if channel else None
    )
    data["youtube_url"] = f"https://www.youtube.com/watch?v={row['video_id']}"
    filepath = row["filepath"]
    data["file_exists"] = bool(filepath) and Path(filepath).exists()
    return data


def latest_video_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """SELECT video_id FROM videos
           WHERE status = 'downloaded' AND downloaded_at IS NOT NULL
           ORDER BY downloaded_at DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT video_id FROM videos ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    return row["video_id"] if row else None


def _row_summary(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def build_dashboard(
    conn: sqlite3.Connection, config: Config, status: dict
) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    per_status: dict[str, dict] = {}
    for row in conn.execute(
        """SELECT status, COUNT(*) AS n,
                  COALESCE(SUM(file_size_bytes), 0) AS bytes,
                  COALESCE(SUM(duration_seconds), 0) AS seconds,
                  COALESCE(SUM(sponsorblock_cuts), 0) AS cuts,
                  COALESCE(SUM(attempt_count), 0) AS failed_attempts
           FROM videos GROUP BY status"""
    ):
        per_status[row["status"]] = _row_summary(row)

    def stat(name: str, field: str) -> int:
        entry = per_status.get(name)
        return int(entry[field]) if entry else 0

    downloaded_n = stat("downloaded", "n")
    perm_failed_n = stat("permanently_failed", "n")
    pending_n = stat("pending", "n")
    skipped_n = stat("skipped", "n")

    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    recent = conn.execute(
        """SELECT
             SUM(CASE WHEN downloaded_at >= ? THEN 1 ELSE 0 END) AS n_7d,
             COALESCE(SUM(CASE WHEN downloaded_at >= ?
                               THEN file_size_bytes ELSE 0 END), 0) AS bytes_7d,
             SUM(CASE WHEN downloaded_at >= ? THEN 1 ELSE 0 END) AS n_30d,
             MIN(downloaded_at) AS first_at,
             MAX(downloaded_at) AS last_at
           FROM videos WHERE status = 'downloaded'""",
        (cutoff_7d, cutoff_7d, cutoff_30d),
    ).fetchone()

    biggest = conn.execute(
        """SELECT video_id, title, channel_handle, file_size_bytes
           FROM videos WHERE status = 'downloaded'
             AND file_size_bytes IS NOT NULL
           ORDER BY file_size_bytes DESC LIMIT 1"""
    ).fetchone()
    longest = conn.execute(
        """SELECT video_id, title, channel_handle, duration_seconds
           FROM videos WHERE status = 'downloaded'
             AND duration_seconds IS NOT NULL
           ORDER BY duration_seconds DESC LIMIT 1"""
    ).fetchone()
    last_download = conn.execute(
        """SELECT video_id, title, channel_handle, downloaded_at,
                  file_size_bytes, duration_seconds, sponsorblock_cuts
           FROM videos WHERE status = 'downloaded' AND downloaded_at IS NOT NULL
           ORDER BY downloaded_at DESC LIMIT 1"""
    ).fetchone()
    last_error = conn.execute(
        """SELECT video_id, title, channel_handle, last_error, last_attempt_at,
                  attempt_count, status
           FROM videos WHERE last_error IS NOT NULL
           ORDER BY last_attempt_at DESC LIMIT 1"""
    ).fetchone()

    channels = conn.execute(
        """SELECT COUNT(*) AS n, MAX(last_checked_at) AS last_checked_at
           FROM channels"""
    ).fetchone()

    pending = _pending_rows(conn)
    queue = estimate_queue(
        pending,
        now=now,
        next_download_at=status.get("next_download_at"),
        cooldown_seconds=config.post_download_cooldown_seconds,
        retry_delay_seconds=config.poll_interval_seconds,
    )
    deferred = sum(1 for e in queue if e["kind"] == "premiere")
    held_back = sum(1 for e in queue if e["kind"] == "retry")
    scheduled = [e for e in queue if e["kind"] == "scheduled"]

    per_day_rows = conn.execute(
        """SELECT substr(downloaded_at, 1, 10) AS day, COUNT(*) AS n,
                  COALESCE(SUM(file_size_bytes), 0) AS bytes
           FROM videos
           WHERE status = 'downloaded' AND downloaded_at >= ?
           GROUP BY day""",
        (cutoff_30d,),
    ).fetchall()
    by_day = {row["day"]: row for row in per_day_rows}
    per_day = []
    for offset in range(29, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        row = by_day.get(day)
        per_day.append(
            {
                "day": day,
                "count": int(row["n"]) if row else 0,
                "bytes": int(row["bytes"]) if row else 0,
            }
        )

    per_channel = [
        _row_summary(row)
        for row in conn.execute(
            """SELECT channel_handle, COUNT(*) AS n,
                      COALESCE(SUM(file_size_bytes), 0) AS bytes
               FROM videos WHERE status = 'downloaded'
               GROUP BY channel_handle ORDER BY n DESC, channel_handle ASC"""
        )
    ]

    disk = None
    try:
        usage = shutil.disk_usage(config.download_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        pass

    try:
        subscription_count = len(read_subscriptions(config.subscriptions_path))
    except OSError:
        subscription_count = None

    failed_attempts_total = sum(
        int(entry["failed_attempts"]) for entry in per_status.values()
    )
    decided = downloaded_n + perm_failed_n
    success_rate = (downloaded_n / decided * 100) if decided else None
    all_attempts = downloaded_n + failed_attempts_total
    attempt_success_rate = (
        (downloaded_n / all_attempts * 100) if all_attempts else None
    )

    return {
        "now": now_iso,
        "daemon": {
            **status,
            "poll_interval_seconds": config.poll_interval_seconds,
            "post_download_cooldown_seconds":
                config.post_download_cooldown_seconds,
            "max_download_attempts": config.max_download_attempts,
            "download_dir": str(config.download_dir),
            "db_path": str(config.db_path),
            "subscription_count": subscription_count,
        },
        "totals": {
            "downloaded": downloaded_n,
            "pending": pending_n,
            "permanently_failed": perm_failed_n,
            "skipped": skipped_n,
            "videos": downloaded_n + pending_n + perm_failed_n + skipped_n,
            "channels": int(channels["n"]),
            "channels_last_checked_at": channels["last_checked_at"],
        },
        "library": {
            "total_bytes": stat("downloaded", "bytes"),
            "total_duration_seconds": stat("downloaded", "seconds"),
            "sponsorblock_cuts": stat("downloaded", "cuts"),
            "downloads_7d": int(recent["n_7d"] or 0),
            "bytes_7d": int(recent["bytes_7d"] or 0),
            "downloads_30d": int(recent["n_30d"] or 0),
            "first_download_at": recent["first_at"],
            "last_download_at": recent["last_at"],
            "last_download": _row_summary(last_download),
            "biggest": _row_summary(biggest),
            "longest": _row_summary(longest),
        },
        "errors": {
            "permanently_failed": perm_failed_n,
            "pending_with_errors": held_back
            + sum(
                1
                for e in queue
                if e["kind"] != "retry" and e["attempt_count"] > 0
            ),
            "failed_attempts_total": failed_attempts_total,
            "success_rate": success_rate,
            "attempt_success_rate": attempt_success_rate,
            "last_error": _row_summary(last_error),
        },
        "queue": {
            "size": pending_n,
            "deferred_premieres": deferred,
            "held_back": held_back,
            "next": queue[0] if queue else None,
            "drained_at": scheduled[-1]["eta"] if scheduled else None,
        },
        "charts": {
            "per_day": per_day,
            "per_channel": per_channel,
        },
        "disk": disk,
    }


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, status: DaemonStatus):
        self.config = config
        self.status = status
        super().__init__((config.web_host, config.web_port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: WebServer

    # The daemon's JSON logs go to journald; per-request lines are debug-only.
    def log_message(self, format: str, *args) -> None:
        log.debug("web_request", extra={"request": format % args})

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("web_error", extra={"path": self.path, "error": str(e)})
            try:
                self._json({"error": str(e)}, status=500)
            except OSError:
                pass

    def _route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in _STATIC:
            filename, content_type = _STATIC[path]
            file = _WEBUI_DIR / filename
            if not file.exists():
                self._json({"error": "not found"}, status=404)
                return
            self._send(200, content_type, file.read_bytes())
            return
        if not path.startswith("/api/"):
            self._json({"error": "not found"}, status=404)
            return

        params = parse_qs(parsed.query)
        conn = _connect_ro(self.server.config.db_path)
        try:
            self._route_api(path, params, conn)
        finally:
            conn.close()

    def _route_api(self, path: str, params: dict, conn: sqlite3.Connection):
        config = self.server.config
        status = self.server.status.snapshot()
        if path == "/api/dashboard":
            self._json(build_dashboard(conn, config, status))
        elif path == "/api/timeline/past":
            before = (params.get("before") or [None])[0]
            limit = min(
                int((params.get("limit") or ["50"])[0]), _TIMELINE_PAGE_LIMIT
            )
            self._json({"events": timeline_past(conn, before, limit)})
        elif path == "/api/timeline/future":
            queue = estimate_queue(
                _pending_rows(conn),
                now=datetime.now(timezone.utc),
                next_download_at=status.get("next_download_at"),
                cooldown_seconds=config.post_download_cooldown_seconds,
                retry_delay_seconds=config.poll_interval_seconds,
            )
            self._json({"events": queue})
        elif path == "/api/video/latest":
            video_id = latest_video_id(conn)
            if video_id is None:
                self._json({"error": "geen video's in de database"}, status=404)
                return
            self._json(video_detail(conn, video_id))
        elif path.startswith("/api/video/"):
            video_id = path.removeprefix("/api/video/")
            detail = video_detail(conn, video_id)
            if detail is None:
                self._json({"error": "video niet gevonden"}, status=404)
                return
            self._json(detail)
        else:
            self._json({"error": "not found"}, status=404)


def start_web_server(config: Config, status: DaemonStatus) -> WebServer | None:
    """Start the portal in a background thread. A failure to bind is logged
    and leaves the daemon running without a portal."""
    if not config.web_enabled:
        return None
    try:
        server = WebServer(config, status)
    except OSError as e:
        log.error(
            "web_start_failed",
            extra={
                "host": config.web_host,
                "port": config.web_port,
                "error": str(e),
            },
        )
        return None
    thread = threading.Thread(
        target=server.serve_forever, name="cleantube-web", daemon=True
    )
    thread.start()
    log.info(
        "web_started",
        extra={"host": config.web_host, "port": config.web_port},
    )
    return server
