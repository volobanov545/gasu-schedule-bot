"""Owner-facing command line for setup, health, backup, import, and runtime."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from szs_hub.alerting import (
    AlertCode,
    AlertDeliveryError,
    AlertOutcome,
    AlertSettings,
    AlertStateError,
    send_owner_alert,
)
from szs_hub.archive import (
    TelegramDesktopImporter,
    TelegramDesktopImportPreflight,
    inspect_telegram_desktop_export,
    reconcile_telegram_desktop_import,
)
from szs_hub.backup import create_backup, restore_backup, sqlite_database_path
from szs_hub.config import Settings
from szs_hub.health import HealthState, build_health_report
from szs_hub.storage.database import (
    create_database_engine,
    create_session_factory,
    sqlite_wal_is_safe,
)
from szs_hub.storage.models import AdminEvent
from szs_hub.storage.process_lock import (
    ProcessLock,
    ProcessLockUnavailableError,
    database_lock_path,
)

_SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MigrationArtifactsError(RuntimeError):
    """The CLI cannot identify one complete, trusted Alembic tree."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="szs-hub", description="Управление SZS Hub")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="проверить локальную среду без сетевых запросов")
    subcommands.add_parser("migrate", help="применить миграции базы данных")
    health = subcommands.add_parser(
        "health", help="проверить базу, очереди и свежесть расписания"
    )
    health.add_argument(
        "--offline",
        action="store_true",
        help="проверить восстановленную БД без требований к запущенному runtime",
    )
    subcommands.add_parser("run", help="запустить бота и фоновые задачи")
    subcommands.add_parser(
        "publish-tomorrow",
        help="однократно отправить завтрашнее расписание (для GitHub Actions)",
    )
    dispatch = subcommands.add_parser(
        "dispatch-tomorrow",
        help="получить две недели расписания в GitVerse и передать в GitHub",
    )
    dispatch.add_argument(
        "--force-digest",
        action="store_true",
        help="отправить вечернюю карточку вне обычного времени для проверки вида",
    )
    subcommands.add_parser(
        "probe-spbgasu",
        help="показать только безопасную структуру ответа расписания без отправки",
    )
    subcommands.add_parser(
        "publish-dispatched",
        help="отправить в Telegram проверенную карточку от GitVerse",
    )
    subcommands.add_parser(
        "publish-reminder",
        help="отправить напоминание о первой или следующей паре, если оно наступило",
    )

    alert = subcommands.add_parser(
        "alert", help="отправить владельцу системное оповещение"
    )
    alert.add_argument("--code", type=AlertCode, choices=tuple(AlertCode), required=True)

    backup = subcommands.add_parser("backup", help="создать и проверить резервную копию")
    backup.add_argument("--output", type=Path)

    restore = subcommands.add_parser("restore", help="восстановить БД с защитной копией")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument(
        "--confirmed-offline",
        action="store_true",
        help="подтверждение, что SZS Hub остановлен",
    )

    import_plan = subcommands.add_parser(
        "import-plan", help="проверить Telegram Desktop result.json без записи в БД"
    )
    import_plan.add_argument("export", type=Path)
    import_apply = subcommands.add_parser(
        "import-apply",
        help="применить заранее проверенный Telegram Desktop export офлайн",
    )
    import_apply.add_argument("export", type=Path)
    import_apply.add_argument("--fingerprint", required=True)
    import_apply.add_argument("--backup-output", type=Path, required=True)
    import_apply.add_argument("--confirmed-offline", action="store_true")
    import_apply.add_argument("--accept-parser-failures", action="store_true")
    import_apply.add_argument("--accept-missing-media", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "import-plan":
        try:
            return _import_plan(args.export)
        except (OSError, ValueError) as exc:
            print(f"Импорт не запланирован: {exc}", file=sys.stderr)
            return 2
    if args.command == "alert":
        return _owner_alert(args.code)

    try:
        settings = Settings()
    except ValidationError as exc:
        print("Конфигурация неполна:", file=sys.stderr)
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(item) for item in error["loc"])
            print(f"- {location}: {error['msg']}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return _doctor(settings)
    if args.command == "import-apply":
        return _import_apply(
            settings,
            export_path=args.export,
            expected_fingerprint=args.fingerprint,
            backup_output=args.backup_output,
            confirmed_offline=bool(args.confirmed_offline),
            accept_parser_failures=bool(args.accept_parser_failures),
            accept_missing_media=bool(args.accept_missing_media),
        )
    if args.command == "migrate":
        try:
            _migrate(settings)
        except MigrationArtifactsError as exc:
            print(f"Миграции не запущены: {exc}", file=sys.stderr)
            return 2
        print("Миграции применены.")
        return 0
    if args.command == "health":
        return asyncio.run(_health(settings, offline=bool(args.offline)))
    if args.command == "publish-tomorrow":
        from szs_hub.publisher import publish_tomorrow

        try:
            message_id = asyncio.run(publish_tomorrow(settings))
        except Exception as exc:
            print(f"Расписание не отправлено: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"Расписание отправлено, Telegram message_id={message_id}.")
        return 0
    if args.command == "dispatch-tomorrow":
        from szs_hub.publisher import dispatch_tomorrow

        try:
            asyncio.run(
                dispatch_tomorrow(
                    settings,
                    github_token=os.environ.get("BRIDGE_GH_TOKEN", ""),
                    github_repository=os.environ.get("BRIDGE_GH_REPOSITORY", ""),
                    force_digest=bool(args.force_digest),
                )
            )
        except Exception as exc:
            print(f"Карточка не передана: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print("Карточка передана в GitHub.")
        return 0
    if args.command == "probe-spbgasu":
        from szs_hub.schedule.spbgasu import SpbGasuClient

        group_key = (settings.spbgasu_group_id or "").strip()
        if not group_key:
            print("Диагностика не запущена: SPBGASU_GROUP_ID не задан.", file=sys.stderr)
            return 2

        async def probe() -> str:
            client = SpbGasuClient(base_url=settings.spbgasu_base_url)
            try:
                return await client.probe_group_schema(group_key)
            finally:
                await client.aclose()

        try:
            print(asyncio.run(probe()))
        except Exception as exc:
            print(f"Диагностика не выполнена: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "publish-dispatched":
        from szs_hub.publisher import publish_dispatched

        try:
            message_ids = asyncio.run(
                publish_dispatched(
                    settings,
                    text_b64=os.environ.get("SCHEDULE_TEXT_B64", ""),
                    group_key=os.environ.get("SCHEDULE_GROUP", ""),
                    state_path=Path(
                        os.environ.get(
                            "SCHEDULE_STATE_PATH", ".schedule-state/state.json"
                        )
                    ),
                )
            )
        except Exception as exc:
            print(f"Карточка не отправлена: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if message_ids:
            ids = ", ".join(str(item) for item in message_ids)
            print(f"Telegram обновлён, message_id={ids}.")
        else:
            print("Расписание проверено: изменений нет, Telegram не беспокоим.")
        return 0
    if args.command == "publish-reminder":
        from szs_hub.publisher import publish_due_reminder

        try:
            message_ids = asyncio.run(
                publish_due_reminder(
                    settings,
                    state_path=Path(
                        os.environ.get(
                            "SCHEDULE_STATE_PATH", ".schedule-state/state.json"
                        )
                    ),
                )
            )
        except Exception as exc:
            print(f"Напоминание не отправлено: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if message_ids:
            print(f"Напоминание отправлено, Telegram message_id={message_ids[0]}.")
        else:
            print("Сейчас напоминание не требуется.")
        return 0
    if args.command == "backup":
        output = args.output or _default_backup_path()
        backup_result = create_backup(settings.database_url, output)
        print(f"Резервная копия создана и проверена: {backup_result}")
        return 0
    if args.command == "restore":
        restore_result = restore_backup(
            args.input,
            settings.database_url,
            confirmed_offline=bool(args.confirmed_offline),
        )
        print(f"База восстановлена: {restore_result.restored_path}")
        if restore_result.safety_copy:
            print(f"Предыдущее состояние сохранено: {restore_result.safety_copy}")
        return 0
    if args.command == "run":
        from szs_hub.app import run

        asyncio.run(run(settings))
        return 0
    raise AssertionError("unknown command")


def _doctor(settings: Settings) -> int:
    fts5 = False
    with sqlite3.connect(":memory:") as database:
        try:
            database.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
            fts5 = True
        except sqlite3.OperationalError:
            pass
    version = sqlite3.sqlite_version_info
    wal_safe = sqlite_wal_is_safe(
        version,
        backport_confirmed=settings.sqlite_wal_backport_confirmed,
    )
    checks = {
        "environment": settings.app_env.value,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "sqlite_wal_safe": wal_safe,
        "fts5": fts5,
        "telegram_configured": settings.telegram_bot_token is not None,
        "schedule_configured": bool(settings.spbgasu_group_id),
        "ai_enabled": settings.ai_enabled,
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if fts5 and (wal_safe or settings.app_env.value == "local") else 1


def _migrate(settings: Settings) -> None:
    config_path, script_location = _find_migration_artifacts()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _find_migration_artifacts(
    *,
    working_directory: Path | None = None,
    source_project_root: Path | None = None,
) -> tuple[Path, Path]:
    """Locate one complete Alembic tree without silently mixing release/source files."""

    try:
        work_root = (working_directory or Path.cwd()).resolve()
    except OSError as exc:
        raise MigrationArtifactsError("рабочий каталог недоступен") from exc
    source_root = (source_project_root or _SOURCE_PROJECT_ROOT).resolve()

    work_state = _migration_artifact_state(work_root)
    source_state = _migration_artifact_state(source_root)

    if work_state == "partial":
        raise MigrationArtifactsError("в рабочем каталоге найден неполный набор Alembic")
    if source_root != work_root and work_state == "complete" and source_state == "complete":
        raise MigrationArtifactsError("найдено несколько полных наборов Alembic")
    if work_state == "complete":
        return work_root / "alembic.ini", work_root / "alembic"

    if source_state == "partial":
        raise MigrationArtifactsError("в исходном дереве найден неполный набор Alembic")
    if source_state == "complete":
        return source_root / "alembic.ini", source_root / "alembic"

    raise MigrationArtifactsError("alembic.ini и каталог alembic не найдены")


def _migration_artifact_state(root: Path) -> str:
    config = root / "alembic.ini"
    scripts = root / "alembic"
    markers = (config, scripts, scripts / "env.py", scripts / "versions")
    if config.is_file() and scripts.is_dir() and markers[2].is_file() and markers[3].is_dir():
        return "complete"
    if any(marker.exists() for marker in markers):
        return "partial"
    return "absent"


async def _health(settings: Settings, *, offline: bool = False) -> int:
    engine = create_database_engine(
        settings.database_url,
        environment=settings.app_env.value,
        sqlite_wal_backport_confirmed=settings.sqlite_wal_backport_confirmed,
    )
    try:
        report = await build_health_report(
            create_session_factory(engine),
            expect_schedule=bool(settings.spbgasu_group_id) and not offline,
            schedule_stale_after=timedelta(minutes=settings.schedule_stale_after_minutes),
            expect_runtime=not offline,
            runtime_stale_after=timedelta(
                minutes=settings.runtime_heartbeat_stale_minutes
            ),
            expect_telegram=settings.telegram_bot_token is not None and not offline,
            telegram_probe_stale_after=timedelta(
                minutes=settings.telegram_probe_stale_minutes
            ),
            backup_status_path=None if offline else settings.backup_status_path,
            backup_stale_after=timedelta(hours=settings.backup_stale_after_hours),
        )
    finally:
        await engine.dispose()
    payload = report.as_dict()
    payload["mode"] = "offline" if offline else "operational"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.state is HealthState.OK else 1


def _default_backup_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("backups") / f"szs-hub-{stamp}.sqlite3"


def _owner_alert(code: AlertCode) -> int:
    try:
        settings = AlertSettings()
    except ValidationError as exc:
        print("Конфигурация оповещений неполна:", file=sys.stderr)
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(item) for item in error["loc"])
            print(f"- {location}: {error['msg']}", file=sys.stderr)
        return 2

    try:
        outcome = asyncio.run(send_owner_alert(settings, code))
    except AlertDeliveryError as exc:
        print(f"Оповещение владельцу не доставлено: {exc.category}", file=sys.stderr)
        return 1
    except AlertStateError as exc:
        print(
            f"Оповещение доставлено, но защита от повторов недоступна: {exc.category}",
            file=sys.stderr,
        )
        return 1

    if outcome is AlertOutcome.SUPPRESSED:
        print("Недавнее оповещение этого типа уже доставлено; повтор подавлен.")
    else:
        print("Оповещение владельцу доставлено.")
    return 0


def _import_plan(path: Path) -> int:
    preflight = inspect_telegram_desktop_export(path)
    report = preflight.plan.report
    payload = {
        "source_fingerprint": preflight.source_fingerprint,
        "raw_chat_id": report.raw_chat_id,
        "chat_type": report.chat_type,
        "normalized_chat_id": report.normalized_chat_id,
        "messages_found": report.messages_found,
        "messages_planned": report.messages_planned,
        "duplicates_skipped": report.duplicates_skipped,
        "users": report.users,
        "documents": report.documents,
        "photos": report.photos,
        "videos": report.videos,
        "topics": report.topics,
        "media_groups": report.media_groups,
        "failures": [
            {"message_id": failure.message_id, "reason": failure.reason}
            for failure in report.failures
        ],
        "missing_media": [
            {
                "message_id": item.message_id,
                "relative_path": item.relative_path,
                "reason": item.reason,
            }
            for item in preflight.missing_media
        ],
        "limitations": list(report.limitations),
        "date_from": report.date_from.isoformat() if report.date_from else None,
        "date_to": report.date_to.isoformat() if report.date_to else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if report.failures else 0


def _import_apply(
    settings: Settings,
    *,
    export_path: Path,
    expected_fingerprint: str,
    backup_output: Path,
    confirmed_offline: bool,
    accept_parser_failures: bool,
    accept_missing_media: bool,
) -> int:
    if not confirmed_offline:
        print("Импорт требует подтверждения, что SZS Hub остановлен.", file=sys.stderr)
        return 2
    target_chat_id = settings.target_chat_id
    if target_chat_id is None:
        print("Импорт требует настроенный TARGET_CHAT_ID.", file=sys.stderr)
        return 2
    try:
        prepared = inspect_telegram_desktop_export(export_path)
    except (OSError, ValueError) as exc:
        print(f"Экспорт не прошёл проверку: {type(exc).__name__}.", file=sys.stderr)
        return 2
    if not hmac.compare_digest(prepared.source_fingerprint, expected_fingerprint):
        print("Fingerprint экспорта не совпадает с проверенным планом.", file=sys.stderr)
        return 2
    if prepared.plan.report.normalized_chat_id != target_chat_id:
        print("Экспорт относится не к настроенной Telegram-группе.", file=sys.stderr)
        return 2
    if prepared.plan.report.failures and not accept_parser_failures:
        print(
            "В плане есть необработанные объекты; требуется явное "
            "--accept-parser-failures.",
            file=sys.stderr,
        )
        return 2
    if prepared.missing_media and not accept_missing_media:
        print(
            "В экспорте отсутствуют media-файлы; требуется явное "
            "--accept-missing-media.",
            file=sys.stderr,
        )
        return 2

    try:
        database_path = sqlite_database_path(settings.database_url)
        backup_path = backup_output.expanduser().resolve()
        if backup_path.exists():
            raise FileExistsError("backup target already exists")
        if backup_path.is_relative_to(prepared.resolved_path.parent):
            raise ValueError("backup must be stored outside the disposable export tree")
        with ProcessLock(database_lock_path(database_path)):
            create_backup(settings.database_url, backup_path)
            result = asyncio.run(
                _apply_and_reconcile_import(
                    settings,
                    export_path=prepared.resolved_path,
                    expected_fingerprint=expected_fingerprint,
                    prepared=prepared,
                )
            )
    except ProcessLockUnavailableError:
        print("Импорт остановлен: runtime всё ещё держит базу.", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            f"Импорт не подтверждён как успешный: {type(exc).__name__}.",
            file=sys.stderr,
        )
        return 1

    result["backup_created"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def _apply_and_reconcile_import(
    settings: Settings,
    *,
    export_path: Path,
    expected_fingerprint: str,
    prepared: TelegramDesktopImportPreflight,
) -> dict[str, object]:
    target_chat_id = settings.target_chat_id
    if target_chat_id is None:
        raise ValueError("target chat is not configured")
    engine = create_database_engine(
        settings.database_url,
        environment=settings.app_env.value,
        sqlite_wal_backport_confirmed=settings.sqlite_wal_backport_confirmed,
    )
    sessions = create_session_factory(engine)
    try:
        report = await TelegramDesktopImporter(target_chat_id=target_chat_id).run(
            sessions,
            export_path,
            expected_fingerprint=expected_fingerprint,
        )
        reconciliation = await reconcile_telegram_desktop_import(sessions, prepared)
        if report.status != "succeeded" or not reconciliation.complete:
            raise RuntimeError("post-import reconciliation failed")
        await _record_import_audit(sessions, report.import_run_id, report.source_fingerprint)
    finally:
        await engine.dispose()
    return {
        "status": report.status,
        "import_run_id": report.import_run_id,
        "source_fingerprint": report.source_fingerprint,
        "messages_found": report.messages_found,
        "messages_planned": report.messages_planned,
        "messages_processed": report.messages_processed,
        "messages_created": report.messages_created,
        "messages_updated": report.messages_updated,
        "users_created": report.users_created,
        "topics_created": report.topics_created,
        "media_groups_created": report.media_groups_created,
        "files_created": report.files_created,
        "parser_failure_count": len(report.parser_failures),
        "missing_media_count": len(report.missing_media),
        "reconciliation": {
            "complete": reconciliation.complete,
            "messages_archived": reconciliation.messages_archived,
            "search_documents_verified": reconciliation.search_documents_verified,
            "files_archived": reconciliation.files_archived,
            "reply_links_checked": reconciliation.reply_links_checked,
            "reply_link_mismatches": reconciliation.reply_link_mismatches,
        },
    }


async def _record_import_audit(
    sessions: async_sessionmaker[AsyncSession],
    import_run_id: int,
    fingerprint: str,
) -> None:
    async with sessions() as session, session.begin():
        target_id = str(import_run_id)
        existing = await session.scalar(
            select(AdminEvent.id).where(
                AdminEvent.event_type == "telegram_desktop_import_verified",
                AdminEvent.target_type == "import_run",
                AdminEvent.target_id == target_id,
            )
        )
        if existing is None:
            session.add(
                AdminEvent(
                    event_type="telegram_desktop_import_verified",
                    target_type="import_run",
                    target_id=target_id,
                    details={"source_fingerprint": fingerprint},
                )
            )
