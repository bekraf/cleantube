# cleantube

Auto-downloads new YouTube uploads from your subscribed channels with the
sponsor reads stripped via SponsorBlock, so the files hit disk without ads.

No UI, no web frontend, nothing. Long-lived process, let systemd babysit it.

## What you need

- Python 3.11+
- ffmpeg
- yt-dlp

## Running it

```sh
python -m cleantube
```

Reads `cleantube.toml` and `subscriptions.txt` from the current directory.
`-c some/other.toml` if the config lives somewhere else.

## Subscriptions

`subscriptions.txt` — one channel URL per line, `#` for comments. Re-read
every poll cycle, so edits take effect within the hour without a restart.

```text
# science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/@SabineHossenfelder
```

