"""Telegram handlers that keep group automation quiet and private access fail-closed."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, MessageReactionUpdated
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.ai.provider import AIError
from szs_hub.ai.quota import AIQuotaExceeded
from szs_hub.archive.live import LiveArchive
from szs_hub.assistant.conversation import ConversationStore, render_answer_with_sources
from szs_hub.assistant.service import AssistantAccessDenied, AssistantService
from szs_hub.attendance.service import AttendanceService
from szs_hub.config import Settings
from szs_hub.domain.attendance import AttendanceAction
from szs_hub.jobs.queue import JobQueue
from szs_hub.materials.automation import admit_material_message
from szs_hub.materials.processing import admit_material_extraction
from szs_hub.schedule.coordinator import ScheduleAccess, ScheduleCoordinator, monday_for
from szs_hub.schedule.render import render_day_card
from szs_hub.schedule.service import ScheduleFreshnessState
from szs_hub.schedule.ui import (
    ScheduleView,
    parse_schedule_callback,
    render_week_card,
    schedule_keyboard,
)
from szs_hub.telegram.membership_store import TelegramMembershipStore
from szs_hub.telegram.updates import attendance_event_from_update


@dataclass(frozen=True, slots=True)
class HandlerServices:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    live_archive: LiveArchive
    memberships: TelegramMembershipStore
    attendance: type[AttendanceService]
    jobs: JobQueue
    schedule: ScheduleCoordinator
    assistant: AssistantService | None
    conversations: ConversationStore | None


def build_router(services: HandlerServices) -> Router:
    router = Router(name="szs_hub")
    settings = services.settings
    local_timezone = _load_timezone(settings.timezone)

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        await message.answer(
            "Привет! Здесь можно посмотреть расписание и спросить о памяти группы.\n\n"
            "Отдельной регистрации нет: для доступа к истории я проверяю участие в группе."
        )

    @router.message(Command("schedule"))
    async def schedule_command(message: Message) -> None:
        today = datetime.now(local_timezone).date()
        access = await services.schedule.get_week(monday_for(today))
        text = render_schedule_view(access, view=ScheduleView.TOMORROW, today=today)
        await message.answer(
            text,
            reply_markup=schedule_keyboard(active=ScheduleView.TOMORROW),
        )

    @router.callback_query(F.data.startswith("schedule:v1:"))
    async def schedule_callback(callback: CallbackQuery) -> None:
        view = parse_schedule_callback(callback.data)
        if view is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await callback.answer("Открываю…")
        today = datetime.now(local_timezone).date()
        requested_day = today if view is ScheduleView.TODAY else today + timedelta(days=1)
        week = monday_for(today if view is ScheduleView.WEEK else requested_day)
        access = await services.schedule.get_week(week)
        text = render_schedule_view(access, view=view, today=today)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                text,
                reply_markup=schedule_keyboard(active=view),
            )

    @router.message_reaction()
    async def reaction(update: MessageReactionUpdated) -> None:
        if update.chat.id != settings.target_chat_id:
            return
        event = attendance_event_from_update(
            update,
            expected_reaction=settings.attendance_reaction,
        )
        if event is None or update.user is None:
            return
        async with services.sessions() as session, session.begin():
            result = await services.attendance(session).apply_reaction(
                chat_id=update.chat.id,
                message_id=update.message_id,
                event=event,
                display_name=update.user.full_name,
                username=update.user.username,
            )
        if result.action in {AttendanceAction.MARK, AttendanceAction.UNMARK}:
            assert result.session_id is not None
            bucket = int(event.occurred_at.timestamp()) // 5
            await services.jobs.enqueue(
                kind="attendance.summary",
                idempotency_key=f"attendance-summary:{result.session_id}:{bucket}",
                payload={"session_id": result.session_id},
                max_attempts=6,
            )

    @router.chat_member()
    async def chat_member(update: ChatMemberUpdated) -> None:
        async with services.sessions() as session, session.begin():
            await services.memberships.apply_chat_member_update(session, update)

    @router.edited_message()
    async def edited_message(message: Message) -> None:
        async with services.sessions() as session, session.begin():
            archived = await services.live_archive.ingest(session, message)
        if archived.stored_message_id is not None:
            if settings.material_extraction_enabled:
                await admit_material_extraction(services.jobs, message)
            await admit_material_message(services.jobs, message)

    @router.message(F.chat.id == settings.target_chat_id)
    async def group_message(message: Message) -> None:
        async with services.sessions() as session, session.begin():
            await services.memberships.observe_message_sender(session, message)
            archived = await services.live_archive.ingest(session, message)
        if archived.stored_message_id is not None:
            if settings.material_extraction_enabled:
                await admit_material_extraction(services.jobs, message)
            await admit_material_message(services.jobs, message)

    @router.message(F.chat.type == "private", F.text)
    async def private_assistant(message: Message) -> None:
        if message.from_user is None or message.text is None:
            return
        if services.assistant is None or services.conversations is None:
            await message.answer(
                "Помощник пока выключен. Расписание и отметки продолжают работать."
            )
            return
        try:
            previous = await services.conversations.previous_turns(
                telegram_user_id=message.from_user.id
            )
            answer = await services.assistant.answer(
                user_id=message.from_user.id,
                question=message.text,
                previous_turns=previous,
            )
            await services.conversations.record_exchange(
                telegram_user_id=message.from_user.id,
                display_name=message.from_user.full_name,
                question=message.text,
                answer=answer,
            )
        except AssistantAccessDenied:
            await message.answer("Доступ к памяти есть только у текущих участников группы.")
            return
        except AIQuotaExceeded as exc:
            await message.answer(escape(str(exc)))
            return
        except AIError:
            await message.answer(
                "Помощник сейчас недоступен. Расписание и отметки продолжают работать."
            )
            return
        await message.answer(render_answer_with_sources(answer, max_visible_chars=4_096))

    return router


def render_schedule_view(
    access: ScheduleAccess,
    *,
    view: ScheduleView,
    today: date,
) -> str:
    if access.schedule is None:
        return "📅 <b>Расписание пока недоступно</b>\n\nНет подтверждённой сохранённой версии."
    lessons = access.schedule.snapshot.lessons
    if view is ScheduleView.WEEK:
        text = render_week_card(monday=monday_for(today), lessons=lessons)
    else:
        day = today if view is ScheduleView.TODAY else today + timedelta(days=1)
        label = "Сегодня" if view is ScheduleView.TODAY else "Завтра"
        text = render_day_card(
            day,
            (lesson for lesson in lessons if lesson.day == day),
            relative_label=label,
        )

    freshness = access.schedule.freshness
    if freshness.state is not ScheduleFreshnessState.FRESH:
        confirmed = (
            freshness.confirmed_at.astimezone().strftime("%d.%m · %H:%M")
            if freshness.confirmed_at
            else "неизвестно"
        )
        text += (
            f"\n\n<i>Последнее подтверждение: {escape(confirmed)}. "
            "Источник сейчас не обновлён.</i>"
        )
    return text


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name=name)
        raise
