from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from szs_hub.ai.provider import AIResponse, ChatMessage
from szs_hub.ai.quota import AIQuotaExceeded, DurableDailyQuota, QuotaLimitedProvider
from szs_hub.storage.database import (
    create_database_engine,
    create_schema,
    create_session_factory,
)


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AIResponse:
        self.calls += 1
        return AIResponse("ok", prompt_tokens=12, completion_tokens=8)


class PartialUsageProvider:
    async def chat(
        self,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AIResponse:
        return AIResponse("ok", prompt_tokens=None, completion_tokens=1)


@pytest.mark.asyncio
async def test_quota_is_durable_and_blocks_before_provider_call(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'quota.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    provider = FakeProvider()
    try:
        quota = DurableDailyQuota(
            session_factory=sessions,
            request_limit=1,
            token_limit=1_000,
            tokens_per_request=100,
            today=lambda: date(2026, 9, 1),
        )
        limited = QuotaLimitedProvider(provider, quota)
        result = await limited.chat([ChatMessage("user", "test")])
        assert result.text == "ok"
        with pytest.raises(AIQuotaExceeded):
            await limited.chat([ChatMessage("user", "again")])
        assert provider.calls == 1

        second_instance = DurableDailyQuota(
            session_factory=sessions,
            request_limit=1,
            token_limit=1_000,
            tokens_per_request=100,
            today=lambda: date(2026, 9, 1),
        )
        with pytest.raises(AIQuotaExceeded):
            await second_instance.reserve()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_provider_usage_keeps_conservative_reservation(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'reserve.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        quota = DurableDailyQuota(
            session_factory=sessions,
            request_limit=10,
            token_limit=100,
            tokens_per_request=100,
            today=lambda: date(2026, 9, 1),
        )
        reservation = await quota.reserve()
        await quota.reconcile(reservation, actual_tokens=None)
        with pytest.raises(AIQuotaExceeded):
            await quota.reserve()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_provider_usage_keeps_conservative_reservation(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'partial.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    try:
        quota = DurableDailyQuota(
            session_factory=sessions,
            request_limit=10,
            token_limit=100,
            tokens_per_request=100,
            today=lambda: date(2026, 9, 1),
        )
        limited = QuotaLimitedProvider(PartialUsageProvider(), quota)
        await limited.chat([ChatMessage("user", "first")])
        with pytest.raises(AIQuotaExceeded):
            await limited.chat([ChatMessage("user", "second")])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_per_user_and_monthly_limits_are_independent(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'scoped.db').as_posix()}")
    sessions = create_session_factory(engine)
    await create_schema(engine)
    current_day = [date(2026, 9, 1)]
    provider = FakeProvider()
    try:
        quota = DurableDailyQuota(
            session_factory=sessions,
            request_limit=10,
            token_limit=10_000,
            tokens_per_request=100,
            per_user_request_limit=1,
            monthly_request_limit=2,
            monthly_token_limit=10_000,
            today=lambda: current_day[0],
        )
        limited = QuotaLimitedProvider(provider, quota)
        await limited.chat([ChatMessage("user", "one")], user_id=1)
        with pytest.raises(AIQuotaExceeded, match="Ваш дневной"):
            await limited.chat([ChatMessage("user", "again")], user_id=1)
        await limited.chat([ChatMessage("user", "two")], user_id=2)
        current_day[0] = date(2026, 9, 2)
        with pytest.raises(AIQuotaExceeded, match="Месячный"):
            await limited.chat([ChatMessage("user", "three")], user_id=3)
        current_day[0] = date(2026, 10, 1)
        await limited.chat([ChatMessage("user", "new month")], user_id=3)
        assert provider.calls == 3
    finally:
        await engine.dispose()
