"""Stateless schedule publication for GitHub Actions."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pydantic import SecretStr

from szs_hub.config import Settings
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
            source_url=settings.spbgasu_base_url,
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
    source: ScheduleSource | None = None,
    bridge: ScheduleBridge | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> None:
    """Fetch on a Russian CI runner and dispatch a prepared card to GitHub."""

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
        text = await build_tomorrow_card(
            schedule_source,
            group_key=group_key,
            now=now,
            timezone=settings.timezone,
            source_url=settings.spbgasu_base_url,
        )
        await schedule_bridge.dispatch(
            repository=repository,
            token=token,
            text_b64=encode_schedule_card(text),
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
    destination: ScheduleDestination | None = None,
) -> int:
    """Validate a GitVerse-produced card and publish it from GitHub."""

    token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    chat_id = _required_int(settings.target_chat_id, "TARGET_CHAT_ID", negative=True)
    topic_id = _required_int(settings.schedule_topic_id, "SCHEDULE_TOPIC_ID", negative=False)
    expected_group = _required_text(settings.spbgasu_group_id, "SPBGASU_GROUP_ID")
    if group_key.strip().casefold() != expected_group.casefold():
        raise ValueError("bridge payload group does not match SPBGASU_GROUP_ID")
    text = decode_schedule_card(text_b64)

    own_bot = destination is None
    bot: Bot | None = None
    if destination is None:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        schedule_destination: ScheduleDestination = AiogramScheduleDestination(bot)
    else:
        schedule_destination = destination
    try:
        return await schedule_destination.send(chat_id=chat_id, topic_id=topic_id, text=text)
    finally:
        if own_bot and bot is not None:
            await bot.session.close()


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
    if not text.endswith('<a href="https://rasp.spbgasu.ru/">Источник: СПбГАСУ</a>'):
        raise ValueError("bridge payload has an unexpected source")
    if len(text) > 4096:
        raise ValueError("bridge schedule payload exceeds Telegram limit")


async def build_tomorrow_card(
    source: ScheduleSource,
    *,
    group_key: str,
    now: datetime,
    timezone: str,
    source_url: str,
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
    clean_url = source_url.rstrip("/") + "/"
    return f'{card}\n\n<a href="{clean_url}">Источник: СПбГАСУ</a>'


async def _close_source(source: object) -> None:
    close = getattr(source, "aclose", None)
    if close is not None:
        await close()


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
