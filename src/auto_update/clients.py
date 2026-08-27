from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auto_update.config import NetworkConfig


_logger = logging.getLogger(__name__)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_METADATA_PATTERN = re.compile(
    r"<!--\s*lcta-auto-update:(?P<data>\{.*?\})\s*-->", re.DOTALL
)


@dataclass(frozen=True)
class RawStatus:
    token: str
    created_at: str


@dataclass(frozen=True)
class GitHubAsset:
    name: str
    browser_download_url: str
    content_type: str
    size: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "GitHubAsset":
        return cls(
            name=str(data.get("name", "")),
            browser_download_url=str(data.get("browser_download_url") or ""),
            content_type=str(data.get("content_type") or ""),
            size=int(data.get("size") or 0),
        )


@dataclass(frozen=True)
class GitHubRelease:
    tag_name: str
    body: str
    published_at: str
    zipball_url: str
    assets: list[GitHubAsset] = field(default_factory=list)
    draft: bool = False
    prerelease: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "GitHubRelease":
        assets = [
            GitHubAsset.from_api(item)
            for item in data.get("assets", [])
            if isinstance(item, dict)
        ]
        return cls(
            tag_name=str(data.get("tag_name", "")),
            body=str(data.get("body") or ""),
            published_at=str(data.get("published_at") or ""),
            zipball_url=str(data.get("zipball_url") or ""),
            assets=assets,
            draft=bool(data.get("draft", False)),
            prerelease=bool(data.get("prerelease", False)),
        )

    @property
    def metadata(self) -> dict[str, Any]:
        match = _METADATA_PATTERN.search(self.body)
        if not match:
            return {}
        try:
            value = json.loads(match.group("data"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


class HttpClient:
    def __init__(self, network: NetworkConfig):
        self._network = network
        self._session = requests.Session()
        retry = Retry(
            total=network.retries,
            connect=network.retries,
            read=network.retries,
            status=network.retries,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.headers.update({"User-Agent": "LCTA-auto-update"})

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        response = self._session.get(
            url, timeout=self._network.timeout, headers=headers
        )
        response.raise_for_status()
        return response.json()

    def download(
        self,
        url: str,
        destination: Path,
        headers: dict[str, str] | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _logger.info("下载 %s", url)
        with self._session.get(
            url, timeout=self._network.timeout, stream=True, headers=headers
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if destination.stat().st_size == 0:
            raise RuntimeError(f"下载结果为空: {url}")


def fetch_raw_status(client: HttpClient, status_url: str) -> RawStatus:
    data = client.get_json(status_url)
    if not isinstance(data, dict):
        raise RuntimeError("status API 响应必须是 JSON 对象")
    latest = data.get("latest_token")
    if not isinstance(latest, dict):
        raise RuntimeError("status API 缺少 latest_token")
    token = latest.get("token")
    created_at = latest.get("created_at")
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError("status API 返回了非法 token")
    if not isinstance(created_at, str) or not created_at.strip():
        raise RuntimeError("status API 缺少 latest_token.created_at")
    return RawStatus(token=token, created_at=created_at.strip())


class GitHubClient:
    api_root = "https://api.github.com"

    def __init__(self, client: HttpClient, token: str = ""):
        self._client = client
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def latest_stable_release(self, repository: str) -> GitHubRelease:
        data = self._client.get_json(
            f"{self.api_root}/repos/{repository}/releases/latest",
            headers=self._headers,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"GitHub 最新 Release 响应无效: {repository}")
        release = GitHubRelease.from_api(data)
        if release.draft or release.prerelease or not release.tag_name:
            raise RuntimeError(f"没有可用的正式 Release: {repository}")
        return release

    def list_releases(self, repository: str, limit: int = 100) -> list[GitHubRelease]:
        data = self._client.get_json(
            f"{self.api_root}/repos/{repository}/releases?per_page={limit}",
            headers=self._headers,
        )
        if not isinstance(data, list):
            raise RuntimeError(f"GitHub Release 列表响应无效: {repository}")
        return [GitHubRelease.from_api(item) for item in data if isinstance(item, dict)]

    def find_cooked_asset(
        self, release: GitHubRelease, prefix: str, fmt: str
    ) -> GitHubAsset:
        """在 Release 资产中定位 cooked LLC 归档。

        匹配规则：资产名以 ``fmt`` 为后缀，且（当 ``prefix`` 非空时）以
        ``prefix`` 开头。需唯一命中，否则抛出带可用资产清单的清晰错误。
        """
        fmt = (fmt or "").lower()
        if not fmt:
            raise RuntimeError("cooked_asset_format 不能为空")
        candidates = [
            asset
            for asset in release.assets
            if asset.name.lower().endswith(f".{fmt}")
            and (not prefix or asset.name.startswith(prefix))
        ]
        if not candidates:
            available = ", ".join(a.name for a in release.assets) or "(无资产)"
            raise RuntimeError(
                f"在 Release {release.tag_name} 中未找到匹配的 cooked 资产"
                f"(prefix={prefix!r}, format={fmt!r})；可用资产: {available}"
            )
        if len(candidates) > 1:
            names = ", ".join(a.name for a in candidates)
            raise RuntimeError(
                f"匹配到多个 cooked 资产，无法唯一确定: {names}"
            )
        return candidates[0]

    def download_release_asset(
        self, asset: GitHubAsset, destination: Path
    ) -> None:
        self._client.download(
            asset.browser_download_url, destination, headers=self._headers
        )


def find_release_for_token(
    releases: list[GitHubRelease], token: str
) -> GitHubRelease | None:
    for release in releases:
        if release.metadata.get("raw_token") == token:
            return release
    return None


def build_release_metadata(data: dict[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- lcta-auto-update:{serialized} -->"
