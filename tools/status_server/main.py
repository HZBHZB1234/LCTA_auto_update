"""启动入口: 加载配置、启动监控线程、运行 Flask 服务。"""
from __future__ import annotations

import logging
import sys
import threading

from .app import create_app
from .checker import Checker, StatusHolder
from .config import (
    ConfigError,
    ServerConfig,
    ensure_config_ready,
    find_config,
    resolve_asset,
)
from .monitor import monitor_loop
from .schedule import weekday_name
from .state import StateStore


_logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    _configure_logging()
    try:
        config_path = find_config()
        config = ServerConfig.load(config_path)
        ensure_config_ready(config)
        config_dir = config_path.resolve().parent
        asset = resolve_asset(config, config_dir)
    except ConfigError as exc:
        _logger.error("%s", exc)
        return 1

    schedule = config.schedule
    if schedule.enabled:
        _logger.info(
            "调度: 每%s %02d:00-%02d:00(北京时间),每 %d 秒遍历一次",
            weekday_name(schedule.update_dow),
            schedule.start_hour,
            schedule.end_hour,
            schedule.interval,
        )
    else:
        _logger.info("调度已禁用,每 %d 秒遍历一次", schedule.interval)
    _logger.info(
        "asset=%s host=%s port=%d steamcmd=%s dispatch=%s",
        asset,
        config.host,
        config.port,
        config.steamcmd.path or "(未启用)",
        config.dispatch,
    )

    holder = StatusHolder()
    state = StateStore(config.state)
    checker = Checker(config, holder, state, asset, config_dir)
    manual_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_loop,
        args=(checker, schedule, manual_event),
        daemon=True,
    )
    monitor.start()

    _logger.info(
        "状态服务已启动: http://%s:%d/api/status (state=%s, 手动触发 POST /api/check)",
        config.host,
        config.port,
        config.state,
    )
    create_app(holder, manual_event).run(
        host=config.host, port=config.port, threaded=True
    )
    return 0
