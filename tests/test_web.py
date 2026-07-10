import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cleantube.config import Config
from cleantube.db import Database
from cleantube.status import DaemonStatus
from cleantube.web import WebServer, estimate_queue

CHANNEL_URL = "https://www.youtube.com/@chan"


def make_config(tmp_path, **overrides) -> Config:
    values = dict(
        backfill_count=3,
        poll_interval_seconds=3600,
        post_download_cooldown_seconds=1800,
        download_dir=tmp_path / "downloaded",
        video_format="best",
        max_download_attempts=3,
        db_path=tmp_path / "test.db",
        subscriptions_path=tmp_path / "subscriptions.txt",
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=0,  # ephemeral port for tests
    )
    values.update(overrides)
    return Config(**values)


def seed_db(db: Database) -> None:
    db.upsert_channel("@chan", CHANNEL_URL)
    db.mark_channel_checked("@chan")

    db.insert_pending_video(
        video_id="v_ok", channel_handle="@chan", title="ok video",
        upload_date="2026-07-01", duration_seconds=600,
    )
    db.mark_downloaded(
        video_id="v_ok", filepath=Path("/dl/ok.mp4"),
        file_size_bytes=1_000_000, sponsorblock_cuts=2,
    )

    db.insert_pending_video(
        video_id="v_new", channel_handle="@chan", title="new video",
        upload_date="2026-07-09", duration_seconds=300,
    )

    db.insert_pending_video(
        video_id="v_flaky", channel_handle="@chan", title="flaky video",
        upload_date="2026-07-08", duration_seconds=120,
    )
    db.record_failure("v_flaky", "yt-dlp exploded")

    db.insert_pending_video(
        video_id="v_dead", channel_handle="@chan", title="dead video",
        upload_date="2026-07-02", duration_seconds=60,
    )
    db.record_failure("v_dead", "gone forever")
    db.mark_permanently_failed("v_dead")

    db.insert_skipped_video(
        video_id="v_base", channel_handle="@chan", title="baseline video",
        upload_date="2026-06-01", duration_seconds=60,
    )

    airs_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    db.insert_pending_video(
        video_id="v_prem", channel_handle="@chan", title="premiere video",
        upload_date="2026-07-11", duration_seconds=900, available_at=airs_at,
    )


