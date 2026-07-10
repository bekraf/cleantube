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

- **Python 3.11+**
- **yt-dlp**
- **ffmpeg**

## Running

```sh
python -m cleantube                    # uses ./cleantube.toml if present
python -m cleantube -c /path/to.toml
```

Configuration lives in `cleantube.toml` (gitignored, machine-local); copy
[cleantube.example.toml](cleantube.example.toml) to get started. Every key is
optional and documented there. 

[subscriptions.txt](subscriptions.txt) is one channel URL per
line (`@handle` or `/channel/UC…`), `#` can be used for comments. Example subscriptions file:

```text
# science
https://www.youtube.com/@kurzgesagt
https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q
```

## Web portal

The daemon serves a small web portal (default `http://<host>:8320`, stdlib
only, no dependencies) with three tabs:

- **Dashboard** — live stats: scan/download schedule, queue, library totals,
  error rates and download charts.
- **Dagboek** — a diary timeline; scroll up through the download history
  (failures tinted red), scroll down into the queued future with estimated
  download times. Click any video to jump to its detail page.
- **Video** — every database field of the selected (or latest) video.

There is no authentication; it is meant for the local network. Configure with
`web_enabled`, `web_host` and `web_port` in `cleantube.toml`.
