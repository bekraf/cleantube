import re
from pathlib import Path

_HANDLE_RE = re.compile(r"@([A-Za-z0-9._-]+)")


def read_subscriptions(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in seen:
            continue
        seen.add(stripped)
        urls.append(stripped)
    return urls


def channel_videos_url(channel_url: str) -> str:
    url = channel_url.rstrip("/")
    if url.endswith("/videos") or url.endswith("/streams") or url.endswith("/shorts"):
        return url
    return url + "/videos"


def extract_handle(channel_url: str) -> str | None:
    match = _HANDLE_RE.search(channel_url)
    if match:
        return "@" + match.group(1)
    return None
