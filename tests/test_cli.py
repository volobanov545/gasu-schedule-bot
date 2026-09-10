from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from szs_hub import cli
from szs_hub.alerting import AlertCode, AlertOutcome, AlertSettings
from szs_hub.archive import inspect_telegram_desktop_export
from szs_hub.cli import MigrationArtifactsError, build_parser, main
from szs_hub.config import Settings
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)
from szs_hub.storage.models import AdminEvent, ImportRun, Message, SearchDocument


def test_parser_exposes_owner_operations() -> None:
    parser = build_parser()
    for command in (
        "doctor",
        "migrate",
        "health",
        "run",
        "publish-tomorrow",
        "alert",
        "backup",
        "restore",
        "import-plan",
        "import-apply",
    ):
        extra = {
            "alert": ["--code", "health"],
            "restore": ["--input", "x"],
            "import-plan": ["result.json"],
            "import-apply": [
                "result.json",
                "--fingerprint",
                "sha256:" + "a" * 64,
                "--backup-output",
                "before.sqlite3",
            ],
        }.get(command, [])
        assert parser.parse_args([command, *extra]).command == command


def test_health_parser_exposes_explicit_offline_restore_mode() -> None:
    args = build_parser().parse_args(["health", "--offline"])

    assert args.command == "health"
    assert args.offline is True


