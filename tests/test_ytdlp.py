from pathlib import Path

from cleantube.ytdlp import _count_sponsorblock_cuts, build_output_path


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
