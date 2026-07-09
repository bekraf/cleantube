# cleantube

A small Linux daemon that watches a list of YouTube channels and downloads
every new upload with the sponsor segments already cut out, using the
crowd-sourced [SponsorBlock](https://sponsor.ajay.app/) database. No UI: it
polls, it downloads, files show up in a directory.

It removes creator-embedded sponsor reads only. YouTube's own pre/mid-roll
ads are injected at streaming time and never end up in downloaded files
anyway.

## How it works

- Every hour (configurable) it re-reads `subscriptions.txt` and checks each
  channel for new uploads.
- The first time it sees a channel it downloads the newest `backfill_count`
  videos (default 3). Set `backfill_count = 0` to download nothing and just
  record a baseline — useful for big subscription lists where you only want
  videos published from that point on.
- After that, each channel has a watermark date in the database: anything
  newer is added to a download queue.
- The queue is worked through one video at a time, oldest upload first, with
  `post_download_cooldown_seconds` of spacing between downloads. Scanning and
  downloading are decoupled, so the spacing can be generous (rate-limit
  safety) without delaying the scans.
- Upcoming premieres are queued with an explicit availability moment
  (scheduled release + video duration + a processing margin) and are only
  offered for download once they have aired in full.
- Failed downloads re-enter the queue after a poll interval, up to
  `max_download_attempts` (default 3), then marked permanently failed and
  never touched again.

Videos land flat in one directory as
`<channel> - <date> - <title> [<video-id>].mp4`. All state lives in a single
SQLite file; the filesystem is never scanned, the database is the only memory
of what was downloaded.

## Requirements

- **Python 3.11+** — stdlib only, nothing to `pip install`.
- **yt-dlp** — does all metadata fetching and downloading. It must be
  *recent*: YouTube changes constantly and stale versions fail silently (a
  year-old build returns zero videos for a channel, without an error). Distro
  packages are usually too old — install the latest release from GitHub and
  update it every few months.
- **ffmpeg** — really needed: yt-dlp uses it to merge the separately
  downloaded video and audio streams, and the SponsorBlock cutting itself is
  done by ffmpeg.

## Running

```sh
python -m cleantube                    # uses ./cleantube.toml if present
python -m cleantube -c /path/to.toml
```

Configuration lives in [cleantube.toml](cleantube.toml); every key is
optional and the file documents them all. `subscriptions.txt` is one channel
URL per line (`@handle` or `/channel/UC…` form), `#` for comments, re-read
every cycle:

```text
# science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q
```

It is a plain foreground process that logs JSON lines to stdout, so any
supervisor works. One thing to know when writing a service unit: on shutdown
the daemon finishes the download in progress (a second signal aborts it), so
give it `KillMode=mixed` and a generous `TimeoutStopSec` — otherwise systemd
kills yt-dlp mid-file.

## Tests

```sh
python -m pytest
```