def test_offline_health_does_not_require_live_heartbeat_probe_or_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "restored.sqlite3"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        telegram_bot_token=SecretStr("123456:token"),
        spbgasu_group_id="42",
        backup_status_path=tmp_path / "missing-backup-marker",
        _env_file=None,
    )

    async def prepare() -> None:
        engine = create_database_engine(settings.database_url)
        try:
            await create_schema(engine)
        finally:
            await engine.dispose()

    asyncio.run(prepare())

    assert asyncio.run(cli._health(settings, offline=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "offline"
    assert payload["state"] == "ok"
    assert payload["last_runtime_heartbeat"] is None
    assert payload["last_telegram_probe"] is None
    assert payload["last_backup_success"] is None


def test_alert_parser_rejects_unknown_instance_code() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["alert", "--code", "../../unit"])


def test_alert_runs_before_full_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alert_settings = AlertSettings(
        telegram_bot_token=SecretStr("123456:alert-token"),
        alert_user_id=123,
        _env_file=None,
    )

    def forbidden_settings() -> None:
        raise AssertionError("systemd alert must not load unrelated runtime settings")

    async def suppressed(settings: AlertSettings, code: AlertCode) -> AlertOutcome:
        assert settings is alert_settings
        assert code is AlertCode.HEALTH
        return AlertOutcome.SUPPRESSED

    monkeypatch.setattr(cli, "Settings", forbidden_settings)
    monkeypatch.setattr(cli, "AlertSettings", lambda: alert_settings)
    monkeypatch.setattr(cli, "send_owner_alert", suppressed)

    assert main(["alert", "--code", "health"]) == 0
    assert "повтор подавлен" in capsys.readouterr().out


def test_import_plan_is_read_only_and_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = tmp_path / "result.json"
    export.write_text(
        json.dumps(
            {
                "id": 1_234_567_890,
                "name": "СЗС test",
                "type": "private_supergroup",
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-09-01T10:00:00+00:00",
                        "from": "Анна",
                        "from_id": "user10",
                        "text": "Привет",
                        "file": "files/missing.pdf",
                        "file_name": "missing.pdf",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert main(["import-plan", str(export)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["messages_planned"] == 1
    assert payload["raw_chat_id"] == 1_234_567_890
    assert payload["chat_type"] == "private_supergroup"
    assert payload["normalized_chat_id"] == -1_001_234_567_890
    assert payload["source_fingerprint"].startswith("sha256:")
    assert payload["missing_media"] == [
        {
            "message_id": 1,
            "relative_path": "files/missing.pdf",
            "reason": "missing",
        }
    ]
    assert payload["topics"] == 0
    assert payload["media_groups"] == 0
    assert len(payload["limitations"]) == 2


def test_import_plan_rejects_unknown_chat_type_without_database_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export = tmp_path / "result.json"
    export.write_text(
        json.dumps({"id": 123, "type": "personal_chat", "messages": []}),
        encoding="utf-8",
    )

    assert main(["import-plan", str(export)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported Telegram Desktop export chat type" in captured.err


def test_import_plan_does_not_load_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export = tmp_path / "result.json"
    export.write_text(
        json.dumps(
            {
                "id": 42,
                "type": "private_supergroup",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    def forbidden_settings() -> None:
        raise AssertionError("offline import inspection must not load .env")

    monkeypatch.setattr(cli, "Settings", forbidden_settings)
    assert main(["import-plan", str(export)]) == 0


def test_import_apply_requires_reviewed_fingerprint_backup_lock_and_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export_directory = tmp_path / "export"
    export_directory.mkdir()
    export = export_directory / "result.json"
    export.write_text(
        json.dumps(
            {
                "id": 1_234_567_890,
                "type": "private_supergroup",
                "messages": [
                    {
                        "id": 10,
                        "type": "message",
                        "date": "2026-09-01T10:00:00+00:00",
                        "from": "Анна",
                        "from_id": "user10",
                        "text": "Первая запись",
                    },
                    {
                        "id": 11,
                        "type": "message",
                        "date": "2026-09-01T10:01:00+00:00",
                        "from": "Борис",
                        "from_id": "user11",
                        "text": "Ответ",
                        "reply_to_message_id": 10,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = tmp_path / "state" / "archive.sqlite3"
    database.parent.mkdir()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        target_chat_id=-1_001_234_567_890,
        _env_file=None,
    )

    async def prepare() -> None:
        engine = create_database_engine(settings.database_url)
        try:
            await create_schema(engine)
        finally:
            await engine.dispose()

    asyncio.run(prepare())
    fingerprint = inspect_telegram_desktop_export(export).source_fingerprint
    backup = tmp_path / "backups" / "before-import.sqlite3"
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert (
        main(
            [
                "import-apply",
                str(export),
                "--fingerprint",
                fingerprint,
                "--backup-output",
                str(backup),
                "--confirmed-offline",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert result["backup_created"] is True
    assert result["reconciliation"] == {
        "complete": True,
        "messages_archived": 2,
        "search_documents_verified": 2,
        "files_archived": 0,
        "reply_links_checked": 1,
        "reply_link_mismatches": 0,
    }
    assert backup.is_file()

    async def verify() -> None:
        engine = create_database_engine(settings.database_url)
        sessions = create_session_factory(engine)
        try:
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Message)) == 2
                assert (
                    await session.scalar(select(func.count()).select_from(SearchDocument))
                    == 2
                )
                assert await session.scalar(select(func.count()).select_from(ImportRun)) == 1
                assert await session.scalar(select(func.count()).select_from(AdminEvent)) == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_import_apply_rejects_changed_fingerprint_before_backup_or_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export = tmp_path / "result.json"
    export.write_text(
        json.dumps(
            {"id": 1_234_567_890, "type": "private_supergroup", "messages": []}
        ),
        encoding="utf-8",
    )
    backup = tmp_path.parent / f"{tmp_path.name}-backup.sqlite3"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'missing.sqlite3').as_posix()}",
        target_chat_id=-1_001_234_567_890,
        _env_file=None,
    )

    result = cli._import_apply(
        settings,
        export_path=export,
        expected_fingerprint="sha256:" + "0" * 64,
        backup_output=backup,
        confirmed_offline=True,
        accept_parser_failures=False,
        accept_missing_media=False,
    )

    assert result == 2
    assert not backup.exists()
    assert not (tmp_path / "missing.sqlite3").exists()
    assert "Fingerprint" in capsys.readouterr().err


def test_migration_artifacts_use_complete_release_working_directory(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _write_migration_tree(release)

    config, scripts = cli._find_migration_artifacts(
        working_directory=release,
        source_project_root=tmp_path / "installed-wheel",
    )

    assert config == release.resolve() / "alembic.ini"
    assert scripts == release.resolve() / "alembic"


def test_migration_artifacts_fall_back_to_complete_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_migration_tree(source)

    config, scripts = cli._find_migration_artifacts(
        working_directory=tmp_path / "unrelated-working-directory",
        source_project_root=source,
    )

    assert config == source.resolve() / "alembic.ini"
    assert scripts == source.resolve() / "alembic"


def test_migration_artifacts_reject_partial_release_even_with_source_fallback(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    source = tmp_path / "source"
    _write_migration_tree(source)

    with pytest.raises(MigrationArtifactsError, match="неполный набор"):
        cli._find_migration_artifacts(
            working_directory=release,
            source_project_root=source,
        )


def test_migration_artifacts_reject_missing_and_ambiguous_trees(tmp_path: Path) -> None:
    with pytest.raises(MigrationArtifactsError, match="не найдены"):
        cli._find_migration_artifacts(
            working_directory=tmp_path / "missing-work",
            source_project_root=tmp_path / "missing-source",
        )

    release = tmp_path / "release"
    source = tmp_path / "source"
    _write_migration_tree(release)
    _write_migration_tree(source)
    with pytest.raises(MigrationArtifactsError, match="несколько"):
        cli._find_migration_artifacts(
            working_directory=release,
            source_project_root=source,
        )


def test_migrate_configures_alembic_from_release_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    _write_migration_tree(release)
    monkeypatch.chdir(release)
    monkeypatch.setattr(cli, "_SOURCE_PROJECT_ROOT", tmp_path / "installed-wheel")
    observed: dict[str, str | None] = {}

    def capture_upgrade(config: object, revision: str) -> None:
        assert isinstance(config, cli.Config)
        observed["config"] = config.config_file_name
        observed["scripts"] = config.get_main_option("script_location")
        observed["url"] = config.get_main_option("sqlalchemy.url")
        observed["revision"] = revision

    monkeypatch.setattr(cli.command, "upgrade", capture_upgrade)

    cli._migrate(Settings(database_url="sqlite+aiosqlite:///release.sqlite3"))

    assert observed == {
        "config": str(release.resolve() / "alembic.ini"),
        "scripts": str(release.resolve() / "alembic"),
        "url": "sqlite+aiosqlite:///release.sqlite3",
        "revision": "head",
    }


def _write_migration_tree(root: Path) -> None:
    scripts = root / "alembic"
    (scripts / "versions").mkdir(parents=True)
    (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (scripts / "env.py").write_text("# test migration environment\n", encoding="utf-8")
