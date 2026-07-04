import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from .config import load_config
from .daemon import Daemon
from .db import Database
from .logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleantube",
        description="Daemon that downloads new YouTube subs with SponsorBlock cuts.",
    )
    parser.add_argument(
        "-c", "--config",
        default="cleantube.toml",
        help="Path to TOML config file (default: cleantube.toml).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CLEANTUBE_LOG_LEVEL", "INFO"),
        help="Log level (default: INFO).",
    )
    args = parser.parse_args(argv)

    setup_logging(level=args.log_level)
    log = logging.getLogger("cleantube")

    missing = [tool for tool in ("yt-dlp", "ffmpeg") if shutil.which(tool) is None]
    if missing:
        log.error("missing_dependencies", extra={"tools": missing})
        return 1

    config_path = Path(args.config)
    log.info("config_loading", extra={"path": str(config_path)})
    config = load_config(config_path)
    log.info(
        "config_loaded",
        extra={
            "subscriptions_path": str(config.subscriptions_path),
            "download_dir": str(config.download_dir),
            "db_path": str(config.db_path),
            "poll_interval_seconds": config.poll_interval_seconds,
        },
    )

    config.download_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)

    daemon = Daemon(config, db)
    try:
        daemon.run()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
