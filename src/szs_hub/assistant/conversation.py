"""Durable, short follow-up context for private assistant chats."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from html import escape
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.ai.provider import ChatMessage, Role
from szs_hub.assistant.service import AssistantAnswer, AssistantSource
from szs_hub.storage.base import utc_now
from szs_hub.storage.models import AIConversation, AIMessage, User


class ConversationStore:
    """Keep enough context for natural follow-ups, not an unbounded hidden profile."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl: timedelta = timedelta(hours=24),
        max_previous_messages: int = 8,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("conversation TTL must be positive")
        if not 2 <= max_previous_messages <= 40:
            raise ValueError("previous-message limit must be between 2 and 40")
        self._sessions = session_factory
        self._ttl = ttl
        self._max_previous_messages = max_previous_messages
        self._clock = clock

    async def previous_turns(self, *, telegram_user_id: int) -> tuple[ChatMessage, ...]:
        now = self._clock()
        async with self._sessions() as session:
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            if user is None:
                return ()
            conversation = await self._active_conversation(session, user.id, now)
            if conversation is None:
                return ()
            rows = list(
                await session.scalars(
                    select(AIMessage)
                    .where(
                        AIMessage.conversation_id == conversation.id,
                        AIMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
                    .limit(self._max_previous_messages)
                )
            )
        return tuple(
            ChatMessage(cast(Role, row.role), row.content) for row in reversed(rows)
        )

    async def record_exchange(
        self,
        *,
        telegram_user_id: int,
        display_name: str | None,
        question: str,
        answer: AssistantAnswer,
        token_count: int | None = None,
    ) -> int:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            if user is None:
                user = User(
                    telegram_user_id=telegram_user_id,
                    display_name=display_name,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(user)
                await session.flush()
            else:
                user.display_name = display_name or user.display_name
                user.last_seen_at = max(user.last_seen_at, now)

            conversation = await self._active_conversation(session, user.id, now)
            if conversation is None:
                conversation = AIConversation(
                    user_id=user.id,
                    status="active",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + self._ttl,
                )
                session.add(conversation)
                await session.flush()
            conversation.updated_at = now
            conversation.expires_at = now + self._ttl
            session.add_all(
                [
                    AIMessage(
                        conversation_id=conversation.id,
                        role="user",
                        content=question.strip(),
                        created_at=now,
                    ),
                    AIMessage(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=_truncate_text(answer.text, 4_096),
                        provider="openai_compatible",
                        model=answer.model,
                        token_count=token_count,
                        sources=[
                            {
                                "label": source.label,
                                "chat_id": source.chat_id,
                                "message_id": source.message_id,
                                "telegram_link": source.telegram_link,
                            }
                            for source in answer.sources
                        ],
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            return conversation.id

    async def prune(self, *, older_than: datetime) -> int:
        """Delete expired conversation content under an explicit retention job."""

        if older_than.tzinfo is None:
            raise ValueError("retention boundary must be timezone-aware")
        async with self._sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    delete(AIConversation)
                    .where(AIConversation.updated_at < older_than)
                    .returning(AIConversation.id)
                )
            )
        return len(ids)

    @staticmethod
    async def _active_conversation(
        session: AsyncSession,
        user_id: int,
        now: datetime,
    ) -> AIConversation | None:
        result = await session.scalar(
            select(AIConversation)
            .where(
                AIConversation.user_id == user_id,
                AIConversation.status == "active",
                AIConversation.expires_at > now,
            )
            .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())
            .limit(1)
        )
        return result


def render_answer_with_sources(
    answer: AssistantAnswer,
    *,
    max_visible_chars: int = 4_096,
) -> str:
    """Render complete HTML entities while respecting Telegram's visible-text limit."""

    if max_visible_chars < 1:
        raise ValueError("answer rendering limit must be positive")

    linked = [
        source
        for source in answer.sources
        if source.telegram_link
        and source.telegram_link.startswith("https://t.me/")
        and len(source.telegram_link) <= 512
    ]
    if not linked:
        return escape(_truncate_text(answer.text, max_visible_chars))

    # Source labels are generated locally (S1, S2, …), but keep the renderer bounded
    # if old/imported rows contain unexpected values. Prefer the answer over a long list.
    selected: list[AssistantSource] = []
    source_visible = len("\n\nИсточники\n")
    source_budget = max(1, max_visible_chars // 3)
    for source in linked:
        label = source.label[:100]
        added = len(label) + (1 if selected else 0)
        if source_visible + added > source_budget:
            break
        selected.append(
            AssistantSource(
                label=label,
                chat_id=source.chat_id,
                message_id=source.message_id,
                telegram_link=source.telegram_link,
            )
        )
        source_visible += added

    if not selected or source_visible >= max_visible_chars:
        return escape(_truncate_text(answer.text, max_visible_chars))

    rendered_answer = escape(
        _truncate_text(answer.text, max_visible_chars - source_visible)
    )
    links = "\n".join(
        f'<a href="{escape(source.telegram_link or "", quote=True)}">'
        f"{escape(source.label)}</a>"
        for source in selected
    )
    return f"{rendered_answer}\n\n<b>Источники</b>\n{links}"


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return f"{value[: limit - 1]}…"
