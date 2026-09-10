"""Composition root for the durable polling application."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.ai.provider import OpenAICompatibleProvider
from szs_hub.ai.quota import DurableDailyQuota, QuotaLimitedProvider
from szs_hub.archive.live import LiveArchive
from szs_hub.assistant.conversation import ConversationStore
from szs_hub.assistant.retriever import SQLiteArchiveRetriever
from szs_hub.assistant.service import AssistantService
from szs_hub.attendance.service import AttendanceService
from szs_hub.backup import sqlite_database_path
from szs_hub.config import FeatureProfile, Settings, TelegramMode
from szs_hub.jobs.application import ApplicationJobHandlers
from szs_hub.jobs.queue import JobQueue, OutboxQueue
from szs_hub.jobs.worker import JobWorker, OutboxWorker
from szs_hub.logging_config import configure_logging
from szs_hub.materials.automation import MaterialJobHandlers
from szs_hub.materials.processing import (
    MATERIAL_EXTRACTION_VERSION,
    MaterialExtractionJobHandlers,
)
from szs_hub.schedule.coordinator import ScheduleCoordinator
from szs_hub.schedule.service import ScheduleSyncService
from szs_hub.schedule.spbgasu import SpbGasuClient
from szs_hub.storage.base import utc_now
from szs_hub.storage.database import create_database_engine, create_session_factory
from szs_hub.storage.models import SystemSetting
from szs_hub.storage.process_lock import ProcessLock, database_lock_path
from szs_hub.telegram.application_sender import ApplicationTelegramSender
from szs_hub.telegram.file_download import AiogramMaterialFileDownloader
from szs_hub.telegram.handlers import HandlerServices, build_router
from szs_hub.telegram.membership import ChatMemberSource, TelegramMembershipGateway
from szs_hub.telegram.membership_store import TelegramMembershipStore
from szs_hub.telegram.runtime import DurableUpdatePump, UpdateDispatcher, UpdateSource
from szs_hub.telegram.sender import TelegramOutboxSender, TelegramSendAPI

logger = logging.getLogger(__name__)


class RuntimeConfigurationError(ValueError):
    """A setting required by the selected runtime is missing or unsupported."""


@dataclass(frozen=True, slots=True)
class RecurringJob:
    kind: str
    idempotency_key: str
    payload: Mapping[str, object]


async def run(settings: Settings) -> None:
    """Run the selected feature profile until stopped."""

    _validate_runtime_settings(settings)
    configure_logging(settings.log_level)

    token = _required_secret(settings.telegram_bot_token, "telegram_bot_token")
    target_chat_id = _required_int(settings.target_chat_id, "target_chat_id")
    group_key = _required_text(settings.spbgasu_group_id, "spbgasu_group_id")
    engine = create_database_engine(
        settings.database_url,
        environment=settings.app_env.value,
        sqlite_wal_backport_confirmed=settings.sqlite_wal_backport_confirmed,
    )
    process_lock = ProcessLock(database_lock_path(sqlite_database_path(settings.database_url)))
    try:
        process_lock.acquire()
    except BaseException:
        await engine.dispose()
        raise
    bot: Bot | None = None
    schedule_client: SpbGasuClient | None = None
    ai_client: OpenAICompatibleProvider | None = None
    dispatcher: Dispatcher | None = None
    stop = asyncio.Event()

    try:
        sessions = create_session_factory(engine)
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        schedule_client = SpbGasuClient(base_url=settings.spbgasu_base_url)
        schedule_service = ScheduleSyncService(
            client=schedule_client,
            session_factory=sessions,
            clock=utc_now,
            fresh_for=timedelta(minutes=_schedule_freshness_minutes(settings)),
            retain_teacher_names=settings.feature_profile is FeatureProfile.FULL,
        )
        schedule = ScheduleCoordinator(
            group_key=group_key,
            bootstrap_client=schedule_client,
            sync_service=schedule_service,
            clock=utc_now,
            timezone=settings.timezone,
        )
        jobs = JobQueue(sessions, worker_id=_worker_id())
        outbox = OutboxQueue(sessions)
        pump: DurableUpdatePump | None = None
        material_worker: JobWorker | None = None

        if _receives_telegram_updates(settings):
            dispatcher = Dispatcher(disable_fsm=True)
            material_queue = JobQueue(
                sessions,
                worker_id=f"{_worker_id()}:materials",
            )
            assistant, conversations, ai_client = await _build_assistant(
                settings=settings,
                sessions=sessions,
                bot=bot,
                target_chat_id=target_chat_id,
            )
            services = HandlerServices(
                settings=settings,
                sessions=sessions,
                live_archive=LiveArchive(target_chat_id=target_chat_id),
                memberships=TelegramMembershipStore(target_chat_id=target_chat_id),
                attendance=AttendanceService,
                jobs=jobs,
                schedule=schedule,
                assistant=assistant,
                conversations=conversations,
            )
            dispatcher.include_router(build_router(services))
            pump = DurableUpdatePump(
                session_factory=sessions,
                source=cast(UpdateSource, bot),
                dispatcher=cast(UpdateDispatcher, dispatcher),
                bot_context=bot,
            )

            material_handlers = MaterialJobHandlers(
                settings=settings,
                session_factory=sessions,
                outbox=outbox,
                clock=utc_now,
            )
            material_extraction_handlers = MaterialExtractionJobHandlers(
                target_chat_id=target_chat_id,
                session_factory=sessions,
                jobs=material_queue,
                downloader=AiogramMaterialFileDownloader(bot),
                clock=utc_now,
            )
            material_worker = JobWorker(
                material_queue,
                {
                    **material_handlers.mapping,
                    **(
                        material_extraction_handlers.mapping
                        if settings.material_extraction_enabled
                        else {}
                    ),
                },
                lease=timedelta(minutes=5),
            )
        application_handlers = ApplicationJobHandlers(
            settings=settings,
            session_factory=sessions,
            schedule=schedule,
            jobs=jobs,
            outbox=outbox,
            clock=utc_now,
        )
        job_worker = JobWorker(
            jobs,
            application_handlers.mapping,
            lease=timedelta(minutes=5),
        )
        telegram_sender = TelegramOutboxSender(cast(TelegramSendAPI, bot))
        outbox_worker = OutboxWorker(
            outbox,
            ApplicationTelegramSender(
                sender=telegram_sender,
                session_factory=sessions,
                settings=settings,
                membership_gateway=(
                    TelegramMembershipGateway(cast(ChatMemberSource, bot))
                    if settings.feature_profile is FeatureProfile.FULL
                    else None
                ),
            ),
        )

        with _signal_handlers(stop):
            await _telegram_startup_probe(bot, settings)
            await _record_telegram_probe(sessions, ok=True)
            await _record_runtime_configuration(sessions, settings)
            await bot.delete_webhook(
                drop_pending_updates=not _receives_telegram_updates(settings)
            )
            if dispatcher is not None:
                await dispatcher.emit_startup(bot=bot)
            try:
                async with asyncio.TaskGroup() as tasks:
                    running = [
                        tasks.create_task(job_worker.run(stop), name="job-worker"),
                        tasks.create_task(outbox_worker.run(stop), name="outbox-worker"),
                        tasks.create_task(
                            _schedule_recurring(settings, jobs, stop),
                            name="recurring-scheduler",
                        ),
                        tasks.create_task(
                            _probe_telegram_periodically(
                                bot,
                                settings,
                                sessions,
                                stop,
                            ),
                            name="telegram-rights-probe",
                        ),
                    ]
                    if pump is not None:
                        running.extend(
                            [
                                tasks.create_task(
                                    _poll_updates(pump, stop), name="telegram-poller"
                                ),
                                tasks.create_task(
                                    _process_updates(pump, stop), name="update-worker"
                                ),
                            ]
                        )
                    if material_worker is not None:
                        running.append(
                            tasks.create_task(
                                material_worker.run(stop),
                                name="material-worker",
                            )
                        )
                    await stop.wait()
                    for task in running:
                        task.cancel()
            finally:
                if dispatcher is not None:
                    await dispatcher.emit_shutdown(bot=bot)
    finally:
        stop.set()
        try:
            await _close_runtime_resources(ai_client, schedule_client, bot)
        finally:
            try:
                await engine.dispose()
            finally:
                process_lock.release()


def planned_recurring_jobs(settings: Settings, now: datetime) -> tuple[RecurringJob, ...]:
    """Return deterministic queue admissions for the current time buckets."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")
    sync_seconds = settings.schedule_sync_minutes * 60
    sync_bucket = int(now.timestamp()) // sync_seconds
    planned = [RecurringJob("schedule.sync", f"schedule-sync:{sync_bucket}", {})]
    minute_bucket = int(now.timestamp()) // 60
    local_now = now.astimezone(_load_timezone(settings.timezone))
    local_day = local_now.date().isoformat()
    if settings.feature_profile is FeatureProfile.FULL:
        planned.extend(
            [
                RecurringJob(
                    "maintenance.minute",
                    f"maintenance-minute:{minute_bucket}",
                    {},
                ),
                RecurringJob(
                    "maintenance.retention",
                    f"maintenance-retention:{local_day}",
                    {},
                ),
            ]
        )
    else:
        planned.append(
            RecurringJob(
                "runtime.heartbeat",
                f"runtime-heartbeat:{minute_bucket}",
                {},
            )
        )
    if local_now.time().replace(tzinfo=None) >= settings.evening_schedule_time:
        planned.append(
            RecurringJob("schedule.evening", f"schedule-evening:{local_day}", {})
        )
    return tuple(planned)


