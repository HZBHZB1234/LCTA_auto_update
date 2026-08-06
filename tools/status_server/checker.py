"""一次遍历: 更新游戏 → 扫描 assets → 更新状态 → 触发 dispatch。"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from . import github, steamcmd
from .config import ServerConfig, steam_asset_path
from .state import StateStore
from .tokens import extract_tokens, verify_token


_logger = logging.getLogger(__name__)


class StatusHolder:
    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def set(self, payload: dict[str, Any] | None) -> None:
        with self._lock:
            self._payload = payload

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            return self._payload


def build_payload(
    token: str,
    f_token: str | None,
    *,
    created_at: datetime.datetime | None = None,
    hash_value: str | None = None,
) -> dict[str, Any]:
    latest: dict[str, Any] = {
        "created_at": (created_at or datetime.datetime.now()).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "f_token": f_token,
        "token": token,
    }
    if hash_value is not None:
        latest["hash"] = hash_value
    return {"latest_token": latest}


class Checker:
    """一次遍历: 更新游戏 → 扫描 assets → 更新状态 → 触发 dispatch。

    更新源选择: 显式配置的 asset 优先(直接扫描,不跑 steamcmd);否则
    优先使用已登录 Steam 客户端安装的版本(游戏由 Steam 自动更新);
    Steam 资产不存在时回退 steamcmd;steamcmd 失败后再检查 Steam
    资产是否已出现,有则回退扫描。

    首次运行(状态文件无基线)时把当前 token 记录为基线,不触发
    dispatch,避免把已存在多时的 token 误报为新 token。
    """

    def __init__(
        self,
        config: ServerConfig,
        holder: StatusHolder,
        state: StateStore,
        asset: Path,
        config_dir: Path,
    ) -> None:
        self._config = config
        self._holder = holder
        self._state = state
        self._asset = asset
        self._config_dir = config_dir
        self._last_token: str | None = None
        self._verified_token: str | None = None

    def _pick_asset(self) -> Path:
        if self._config.asset:
            _logger.info("使用显式配置的 asset: %s", self._asset)
            return self._asset
        steam_asset = steam_asset_path(self._config, self._config_dir)
        if steam_asset.is_file():
            _logger.info("优先使用 Steam 客户端安装的版本 (%s),跳过 steamcmd", steam_asset)
            return steam_asset
        if self._config.steamcmd.path:
            steamcmd.run_steamcmd(self._config.steamcmd)
        asset = next(
            (p for p in (steam_asset, self._asset) if p.is_file()),
            self._asset,
        )
        if asset == steam_asset and self._config.steamcmd.path:
            _logger.warning("steamcmd 更新失败,回退扫描 Steam 安装的版本")
        return asset

    def run(self) -> None:
        asset = self._pick_asset()
        if not asset.is_file():
            _logger.warning("%s 不存在,跳过本次扫描", asset)
            return
        token, f_token = extract_tokens(asset.read_bytes())
        if token is None:
            _logger.warning("%s 未发现 CDN token", self._asset)
            return

        baseline = self._state.baseline
        first_run = not baseline
        is_new = not first_run and token != self._last_token and token != baseline
        self._last_token = token
        if is_new:
            _logger.info(
                "发现新 token %s,等待 %d 秒确认稳定",
                token,
                self._config.stability,
            )
            if self._config.stability:
                time.sleep(self._config.stability)
        if token != baseline:
            self._state.set_seen(token)

        hash_value: str | None = None
        if self._config.verify and token != self._verified_token:
            self._verified_token = token
            hash_value = verify_token(token)
            _logger.info("token %s 校验: %s", token, hash_value)
        self._holder.set(build_payload(token, f_token, hash_value=hash_value))
        _logger.info("扫描到最新 token: %s (f_token=%s)", token, f_token)

        if first_run:
            self._state.set_dispatched(token)
            _logger.info(
                "首次运行: 将 %s 记录为基线,不触发 dispatch", token
            )
            return
        if self._config.dispatch and token != self._state.dispatched_token:
            try:
                github.dispatch_update(self._config, token)
            except (HTTPError, URLError, OSError, RuntimeError) as exc:
                _logger.error("token %s 触发 dispatch 失败: %s", token, exc)
            else:
                self._state.set_dispatched(token)
                _logger.info(
                    "已对 token %s 触发 %s", token, self._config.event_type
                )
