"""配置模型、YAML 加载与路径解析。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = SCRIPT_DIR / "config.yaml"
DEFAULT_CONFIG = SCRIPT_DIR / "default_config.yaml"

STEAM_DEFAULT = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Limbus Company"
    r"\LimbusCompany_Data\resources.assets"
)


class ConfigError(ValueError):
    """配置文件无效。"""


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    update_dow: int
    start_hour: int
    end_hour: int
    interval: int


@dataclass(frozen=True)
class SteamConfig:
    install_dir: str


@dataclass(frozen=True)
class SteamcmdConfig:
    path: str
    app_id: int
    install_dir: str
    login: str
    validate: bool
    timeout: int


@dataclass(frozen=True)
class ServerConfig:
    asset: str
    host: str
    port: int
    stability: int
    schedule: ScheduleConfig
    steam: SteamConfig
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
        steam = _object(raw, "steam")
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
            steam=SteamConfig(
                install_dir=_optional_string(steam, "install_dir"),
            ),
            steamcmd=SteamcmdConfig(
                path=_optional_string(steamcmd, "path"),
                app_id=app_id,
                install_dir=_optional_string(steamcmd, "install_dir"),
                login=_string(steamcmd, "login"),
                validate=_boolean(steamcmd, "validate"),
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


def steam_asset_path(config: ServerConfig, config_dir: Path) -> Path:
    """已登录 Steam 客户端安装目录中的 resources.assets。"""
    if config.steam.install_dir:
        install_dir = Path(config.steam.install_dir)
        if not install_dir.is_absolute():
            install_dir = config_dir / install_dir
        return (
            install_dir
            / "steamapps"
            / "common"
            / "Limbus Company"
            / "LimbusCompany_Data"
            / "resources.assets"
        )
    return Path(STEAM_DEFAULT)


def resolve_asset(config: ServerConfig, config_dir: Path) -> Path:
    if config.asset:
        path = Path(config.asset)
        return path if path.is_absolute() else config_dir / path
    if config.steamcmd.install_dir:
        install_dir = Path(config.steamcmd.install_dir)
        if not install_dir.is_absolute():
            install_dir = config_dir / install_dir
        return install_dir / "LimbusCompany_Data" / "resources.assets"
    if config.steam.install_dir:
        install_dir = Path(config.steam.install_dir)
        if not install_dir.is_absolute():
            install_dir = config_dir / install_dir
        return (
            install_dir
            / "steamapps"
            / "common"
            / "Limbus Company"
            / "LimbusCompany_Data"
            / "resources.assets"
        )
    steam_asset = steam_asset_path(config, config_dir)
    if steam_asset.is_file():
        return steam_asset
    if config.steamcmd.path:
        steamcmd_path = Path(config.steamcmd.path)
        if not steamcmd_path.is_absolute():
            steamcmd_path = config_dir / steamcmd_path
        return (
            steamcmd_path.parent
            / "steamapps"
            / "common"
            / "Limbus Company"
            / "LimbusCompany_Data"
            / "resources.assets"
        )
    raise ConfigError(
        "asset 与 steam.install_dir 均未配置,且默认 Steam 安装路径不存在,"
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
