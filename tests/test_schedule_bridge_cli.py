from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from szs_hub.publisher import ScheduleBridgeSettings
from szs_hub.schedule import bridge_cli


@pytest.fixture(autouse=True)
def bridge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SPBGASU_GROUP_ID",
        "SPBGASU_BASE_URL",
        "TIMEZONE",
        "BRIDGE_GH_TOKEN",
        "BRIDGE_GH_REPOSITORY",
        "SPBGASU_PROBE_SCHEMA",
        "SCHEDULE_FORCE_DIGEST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_bridge_core_runs_without_full_application_dependencies() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import sys
        from datetime import UTC, datetime

        sys.path.insert(0, sys.argv[1])
        blocked = {
            "aiogram", "aiohttp", "aiosqlite", "alembic", "fastapi", "pydantic",
            "pydantic_settings", "snowballstemmer", "sqlalchemy", "uvicorn",
        }

        class BlockApplicationDependencies(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in blocked:
                    raise RuntimeError("bridge imported full dependency: " + fullname)
                return None

        sys.meta_path.insert(0, BlockApplicationDependencies())

        from szs_hub.publisher import dispatch_tomorrow
        from szs_hub.schedule.bridge_cli import BridgeSettings, main
        from szs_hub.schedule.ci import decode_schedule_envelope
        from szs_hub.schedule.spbgasu import (
            SchedulePageBootstrap, WeeklyLesson, WeeklySchedule, WeekParity,
        )

        class Source:
            async def fetch_bootstrap(self):
                return SchedulePageBootstrap(current_week_number=5, groups=("test",))

            async def fetch_group(self, group_key):
                return WeeklySchedule(group_key=group_key, lessons=(WeeklyLesson(
                    weekday=0, slot=1, parity=WeekParity.NUMERATOR, subject="Math",
                    group=group_key, auditorium="407/1", professor=None, source_date=None,
                ),))

        class Bridge:
            called = False

            async def dispatch(self, *, repository, token, text_b64, group_key):
                envelope = decode_schedule_envelope(text_b64)
                assert envelope.group_key == group_key == "test"
                assert len(envelope.lessons) == 1
                assert envelope.force_digest is True
                self.called = True

        bridge = Bridge()
        asyncio.run(dispatch_tomorrow(
            BridgeSettings("test", "https://rasp.spbgasu.ru", "Europe/Moscow"),
            github_token="test-token", github_repository="owner/repo", force_digest=True,
            source=Source(), bridge=bridge,
            clock=lambda: datetime(2026, 10, 26, 12, tzinfo=UTC),
        ))
        assert bridge.called
        try:
            main(["--help"])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("help should exit")
        assert not blocked.intersection(sys.modules)
        """
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-X", "utf8", "-c", script, str(source_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_schedule_service_public_exports_remain_compatible() -> None:
    from szs_hub.schedule import ScheduleSyncService
    from szs_hub.schedule.service import ScheduleSyncService as CanonicalService

    assert ScheduleSyncService is CanonicalService
    with pytest.raises(AttributeError):
        _ = __import__("szs_hub.schedule", fromlist=["missing"]).missing


@pytest.mark.parametrize(
    ("arguments", "environment_value", "expected"),
    [([], None, False), ([], "false", False), ([], "TRUE", False),
     ([], "true", True), (["--force-digest"], "false", True)],
)
def test_bridge_cli_dispatches_with_only_bridge_configuration(
    arguments: list[str],
    environment_value: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SPBGASU_GROUP_ID", "test-group")
    monkeypatch.setenv("BRIDGE_GH_TOKEN", "test-token")
    monkeypatch.setenv("BRIDGE_GH_REPOSITORY", "owner/repo")
    if environment_value is not None:
        monkeypatch.setenv("SCHEDULE_FORCE_DIGEST", environment_value)
    calls: list[bool] = []

    async def dispatch(
        settings: ScheduleBridgeSettings,
        *,
        github_token: str,
        github_repository: str,
        force_digest: bool,
    ) -> None:
        assert settings.spbgasu_group_id == "test-group"
        assert settings.spbgasu_base_url == "https://rasp.spbgasu.ru"
        assert settings.timezone == "Europe/Moscow"
        assert github_token == "test-token"  # noqa: S105 - deliberately fake credential
        assert github_repository == "owner/repo"
        calls.append(force_digest)

    monkeypatch.setattr(bridge_cli, "dispatch_tomorrow", dispatch)

    assert bridge_cli.main(arguments) == 0
    assert calls == [expected]
    assert "передана в GitHub" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["--probe-schema"], []])
@pytest.mark.parametrize("fails", [False, True])
def test_bridge_probe_needs_no_github_token_and_closes_source(
    arguments: list[str],
    fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SPBGASU_GROUP_ID", " test-group ")
    monkeypatch.setenv("SPBGASU_BASE_URL", "https://example.test")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("SCHEDULE_FORCE_DIGEST", "true")
    if not arguments:
        monkeypatch.setenv("SPBGASU_PROBE_SCHEMA", "true")

    class ProbeSource:
        closed = False

        def __init__(self, *, base_url: str) -> None:
            assert base_url == "https://example.test"

        async def probe_group_schema(self, group_key: str) -> str:
            assert group_key == "test-group"
            if fails:
                raise RuntimeError("safe diagnostic error")
            return "parsed lessons=12"

        async def aclose(self) -> None:
            self.closed = True

    source = ProbeSource(base_url="https://example.test")
    monkeypatch.setattr(bridge_cli, "SpbGasuClient", lambda **kwargs: source)

    assert bridge_cli.main(arguments) == (1 if fails else 0)
    assert source.closed
    captured = capsys.readouterr()
    if fails:
        assert "RuntimeError: safe diagnostic error" in captured.err
    else:
        assert captured.out.strip() == "parsed lessons=12"


def test_bridge_cli_rejects_missing_configuration_without_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bridge_cli.main([]) == 1
    assert "SPBGASU_GROUP_ID" in capsys.readouterr().err
    assert bridge_cli.main(["--probe-schema"]) == 1
    assert "SPBGASU_GROUP_ID" in capsys.readouterr().err
