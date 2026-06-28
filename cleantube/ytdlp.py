import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_FS_BAD = re.compile(r"[\x00-\x1f/]")
_SPONSOR_FOUND_RE = re.compile(
    r"\[SponsorBlock\][^\n]*?(\d+)\s+\w+\s+segment", re.IGNORECASE
)
_SPONSOR_REMOVED_RE = re.compile(
    r"Removed?\s+(\d+)\s+segments", re.IGNORECASE
)
_SPONSOR_CHAPTER_RE = re.compile(
    r"\[ModifyChapters\][^\n]*Removing chapter", re.IGNORECASE
)


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


def fetch_channel_video_ids(channel_url: str) -> list[str]:
    """Return video IDs from a channel's /videos tab, newest first."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print", "%(id)s",
        "--no-warnings",
        channel_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
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


def build_output_path(
    download_dir: Path,
    channel_handle: str,
    upload_date: str,
    title: str,
    video_id: str,
) -> Path:
    handle_bare = _sanitize_component(channel_handle.lstrip("@"))
    sanitized_title = _sanitize_component(title)
    # Leave headroom inside the 255-byte component limit for the prefix/suffix.
    suffix_len = len(handle_bare) + len(upload_date) + len(video_id) + len(" -  -  [].mp4")
    max_title_len = max(20, 240 - suffix_len)
    if len(sanitized_title) > max_title_len:
        sanitized_title = sanitized_title[:max_title_len].rstrip()
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
        "-o", _escape_template(str(output_path)),
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
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
        matches = sorted(final_path.parent.glob(final_path.stem + ".*"))
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
    chapter_removals = sum(1 for l in lines if _SPONSOR_CHAPTER_RE.search(l))
    if chapter_removals:
        return chapter_removals
    for l in lines:
        m = _SPONSOR_REMOVED_RE.search(l)
        if m:
            return int(m.group(1))
    total = 0
    for l in lines:
        m = _SPONSOR_FOUND_RE.search(l)
        if m:
            total += int(m.group(1))
    return total
