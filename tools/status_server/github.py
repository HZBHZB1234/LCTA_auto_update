"""GitHub repository_dispatch 触发。"""
from __future__ import annotations

import json
from urllib import request as urllib_request

from .config import ServerConfig


GITHUB_API_ROOT = "https://api.github.com"


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
