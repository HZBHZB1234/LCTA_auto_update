#!/usr/bin/env python3
"""LCTA 自托管状态服务。

游戏主要在每周四北京时间 10:00-13:00 更新。服务在该窗口内每 15 分钟
遍历一次: 先执行 steamcmd 更新游戏,再扫描 resources.assets 提取最新
CDN token,提供与 limbus-api.voidfissure.de/api/status 相同格式的 HTTP
接口,并在发现新 token 时自动调用 GitHub repository_dispatch 触发自动
更新工作流。窗口外不运行,保留 POST /api/check 手动触发作为兜底。

配置全部来自 YAML(不使用命令行参数),按以下优先级查找:
    1. 环境变量 LCTA_STATUS_CONFIG 指定的路径(必须存在)
    2. 脚本同目录的 config.yaml(本地配置,不提交到仓库)
    3. 脚本同目录的 default_config.yaml(仓库内默认模板)

用法:
    python tools/status_server.py
"""
from __future__ import annotations

import datetime
import http.server
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = SCRIPT_DIR / "config.yaml"
DEFAULT_CONFIG = SCRIPT_DIR / "default_config.yaml"

STEAM_DEFAULT = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Limbus Company"
    r"\LimbusCompany_Data\resources.assets"
)

L_TOKEN_RE = re.compile(
    rb"downloadcommon\.limbuscompanycdn\.org/(l\d{8}_[A-Za-z0-9_-]+)"
)
F_TOKEN_RE = re.compile(
    rb"downloadfmod\.limbuscompanycdn\.org/(f\d{8}_[A-Za-z0-9_-]+)"
)

GITHUB_API_ROOT = "https://api.github.com"
_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

_logger = logging.getLogger("lcta-status-server")


class ConfigError(ValueError):
    """配置文件无效。"""


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    update_dow: int
    start_hour: int
    end_hour: int
    interval: int


@dataclass(frozen=True)
class SteamcmdConfig:
    path: str
    app_id: int
    install_dir: str
    login: str
    timeout: int


@dataclass(frozen=True)
class ServerConfig:
    asset: str
    host: str
    port: int
    stability: int
    schedule: ScheduleConfig
    steamcmd: SteamcmdConfig
    repository: str
    event_type: str
    token: str
    state: Path
    dispatch: bool
    verify: bool

    @classmethod
    def load(cls, path: Path) -> "ServerConfig":
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在: {path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件不是有效 YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件根节点必须是对象")

        base_dir = path.resolve().parent
        server = _object(raw, "server")
        polling = _object(raw, "polling")
        schedule = _object(raw, "schedule")
        steamcmd = _object(raw, "steamcmd")
        github = _object(raw, "github")

        start_hour = _integer(schedule, "start_hour", minimum=0, maximum=23)
        end_hour = _integer(schedule, "end_hour", minimum=1, maximum=24)
        if start_hour >= end_hour:
            raise ConfigError("schedule.start_hour 必须小于 end_hour")
        app_id = _integer(steamcmd, "app_id", minimum=1, maximum=2**31 - 1)

        return cls(
            asset=_optional_string(raw, "asset"),
            host=_string(server, "host"),
            port=_integer(server, "port", minimum=1, maximum=65535),
            stability=_integer(polling, "stability", minimum=0, maximum=3600),
            schedule=ScheduleConfig(
                enabled=_boolean(schedule, "enabled"),
                update_dow=_integer(schedule, "update_dow", minimum=0, maximum=6),
                start_hour=start_hour,
                end_hour=end_hour,
                interval=_integer(schedule, "interval", minimum=1, maximum=86400),
            ),
            steamcmd=SteamcmdConfig(
                path=_optional_string(steamcmd, "path"),
                app_id=app_id,
                install_dir=_optional_string(steamcmd, "install_dir"),
                login=_string(steamcmd, "login"),
                timeout=_integer(steamcmd, "timeout", minimum=60, maximum=86400),
            ),
            repository=_repository(github, "repository"),
            event_type=_string(github, "event_type"),
            token=_optional_string(github, "token"),
            state=_resolve_path(_string(raw, "state"), base_dir),
            dispatch=_boolean(raw, "dispatch"),
            verify=_boolean(raw, "verify"),
        )


def find_config() -> Path:
    env_path = os.getenv("LCTA_STATUS_CONFIG", "")
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise ConfigError(f"LCTA_STATUS_CONFIG 指向的文件不存在: {path}")
        return path
    if LOCAL_CONFIG.is_file():
        return LOCAL_CONFIG
    return DEFAULT_CONFIG


def resolve_asset(config: ServerConfig, config_dir: Path) -> Path:
    if config.asset:
        path = Path(config.asset)
        return path if path.is_absolute() else config_dir / path
    if config.steamcmd.install_dir:
        install_dir = Path(config.steamcmd.install_dir)
        if not install_dir.is_absolute():
            install_dir = config_dir / install_dir
        return install_dir / "LimbusCompany_Data" / "resources.assets"
    if STEAM_DEFAULT.exists():
        return STEAM_DEFAULT
    raise ConfigError(
        "asset 与 steamcmd.install_dir 均未配置,且默认 Steam 安装路径不存在,"
        "请在 config.yaml 中指定"
    )


def ensure_config_ready(config: ServerConfig) -> None:
    if config.dispatch and not config.token:
        raise ConfigError(
            "github.token 为空且 dispatch 已启用;请在本地 config.yaml 中填写 "
            "(该文件不会提交到仓库)"
        )


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 必须是对象")
    return value


def _string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value.strip()


def _boolean(parent: dict[str, Any], key: str) -> bool:
    value = parent.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是布尔值")
    return value


def _integer(
    parent: dict[str, Any], key: str, *, minimum: int, maximum: int
) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} 必须位于 {minimum} 到 {maximum} 之间")
    return value


