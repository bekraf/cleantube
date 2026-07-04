from cleantube.subscriptions import (
    channel_videos_url,
    extract_handle,
    read_subscriptions,
)


def test_read_subscriptions_skips_comments_blanks_and_dupes(tmp_path):
    path = tmp_path / "subscriptions.txt"
    path.write_text(
        "# science\n"
        "https://www.youtube.com/@kurzgesagt\n"
        "\n"
        "  https://www.youtube.com/@SabineHossenfelder  \n"
        "https://www.youtube.com/@kurzgesagt\n"
    )
    assert read_subscriptions(path) == [
        "https://www.youtube.com/@kurzgesagt",
        "https://www.youtube.com/@SabineHossenfelder",
    ]


def test_read_subscriptions_missing_file(tmp_path):
    assert read_subscriptions(tmp_path / "nope.txt") == []


def test_channel_videos_url_appends_videos():
    assert (
        channel_videos_url("https://www.youtube.com/@kurzgesagt")
        == "https://www.youtube.com/@kurzgesagt/videos"
    )
    assert (
        channel_videos_url("https://www.youtube.com/@kurzgesagt/")
        == "https://www.youtube.com/@kurzgesagt/videos"
    )


def test_channel_videos_url_keeps_explicit_tab():
    assert (
        channel_videos_url("https://www.youtube.com/@kurzgesagt/videos")
        == "https://www.youtube.com/@kurzgesagt/videos"
    )
    assert (
        channel_videos_url("https://www.youtube.com/@kurzgesagt/streams")
        == "https://www.youtube.com/@kurzgesagt/streams"
    )


def test_extract_handle():
    assert extract_handle("https://www.youtube.com/@kurzgesagt") == "@kurzgesagt"
    assert extract_handle("https://www.youtube.com/@a.b_c-d/videos") == "@a.b_c-d"
    assert extract_handle("https://www.youtube.com/channel/UCxyz") is None
