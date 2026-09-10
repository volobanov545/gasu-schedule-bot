"""Conservative durable daily quotas for an optional AI provider."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.ai.provider import AIProvider, AIResponse, ChatMessage
from szs_hub.storage.models import SystemSetting


class AIQuotaExceeded(RuntimeError):
    """The configured safety limit was reached before contacting the provider."""


class DateSource(Protocol):
    def __call__(self) -> date: ...


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    day: date
    reserved_tokens: int


class DurableDailyQuota:
    """Reserve cost before a request; crashes therefore overcount, never undercount."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        request_limit: int,
        token_limit: int,
        tokens_per_request: int,
        per_user_request_limit: int | None = None,
        monthly_request_limit: int | None = None,
        monthly_token_limit: int | None = None,
        today: DateSource | None = None,
    ) -> None:
        if request_limit < 1 or token_limit < 1 or tokens_per_request < 1:
            raise ValueError("AI quota values must be positive")
        self._sessions = session_factory
        self._request_limit = request_limit
        self._token_limit = token_limit
        self._tokens_per_request = tokens_per_request
        self._per_user_request_limit = per_user_request_limit
        self._monthly_request_limit = monthly_request_limit
        self._monthly_token_limit = monthly_token_limit
        self._today = today or (lambda: datetime.now(UTC).date())

    async def reserve(self, *, user_id: int | None = None) -> QuotaReservation:
        day = self._today()
        key = _key(day)
        async with self._sessions() as session, session.begin():
            setting = await session.scalar(select(SystemSetting).where(SystemSetting.key == key))
            usage = _usage(setting.value if setting is not None else None)
            if usage["requests"] >= self._request_limit:
                raise AIQuotaExceeded("Дневной лимит запросов помощника исчерпан.")
            if usage["tokens"] + self._tokens_per_request > self._token_limit:
                raise AIQuotaExceeded("Дневной лимит помощника исчерпан.")
            month_setting = await session.scalar(
                select(SystemSetting).where(SystemSetting.key == _month_key(day))
            )
            month_usage = _usage(month_setting.value if month_setting is not None else None)
            if (
                self._monthly_request_limit is not None
                and month_usage["requests"] >= self._monthly_request_limit
            ):
                raise AIQuotaExceeded("Месячный лимит запросов помощника исчерпан.")
            if (
                self._monthly_token_limit is not None
                and month_usage["tokens"] + self._tokens_per_request
                > self._monthly_token_limit
            ):
                raise AIQuotaExceeded("Месячный лимит помощника исчерпан.")
            user_setting: SystemSetting | None = None
            if self._per_user_request_limit is not None:
                if user_id is None or user_id <= 0:
                    raise ValueError("a positive user_id is required for per-user AI quota")
                user_setting = await session.scalar(
                    select(SystemSetting).where(SystemSetting.key == _user_key(day, user_id))
                )
                user_usage = _usage(user_setting.value if user_setting is not None else None)
                if user_usage["requests"] >= self._per_user_request_limit:
                    raise AIQuotaExceeded("Ваш дневной лимит запросов помощника исчерпан.")
                _store_usage(session, user_setting, _user_key(day, user_id), user_usage, 0)
            _store_usage(session, setting, key, usage, self._tokens_per_request)
            _store_usage(
                session,
                month_setting,
                _month_key(day),
                month_usage,
                self._tokens_per_request,
            )
        return QuotaReservation(day=day, reserved_tokens=self._tokens_per_request)

    async def reconcile(self, reservation: QuotaReservation, *, actual_tokens: int | None) -> None:
        """Replace the conservative reservation with metered usage when available."""

        if actual_tokens is None or actual_tokens < 0:
            return
        async with self._sessions() as session, session.begin():
            for key in (_key(reservation.day), _month_key(reservation.day)):
                setting = await session.scalar(
                    select(SystemSetting).where(SystemSetting.key == key)
                )
                if setting is not None:
                    usage = _usage(setting.value)
                    usage["tokens"] = max(
                        0,
                        usage["tokens"] - reservation.reserved_tokens + actual_tokens,
                    )
                    setting.value = usage
                    setting.updated_at = datetime.now(UTC)


class QuotaLimitedProvider:
    """AIProvider decorator that enforces the durable quota before network I/O."""

    def __init__(self, provider: AIProvider, quota: DurableDailyQuota) -> None:
        self._provider = provider
        self._quota = quota

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        user_id: int | None = None,
    ) -> AIResponse:
        reservation = await self._quota.reserve(user_id=user_id)
        response = await self._provider.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        prompt_tokens = response.prompt_tokens
        completion_tokens = response.completion_tokens
        actual = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None
            and prompt_tokens >= 0
            and completion_tokens is not None
            and completion_tokens >= 0
            else None
        )
        await self._quota.reconcile(reservation, actual_tokens=actual)
        return response


def _key(day: date) -> str:
    return f"ai_usage:{day.isoformat()}"


def _month_key(day: date) -> str:
    return f"ai_usage_month:{day:%Y-%m}"


def _user_key(day: date, user_id: int) -> str:
    return f"ai_usage_user:{user_id}:{day.isoformat()}"


def _store_usage(
    session: AsyncSession,
    setting: SystemSetting | None,
    key: str,
    usage: dict[str, int],
    reserved_tokens: int,
) -> None:
    value = {
        "requests": usage["requests"] + 1,
        "tokens": usage["tokens"] + reserved_tokens,
    }
    if setting is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)


def _usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"requests": 0, "tokens": 0}
    requests = value.get("requests")
    tokens = value.get("tokens")
    return {
        "requests": requests if isinstance(requests, int) and requests >= 0 else 0,
        "tokens": tokens if isinstance(tokens, int) and tokens >= 0 else 0,
    }
