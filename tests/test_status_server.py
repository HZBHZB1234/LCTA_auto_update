from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import status_server as server

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_BEIJING = timezone(timedelta(hours=8))


def beijing(y: int, m: int, d: int, h: int, minute: int = 0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=_BEIJING)


def schedule(
    enabled: bool = True,
    update_dow: int = 3,
    start_hour: int = 10,
    end_hour: int = 13,
    interval: int = 900,
) -> server.ScheduleConfig:
    return server.ScheduleConfig(
        enabled=enabled,
        update_dow=update_dow,
        start_hour=start_hour,
        end_hour=end_hour,
        interval=interval,
    )


def steamcmd(
    path: str = "",
    app_id: int = 1973530,
    install_dir: str = "",
    login: str = "anonymous",
    timeout: int = 3600,
) -> server.SteamcmdConfig:
    return server.SteamcmdConfig(
        path=path, app_id=app_id, install_dir=install_dir, login=login, timeout=timeout
    )


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
        "  stability: 30\n"
        "schedule:\n"
        "  enabled: true\n"
        "  update_dow: 3\n"
        "  start_hour: 10\n"
        "  end_hour: 13\n"
        "  interval: 900\n"
        "steamcmd:\n"
        "  path: ''\n"
        "  app_id: 1973530\n"
        "  install_dir: ''\n"
        "  login: 'anonymous'\n"
        "  timeout: 3600\n"
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
        payload = server.build_payload("l20260805_abc", None, hash_value="deadbeef")
        assert payload["latest_token"]["hash"] == "deadbeef"
        assert payload["latest_token"]["f_token"] is None


class TestConfig:
    def test_default_config_loads(self) -> None:
        config = server.ServerConfig.load(server.DEFAULT_CONFIG)
        assert config.port == 8080
        assert config.host == "127.0.0.1"
        assert config.schedule.update_dow == 3
        assert config.schedule.start_hour == 10
        assert config.schedule.end_hour == 13
        assert config.schedule.interval == 900
        assert config.steamcmd.app_id == 1973530
        assert config.steamcmd.login == "anonymous"
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

    def test_bad_schedule_order(self, tmp_path: Path) -> None:
        text = minimal_config().replace("start_hour: 10", "start_hour: 13")
        path = write_config(tmp_path, text)
        with pytest.raises(server.ConfigError):
            server.ServerConfig.load(path)

    def test_bad_schedule_dow(self, tmp_path: Path) -> None:
        text = minimal_config().replace("update_dow: 3", "update_dow: 9")
        path = write_config(tmp_path, text)
        with pytest.raises(server.ConfigError):
            server.ServerConfig.load(path)

    def test_bad_app_id(self, tmp_path: Path) -> None:
        text = minimal_config().replace("app_id: 1973530", "app_id: 0")
        path = write_config(tmp_path, text)
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
    def test_env_var_takes_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen = write_config(tmp_path, minimal_config())
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


class TestSchedule:
    # update_dow=3 即周四;2026-08-06 是周四,2026-08-13 是下周四
    def test_in_window(self) -> None:
        sched = schedule()
        assert server.is_in_update_window(beijing(2026, 8, 6, 10), sched)
        assert server.is_in_update_window(beijing(2026, 8, 6, 12, 59), sched)

    def test_out_of_window_boundaries(self) -> None:
        sched = schedule()
        assert not server.is_in_update_window(beijing(2026, 8, 6, 9, 59), sched)
        assert not server.is_in_update_window(beijing(2026, 8, 6, 13), sched)

    def test_other_days_out(self) -> None:
        sched = schedule()
        assert not server.is_in_update_window(beijing(2026, 8, 5, 10), sched)
        assert not server.is_in_update_window(beijing(2026, 8, 7, 10), sched)

    def test_next_check_aligns_to_interval(self) -> None:
        sched = schedule(interval=900)
        assert server.next_check_at(beijing(2026, 8, 6, 10, 7), sched) == beijing(
            2026, 8, 6, 10, 15
        )

    def test_next_check_at_window_start(self) -> None:
        sched = schedule(interval=900)
        assert server.next_check_at(beijing(2026, 8, 6, 10), sched) == beijing(
            2026, 8, 6, 10, 15
        )

    def test_next_check_after_last_slot_skips_to_next_week(self) -> None:
        sched = schedule(interval=900)
        assert server.next_check_at(beijing(2026, 8, 6, 12, 59), sched) == beijing(
            2026, 8, 13, 10
        )

    def test_next_check_outside_window_is_next_thursday(self) -> None:
        sched = schedule()
        assert server.next_check_at(beijing(2026, 8, 5, 10), sched) == beijing(
            2026, 8, 6, 10
        )
        assert server.next_check_at(beijing(2026, 8, 7, 10), sched) == beijing(
            2026, 8, 13, 10
        )


class TestSteamcmd:
    def test_args_without_install_dir(self) -> None:
        config = steamcmd(path="steamcmd.exe", app_id=1973530)
        assert server.build_steamcmd_args(config) == [
            "steamcmd.exe",
            "+login",
            "anonymous",
            "+app_update",
            "1973530",
            "+quit",
        ]

    def test_args_with_install_dir(self) -> None:
        config = steamcmd(
            path="steamcmd.exe", app_id=1973530, install_dir=r"D:\game"
        )
        assert server.build_steamcmd_args(config) == [
            "steamcmd.exe",
            "+force_install_dir",
            r"D:\game",
            "+login",
            "anonymous",
            "+app_update",
            "1973530",
            "+quit",
        ]

    def test_run_steamcmd_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, list[str]] = {}

        def fake_run(args, timeout, capture_output, text, encoding, errors):
            calls["args"] = list(args)
            return SimpleNamespace(returncode=0, stdout="Success. App '1973530' already up to date.\n")

        monkeypatch.setattr(server.subprocess, "run", fake_run)
        assert server.run_steamcmd(steamcmd(path="steamcmd.exe")) is True
        assert calls["args"][0] == "steamcmd.exe"

    def test_run_steamcmd_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(args, timeout, capture_output, text, encoding, errors):
            raise FileNotFoundError("steamcmd.exe")

        monkeypatch.setattr(server.subprocess, "run", fake_run)
        assert server.run_steamcmd(steamcmd(path="steamcmd.exe")) is False


