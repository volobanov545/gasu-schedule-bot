"""Stateless schedule publication for GitHub Actions."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pydantic import SecretStr

from szs_hub.config import Settings
from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ci import (
    ScheduleDeliveryState,
    ScheduleEnvelope,
    changes_are_urgent,
    decode_schedule_envelope,
    due_reminder,
    encode_schedule_envelope,
    load_delivery_state,
    overlapping_changes,
    render_changes_fallback,
    render_digest_fallback,
    render_rich_changes,
    render_rich_digest,
    save_delivery_state,
    should_publish_digest,
)
from szs_hub.schedule.render import render_day_card
from szs_hub.schedule.spbgasu import (
    SchedulePageBootstrap,
    SpbGasuClient,
    WeeklySchedule,
    materialize_week,
    parity_for_week,
)
from szs_hub.storage.base import utc_now


class ScheduleSource(Protocol):
    async def fetch_bootstrap(self) -> SchedulePageBootstrap: ...

    async def fetch_group(self, group_key: str) -> WeeklySchedule: ...


class ScheduleDestination(Protocol):
    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int: ...


class RichScheduleDestination(ScheduleDestination, Protocol):
    async def send_rich(
        self,
        *,
        chat_id: int,
        topic_id: int,
        rich_html: str,
        fallback_html: str,
        silent: bool,
    ) -> int: ...


class ScheduleBridge(Protocol):
    async def dispatch(
        self,
        *,
        repository: str,
        token: str,
        text_b64: str,
        group_key: str,
    ) -> None: ...


class AiogramScheduleDestination:
    """Small outbound-only adapter around the Telegram Bot API."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        message = await self._bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=text,
        )
        return message.message_id


