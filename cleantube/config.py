import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cleantube")


@dataclass(frozen=True)
class Config:
    backfill_count: int
    poll_interval_seconds: int
    post_download_cooldown_seconds: int
    download_dir: Path
    video_format: str
    max_download_attempts: int
    db_path: Path
    subscriptions_path: Path
    web_enabled: bool
    web_host: str
    web_port: int


_DEFAULTS = {
    "backfill_count": 3,
    "poll_interval_seconds": 3600,
    "post_download_cooldown_seconds": 1800,
    "download_dir": "./cleantube/downloaded",
    "video_format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "max_download_attempts": 3,
    "db_path": "./cleantube/cleantube.db",
    "subscriptions_path": "./subscriptions.txt",
    "web_enabled": True,
    "web_host": "0.0.0.0",
    "web_port": 8320,
}


def load_config(path: Path) -> Config:
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
    for key in sorted(set(data) - set(_DEFAULTS)):
        log.warning("config_unknown_key", extra={"key": key})
    merged = {**_DEFAULTS, **data}
    config = Config(
        backfill_count=int(merged["backfill_count"]),
        poll_interval_seconds=int(merged["poll_interval_seconds"]),
        post_download_cooldown_seconds=int(merged["post_download_cooldown_seconds"]),
        download_dir=Path(merged["download_dir"]),
        video_format=str(merged["video_format"]),
        max_download_attempts=int(merged["max_download_attempts"]),
        db_path=Path(merged["db_path"]),
        subscriptions_path=Path(merged["subscriptions_path"]),
        web_enabled=bool(merged["web_enabled"]),
        web_host=str(merged["web_host"]),
        web_port=int(merged["web_port"]),
    )
    if config.backfill_count < 0:
        raise ValueError("backfill_count must be >= 0")
    if config.poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")
    if config.post_download_cooldown_seconds < 0:
        raise ValueError("post_download_cooldown_seconds must be >= 0")
    if config.max_download_attempts < 1:
        raise ValueError("max_download_attempts must be >= 1")
    if not 1 <= config.web_port <= 65535:
        raise ValueError("web_port must be between 1 and 65535")
    return config
