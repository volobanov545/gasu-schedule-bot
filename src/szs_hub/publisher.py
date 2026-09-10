"""Stateless schedule publication for GitHub Actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


async def _close_source(source: ScheduleSource) -> None:
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


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name=name)
        raise
