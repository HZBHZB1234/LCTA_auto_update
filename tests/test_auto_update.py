from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import zipfile

import pytest

from auto_update.archive import extract_zip_safely, find_named_directory
from auto_update.clients import (
    GitHubRelease,
    build_release_metadata,
    fetch_raw_status,
    find_release_for_token,
)
from auto_update.config import AppConfig, ConfigError
from auto_update.config import PublishingConfig
from auto_update.packaging import create_packages
from auto_update.runner import _select_release_version
from auto_update.versioning import is_version_tag, next_version


def test_default_config_loads():
    config_path = Path(__file__).resolve().parents[1] / "src" / "config.json"
    config = AppConfig.load(config_path)

    assert config.sources.raw_languages == ("jp", "kr", "en")
    assert config.sources.cooked_repository == (
        "LocalizeLimbusCompany/LocalizeLimbusCompany"
    )
    assert config.publishing.zip is True
    assert config.publishing.seven_zip is True


def test_config_rejects_disabled_package_formats(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src" / "config.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["publishing"]["zip"] = False
    data["publishing"]["seven_zip"] = False
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="不能同时关闭"):
        AppConfig.load(config_path)


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, "2026073001"),
        ("2026072908", "2026073001"),
        ("2026073001", "2026073002"),
        ("invalid", "2026073001"),
    ],
)
def test_next_version_preserves_date_sequence_logic(previous, expected):
    now = datetime(2026, 7, 30, 12, 0, 0)
    assert next_version(previous, now) == expected


def test_next_version_rejects_sequence_overflow():
    with pytest.raises(RuntimeError, match="超过 99"):
        next_version("2026073099", datetime(2026, 7, 30, 12, 0, 0))


def test_version_tag_is_strict():
    assert is_version_tag("2026073001")
    assert not is_version_tag("raw-l20260730")
    assert not is_version_tag("202607301")


def test_extract_zip_and_find_language_root(tmp_path):
    archive_path = tmp_path / "raw.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("LocalizeTemp_kr/KR_Test.json", "{}")

    destination = tmp_path / "output"
    extract_zip_safely(archive_path, destination)

    language_root = find_named_directory(destination, "LocalizeTemp_kr")
    assert (language_root / "KR_Test.json").is_file()


def test_extract_zip_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.json", "{}")

    with pytest.raises(RuntimeError, match="不安全路径"):
        extract_zip_safely(archive_path, tmp_path / "output")


class FakeHttpClient:
    def __init__(self, data):
        self.data = data

    def get_json(self, _url):
        return self.data


def test_status_token_validation():
    status = fetch_raw_status(
        FakeHttpClient(
            {
                "latest_token": {
                    "token": "l20260730_example-token",
                    "created_at": "2026-07-30 02:40:00",
                }
            }
        ),
        "https://example.invalid/status",
    )
    assert status.token == "l20260730_example-token"


def test_status_rejects_path_injection_token():
    with pytest.raises(RuntimeError, match="非法 token"):
        fetch_raw_status(
            FakeHttpClient(
                {
                    "latest_token": {
                        "token": "../../secret",
                        "created_at": "2026-07-30 02:40:00",
                    }
                }
            ),
            "https://example.invalid/status",
        )


def test_release_metadata_supports_idempotency():
    body = "说明\n\n" + build_release_metadata(
        {"raw_token": "l20260730_token", "cooked_release": "2026073001"}
    )
    release = GitHubRelease(
        tag_name="2026073001",
        body=body,
        published_at="2026-07-30T00:00:00Z",
        zipball_url="https://api.github.com/example.zip",
        draft=False,
        prerelease=False,
    )

    assert release.metadata["raw_token"] == "l20260730_token"
    assert find_release_for_token([release], "l20260730_token") == release
    assert find_release_for_token([release], "other") is None


def test_zip_package_keeps_output_root(tmp_path):
    output = tmp_path / "LLc-CN-LCTA"
    output.mkdir()
    (output / "test.json").write_text("{}", encoding="utf-8")
    config = PublishingConfig(
        zip=True,
        seven_zip=False,
        output_dir="LLc-CN-LCTA",
        asset_prefix="LLc-CN-LCTA",
        upload_diagnostics=True,
    )

    assets = create_packages(output, tmp_path / "assets", "2026073001", config)

    with zipfile.ZipFile(assets[0]) as archive:
        assert archive.namelist() == ["LLc-CN-LCTA/test.json"]


def _release(tag: str, token: str) -> GitHubRelease:
    return GitHubRelease(
        tag_name=tag,
        body=build_release_metadata({"raw_token": token}),
        published_at="2026-07-30T00:00:00Z",
        zipball_url="https://api.github.com/example.zip",
        draft=False,
        prerelease=False,
    )


def test_automatic_duplicate_token_is_skipped():
    release = _release("2026073001", "raw-token")
    version, matching = _select_release_version(
        [release],
        raw_token="raw-token",
        manual_run=False,
        deduplicate=True,
        now=datetime(2026, 7, 30, 12, 0, 0),
    )
    assert version is None
    assert matching == release


def test_manual_duplicate_token_reuses_existing_tag():
    release = _release("2026073001", "raw-token")
    version, matching = _select_release_version(
        [release],
        raw_token="raw-token",
        manual_run=True,
        deduplicate=True,
        now=datetime(2026, 7, 30, 12, 0, 0),
    )
    assert version == "2026073001"
    assert matching == release


def test_new_token_uses_latest_valid_date_sequence_tag():
    releases = [
        _release("invalid", "other"),
        _release("2026073008", "older"),
    ]
    version, matching = _select_release_version(
        releases,
        raw_token="new-token",
        manual_run=False,
        deduplicate=True,
        now=datetime(2026, 7, 30, 12, 0, 0),
    )
    assert version == "2026073009"
    assert matching is None
