# cleantube

Ad-free local mp4 files from a YouTube subscription list.

## How it works

A small Linux daemon that runs 24/7, watches a list of YouTube channels and
downloads every new upload with [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Downloads never contain YouTube's own ads; the creators' embedded sponsor
reads are cut out using the [SponsorBlock](https://sponsor.ajay.app/)
database. New videos go into a queue and are downloaded with lots of sleep
in between to evade rate limiting.

## Requirements

- **Python 3.11+** — stdlib only, nothing to `pip install`.
- **yt-dlp** — must be *recent*: stale versions fail silently (a year-old
  build returns zero videos for a channel). Distro packages are usually too
  old — install the latest GitHub release and update it every few months.
- **ffmpeg** — merges the separately downloaded video and audio streams and
  does the actual SponsorBlock cutting.

## Running

```sh
python -m cleantube                    # uses ./cleantube.toml if present
python -m cleantube -c /path/to.toml
```

Configuration lives in [cleantube.toml](cleantube.toml); every key is
optional and documented there. `subscriptions.txt` is one channel URL per
line (`@handle` or `/channel/UC…`), `#` for comments, re-read every cycle:

```text
# science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q
```

It is a plain foreground process that logs JSON lines to stdout, so any
supervisor works. On shutdown it finishes the download in progress (a second
signal aborts it), so give the service unit `KillMode=mixed` and a generous
`TimeoutStopSec` — otherwise systemd kills yt-dlp mid-file.

## Tests

```sh
python -m pytest
```