def _repository(parent: dict[str, Any], key: str) -> str:
    value = _string(parent, key)
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ConfigError(f"{key} 必须使用 owner/repository 格式")
    return value


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


# ---------------------------------------------------------------- 调度


def beijing_now() -> datetime.datetime:
    return datetime.datetime.now(_BEIJING)


def is_in_update_window(
    now: datetime.datetime, schedule: ScheduleConfig
) -> bool:
    if now.weekday() != schedule.update_dow:
        return False
    return schedule.start_hour <= now.hour < schedule.end_hour


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


# ---------------------------------------------------------------- token


def extract_tokens(data: bytes) -> tuple[str | None, str | None]:
    l_tokens = {m.group(1).decode("ascii") for m in L_TOKEN_RE.finditer(data)}
    f_tokens = {m.group(1).decode("ascii") for m in F_TOKEN_RE.finditer(data)}
    pick = lambda s: max(s, key=lambda t: t[1:9]) if s else None
    return pick(l_tokens), pick(f_tokens)


def verify_token(token: str) -> str | None:
    url = (
        f"https://downloadcommon.limbuscompanycdn.org/{token}"
        "/Assets/LocalizePatch/LocalizePatchInfo.hash"
    )
    req = urllib_request.Request(
        url,
        headers={
            "User-Agent": "UnityPlayer/6000.3.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.read().decode("utf-8", "replace").strip() or None
    except Exception as exc:
        return f"<verify failed: {exc}>"


# ---------------------------------------------------------------- steamcmd


def build_steamcmd_args(config: SteamcmdConfig) -> list[str]:
    args = [config.path]
    if config.install_dir:
        args += ["+force_install_dir", config.install_dir]
    args += ["+login", config.login, "+app_update", str(config.app_id), "+quit"]
    return args


def run_steamcmd(config: SteamcmdConfig) -> bool:
    args = build_steamcmd_args(config)
    _logger.info("执行 steamcmd: %s", " ".join(args))
    try:
        completed = subprocess.run(
            args,
            timeout=config.timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.error("steamcmd 执行失败: %s", exc)
        return False
    tail = " | ".join((completed.stdout or "").strip().splitlines()[-5:])
    if completed.returncode != 0:
        _logger.error("steamcmd 退出码 %d: %s", completed.returncode, tail)
        return False
    _logger.info("steamcmd 完成 (退出码 0): %s", tail)
    return True


# ---------------------------------------------------------------- dispatch


def dispatch_update(config: ServerConfig, token: str) -> None:
    url = f"{GITHUB_API_ROOT}/repos/{config.repository}/dispatches"
    body = json.dumps({"event_type": config.event_type}).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "User-Agent": "LCTA-status-server",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib_request.urlopen(req, timeout=30) as response:
        if response.status != 204:
            raise RuntimeError(f"repository_dispatch 返回 {response.status}")


# ---------------------------------------------------------------- state


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

    def set_dispatched(self, token: str) -> None:
        self._data["dispatched_token"] = token
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------- payload


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


# ---------------------------------------------------------------- 遍历


class Checker:
    """一次遍历: steamcmd 更新 → 扫描 assets → 更新状态 → 触发 dispatch。"""

    def __init__(
        self,
        config: ServerConfig,
        holder: StatusHolder,
        state: StateStore,
        asset: Path,
    ) -> None:
        self._config = config
        self._holder = holder
        self._state = state
        self._asset = asset
        self._last_token: str | None = None
        self._verified_token: str | None = None

    def run(self) -> None:
        if self._config.steamcmd.path:
            run_steamcmd(self._config.steamcmd)
        if not self._asset.is_file():
            _logger.warning("%s 不存在,跳过本次扫描", self._asset)
            return
        token, f_token = extract_tokens(self._asset.read_bytes())
        if token is None:
            _logger.warning("%s 未发现 CDN token", self._asset)
            return

        if token != self._last_token:
            self._last_token = token
            _logger.info(
                "发现新 token %s,等待 %d 秒确认稳定",
                token,
                self._config.stability,
            )
            if self._config.stability:
                time.sleep(self._config.stability)

        hash_value: str | None = None
        if self._config.verify and token != self._verified_token:
            self._verified_token = token
            hash_value = verify_token(token)
            _logger.info("token %s 校验: %s", token, hash_value)
        self._holder.set(build_payload(token, f_token, hash_value=hash_value))
        _logger.info("扫描到最新 token: %s (f_token=%s)", token, f_token)

        if (
            self._config.dispatch
            and token != self._state.dispatched_token
        ):
            try:
                dispatch_update(self._config, token)
            except (HTTPError, URLError, OSError, RuntimeError) as exc:
                _logger.error("token %s 触发 dispatch 失败: %s", token, exc)
            else:
                self._state.set_dispatched(token)
                _logger.info(
                    "已对 token %s 触发 %s", token, self._config.event_type
                )


# ---------------------------------------------------------------- monitor


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


# ---------------------------------------------------------------- http


def make_handler(
    holder: StatusHolder, manual_event: threading.Event
) -> type[http.server.BaseHTTPRequestHandler]:
    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/api/status":
                self._send_json(404, {"error": "not found"})
                return
            payload = holder.get()
            if payload is None:
                self._send_json(503, {"error": "no token found yet"})
            else:
                self._send_json(200, payload)

        def do_POST(self) -> None:
            if self.path != "/api/check":
                self._send_json(404, {"error": "not found"})
                return
            manual_event.set()
            self._send_json(202, {"status": "accepted"})

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            _logger.info("%s - %s", self.address_string(), fmt % args)

    return StatusHandler


# ---------------------------------------------------------------- main


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
        asset = resolve_asset(config, config_path.resolve().parent)
    except ConfigError as exc:
        _logger.error("%s", exc)
        return 1

    schedule = config.schedule
    if schedule.enabled:
        _logger.info(
            "调度: 每周%d %02d:00-%02d:00(北京时间),每 %d 秒遍历一次",
            schedule.update_dow,
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
    checker = Checker(config, holder, state, asset)
    manual_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_loop,
        args=(checker, schedule, manual_event),
        daemon=True,
    )
    monitor.start()

    server = http.server.ThreadingHTTPServer(
        (config.host, config.port), make_handler(holder, manual_event)
    )
    server.daemon_threads = True
    _logger.info(
        "状态服务已启动: http://%s:%d/api/status (state=%s, 手动触发 POST /api/check)",
        config.host,
        config.port,
        config.state,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("正在停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
