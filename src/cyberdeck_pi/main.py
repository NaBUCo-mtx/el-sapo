import argparse
import logging
from pathlib import Path

import uvicorn

from cyberdeck_pi.config import load_settings
from cyberdeck_pi.logging_setup import setup_logging
from cyberdeck_pi.web.app import create_app

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cyberdeck-pi")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/dev.toml"),
        help="Path to a TOML settings file (default: config/dev.toml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    setup_logging()

    settings = load_settings(args.config)
    log.info("Loaded config from %s", args.config)

    app = create_app(settings, config_path=args.config)
    # log_config=None: keep our own logging_setup() formatting instead of
    # letting uvicorn install its own handlers on top of it.
    uvicorn.run(app, host=settings.web.host, port=settings.web.port, log_config=None)


if __name__ == "__main__":
    main()
