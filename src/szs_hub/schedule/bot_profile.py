"""Update the bot glossary through CI, without exposing its token or posting."""

import asyncio
import os

import httpx

from szs_hub.schedule.subjects import bot_description, bot_short_description


async def sync_bot_profile(token: str, client: httpx.AsyncClient) -> None:
    """Compare before writing; verify both default and Russian descriptions."""
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    base_url = f"https://api.telegram.org/bot{token}"
    for language in ("", "ru"):
        for suffix, field, desired in (
            ("Description", "description", bot_description()),
            ("ShortDescription", "short_description", bot_short_description()),
        ):
            params = {"language_code": language}
            current = await _call(client, base_url, f"getMy{suffix}", params)
            if not isinstance(current, dict) or current.get(field) != desired:
                await _call(client, base_url, f"setMy{suffix}", params | {field: desired})
                current = await _call(client, base_url, f"getMy{suffix}", params)
            if not isinstance(current, dict) or current.get(field) != desired:
                raise RuntimeError("Telegram bot glossary verification failed")


async def _call(
    client: httpx.AsyncClient, base_url: str, method: str, params: dict[str, str],
) -> object:
    try:
        response = await client.post(f"{base_url}/{method}", json=params)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        # httpx errors may contain the credential-bearing URL.
        raise RuntimeError("Telegram bot glossary request failed") from None
    if not response.is_success or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram bot glossary request rejected")
    return payload.get("result")


async def _main() -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        await sync_bot_profile(os.environ.get("TELEGRAM_BOT_TOKEN", ""), client)
    print("Bot glossary verified: full description and profile, default and ru.")


if __name__ == "__main__":
    asyncio.run(_main())
