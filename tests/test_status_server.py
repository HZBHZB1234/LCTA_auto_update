from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import status_server as server

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "server.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def minimal_config(token: str = "", port: int = 8080, repo: str = "a/b") -> str:
    return (
        "asset: ''\n"
        "server:\n"
        f"  host: '127.0.0.1'\n  port: {port}\n"
        "polling:\n"
        "  interval: 60\n"
        "  stability: 30\n"
        "github:\n"
        f"  repository: '{repo}'\n"
        "  event_type: 'voidfissure_update'\n"
        f"  token: '{token}'\n"
        "state: 'st.json'\n"
        "dispatch: true\n"
        "verify: false\n"
    )


class TestExtractTokens:
    def test_picks_latest_by_date(self) -> None:
        data = (
            b"https://downloadcommon.limbuscompanycdn.org/l20260101_old/Assets"
            b"https://downloadcommon.limbuscompanycdn.org/l20260730_new/Assets"
        )
        token, f_token = server.extract_tokens(data)
        assert token == "l20260730_new"
        assert f_token is None

    def test_both_tokens(self) -> None:
        data = (
            b"downloadcommon.limbuscompanycdn.org/l20260701_a1/Assets"
            b"downloadfmod.limbuscompanycdn.org/f20260702_b2/Assets"
        )
        token, f_token = server.extract_tokens(data)
        assert token == "l20260701_a1"
        assert f_token == "f20260702_b2"

    def test_no_token(self) -> None:
        assert server.extract_tokens(b"nothing here") == (None, None)


class TestBuildPayload:
    def test_matches_client_format(self) -> None:
        payload = server.build_payload("l20260805_abc", "f20260805_xyz")
        latest = payload["latest_token"]
        assert latest["token"] == "l20260805_abc"
        assert latest["f_token"] == "f20260805_xyz"
        assert TOKEN_PATTERN.fullmatch(latest["token"])
        assert latest["created_at"].strip()

    def test_hash_optional(self) -> None:
        payload = server.build_payload(
            "l20260805_abc", None, hash_value="deadbeef"
        )
        assert payload["latest_token"]["hash"] == "deadbeef"
        assert payload["latest_token"]["f_token"] is None


class TestConfig:
    def test_default_config_loads(self) -> None:
        config = server.ServerConfig.load(server.DEFAULT_CONFIG)
        assert config.port == 8080
        assert config.host == "127.0.0.1"
        assert config.repository == "HZBHZB1234/LCTA_auto_update"
        assert config.dispatch
        assert config.token == ""

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, "server: [unclosed")
        with pytest.raises(server.ConfigError):
            server.ServerConfig.load(path)

    def test_bad_port(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, minimal_config(port=99999))
        with pytest.raises(server.ConfigError):
            server.ServerConfig.load(path)

    def test_bad_repository(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, minimal_config(repo="no-slash"))
        with pytest.raises(server.ConfigError):
            server.ServerConfig.load(path)

    def test_relative_state_resolved_against_config_dir(self, tmp_path: Path) -> None:
        text = minimal_config().replace("state: 'st.json'", "state: 'state/status.json'")
        path = write_config(tmp_path, text)
        config = server.ServerConfig.load(path)
        assert config.state == (tmp_path / "state" / "status.json").resolve()

    def test_ready_check_requires_token_for_dispatch(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, minimal_config(token=""))
        config = server.ServerConfig.load(path)
        with pytest.raises(server.ConfigError):
            server.ensure_config_ready(config)

    def test_ready_check_ok_with_token(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, minimal_config(token="tok"))
        server.ensure_config_ready(server.ServerConfig.load(path))


class TestFindConfig:
    def test_env_var_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        chosen = write_config(tmp_path, "port: 9999\n")
        monkeypatch.setenv("LCTA_STATUS_CONFIG", str(chosen))
        assert server.find_config() == chosen

    def test_env_var_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LCTA_STATUS_CONFIG", str(tmp_path / "nope.yaml"))
        with pytest.raises(server.ConfigError):
            server.find_config()

    def test_local_config_takes_priority_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = tmp_path / "config.yaml"
        local.write_text("", encoding="utf-8")
        monkeypatch.delenv("LCTA_STATUS_CONFIG", raising=False)
        monkeypatch.setattr(server, "LOCAL_CONFIG", local)
        monkeypatch.setattr(server, "DEFAULT_CONFIG", tmp_path / "default.yaml")
        assert server.find_config() == local

    def test_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        default = tmp_path / "default.yaml"
        default.write_text("", encoding="utf-8")
        monkeypatch.delenv("LCTA_STATUS_CONFIG", raising=False)
        monkeypatch.setattr(server, "LOCAL_CONFIG", tmp_path / "missing.yaml")
        monkeypatch.setattr(server, "DEFAULT_CONFIG", default)
        assert server.find_config() == default