@pytest.fixture
def portal(tmp_path):
    config = make_config(tmp_path)
    config.download_dir.mkdir()
    config.subscriptions_path.write_text(f"{CHANNEL_URL}\n")
    db = Database(config.db_path)
    seed_db(db)

    status = DaemonStatus()
    status.update(
        started_at=datetime.now(timezone.utc).isoformat(),
        next_download_at=datetime.now(timezone.utc).isoformat(),
    )
    server = WebServer(config, status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    yield base_url, db
    server.shutdown()
    db.close()


def get(base_url: str, path: str):
    with urllib.request.urlopen(base_url + path) as response:
        return response.status, response.headers, response.read()


def get_json(base_url: str, path: str) -> dict:
    status, _, body = get(base_url, path)
    assert status == 200
    return json.loads(body)


def test_index_and_static_served(portal):
    base_url, _ = portal
    status, headers, body = get(base_url, "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"cleantube" in body
    status, _, _ = get(base_url, "/app.js")
    assert status == 200
    status, _, _ = get(base_url, "/style.css")
    assert status == 200


def test_unknown_path_is_404(portal):
    base_url, _ = portal
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(base_url, "/etc/passwd")
    assert excinfo.value.code == 404


def test_dashboard(portal):
    base_url, _ = portal
    data = get_json(base_url, "/api/dashboard")
    assert data["totals"] == {
        "downloaded": 1,
        "pending": 3,
        "permanently_failed": 1,
        "skipped": 1,
        "videos": 6,
        "channels": 1,
        "channels_last_checked_at": data["totals"]["channels_last_checked_at"],
    }
    assert data["totals"]["channels_last_checked_at"] is not None
    assert data["library"]["total_bytes"] == 1_000_000
    assert data["library"]["sponsorblock_cuts"] == 2
    assert data["library"]["last_download"]["video_id"] == "v_ok"
    assert data["errors"]["failed_attempts_total"] == 2
    assert data["errors"]["success_rate"] == 50.0
    assert data["errors"]["last_error"]["last_error"] in (
        "yt-dlp exploded", "gone forever",
    )
    assert data["queue"]["size"] == 3
    assert data["queue"]["deferred_premieres"] == 1
    assert data["queue"]["held_back"] == 1
    assert data["queue"]["next"]["video_id"] == "v_new"
    assert len(data["charts"]["per_day"]) == 30
    assert sum(d["count"] for d in data["charts"]["per_day"]) == 1
    assert data["charts"]["per_channel"][0]["channel_handle"] == "@chan"
    assert data["daemon"]["subscription_count"] == 1
    assert data["daemon"]["poll_interval_seconds"] == 3600


def test_timeline_past_and_paging(portal):
    base_url, _ = portal
    data = get_json(base_url, "/api/timeline/past?limit=50")
    events = data["events"]
    types = {e["type"] for e in events}
    assert types == {"downloaded", "failed_attempt", "permanently_failed"}
    ats = [e["at"] for e in events]
    assert ats == sorted(ats, reverse=True)
    failed = next(e for e in events if e["type"] == "failed_attempt")
    assert failed["video_id"] == "v_flaky"
    assert failed["error"] == "yt-dlp exploded"

    oldest = ats[-1]
    older = get_json(
        base_url, f"/api/timeline/past?limit=50&before={oldest}"
    )
    assert older["events"] == []


def test_timeline_future_etas(portal):
    base_url, _ = portal
    data = get_json(base_url, "/api/timeline/future")
    events = {e["video_id"]: e for e in data["events"]}
    assert set(events) == {"v_new", "v_flaky", "v_prem"}
    assert events["v_new"]["kind"] == "scheduled"
    assert events["v_flaky"]["kind"] == "retry"
    assert events["v_prem"]["kind"] == "premiere"
    etas = [e["eta"] for e in data["events"]]
    assert etas == sorted(etas)
    # The fresh video downloads first; the failed one waits out its retry
    # delay; the premiere waits for its availability moment.
    assert data["events"][0]["video_id"] == "v_new"


def test_video_detail_has_all_fields(portal):
    base_url, _ = portal
    data = get_json(base_url, "/api/video/v_ok")
    for field in (
        "video_id", "channel_handle", "title", "upload_date",
        "duration_seconds", "filepath", "file_size_bytes",
        "sponsorblock_cuts", "downloaded_at", "status", "attempt_count",
        "last_error", "last_attempt_at", "available_at",
    ):
        assert field in data
    assert data["status"] == "downloaded"
    assert data["youtube_url"] == "https://www.youtube.com/watch?v=v_ok"
    assert data["file_exists"] is False
    assert data["channel"]["handle"] == "@chan"
    assert data["channel"]["url"] == CHANNEL_URL


def test_video_detail_unknown_is_404(portal):
    base_url, _ = portal
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(base_url, "/api/video/nope")
    assert excinfo.value.code == 404


def test_video_latest_is_most_recent_download(portal):
    base_url, _ = portal
    data = get_json(base_url, "/api/video/latest")
    assert data["video_id"] == "v_ok"


def test_estimate_queue_spacing_and_premiere():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    airs_at = (now + timedelta(hours=8)).isoformat()
    rows = [
        {
            "video_id": "a", "title": "a", "channel_handle": "@c",
            "upload_date": "2026-07-01", "duration_seconds": 1,
            "file_size_bytes": None, "sponsorblock_cuts": None,
            "attempt_count": 0, "last_error": None, "last_attempt_at": None,
            "available_at": None,
        },
        {
            "video_id": "prem", "title": "p", "channel_handle": "@c",
            "upload_date": "2026-07-02", "duration_seconds": 1,
            "file_size_bytes": None, "sponsorblock_cuts": None,
            "attempt_count": 0, "last_error": None, "last_attempt_at": None,
            "available_at": airs_at,
        },
        {
            "video_id": "b", "title": "b", "channel_handle": "@c",
            "upload_date": "2026-07-03", "duration_seconds": 1,
            "file_size_bytes": None, "sponsorblock_cuts": None,
            "attempt_count": 0, "last_error": None, "last_attempt_at": None,
            "available_at": None,
        },
    ]
    queue = estimate_queue(
        rows, now=now, next_download_at=None,
        cooldown_seconds=1800, retry_delay_seconds=3600,
    )
    by_id = {e["video_id"]: e for e in queue}
    assert by_id["a"]["eta"] == now.isoformat()
    # The unaired premiere does not block the video behind it.
    assert by_id["b"]["eta"] == (now + timedelta(seconds=1800)).isoformat()
    assert by_id["prem"]["eta"] == airs_at
    assert [e["video_id"] for e in queue] == ["a", "b", "prem"]


def test_estimate_queue_respects_retry_holdback():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    failed_at = (now - timedelta(minutes=10)).isoformat()
    rows = [
        {
            "video_id": "flaky", "title": "f", "channel_handle": "@c",
            "upload_date": "2026-07-01", "duration_seconds": 1,
            "file_size_bytes": None, "sponsorblock_cuts": None,
            "attempt_count": 1, "last_error": "boom",
            "last_attempt_at": failed_at, "available_at": None,
        },
    ]
    queue = estimate_queue(
        rows, now=now, next_download_at=None,
        cooldown_seconds=1800, retry_delay_seconds=3600,
    )
    assert queue[0]["kind"] == "retry"
    expected = datetime.fromisoformat(failed_at) + timedelta(seconds=3600)
    assert queue[0]["eta"] == expected.isoformat()
