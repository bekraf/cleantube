# Cleantube — Requirements

Cleantube is a Linux daemon that watches a list of YouTube channel subscriptions
and downloads new videos as they are published, with sponsor/self-ad segments
trimmed out via the SponsorBlock database.

It is a personal-use tool. Single user, single machine, no UI.

---

## 1. Inputs

### 1.1 `subscriptions.txt`
A plain-text file at the project root, one channel URL per line. Blank lines
and lines starting with `#` are ignored.

Example:

```
# Science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/@SabineHossenfelder
```

The daemon re-reads `subscriptions.txt` at the start of every poll cycle, so
edits take effect within ~1 hour without a restart.

### 1.2 Configuration
A config file (e.g. `cleantube.toml`) exposes at least:
- `backfill_count` — videos to grab on first-ever sight of a channel
  (default: 3)
- `poll_interval_seconds` — time between cycles (default: 3600)
- `post_download_cooldown_seconds` — wait after each successful download
  (default: 1800)
- `download_dir` — output directory (default: `./cleantube/downloaded`)
- `video_format` — yt-dlp format string (default:
  `bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]`)
- `max_download_attempts` — give-up threshold per video (default: 3)

---

## 2. Behavior

### 2.1 Poll loop
The daemon runs continuously. Each cycle:
1. Re-read `subscriptions.txt`.
2. For each channel, fetch the current list of uploaded video IDs via yt-dlp's
   metadata extraction (no actual download).
3. Decide which videos to enqueue (see 2.2).
4. Download enqueued videos one at a time (see 2.3).
5. Sleep `poll_interval_seconds` before the next cycle.

### 2.2 What to download
- **First time seeing a channel** (no rows in `videos` for that channel):
  enqueue the most recent `backfill_count` uploads.
- **Subsequent runs**: enqueue every upload newer than the most recent
  `upload_date` already stored for that channel.
- Skip any video whose ID already has a row marked `downloaded` or
  `permanently_failed` in the DB.

### 2.3 Download
For each enqueued video, in order:
1. Invoke yt-dlp with the configured format and
   `--sponsorblock-remove default` so SponsorBlock segments are cut at
   download time.
2. Write the file to `download_dir` (flat, no per-channel subdirectories).
   Filename pattern:
   `<channel-handle> - <upload_date> - <title> [<video_id>].mp4`
   (e.g. `kurzgesagt - 2026-06-15 - The End of the Universe [abc123XYZ].mp4`)
3. On success: insert a row into `videos` and sleep
   `post_download_cooldown_seconds` before the next download.
4. On failure: increment the video's `attempt_count`, log the error, continue
   to the next video without cooldown. The cooldown exists only to avoid YouTube
   rate-limiting *after a real download*; failed attempts don't consume that
   budget.

### 2.4 Failure policy
- Each video has an `attempt_count` and `last_error` in the DB.
- After `max_download_attempts` failures (default 3), the video is marked
  `permanently_failed` and never retried.
- Permanently-failed videos are logged once at that transition, then ignored.

---

## 3. Storage

### 3.1 Files
All downloaded videos live in `./cleantube/downloaded/` (flat). The channel
name is encoded in the filename per 2.3.2.

### 3.2 SQLite database
Single file: `./cleantube/cleantube.db`. Suggested schema:

```sql
CREATE TABLE channels (
    handle              TEXT PRIMARY KEY,    -- e.g. "@kurzgesagt"
    url                 TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_checked_at     TEXT
);

CREATE TABLE videos (
    video_id            TEXT PRIMARY KEY,    -- YouTube's 11-char ID
    channel_handle      TEXT NOT NULL REFERENCES channels(handle),
    title               TEXT NOT NULL,
    upload_date         TEXT NOT NULL,       -- ISO-8601 (YYYY-MM-DD)
    duration_seconds    INTEGER,
    filepath            TEXT,                -- NULL until downloaded
    file_size_bytes     INTEGER,
    sponsorblock_cuts   INTEGER,             -- count of segments removed
    downloaded_at       TEXT,
    status              TEXT NOT NULL,       -- 'pending' | 'downloaded'
                                             -- | 'permanently_failed'
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_attempt_at     TEXT
);

CREATE INDEX idx_videos_channel_date
    ON videos(channel_handle, upload_date DESC);
```

The DB is the single source of truth for "have we already grabbed this?" —
the filesystem is not scanned.

---

## 4. Operational

### 4.1 Runtime
Python 3.11+. Dependencies:
- `yt-dlp` — download + SponsorBlock integration
- `ffmpeg` — required by yt-dlp for muxing and SponsorBlock cuts (system pkg)
- SQLite via the stdlib `sqlite3` module
- A config parser (stdlib `tomllib` is fine)

### 4.2 Invocation
The daemon runs as a long-lived foreground process (e.g.
`python -m cleantube`). It is expected to be managed by systemd so that
journalctl handles log capture, restarts, and start-on-boot.

### 4.3 Logging
Structured logs to stdout/stderr only. No file logging — the unit file is
responsible for capturing them. Each line should at minimum carry: timestamp,
level, channel/video context (when applicable), message.

### 4.4 Shutdown
SIGTERM and SIGINT must:
- Finish the current download if one is in progress (don't leave half-files).
- Skip the cooldown.
- Exit cleanly with the DB in a consistent state.

---

## 5. Explicitly out of scope (for v1)
- Web UI / TUI / any UI.
- Multi-user, multi-host, or networked deployment.
- Notification on new downloads (email, Slack, etc.).
- Disk-quota or retention management — files accumulate forever until the user
  cleans up.
- Authenticated downloads (members-only / age-restricted content).
- Subtitle download.
- Parallel downloads (serial-with-cooldown is intentional for rate-limit
  safety).

---

## 6. Notes / open questions for future iteration
- The original spec mentioned "youtube-dl will remove ads from YouTube" — this
  is not accurate. YouTube's pre-roll/mid-roll ads are injected by the
  streaming layer and are never present in downloaded files. The only ads
  Cleantube actually removes are creator-embedded sponsor reads, handled by
  SponsorBlock.
- The original spec referenced the SponsorBlock *Firefox addon*; that addon
  operates at browser playback time and is not used here. Cleantube uses the
  same underlying SponsorBlock crowd-sourced database via the public API,
  accessed through yt-dlp's `--sponsorblock-remove` flag.
