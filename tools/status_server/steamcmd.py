"""steamcmd 执行: 构造参数、运行并解析成功标志。"""
from __future__ import annotations

import logging
import re
import subprocess
import time

from .config import SteamcmdConfig


_logger = logging.getLogger(__name__)

STEAMCMD_SUCCESS_RE = re.compile(
    r"(?:Success[!.]?\s+)?App\s+'\d+'\s+(fully\s+installed|already\s+up\s+to\s+date)",
    re.IGNORECASE,
)


def build_steamcmd_args(config: SteamcmdConfig) -> list[str]:
    args = [config.path]
    if config.install_dir:
        args += ["+force_install_dir", config.install_dir]
    args += [
        "+login",
        config.login,
        "+app_license_request",
        str(config.app_id),
        "+app_update",
        str(config.app_id),
    ]
    if config.validate:
        args += ["-validate"]
    args += ["+quit"]
    return args


def run_steamcmd(config: SteamcmdConfig) -> bool:
    """执行 steamcmd,输出实时转发到主程序控制台,返回是否更新成功。

    steamcmd 在大多数失败场景(如 No subscription、下载失败)仍返回
    退出码 0,因此必须同时解析输出中的成功标志。
    """
    args = build_steamcmd_args(config)
    _logger.info("执行 steamcmd: %s", " ".join(args))
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        _logger.error("steamcmd 启动失败: %s", exc)
        return False
    output: list[str] = []
    deadline = time.monotonic() + config.timeout
    while True:
        line = process.stdout.readline() if process.stdout else ""
        if line:
            output.append(line)
            print(line, end="", flush=True)
        elif process.poll() is not None:
            break
        elif time.monotonic() >= deadline:
            process.kill()
            process.wait()
            _logger.error("steamcmd 超过 %d 秒未完成,已终止", config.timeout)
            return False
        else:
            time.sleep(0.1)

    if process.returncode != 0:
        _logger.error("steamcmd 退出码 %d", process.returncode)
        return False
    if not STEAMCMD_SUCCESS_RE.search("\n".join(output)):
        _logger.error("steamcmd 未输出成功标志,视为失败")
        return False
    _logger.info("steamcmd 更新完成 (App %s)", config.app_id)
    return True
