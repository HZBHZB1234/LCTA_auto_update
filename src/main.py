from __future__ import annotations

import logging
import os
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SOURCE_ROOT.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from auto_update.config import AppConfig
from auto_update.runner import run


def configure_logging() -> None:
    log_path = SOURCE_ROOT / "app.log"
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def main() -> int:
    configure_logging()
    config_path = Path(
        os.getenv("LCTA_CONFIG", str(SOURCE_ROOT / "config.json"))
    )
    try:
        config = AppConfig.load(config_path)
        run(PROJECT_ROOT, config)
    except Exception:
        logging.getLogger(__name__).exception("自动更新失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