class TestResolveAsset:
    def test_explicit_asset_wins(self, tmp_path: Path) -> None:
        config = server.ServerConfig(
            asset=str(tmp_path / "a.assets"),
            host="127.0.0.1",
            port=8080,
            stability=30,
            schedule=schedule(),
            steamcmd=steamcmd(install_dir=str(tmp_path / "install")),
            repository="a/b",
            event_type="voidfissure_update",
            token="t",
            state=tmp_path / "s.json",
            dispatch=True,
            verify=False,
        )
        assert server.resolve_asset(config, tmp_path) == tmp_path / "a.assets"

    def test_install_dir_derives_asset(self, tmp_path: Path) -> None:
        config = server.ServerConfig(
            asset="",
            host="127.0.0.1",
            port=8080,
            stability=30,
            schedule=schedule(),
            steamcmd=steamcmd(install_dir=str(tmp_path / "install")),
            repository="a/b",
            event_type="voidfissure_update",
            token="t",
            state=tmp_path / "s.json",
            dispatch=True,
            verify=False,
        )
        assert server.resolve_asset(config, tmp_path) == (
            tmp_path / "install" / "LimbusCompany_Data" / "resources.assets"
        )

    def test_relative_install_dir_against_config_dir(self, tmp_path: Path) -> None:
        config = server.ServerConfig(
            asset="",
            host="127.0.0.1",
            port=8080,
            stability=30,
            schedule=schedule(),
            steamcmd=steamcmd(install_dir="install"),
            repository="a/b",
            event_type="voidfissure_update",
            token="t",
            state=tmp_path / "s.json",
            dispatch=True,
            verify=False,
        )
        assert server.resolve_asset(config, tmp_path) == (
            tmp_path / "install" / "LimbusCompany_Data" / "resources.assets"
        )

    def test_steam_default_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        default = tmp_path / "resources.assets"
        default.write_bytes(b"x")
        monkeypatch.setattr(server, "STEAM_DEFAULT", default)
        config = server.ServerConfig(
            asset="",
            host="127.0.0.1",
            port=8080,
            stability=30,
            schedule=schedule(),
            steamcmd=steamcmd(),
            repository="a/b",
            event_type="voidfissure_update",
            token="t",
            state=tmp_path / "s.json",
            dispatch=True,
            verify=False,
        )
        assert server.resolve_asset(config, tmp_path) == default

    def test_nothing_found_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server, "STEAM_DEFAULT", tmp_path / "missing.assets")
        config = server.ServerConfig(
            asset="",
            host="127.0.0.1",
            port=8080,
            stability=30,
            schedule=schedule(),
            steamcmd=steamcmd(),
            repository="a/b",
            event_type="voidfissure_update",
            token="t",
            state=tmp_path / "s.json",
            dispatch=True,
            verify=False,
        )
        with pytest.raises(server.ConfigError):
            server.resolve_asset(config, tmp_path)


class TestChecker:
    def test_scan_updates_holder_and_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asset = tmp_path / "resources.assets"
        asset.write_bytes(
            b"downloadcommon.limbuscompanycdn.org/l20260806_new/Assets "
            b"downloadfmod.limbuscompanycdn.org/f20260806_fm/Assets"
        )
        config = server.ServerConfig(
            asset=str(asset),
            host="127.0.0.1",
            port=8080,
            stability=0,
            schedule=schedule(),
            steamcmd=steamcmd(),
            repository="a/b",
            event_type="voidfissure_update",
            token="tok",
            state=tmp_path / "state.json",
            dispatch=True,
            verify=False,
        )
        dispatched: list[str] = []

        def fake_dispatch(config: server.ServerConfig, token: str) -> None:
            dispatched.append(token)

        monkeypatch.setattr(server, "dispatch_update", fake_dispatch)
        holder = server.StatusHolder()
        state = server.StateStore(config.state)
        server.Checker(config, holder, state, asset).run()
        assert dispatched == ["l20260806_new"]
        assert state.dispatched_token == "l20260806_new"
        latest = holder.get()["latest_token"]
        assert latest["token"] == "l20260806_new"
        assert latest["f_token"] == "f20260806_fm"

    def test_no_redispatch_for_same_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asset = tmp_path / "resources.assets"
        asset.write_bytes(b"downloadcommon.limbuscompanycdn.org/l20260806_new/Assets")
        config = server.ServerConfig(
            asset=str(asset),
            host="127.0.0.1",
            port=8080,
            stability=0,
            schedule=schedule(),
            steamcmd=steamcmd(),
            repository="a/b",
            event_type="voidfissure_update",
            token="tok",
            state=tmp_path / "state.json",
            dispatch=True,
            verify=False,
        )
        dispatched: list[str] = []
        monkeypatch.setattr(
            server,
            "dispatch_update",
            lambda config, token: dispatched.append(token),
        )
        holder = server.StatusHolder()
        state = server.StateStore(config.state)
        checker = server.Checker(config, holder, state, asset)
        checker.run()
        checker.run()
        assert dispatched == ["l20260806_new"]
