"""已触发 token 的 JSON 状态持久化。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def dispatched_token(self) -> str:
        return str(self._data.get("dispatched_token") or "")

    @property
    def last_token(self) -> str:
        return str(self._data.get("last_token") or "")

    @property
    def baseline(self) -> str:
        """首次运行判定的基线: 最近见过的 token,兼容只有 dispatched_token 的旧状态文件。"""
        return self.last_token or self.dispatched_token

    def set_seen(self, token: str) -> None:
        self._data["last_token"] = token
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_dispatched(self, token: str) -> None:
        self._data["dispatched_token"] = token
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