class TelegramBotApiDestination:
    """Use Bot API 10.3 Rich Messages with a dependable classic fallback."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        return await self._send_classic(
            chat_id=chat_id,
            topic_id=topic_id,
            text=text,
            silent=False,
        )

    async def send_rich(
        self,
        *,
        chat_id: int,
        topic_id: int,
        rich_html: str,
        fallback_html: str,
        silent: bool,
    ) -> int:
        response = await self._client.post(
            f"{self._base_url}/sendRichMessage",
            json={
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "rich_message": {"html": rich_html, "skip_entity_detection": True},
                "disable_notification": silent,
            },
        )
        result = _telegram_message_id(response)
        if result is not None:
            return result
        if response.status_code != 400:
            raise _telegram_delivery_error(response)
        return await self._send_classic(
            chat_id=chat_id,
            topic_id=topic_id,
            text=fallback_html,
            silent=silent,
        )

    async def _send_classic(
        self,
        *,
        chat_id: int,
        topic_id: int,
        text: str,
        silent: bool,
    ) -> int:
        response = await self._client.post(
            f"{self._base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
        )
        result = _telegram_message_id(response)
        if result is None:
            raise _telegram_delivery_error(response)
        return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class GitHubWorkflowBridge:
    """Send one prepared card to the narrow GitHub receiver workflow."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def dispatch(
        self,
        *,
        repository: str,
        token: str,
        text_b64: str,
        group_key: str,
    ) -> None:
        response = await self._client.post(
            f"https://api.github.com/repos/{repository}/actions/workflows/"
            "receive-schedule.yml/dispatches",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "SZS-Hub/0.1",
            },
            json={
                "ref": "main",
                "inputs": {"text_b64": text_b64, "group": group_key},
            },
        )
        if not response.is_success:
            raise RuntimeError(
                f"GitHub receiver dispatch failed (HTTP {response.status_code})"
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def publish_tomorrow(
    settings: Settings,
    *,
    source: ScheduleSource | None = None,
    destination: ScheduleDestination | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> int:
    """Fetch and publish tomorrow's schedule without a database or update receiver."""

    token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    chat_id = _required_int(settings.target_chat_id, "TARGET_CHAT_ID", negative=True)
    topic_id = _required_int(settings.schedule_topic_id, "SCHEDULE_TOPIC_ID", negative=False)
    group_key = _required_text(settings.spbgasu_group_id, "SPBGASU_GROUP_ID")
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("publisher clock must be timezone-aware")

    own_source = source is None
    own_bot = destination is None
    schedule_source = source or SpbGasuClient(base_url=settings.spbgasu_base_url)
    bot: Bot | None = None
    if destination is None:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        schedule_destination: ScheduleDestination = AiogramScheduleDestination(bot)
    else:
        schedule_destination = destination

    try:
        text = await build_tomorrow_card(
            schedule_source,
            group_key=group_key,
            now=now,
            timezone=settings.timezone,
        )
        return await schedule_destination.send(chat_id=chat_id, topic_id=topic_id, text=text)
    finally:
        if own_source:
            await _close_source(schedule_source)
        if own_bot and bot is not None:
            await bot.session.close()


async def dispatch_tomorrow(
    settings: Settings,
    *,
    github_token: str,
    github_repository: str,
    force_digest: bool = False,
    source: ScheduleSource | None = None,
    bridge: ScheduleBridge | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> None:
    """Backward-compatible name for the two-week CI schedule dispatch."""

    await dispatch_schedule(
        settings,
        github_token=github_token,
        github_repository=github_repository,
        force_digest=force_digest,
        source=source,
        bridge=bridge,
        clock=clock,
    )


async def dispatch_schedule(
    settings: Settings,
    *,
    github_token: str,
    github_repository: str,
    force_digest: bool = False,
    source: ScheduleSource | None = None,
    bridge: ScheduleBridge | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> None:
    """Fetch a two-week horizon in Russia and hand it to the GitHub sender."""

    group_key = _required_text(settings.spbgasu_group_id, "SPBGASU_GROUP_ID")
    token = _required_text(github_token, "BRIDGE_GH_TOKEN")
    repository = _required_repository(github_repository)
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("publisher clock must be timezone-aware")

    own_source = source is None
    own_bridge = bridge is None
    schedule_source = source or SpbGasuClient(base_url=settings.spbgasu_base_url)
    schedule_bridge = bridge or GitHubWorkflowBridge()
    try:
        envelope = await build_schedule_envelope(
            schedule_source,
            group_key=group_key,
            now=now,
            timezone=settings.timezone,
            force_digest=force_digest,
        )
        await schedule_bridge.dispatch(
            repository=repository,
            token=token,
            text_b64=encode_schedule_envelope(envelope),
            group_key=group_key,
        )
    finally:
        if own_source:
            await _close_source(schedule_source)
        if own_bridge:
            await _close_source(schedule_bridge)


async def publish_dispatched(
    settings: Settings,
    *,
    text_b64: str,
    group_key: str,
    destination: RichScheduleDestination | None = None,
    state_path: Path = Path(".schedule-state/state.json"),
    clock: Callable[[], datetime] = utc_now,
) -> tuple[int, ...]:
    """Compare, format and publish a validated GitVerse schedule envelope."""

    token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    chat_id = _required_int(settings.target_chat_id, "TARGET_CHAT_ID", negative=True)
    topic_id = _required_int(settings.schedule_topic_id, "SCHEDULE_TOPIC_ID", negative=False)
    expected_group = _required_text(settings.spbgasu_group_id, "SPBGASU_GROUP_ID")
    if group_key.strip().casefold() != expected_group.casefold():
        raise ValueError("bridge payload group does not match SPBGASU_GROUP_ID")
    # Keep the receiver compatible while GitHub and GitVerse update independently.
    legacy_text = _decode_legacy_card(text_b64)
    own_destination = destination is None
    if destination is None:
        schedule_destination: RichScheduleDestination = TelegramBotApiDestination(token)
    else:
        schedule_destination = destination
    try:
        if legacy_text is not None:
            return (
                await schedule_destination.send(
                    chat_id=chat_id,
                    topic_id=topic_id,
                    text=legacy_text,
                ),
            )

        envelope = decode_schedule_envelope(text_b64)
        if envelope.group_key.strip().casefold() != expected_group.casefold():
            raise ValueError("bridge envelope group does not match SPBGASU_GROUP_ID")
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("publisher clock must be timezone-aware")
        local_now = now.astimezone(_load_timezone(settings.timezone))
        state = load_delivery_state(state_path)
        if state.previous is not None and envelope.fetched_at <= state.previous.fetched_at:
            # GitVerse manual and scheduled runs can finish out of order. Never
            # regress the comparison baseline or emit reverse/duplicate changes.
            return ()
        # A forced digest is an operator-requested corrective snapshot. Treat it as
        # a clean rebaseline so a previously empty/broken cache cannot manufacture
        # a wall of "added" lessons before the requested card.
        changes = (
            ()
            if envelope.force_digest
            else overlapping_changes(state.previous, envelope, today=local_now.date())
        )
        sent: list[int] = []

        if changes:
            sent.append(
                await schedule_destination.send_rich(
                    chat_id=chat_id,
                    topic_id=topic_id,
                    rich_html=render_rich_changes(
                        changes,
                        fetched_at=envelope.fetched_at,
                    ),
                    fallback_html=render_changes_fallback(changes),
                    silent=not changes_are_urgent(changes, today=local_now.date()),
                )
            )

        publish_digest = should_publish_digest(state, envelope, local_now=local_now)
        if publish_digest:
            sent.append(
                await schedule_destination.send_rich(
                    chat_id=chat_id,
                    topic_id=topic_id,
                    rich_html=render_rich_digest(
                        envelope,
                        local_now=local_now,
                    ),
                    fallback_html=render_digest_fallback(
                        envelope,
                        local_now=local_now,
                    ),
                    silent=False,
                )
            )

        save_delivery_state(
            state_path,
            ScheduleDeliveryState(
                previous=envelope,
                last_digest_date=local_now.date() if publish_digest else state.last_digest_date,
                sent_reminders=state.sent_reminders,
            ),
        )
        return tuple(sent)
    finally:
        if own_destination:
            await _close_source(schedule_destination)


async def publish_due_reminder(
    settings: Settings,
    *,
    destination: ScheduleDestination | None = None,
    state_path: Path = Path(".schedule-state/state.json"),
    clock: Callable[[], datetime] = utc_now,
) -> tuple[int, ...]:
    """Send one due class reminder from the last schedule snapshot, then mark it sent."""

    token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    chat_id = _required_int(settings.target_chat_id, "TARGET_CHAT_ID", negative=True)
    topic_id = _required_int(settings.schedule_topic_id, "SCHEDULE_TOPIC_ID", negative=False)
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("publisher clock must be timezone-aware")
    local_now = now.astimezone(_load_timezone(settings.timezone))
    state = load_delivery_state(state_path)
    if state.previous is None:
        return ()
    reminder = due_reminder(
        state.previous,
        local_now=local_now,
        sent_markers=state.sent_reminders,
    )
    if reminder is None:
        return ()

    marker, text = reminder
    own_destination = destination is None
    schedule_destination = destination or TelegramBotApiDestination(token)
    try:
        message_id = await schedule_destination.send(
            chat_id=chat_id,
            topic_id=topic_id,
            text=text,
        )
        save_delivery_state(
            state_path,
            ScheduleDeliveryState(
                previous=state.previous,
                last_digest_date=state.last_digest_date,
                sent_reminders=(*state.sent_reminders, marker)[-100:],
            ),
        )
        return (message_id,)
    finally:
        if own_destination:
            await _close_source(schedule_destination)


async def build_schedule_envelope(
    source: ScheduleSource,
    *,
    group_key: str,
    now: datetime,
    timezone: str,
    force_digest: bool = False,
) -> ScheduleEnvelope:
    zone = _load_timezone(timezone)
    local_now = now.astimezone(zone)
    local_today = local_now.date()
    current_monday = local_today - timedelta(days=local_today.weekday())
    bootstrap = await source.fetch_bootstrap()
    weekly = await source.fetch_group(group_key)
    if weekly.group_key.strip() != group_key.strip():
        raise ValueError("schedule source returned a different group")
    lessons: list[Lesson] = []
    for offset in (0, 1):
        monday = current_monday + timedelta(days=offset * 7)
        parity = parity_for_week(
            current_week_number=bootstrap.current_week_number,
            current_monday=current_monday,
            target_monday=monday,
        )
        lessons.extend(
            replace(lesson, teacher=None)
            for lesson in materialize_week(weekly, monday=monday, parity=parity)
        )
    return ScheduleEnvelope(
        group_key=group_key,
        fetched_at=local_now,
        horizon_start=current_monday,
        horizon_end=current_monday + timedelta(days=13),
        lessons=tuple(lessons),
        force_digest=force_digest,
    )


def encode_schedule_card(text: str) -> str:
    _validate_schedule_card(text)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_schedule_card(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid bridge schedule payload") from exc
    _validate_schedule_card(text)
    return text


def _validate_schedule_card(text: str) -> None:
    if not text.startswith("📅 <b>Завтра</b> · "):
        raise ValueError("bridge payload is not a tomorrow schedule card")
    if "rasp.spbgasu.ru" in text or "Источник: СПбГАСУ" in text:
        raise ValueError("bridge payload must not include a source link")
    if len(text) > 4096:
        raise ValueError("bridge schedule payload exceeds Telegram limit")


async def build_tomorrow_card(
    source: ScheduleSource,
    *,
    group_key: str,
    now: datetime,
    timezone: str,
) -> str:
    """Build a minimal card from the current public weekly schedule."""

    local_today = now.astimezone(_load_timezone(timezone)).date()
    tomorrow = local_today + timedelta(days=1)
    current_monday = local_today - timedelta(days=local_today.weekday())
    target_monday = tomorrow - timedelta(days=tomorrow.weekday())

    bootstrap = await source.fetch_bootstrap()
    weekly = await source.fetch_group(group_key)
    if weekly.group_key.strip() != group_key.strip():
        raise ValueError("schedule source returned a different group")
    parity = parity_for_week(
        current_week_number=bootstrap.current_week_number,
        current_monday=current_monday,
        target_monday=target_monday,
    )
    lessons = tuple(
        replace(lesson, teacher=None)
        for lesson in materialize_week(weekly, monday=target_monday, parity=parity)
        if lesson.day == tomorrow
    )
    card = render_day_card(tomorrow, lessons, relative_label="Завтра")
    return card


async def _close_source(source: object) -> None:
    close = getattr(source, "aclose", None)
    if close is not None:
        await close()


def _decode_legacy_card(value: str) -> str | None:
    try:
        raw = base64.b64decode(value, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if not text.startswith("📅 <b>Завтра</b> · "):
        return None
    _validate_schedule_card(text)
    return text


def _telegram_message_id(response: httpx.Response) -> int | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not response.is_success or not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    result = payload.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return message_id if isinstance(message_id, int) and message_id > 0 else None


def _telegram_delivery_error(response: httpx.Response) -> RuntimeError:
    description = "unexpected response"
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("description"), str):
            description = payload["description"][:200]
    except ValueError:
        pass
    return RuntimeError(
        f"Telegram delivery failed (HTTP {response.status_code}: {description})"
    )


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise ValueError(f"missing GitHub Actions secret: {name}")
    return value.get_secret_value()


def _required_int(value: int | None, name: str, *, negative: bool) -> int:
    if value is None or (negative and value >= 0) or (not negative and value <= 0):
        raise ValueError(f"invalid GitHub Actions secret: {name}")
    return value


def _required_text(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"missing GitHub Actions secret: {name}")
    return value.strip()


def _required_repository(value: str) -> str:
    repository = _required_text(value, "BRIDGE_GH_REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid BRIDGE_GH_REPOSITORY")
    return repository


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name=name)
        raise
