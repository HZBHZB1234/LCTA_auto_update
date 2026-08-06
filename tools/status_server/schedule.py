"""更新窗口调度: 判断窗口、对齐到遍历间隔、计算下次检查时间。"""
from __future__ import annotations

import datetime
from datetime import timedelta, timezone

from .config import ScheduleConfig


_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def beijing_now() -> datetime.datetime:
    return datetime.datetime.now(_BEIJING)


def is_in_update_window(
    now: datetime.datetime, schedule: ScheduleConfig
) -> bool:
    if now.weekday() != schedule.update_dow:
        return False
    return schedule.start_hour <= now.hour < schedule.end_hour


_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def weekday_name(update_dow: int) -> str:
    return _WEEKDAY_NAMES[update_dow]


def next_check_at(
    now: datetime.datetime, schedule: ScheduleConfig
) -> datetime.datetime:
    if is_in_update_window(now, schedule):
        window_start = _window_start(now, schedule)
        aligned = window_start + timedelta(
            seconds=_aligned_slots(now, schedule) * schedule.interval
        )
        window_end = now.replace(hour=schedule.end_hour, minute=0, second=0, microsecond=0)
        if aligned < window_end:
            return aligned
    return _next_window_start(now, schedule)


def _window_start(day: datetime.datetime, schedule: ScheduleConfig) -> datetime.datetime:
    return day.replace(
        hour=schedule.start_hour, minute=0, second=0, microsecond=0
    )


def _aligned_slots(now: datetime.datetime, schedule: ScheduleConfig) -> int:
    elapsed = (now - _window_start(now, schedule)).total_seconds()
    if elapsed <= 0:
        return 1
    return int(elapsed // schedule.interval) + 1


def _next_window_start(
    now: datetime.datetime, schedule: ScheduleConfig
) -> datetime.datetime:
    days_ahead = (schedule.update_dow - now.weekday()) % 7
    if days_ahead == 0 and _window_start(now, schedule) <= now:
        days_ahead = 7
    return _window_start(now + timedelta(days=days_ahead), schedule)