def _receives_telegram_updates(settings: Settings) -> bool:
    """Whether the selected profile is allowed to ingest Telegram updates."""

    return settings.feature_profile is FeatureProfile.FULL


async def _schedule_recurring(
    settings: Settings,
    queue: JobQueue,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        now = utc_now()
        for job in planned_recurring_jobs(settings, now):
            await queue.enqueue(
                kind=job.kind,
                idempotency_key=job.idempotency_key,
                payload=job.payload,
                max_attempts=8,
            )
        await _wait_or_stop(stop, 15.0)


async def _poll_updates(pump: DurableUpdatePump, stop: asyncio.Event) -> None:
    offset = await pump.initial_offset()
    while not stop.is_set():
        try:
            offset = await pump.poll_once(offset=offset, poll_timeout=30)
        except TelegramRetryAfter as error:
            await _wait_or_stop(stop, min(60.0, max(1.0, float(error.retry_after))))
        except (TelegramNetworkError, TelegramServerError):
            logger.warning("temporary Telegram polling failure")
            await _wait_or_stop(stop, 3.0)


async def _process_updates(pump: DurableUpdatePump, stop: asyncio.Event) -> None:
    while not stop.is_set():
        processed = await pump.process_ready()
        if processed == 0:
            await _wait_or_stop(stop, 0.5)


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass


async def _build_assistant(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    bot: Bot,
    target_chat_id: int,
) -> tuple[AssistantService | None, ConversationStore | None, OpenAICompatibleProvider | None]:
    if not settings.ai_enabled:
        return None, None, None
    if settings.ai_provider != "openai_compatible":
        raise RuntimeConfigurationError("only the openai_compatible AI provider is supported")

    ai_client = OpenAICompatibleProvider(
        base_url=_required_text(settings.ai_base_url, "ai_base_url"),
        api_key=_required_secret(settings.ai_api_key, "ai_api_key"),
        model=_required_text(settings.ai_model, "ai_model"),
        timeout_seconds=settings.ai_timeout_seconds,
        max_retries=settings.ai_max_retries,
        max_response_bytes=settings.ai_max_response_bytes,
    )
    try:
        quota = DurableDailyQuota(
            session_factory=sessions,
            request_limit=settings.ai_daily_request_limit,
            token_limit=settings.ai_daily_token_limit,
            tokens_per_request=settings.ai_token_reservation_per_request,
            per_user_request_limit=settings.ai_per_user_daily_request_limit,
            monthly_request_limit=settings.ai_monthly_request_limit,
            monthly_token_limit=settings.ai_monthly_token_limit,
        )
        assistant = AssistantService(
            source_chat_id=target_chat_id,
            membership_gateway=TelegramMembershipGateway(cast(ChatMemberSource, bot)),
            retriever=SQLiteArchiveRetriever(sessions, timezone=settings.timezone),
            ai_provider=QuotaLimitedProvider(ai_client, quota),
        )
        return assistant, ConversationStore(sessions), ai_client
    except BaseException:
        await ai_client.aclose()
        raise


async def _close_runtime_resources(
    ai_client: OpenAICompatibleProvider | None,
    schedule_client: SpbGasuClient | None,
    bot: Bot | None,
) -> None:
    closers: list[Awaitable[None]] = []
    if ai_client is not None:
        closers.append(ai_client.aclose())
    if schedule_client is not None:
        closers.append(schedule_client.aclose())
    if bot is not None:
        closers.append(bot.session.close())
    if not closers:
        return
    results = await asyncio.gather(*closers, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.error("runtime resource cleanup failed: %s", type(result).__name__)


def _validate_runtime_settings(settings: Settings) -> None:
    expected_mode = (
        TelegramMode.OUTBOUND_ONLY
        if settings.feature_profile is FeatureProfile.SCHEDULE_ONLY
        else TelegramMode.POLLING
    )
    if settings.telegram_mode is not expected_mode:
        raise RuntimeConfigurationError(
            f"{settings.feature_profile.value} requires TELEGRAM_MODE={expected_mode.value}"
        )
    required: list[tuple[str, object]] = [
        ("telegram_bot_token", settings.telegram_bot_token),
        ("target_chat_id", settings.target_chat_id),
        ("schedule_topic_id", settings.schedule_topic_id),
        ("spbgasu_group_id", settings.spbgasu_group_id),
    ]
    if settings.feature_profile is FeatureProfile.FULL:
        required.extend(
            [
                ("materials_topic_id", settings.materials_topic_id),
                ("headman_user_id", settings.headman_user_id),
            ]
        )
    missing = [name for name, value in required if value is None or value == ""]
    if missing:
        raise RuntimeConfigurationError(
            "runtime configuration is incomplete: " + ", ".join(missing)
        )
    if not settings.live_processing_approved:
        if settings.feature_profile is FeatureProfile.SCHEDULE_ONLY:
            raise RuntimeConfigurationError(
                "Live schedule publishing is paused: verify the public schedule source and "
                "target topic, then explicitly set LIVE_PROCESSING_APPROVED=true."
            )
        raise RuntimeConfigurationError(
            "Live processing is paused: review docs/LEGAL_LAUNCH_RU.md before explicitly "
            "setting LIVE_PROCESSING_APPROVED=true. AI_KILL_SWITCH alone does not stop "
            "group archiving. This acknowledgement is not member consent."
        )


async def _telegram_startup_probe(bot: Bot, settings: Settings) -> None:
    """Fail before polling if the bot cannot enforce the product's access model."""

    target_chat_id = _required_int(settings.target_chat_id, "target_chat_id")
    identity = await bot.get_me()
    chat = await bot.get_chat(target_chat_id)
    if chat.type is not ChatType.SUPERGROUP or chat.is_forum is not True:
        raise RuntimeConfigurationError("target chat must be a forum supergroup")
    bot_member = await bot.get_chat_member(target_chat_id, identity.id)
    if settings.feature_profile is FeatureProfile.SCHEDULE_ONLY:
        can_publish = bot_member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        } or (
            bot_member.status is ChatMemberStatus.RESTRICTED
            and getattr(bot_member, "is_member", None) is True
            and getattr(bot_member, "can_send_messages", None) is not False
        )
        if not can_publish:
            raise RuntimeConfigurationError(
                "the bot must be a current member allowed to send schedule messages"
            )
        return
    if bot_member.status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        raise RuntimeConfigurationError("the bot must be an administrator of the target chat")
    headman_user_id = _required_int(settings.headman_user_id, "headman_user_id")
    headman = await bot.get_chat_member(target_chat_id, headman_user_id)
    if headman.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or (
        headman.status is ChatMemberStatus.RESTRICTED
        and getattr(headman, "is_member", None) is False
    ):
        raise RuntimeConfigurationError("the configured headman is not a current chat member")


async def _probe_telegram_periodically(
    bot: Bot,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> None:
    delay = float(settings.telegram_probe_interval_minutes * 60)
    while not stop.is_set():
        await _wait_or_stop(stop, delay)
        if stop.is_set():
            return
        try:
            await _telegram_startup_probe(bot, settings)
        except RuntimeConfigurationError as error:
            await _record_telegram_probe(
                sessions,
                ok=False,
                error_type=type(error).__name__,
            )
            raise
        except TelegramAPIError as error:
            await _record_telegram_probe(
                sessions,
                ok=False,
                error_type=type(error).__name__,
            )
        else:
            await _record_telegram_probe(sessions, ok=True)


async def _record_telegram_probe(
    sessions: async_sessionmaker[AsyncSession],
    *,
    ok: bool,
    error_type: str | None = None,
) -> None:
    value: dict[str, object] = {"at": utc_now().isoformat(), "ok": ok}
    if error_type is not None:
        value["error_type"] = error_type[:100]
    await _write_setting(sessions, "telegram.probe", value)


async def _record_runtime_configuration(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await _write_setting(sessions, "runtime.configuration", _runtime_configuration(settings))


async def _write_setting(
    sessions: async_sessionmaker[AsyncSession],
    key: str,
    value: dict[str, object],
) -> None:
    async with sessions() as session, session.begin():
        setting = await session.get(SystemSetting, key)
        if setting is None:
            session.add(SystemSetting(key=key, value=value))
        else:
            setting.value = value
            setting.updated_at = utc_now()


def _runtime_configuration(settings: Settings) -> dict[str, object]:
    """Return a recoverable configuration manifest with every secret omitted."""

    manifest: dict[str, object] = {
        "version": 5,
        "app_env": settings.app_env.value,
        "feature_profile": settings.feature_profile.value,
        "timezone": settings.timezone,
        "telegram_mode": settings.telegram_mode.value,
        "target_chat_id": settings.target_chat_id,
        "schedule_topic_id": settings.schedule_topic_id,
        "evening_schedule_time": settings.evening_schedule_time.isoformat(),
        "spbgasu_group_id": settings.spbgasu_group_id,
        "spbgasu_group_name": settings.spbgasu_group_name,
        "schedule_sync_minutes": settings.schedule_sync_minutes,
        "schedule_stale_after_minutes": settings.schedule_stale_after_minutes,
        "runtime_heartbeat_stale_minutes": settings.runtime_heartbeat_stale_minutes,
        "telegram_probe_interval_minutes": settings.telegram_probe_interval_minutes,
        "telegram_probe_stale_minutes": settings.telegram_probe_stale_minutes,
        "backup_status_path": (
            str(settings.backup_status_path) if settings.backup_status_path is not None else None
        ),
        "backup_stale_after_hours": settings.backup_stale_after_hours,
    }
    if settings.feature_profile is FeatureProfile.FULL:
        manifest.update(
            {
                "general_topic_id": settings.general_topic_id,
                "materials_topic_id": settings.materials_topic_id,
                "assistant_topic_id": settings.assistant_topic_id,
                "headman_user_id": settings.headman_user_id,
                "alert_user_id": settings.alert_user_id,
                "admin_user_ids": list(settings.admin_user_ids),
                "attendance_reaction": settings.attendance_reaction,
                "attendance_window_minutes": settings.attendance_window_minutes,
                "ai_provider": settings.ai_provider,
                "ai_model": settings.ai_model,
                "ai_kill_switch": settings.ai_kill_switch,
                "ai_max_retries": settings.ai_max_retries,
                "ai_max_response_bytes": settings.ai_max_response_bytes,
                "ai_daily_request_limit": settings.ai_daily_request_limit,
                "ai_daily_token_limit": settings.ai_daily_token_limit,
                "ai_per_user_daily_request_limit": settings.ai_per_user_daily_request_limit,
                "ai_monthly_request_limit": settings.ai_monthly_request_limit,
                "ai_monthly_token_limit": settings.ai_monthly_token_limit,
                "ai_token_reservation_per_request": settings.ai_token_reservation_per_request,
                "material_auto_copy_threshold": settings.material_auto_copy_threshold,
                "material_admin_review_threshold": settings.material_admin_review_threshold,
                "material_extraction_enabled": settings.material_extraction_enabled,
                "material_extraction_version": MATERIAL_EXTRACTION_VERSION,
                "material_ocr_enabled": False,
                "material_auto_delete_source": False,
            }
        )
    return manifest


def _schedule_freshness_minutes(settings: Settings) -> int:
    expected_refresh = max(30, settings.schedule_sync_minutes * 2)
    return min(settings.schedule_stale_after_minutes, expected_refresh)


def _worker_id() -> str:
    hostname = "".join(
        character if character.isalnum() or character in "-." else "-"
        for character in socket.gethostname()
    )
    return f"{hostname[:96]}:{os.getpid()}"


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name=name)
        raise


def _required_int(value: int | None, name: str) -> int:
    if value is None:
        raise RuntimeConfigurationError(f"required setting is missing: {name}")
    return value


def _required_text(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise RuntimeConfigurationError(f"required setting is missing: {name}")
    return value.strip()


def _required_secret(value: object, name: str) -> str:
    from pydantic import SecretStr

    if not isinstance(value, SecretStr) or not value.get_secret_value():
        raise RuntimeConfigurationError(f"required setting is missing: {name}")
    return value.get_secret_value()


@contextmanager
def _signal_handlers(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for watched in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(watched, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(watched)
    try:
        yield
    finally:
        for watched in installed:
            loop.remove_signal_handler(watched)
