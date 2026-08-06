"""LCTA 自托管状态服务。

游戏主要在每周四北京时间 10:00-13:00 更新。服务在该窗口内每 15 分钟
遍历一次: 优先使用已登录 Steam 客户端安装的游戏(游戏更新由 Steam
自动完成),Steam 资产不存在时回退执行 steamcmd,再扫描 resources.assets
提取最新 CDN token,提供与 limbus-api.voidfissure.de/api/status 相同格式
的 HTTP 接口,并在发现新 token 时自动调用 GitHub repository_dispatch 触发
自动更新工作流。窗口外不运行,保留 POST /api/check 手动触发作为兜底。

配置全部来自 YAML(不使用命令行参数),按以下优先级查找:
    1. 环境变量 LCTA_STATUS_CONFIG 指定的路径(必须存在)
    2. 脚本同目录的 config.yaml(本地配置,不提交到仓库)
    3. 脚本同目录的 default_config.yaml(仓库内默认模板)

用法:
    python -m tools.status_server
"""
from .config import (
    DEFAULT_CONFIG,
    LOCAL_CONFIG,
    SCRIPT_DIR,
    STEAM_DEFAULT,
    ConfigError,
    ScheduleConfig,
    ServerConfig,
    SteamConfig,
    SteamcmdConfig,
    ensure_config_ready,
    find_config,
    resolve_asset,
    steam_asset_path,
)
from .schedule import (
    beijing_now,
    is_in_update_window,
    next_check_at,
    weekday_name,
)
from .tokens import extract_tokens, verify_token
from .steamcmd import build_steamcmd_args, run_steamcmd
from .github import dispatch_update
from .state import StateStore
from .checker import Checker, StatusHolder, build_payload
from .monitor import monitor_loop
from .app import create_app
from .main import main

__all__ = [
    "DEFAULT_CONFIG",
    "LOCAL_CONFIG",
    "SCRIPT_DIR",
    "STEAM_DEFAULT",
    "ConfigError",
    "ScheduleConfig",
    "ServerConfig",
    "SteamConfig",
    "SteamcmdConfig",
    "ensure_config_ready",
    "find_config",
    "resolve_asset",
    "steam_asset_path",
    "beijing_now",
    "is_in_update_window",
    "next_check_at",
    "weekday_name",
    "extract_tokens",
    "verify_token",
    "build_steamcmd_args",
    "run_steamcmd",
    "dispatch_update",
    "StateStore",
    "Checker",
    "StatusHolder",
    "build_payload",
    "monitor_loop",
    "create_app",
    "main",
]
