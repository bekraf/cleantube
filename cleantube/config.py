import tomllib
from dataclasses import dataclass
from pathlib import Path


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


_DEFAULTS = {
    "backfill_count": 3,
    "poll_interval_seconds": 3600,
    "post_download_cooldown_seconds": 1800,
    "download_dir": "./cleantube/downloaded",
    "video_format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "max_download_attempts": 3,
    "db_path": "./cleantube/cleantube.db",
    "subscriptions_path": "./subscriptions.txt",
}


def load_config(path: Path) -> Config:
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
    merged = {**_DEFAULTS, **data}
    return Config(
        backfill_count=int(merged["backfill_count"]),
        poll_interval_seconds=int(merged["poll_interval_seconds"]),
        post_download_cooldown_seconds=int(merged["post_download_cooldown_seconds"]),
        download_dir=Path(merged["download_dir"]),
        video_format=str(merged["video_format"]),
        max_download_attempts=int(merged["max_download_attempts"]),
        db_path=Path(merged["db_path"]),
        subscriptions_path=Path(merged["subscriptions_path"]),
    )
