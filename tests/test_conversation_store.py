from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest

from szs_hub.assistant.conversation import ConversationStore, render_answer_with_sources
from szs_hub.assistant.service import AssistantAnswer, AssistantSource
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)

_HTML_TAG = re.compile(r"<[^>]+>")


@pytest.mark.asyncio
async def test_follow_up_context_is_durable_and_expires(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'chat.db').as_posix()}")
    sessions = create_session_factory(engine)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await create_schema(engine)
    try:
        store = ConversationStore(sessions, clock=lambda: now)
        answer = AssistantAnswer(
            "Поездку предложила Анна [S1].",
            (AssistantSource("S1", -1001, 42, "https://t.me/c/1/42"),),
            "test-model",
        )
        await store.record_exchange(
            telegram_user_id=10,
            display_name="Пётр",
            question="Кто предложил поездку?",
            answer=answer,
            token_count=20,
        )
        second = ConversationStore(sessions, clock=lambda: now + timedelta(hours=1))
        turns = await second.previous_turns(telegram_user_id=10)
        assert [turn.role for turn in turns] == ["user", "assistant"]
        assert turns[-1].content == answer.text
        assert "https://t.me/c/1/42" in render_answer_with_sources(answer)

        expired = ConversationStore(sessions, clock=lambda: now + timedelta(days=2))
        assert await expired.previous_turns(telegram_user_id=10) == ()
    finally:
        await engine.dispose()


def test_answer_rendering_never_slices_an_html_entity_or_tag() -> None:
    answer = AssistantAnswer(
        "<" * 5_000,
        (AssistantSource("S1", -1001, 42, "https://t.me/c/1/42"),),
        "test-model",
    )

    rendered = render_answer_with_sources(answer, max_visible_chars=4_096)
    visible = unescape(_HTML_TAG.sub("", rendered))

    assert len(visible) <= 4_096
    assert rendered.count("<a ") == rendered.count("</a>") == 1
    assert rendered.count("<b>") == rendered.count("</b>") == 1
    assert "&l…" not in rendered


@pytest.mark.asyncio
async def test_persisted_assistant_answer_is_bounded(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'bounded.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        store = ConversationStore(
            sessions,
            clock=lambda: datetime(2026, 9, 1, 12, tzinfo=UTC),
        )
        await store.record_exchange(
            telegram_user_id=11,
            display_name="Пётр",
            question="Длинный ответ?",
            answer=AssistantAnswer("x" * 5_000, (), "test-model"),
        )
        turns = await store.previous_turns(telegram_user_id=11)
        assert len(turns[-1].content) == 4_096
        assert turns[-1].content.endswith("…")
    finally:
        await engine.dispose()
