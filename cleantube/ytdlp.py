import glob
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_FS_BAD = re.compile(r"[\x00-\x1f/]")
# yt-dlp's SponsorBlockPP prints exactly:
#   [SponsorBlock] Found <N> segments in the SponsorBlock database
# (or "No matching segments were found ..." when there are none).
_SPONSOR_FOUND_RE = re.compile(
    r"\[SponsorBlock\] Found (\d+) segments?", re.IGNORECASE
)

# Kill switch for a yt-dlp stalled on a dead connection; --socket-timeout
# catches most hangs, this catches the rest. Downloads get no such cap.
_METADATA_TIMEOUT = 600
_SOCKET_TIMEOUT_ARGS = ["--socket-timeout", "30"]


@dataclass(frozen=True)
class VideoMeta:
    video_id: str
    title: str
    upload_date: str  # YYYY-MM-DD
    duration_seconds: int | None
    channel_handle: str | None  # "@handle" or None


@dataclass(frozen=True)
class DownloadResult:
    filepath: Path
    file_size_bytes: int
    sponsorblock_cuts: int


def fetch_channel_video_ids(channel_url: str, limit: int) -> list[str]:
    """Return the newest `limit` video IDs from a channel's /videos tab."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--playlist-items", f":{limit}",
        "--print", "%(id)s",
        "--no-warnings",
        *_SOCKET_TIMEOUT_ARGS,
        channel_url,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
        timeout=_METADATA_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp flat playlist failed (rc={result.returncode}): "
            f"{result.stderr.strip()[-500:]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def fetch_video_metadata(video_id: str) -> VideoMeta:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        *_SOCKET_TIMEOUT_ARGS,
        url,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
        timeout=_METADATA_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp metadata failed (rc={result.returncode}): "
            f"{result.stderr.strip()[-500:]}"
        )
    info = json.loads(result.stdout)

    raw_date = info.get("upload_date") or ""
    if len(raw_date) == 8 and raw_date.isdigit():
        upload_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        upload_date = ""

    handle = None
    candidate = info.get("uploader_id") or info.get("channel_url", "")
    if isinstance(candidate, str):
        if candidate.startswith("@"):
            handle = candidate
        else:
            m = re.search(r"@([A-Za-z0-9._-]+)", candidate)
            if m:
                handle = "@" + m.group(1)

    duration = info.get("duration")
    return VideoMeta(
        video_id=info["id"],
        title=info.get("title", "") or "",
        upload_date=upload_date,
        duration_seconds=int(duration) if duration else None,
        channel_handle=handle,
    )


def _sanitize_component(s: str) -> str:
    return _FS_BAD.sub("_", s).strip()


def _truncate_utf8(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def build_output_path(
    download_dir: Path,
    channel_handle: str,
    upload_date: str,
    title: str,
    video_id: str,
) -> Path:
    handle_bare = _sanitize_component(channel_handle.lstrip("@"))
    sanitized_title = _sanitize_component(title)
    # Filename components are capped at 255 *bytes* on Linux, and yt-dlp's
    # intermediate files (".f<id>.<ext>", ".part") need room on top of the
    # final name, so budget 240 bytes and truncate the title to fit.
    overhead = len(
        f"{handle_bare} - {upload_date} -  [{video_id}].mp4".encode("utf-8")
    )
    sanitized_title = _truncate_utf8(sanitized_title, max(20, 240 - overhead))
    filename = f"{handle_bare} - {upload_date} - {sanitized_title} [{video_id}].mp4"
    return download_dir / filename


def _escape_template(s: str) -> str:
    return s.replace("%", "%%")


def download_video(
    *,
    video_id: str,
    output_path: Path,
    video_format: str,
    register_process: Callable[[subprocess.Popen | None], None],
) -> DownloadResult:
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "-f", video_format,
        "--merge-output-format", "mp4",
        "--sponsorblock-remove", "default",
        "--no-progress",
        "--no-warnings",
        "--newline",
        *_SOCKET_TIMEOUT_ARGS,
        "-o", _escape_template(str(output_path)),
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # Own session, so a terminal Ctrl-C reaches only the daemon and the
        # in-flight download can run to completion (see requirement 4.4).
        start_new_session=True,
    )
    register_process(proc)
    try:
        output_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            log.debug("yt-dlp: %s", line)
        rc = proc.wait()
    finally:
        register_process(None)

    if rc != 0:
        tail = "\n".join(output_lines[-10:])
        raise RuntimeError(f"yt-dlp download failed (rc={rc}): {tail}")

    final_path = output_path
    if not final_path.exists():
        # The stem contains "[<video_id>]", which glob reads as a character
        # class — escape it or the fallback never matches.
        matches = sorted(final_path.parent.glob(glob.escape(final_path.stem) + ".*"))
        if not matches:
            raise RuntimeError(f"output file not found: {final_path}")
        final_path = matches[0]

    file_size = final_path.stat().st_size
    sponsorblock_cuts = _count_sponsorblock_cuts(output_lines)
    return DownloadResult(
        filepath=final_path,
        file_size_bytes=file_size,
        sponsorblock_cuts=sponsorblock_cuts,
    )


def _count_sponsorblock_cuts(lines: list[str]) -> int:
    for line in lines:
        m = _SPONSOR_FOUND_RE.search(line)
        if m:
            return int(m.group(1))
    return 0
