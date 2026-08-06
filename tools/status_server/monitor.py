"""后台监控循环: 按调度执行遍历,响应手动触发。"""
from __future__ import annotations

import datetime
import logging
import threading
from datetime import timedelta

from .checker import Checker
from .config import ScheduleConfig
from .schedule import beijing_now, next_check_at


_logger = logging.getLogger(__name__)


def monitor_loop(
    checker: Checker,
    schedule: ScheduleConfig,
    manual_event: threading.Event,
) -> None:
    next_check: datetime.datetime | None = None
    while True:
        if manual_event.is_set():
            manual_event.clear()
            _logger.info("收到手动触发,立即执行遍历")
            checker.run()
            next_check = None
        now = beijing_now()
        if next_check is None or now >= next_check:
            checker.run()
            if schedule.enabled:
                next_check = next_check_at(beijing_now(), schedule)
            else:
                next_check = beijing_now() + timedelta(seconds=schedule.interval)
        remaining = max(1.0, (next_check - beijing_now()).total_seconds())
        manual_event.wait(timeout=min(remaining, 3600))
