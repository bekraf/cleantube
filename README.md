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

## systemd

Example unit (`~/.config/systemd/user/cleantube.service`):

```ini
[Unit]
Description=Cleantube YouTube subscription downloader
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/cleantube
ExecStart=/usr/bin/python3 -m cleantube
Restart=on-failure
RestartSec=30
# On stop, signal only the daemon so the in-flight yt-dlp download can
# finish; SIGKILL the whole group only after the (generous) timeout.
KillMode=mixed
TimeoutStopSec=1h

[Install]
WantedBy=default.target
```

```sh
systemctl --user enable --now cleantube
journalctl --user -u cleantube -f
```

Signals: the first SIGTERM/SIGINT finishes the current download, skips the
cooldown, and exits cleanly. A second one aborts the download (yt-dlp leaves a
resumable `.part` file, so nothing is lost).

## Tests

```sh
python -m pytest
```
