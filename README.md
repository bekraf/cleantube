# cleantube

Personal YouTube subscription daemon. Watches a list of channels, grabs new
uploads as they go up, and runs them through SponsorBlock so the
creator-baked sponsor reads get cut out before the file lands on disk.

No UI, no web frontend, no nothing. It's a long-lived process you let systemd
babysit.

## What you need

- Python 3.11+
- ffmpeg (`pacman -S ffmpeg` on Arch)
- yt-dlp (`pip install yt-dlp`, or your distro's package)

## Running it

```
python -m cleantube
```

That's it. By default it reads `cleantube.toml` and `subscriptions.txt` from
the current directory, writes the SQLite DB to `./cleantube/cleantube.db`, and
dumps mp4s into `./cleantube/downloaded/`. `-c some/other.toml` if you keep
the config somewhere else.

In real use you want it under systemd. Something like:

```ini
[Unit]
Description=cleantube
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/you/cleantube
ExecStart=/usr/bin/python -m cleantube
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Logs are JSON on stdout, one event per line, so `journalctl -fu cleantube`
gives you something readable with `jq` if you want.

## Subscriptions

`subscriptions.txt` — one channel URL per line, `#` for comments, blank
lines fine. Re-read at the top of every poll cycle, so you can add or remove
channels while it's running and changes take effect within the hour.

```
# science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/@SabineHossenfelder
```

Use `@handle` URLs. Other URL shapes (channel IDs, `/c/Name`) probably work
since the daemon falls back to learning the handle from yt-dlp's metadata,
but I haven't tested them and I'm not promising they will.

## Config

Everything is optional. Defaults shown:

```toml
backfill_count = 3                    # new channel? grab the last N uploads
poll_interval_seconds = 3600          # check every hour
post_download_cooldown_seconds = 1800 # 30 min between downloads
download_dir = "./cleantube/downloaded"
db_path = "./cleantube/cleantube.db"
video_format = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"
max_download_attempts = 3             # then give up and stop retrying
```

The cooldown is the knob that matters. YouTube will hand you 429s if you
hammer them; 30 minutes between downloads has been fine in practice.

## How it decides what to download

- First time it sees a channel: grabs the most recent `backfill_count` videos.
- Every cycle after: anything with an `upload_date` newer than the latest one
  it already has stored for that channel.
- Already downloaded? Skipped.
- Failed `max_download_attempts` times? Marked `permanently_failed` and never
  retried.

The DB is the source of truth — the daemon never scans the filesystem. If you
delete an mp4 by hand, it will not be re-downloaded. If that's not what you
want, delete the row too.

## What it doesn't do

- Pre-roll or mid-roll YouTube ads. Those are injected by the player at
  stream time and aren't present in downloaded files. The only ads cleantube
  actually removes are creator-read sponsor segments, via SponsorBlock.
- Members-only / age-restricted content. No auth.
- Shorts, livestreams, subtitles.
- Parallel downloads — serial-with-cooldown is on purpose.
- Cleaning up after itself. Files pile up forever. Disk space is your
  problem.

## Stopping it

SIGTERM or Ctrl-C: finishes the current download (so you never end up with a
half-file), skips the cooldown, exits cleanly. `systemctl stop` does the
right thing.

## Layout

```
cleantube/                 the package
cleantube.toml             config
subscriptions.txt          channel list
cleantube/cleantube.db     SQLite, created on first run
cleantube/downloaded/      flat dir of mp4s, created on first run
```

The downloaded filenames look like:

```
kurzgesagt - 2026-06-16 - How Are Memories Stored Inside Your Brain? [PqtggjVAi8M].mp4
```

Handle, upload date, title, video ID. The ID in brackets is the disambiguator
so two videos with the same title don't collide.
