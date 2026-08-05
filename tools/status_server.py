#!/usr/bin/env python3
"""LCTA 自托管状态服务。

扫描本地游戏安装的 resources.assets 提取最新 CDN 下载 token,提供与
limbus-api.voidfissure.de/api/status 相同格式的 HTTP 接口,并在发现新
token 时自动调用 GitHub repository_dispatch 触发自动更新工作流,替代
对外部服务的依赖。

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
import sys
import threading
import time
from dataclasses import dataclass
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

_logger = logging.getLogger("lcta-status-server")


class ConfigError(ValueError):
    """配置文件无效。"""


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class ServerConfig:
    asset: str
    host: str
    port: int
    interval: int
    stability: int
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
        github = _object(raw, "github")

        asset = _optional_string(raw, "asset")
        repository = _repository(github, "repository")
        state = _resolve_path(_string(raw, "state"), base_dir)

        return cls(
            asset=asset,
            host=_string(server, "host"),
            port=_integer(server, "port", minimum=1, maximum=65535),
            interval=_integer(polling, "interval", minimum=1, maximum=86400),
            stability=_integer(polling, "stability", minimum=0, maximum=86400),
            repository=repository,
            event_type=_string(github, "event_type"),
            token=_optional_string(github, "token"),
            state=state,
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
    if not config.asset:
        if STEAM_DEFAULT.exists():
            return STEAM_DEFAULT
        raise ConfigError(
            "asset 未配置,且默认 Steam 安装路径不存在,请在 config.yaml 中指定"
        )
    path = Path(config.asset)
    if not path.is_absolute():
        path = config_dir / path
    return path


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


# ---------------------------------------------------------------- monitor


def monitor_loop(
    config: ServerConfig,
    holder: StatusHolder,
    state: StateStore,
    asset: Path,
) -> None:
    last_mtime: float | None = None
    last_size: int | None = None
    candidate: str | None = None
    stable_since: float | None = None
    verified_token: str | None = None
    hash_value: str | None = None

    while True:
        try:
            if not asset.is_file():
                if last_mtime is not None:
                    _logger.warning("%s 不存在", asset)
                    last_mtime = None
                time.sleep(config.interval)
                continue

            mtime = asset.stat().st_mtime
            size = asset.stat().st_size
            if (mtime, size) != (last_mtime, last_size):
                last_mtime, last_size = mtime, size
                token, f_token = extract_tokens(asset.read_bytes())
                if token is None:
                    _logger.warning("%s 未发现 CDN token", asset)
                else:
                    now = time.time()
                    if token != candidate:
                        candidate = token
                        stable_since = now
                    if config.verify and token != verified_token:
                        verified_token = token
                        hash_value = verify_token(token)
                        _logger.info("token %s 校验: %s", token, hash_value)
                    holder.set(
                        build_payload(
                            token, f_token, hash_value=hash_value
                        )
                    )
                    _logger.info("扫描到最新 token: %s (f_token=%s)", token, f_token)

            if (
                config.dispatch
                and candidate
                and candidate != state.dispatched_token
                and stable_since is not None
            ):
                remaining = config.stability - (time.time() - stable_since)
                if remaining <= 0:
                    try:
                        dispatch_update(config, candidate)
                    except (HTTPError, URLError, OSError, RuntimeError) as exc:
                        _logger.error(
                            "token %s 触发 dispatch 失败: %s (下轮重试)",
                            candidate,
                            exc,
                        )
                    else:
                        state.set_dispatched(candidate)
                        _logger.info("已对 token %s 触发 %s", candidate, config.event_type)
                else:
                    _logger.info("token %s 等待稳定窗口 (%.0fs)", candidate, remaining)
        except OSError as exc:
            _logger.error("轮询出错: %s", exc)
        time.sleep(config.interval)


# ---------------------------------------------------------------- http


def make_handler(holder: StatusHolder) -> type[http.server.BaseHTTPRequestHandler]:
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

    _logger.info("配置: %s", config_path)
    _logger.info("asset=%s host=%s port=%d dispatch=%s", asset, config.host, config.port, config.dispatch)

    holder = StatusHolder()
    state = StateStore(config.state)
    monitor = threading.Thread(
        target=monitor_loop,
        args=(config, holder, state, asset),
        daemon=True,
    )
    monitor.start()

    server = http.server.ThreadingHTTPServer(
        (config.host, config.port), make_handler(holder)
    )
    server.daemon_threads = True
    _logger.info(
        "状态服务已启动: http://%s:%d/api/status (state=%s)",
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
