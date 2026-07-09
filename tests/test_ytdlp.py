import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cleantube.ytdlp as ytdlp_mod
from cleantube.ytdlp import (
    UnavailableError,
    _count_sponsorblock_cuts,
    _raise_ytdlp_failure,
    build_output_path,
    fetch_video_metadata,
)


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _fake_yt_dlp_json(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(
        ytdlp_mod.subprocess, "run",
        lambda *args, **kwargs: _FakeCompleted(json.dumps(payload)),
    )


def test_build_output_path_basic():
    path = build_output_path(
        Path("/dl"), "@kurzgesagt", "2026-06-15", "The End of the Universe", "abc123XYZ"
    )
    assert path == Path(
        "/dl/kurzgesagt - 2026-06-15 - The End of the Universe [abc123XYZ].mp4"
    )


def test_build_output_path_sanitizes_slashes_and_control_chars():
    path = build_output_path(Path("/dl"), "@chan", "2026-01-01", "a/b\x00c", "id123")
    assert "/" not in path.name
    assert "\x00" not in path.name


def test_build_output_path_truncates_by_bytes_not_chars():
    # 300 four-byte emoji = 1200 bytes; the char-based limit of 240 would
    # still overflow the 255-byte filename cap.
    path = build_output_path(Path("/dl"), "@chan", "2026-01-01", "🎬" * 300, "abc123XYZ")
    assert len(path.name.encode("utf-8")) <= 255
    assert path.name.endswith("[abc123XYZ].mp4")


def test_count_sponsorblock_cuts_matches_real_yt_dlp_output():
    lines = [
        "[youtube] abc: Downloading webpage",
        "[SponsorBlock] Fetching SponsorBlock segments",
        "[SponsorBlock] Found 3 segments in the SponsorBlock database",
        "[ModifyChapters] Removing chapters from /dl/video.mp4",
    ]
    assert _count_sponsorblock_cuts(lines) == 3


def test_count_sponsorblock_cuts_no_segments():
    lines = [
        "[SponsorBlock] Fetching SponsorBlock segments",
        "[SponsorBlock] No matching segments were found in the SponsorBlock database",
    ]
    assert _count_sponsorblock_cuts(lines) == 0


def test_count_sponsorblock_cuts_empty_output():
    assert _count_sponsorblock_cuts([]) == 0


# Real messages observed in production logs.
@pytest.mark.parametrize(
    "message, reason",
    [
        ("ERROR: [youtube] abc: Join this channel to get access to "
         "members-only content like this video, and other exclusive perks.",
         "members_only"),
        ("ERROR: [youtube] abc: This video is available to this channel's "
         "members on level: Seriously (or any higher level).",
         "members_only"),
        ("ERROR: [youtube] abc: Sign in to confirm your age. This video may "
         "be inappropriate for some users.",
         "age_restricted"),
        ("ERROR: [youtube] abc: Premieres in 4 hours", "premiere"),
        ("ERROR: [youtube] abc: This video is not available", "unavailable"),
        ("ERROR: [youtube] abc: Video unavailable. This video is not "
         "available",
         "unavailable"),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been "
         "granted access to this video",
         "private"),
        ("ERROR: [youtube:tab] @chan: This channel does not have a videos "
         "tab",
         "no_videos_tab"),
    ],
)
def test_expected_failures_raise_unavailable(message, reason):
    with pytest.raises(UnavailableError) as exc_info:
        _raise_ytdlp_failure(f"yt-dlp metadata failed (rc=1): {message}")
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "message",
    [
        # Bot detection is the rate-limit signal; must stay an error.
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication.",
        # A dead subscription entry is actionable; must stay an error.
        "ERROR: [youtube:tab] @gone: YouTube said: This channel does not "
        "exist.",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    ],
)
def test_operational_failures_stay_errors(message):
    with pytest.raises(RuntimeError) as exc_info:
        _raise_ytdlp_failure(f"yt-dlp metadata failed (rc=1): {message}")
    assert not isinstance(exc_info.value, UnavailableError)


def test_metadata_upcoming_premiere_gets_available_at(monkeypatch):
    release_ts = 1_800_000_000
    _fake_yt_dlp_json(monkeypatch, {
        "id": "prem123",
        "title": "Big premiere",
        "duration": 600,
        "live_status": "is_upcoming",
        "release_timestamp": release_ts,
        "uploader_id": "@chan",
    })
    meta = fetch_video_metadata("prem123")
    release = datetime.fromtimestamp(release_ts, timezone.utc)
    # Downloadable after it aired in full: release + duration + margin.
    assert meta.available_at == (
        release + timedelta(seconds=600 + 900)
    ).isoformat()
    # No upload_date yet; the scheduled release date stands in.
    assert meta.upload_date == release.strftime("%Y-%m-%d")


def test_metadata_normal_video_has_no_available_at(monkeypatch):
    _fake_yt_dlp_json(monkeypatch, {
        "id": "abc123",
        "title": "Normal video",
        "upload_date": "20260701",
        "duration": 100,
        "uploader_id": "@chan",
    })
    meta = fetch_video_metadata("abc123")
    assert meta.available_at is None
    assert meta.upload_date == "2026-07-01"
