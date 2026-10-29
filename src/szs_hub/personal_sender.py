"""Outbound personal-account transport; the session never enters schedule state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_CHECKOUT_BRIDGE = Path.cwd() / "tools/telegram-personal/bridge.mjs"
_SOURCE_BRIDGE = Path(__file__).resolve().parents[2] / "tools/telegram-personal/bridge.mjs"
# CI installs a wheel into site-packages, but keeps the Node bridge in checkout.
_BRIDGE_SCRIPT = _CHECKOUT_BRIDGE if _CHECKOUT_BRIDGE.is_file() else _SOURCE_BRIDGE


class PersonalScheduleDestination:
    """A bounded JSONL bridge to mtcute, with no Telegram update listener."""

    def __init__(self, credentials: str, *, chat_id: int, topic_id: int) -> None:
        try:
            data = json.loads(credentials)
            if not isinstance(data, dict):
                raise ValueError
            user_id = data["user_id"]
            if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                raise ValueError
            if data["chat_id"] != chat_id or data["topic_id"] != topic_id:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ValueError("personal Telegram credentials do not match the configured target") \
                from None
        self.binding = f"user:{user_id}"
        self.card_key = f"{self.binding}:{chat_id}:{topic_id}"
        self._credentials = credentials
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._counter = 0

    async def prepare(self) -> None:
        """Verify identity, group permissions and topic before publishing anything."""

        actual = await self._rpc("preflight")
        if actual != self.binding:
            await self.aclose()
            raise RuntimeError("personal Telegram identity mismatch")

    async def send(self, *, chat_id: int, topic_id: int, text: str) -> int:
        return await self._message_id("send", chat_id=chat_id, topic_id=topic_id, text=text)

    async def send_rich(
        self, *, chat_id: int, topic_id: int, rich_html: str, fallback_html: str, silent: bool,
    ) -> int:
        # Never silently replace the requested table with classic/plain text.
        return await self._message_id(
            "send_rich", chat_id=chat_id, topic_id=topic_id, rich_html=rich_html, silent=silent,
        )

    async def edit_rich(
        self, *, chat_id: int, message_id: int, rich_html: str, fallback_html: str,
    ) -> int:
        return await self._message_id(
            "edit_rich", chat_id=chat_id, message_id=message_id, rich_html=rich_html,
        )

    async def pin_message(self, *, chat_id: int, message_id: int) -> None:
        await self._rpc("pin_message", chat_id=chat_id, message_id=message_id)

    async def _message_id(self, method: str, **params: object) -> int:
        value = await self._rpc(method, **params)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RuntimeError("personal Telegram returned an invalid message ID")
        return value

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is None:
            executable = shutil.which("node")
            if executable is None or not _BRIDGE_SCRIPT.is_file():
                raise RuntimeError("personal Telegram requires Node.js and the repository checkout")
            environment = dict(os.environ)
            environment["TELEGRAM_USER_CREDENTIALS"] = self._credentials
            self._process = await asyncio.create_subprocess_exec(
                executable, str(_BRIDGE_SCRIPT), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=environment, limit=1_048_576,
            )
        return self._process

    async def _rpc(self, method: str, **params: object) -> object:
        async with self._lock:
            process = await self._start()
            assert process.stdin is not None and process.stdout is not None
            self._counter += 1
            request = {"id": self._counter, "method": method, "params": params}
            try:
                process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), timeout=45)
                response: Any = json.loads(line)
            except (TimeoutError, OSError, ValueError):
                await self._stop()
                raise RuntimeError("personal Telegram transport unavailable; not retried") from None
            if not isinstance(response, dict) or response.get("id") != self._counter:
                await self._stop()
                raise RuntimeError("personal Telegram transport protocol error")
            if response.get("ok") is not True:
                code = response.get("error")
                if code == "MESSAGE_NOT_FOUND":
                    raise RuntimeError("message to edit not found")
                # Only machine codes are allowed into public Actions logs.
                safe_code = (
                    code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                    else "DELIVERY_FAILED"
                )
                raise RuntimeError(f"personal Telegram: {safe_code}")
            return response.get("result")

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def aclose(self) -> None:
        if self._process is None:
            return
        try:
            await self._rpc("close")
            assert self._process is not None
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except (RuntimeError, TimeoutError, OSError):
            pass
        finally:
            await self._stop()
