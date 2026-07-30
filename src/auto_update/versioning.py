from __future__ import annotations

from datetime import datetime
import re


VERSION_PATTERN = re.compile(r"^(?P<date>\d{8})(?P<sequence>\d{2})$")


def is_version_tag(value: str) -> bool:
    return VERSION_PATTERN.fullmatch(value) is not None


def next_version(previous_tag: str | None, now: datetime) -> str:
    current_date = now.strftime("%Y%m%d")
    if not previous_tag:
        return f"{current_date}01"

    match = VERSION_PATTERN.fullmatch(previous_tag)
    if not match:
        return f"{current_date}01"

    if match.group("date") != current_date:
        return f"{current_date}01"

    sequence = int(match.group("sequence")) + 1
    if sequence > 99:
        raise RuntimeError("当日版本序号已超过 99")
    return f"{current_date}{sequence:02d}"
