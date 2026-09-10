from __future__ import annotations

import httpx
import pytest

from szs_hub.ai.provider import (
    AIProtocolError,
    AIUnavailableError,
    ChatMessage,
    OpenAICompatibleProvider,
)


@pytest.mark.asyncio
async def test_chat_parses_text_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "Ответ"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example.test/v1",
        api_key="secret-key",
        model="deepseek-chat",
        client=client,
    )

    result = await provider.chat([ChatMessage("user", "Привет")])

    assert result.text == "Ответ"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_after_is_bounded_and_retries() -> None:
    attempts = 0
    delays: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "999"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example.test/v1",
        api_key="secret-key",
        model="deepseek-chat",
        max_retries=1,
        client=client,
        sleep=fake_sleep,
    )

    assert (await provider.chat([ChatMessage("user", "hello")])).text == "ok"
    assert attempts == 2
    assert delays == [30.0]
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_exhaustion_is_a_safe_unavailable_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def no_sleep(_delay: float) -> None:
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example.test/v1",
        api_key="do-not-leak",
        model="deepseek-chat",
        max_retries=1,
        client=client,
        sleep=no_sleep,
    )

    with pytest.raises(AIUnavailableError) as error:
        await provider.chat([ChatMessage("user", "hello")])

    assert "do-not-leak" not in str(error.value)
    assert "do-not-leak" not in repr(provider)
    await client.aclose()


@pytest.mark.asyncio
async def test_malformed_response_is_not_treated_as_an_answer() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example.test/v1",
        api_key="secret-key",
        model="deepseek-chat",
        client=client,
    )

    with pytest.raises(AIProtocolError, match="malformed"):
        await provider.chat([ChatMessage("user", "hello")])
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_response_is_rejected() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://ai.example.test/v1",
        api_key="secret-key",
        model="deepseek-chat",
        max_response_bytes=100,
        client=client,
    )

    with pytest.raises(AIProtocolError, match="size limit"):
        await provider.chat([ChatMessage("user", "hello")])
    await client.aclose()
