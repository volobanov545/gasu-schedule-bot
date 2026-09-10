"""Minimal OpenAI-compatible client with bounded retries and redacted failures."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

Role = Literal["system", "user", "assistant", "tool"]
Sleep = Callable[[float], Awaitable[None]]


class AIError(RuntimeError):
    """Base error safe to surface in metadata-only logs."""


class AIUnavailableError(AIError):
    """Temporary provider or network failure."""


class AIProtocolError(AIError):
    """Provider returned a structurally invalid response."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class AIResponse:
    text: str
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class AIProvider(Protocol):
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        user_id: int | None = None,
    ) -> AIResponse: ...


class OpenAICompatibleProvider:
    """Provider adapter for DeepSeek and other Chat Completions-compatible APIs."""

    _RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_response_bytes: int = 1_000_000,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("AI base URL cannot be empty")
        if not api_key:
            raise ValueError("AI API key cannot be empty")
        if not model.strip():
            raise ValueError("AI model cannot be empty")
        if max_retries < 0:
            raise ValueError("max retries cannot be negative")
        if max_response_bytes < 1:
            raise ValueError("max response bytes must be positive")

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._max_response_bytes = max_response_bytes
        self._sleep = sleep
        self._random = random_source or random.Random()  # noqa: S311
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"model={self._model!r}, api_key=<redacted>)"
        )

    async def __aenter__(self) -> OpenAICompatibleProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        user_id: int | None = None,
    ) -> AIResponse:
        if not messages:
            raise ValueError("at least one chat message is required")
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        del user_id  # used only by quota decorators
        for attempt in range(self._max_retries + 1):
            try:
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                ) as response:
                    retry_after = response.headers.get("Retry-After")
                    if response.status_code not in self._RETRYABLE_STATUSES:
                        if response.is_error:
                            raise AIError(
                                f"AI provider rejected request (HTTP {response.status_code})"
                            )
                        body = await _bounded_body(
                            response,
                            limit=self._max_response_bytes,
                        )
                        return self._parse_response(body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self._max_retries:
                    raise AIUnavailableError("AI provider network failure") from exc
                await self._sleep(self._backoff_seconds(attempt, None))
                continue

            if attempt >= self._max_retries:
                raise AIUnavailableError(
                    f"AI provider unavailable (HTTP {response.status_code})"
                )
            await self._sleep(self._backoff_seconds(attempt, retry_after))

        raise AssertionError("retry loop exhausted unexpectedly")

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        base = min(8.0, 0.5 * (2**attempt))
        return float(base + self._random.uniform(0.0, base * 0.2))

    @staticmethod
    def _parse_response(body: bytes) -> AIResponse:
        try:
            data = json.loads(body)
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIProtocolError("AI provider returned malformed JSON") from exc
        if not isinstance(text, str) or not text.strip():
            raise AIProtocolError("AI provider returned an empty answer")

        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        model = data.get("model") if isinstance(data, dict) else None
        return AIResponse(
            text=text,
            model=model if isinstance(model, str) else None,
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
        )


async def _bounded_body(response: httpx.Response, *, limit: int) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65_536):
        if len(content) + len(chunk) > limit:
            raise AIProtocolError("AI provider response exceeds the safe size limit")
        content.extend(chunk)
    return bytes(content)


def _optional_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )
