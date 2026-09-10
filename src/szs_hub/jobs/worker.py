"""Cancellation-safe workers over the durable queues."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol

from szs_hub.jobs.queue import JobClaim, JobQueue, OutboxClaim, OutboxQueue


class JobHandler(Protocol):
    """One registered background-job handler."""

    async def __call__(self, job: JobClaim, /) -> None: ...


class TelegramSender(Protocol):
    """Send one durable envelope and return Telegram's message identifier."""

    async def send(self, message: OutboxClaim) -> int | None: ...


class UnknownJobKindError(RuntimeError):
    """Safe error used when a deployed worker lacks a registered handler."""


class JobWorker:
    """Run registered handlers while leaving cancelled attempts lease-recoverable."""

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler],
        *,
        lease: timedelta = timedelta(minutes=2),
        poll_interval: float = 1.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if not handlers:
            raise ValueError("at least one job handler must be registered")
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease = lease
        self._poll_interval = poll_interval

    async def run_once(self) -> bool:
        """Process at most one due job and report whether one was claimed."""

        claim = await self._queue.claim_due(
            lease=self._lease,
            kinds=self._handlers.keys(),
        )
        if claim is None:
            return False
        handler = self._handlers.get(claim.kind)
        try:
            if handler is None:
                raise UnknownJobKindError
            await handler(claim)
        except Exception as error:
            await self._queue.fail(claim, error)
        else:
            await self._queue.succeed(claim)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until stopped; task cancellation always propagates immediately."""

        while not stop.is_set():
            worked = await self.run_once()
            if not worked:
                await _wait_or_stop(stop, self._poll_interval)


class OutboxWorker:
    """Send durable Telegram messages with the outbox retry policy."""

    def __init__(
        self,
        queue: OutboxQueue,
        sender: TelegramSender,
        *,
        lease: timedelta = timedelta(minutes=2),
        poll_interval: float = 1.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._queue = queue
        self._sender = sender
        self._lease = lease
        self._poll_interval = poll_interval

    async def run_once(self) -> bool:
        """Send at most one due envelope and report whether one was claimed."""

        claim = await self._queue.claim_due(lease=self._lease)
        if claim is None:
            return False
        try:
            telegram_message_id = await self._sender.send(claim)
        except Exception as error:
            await self._queue.fail(claim, error)
        else:
            await self._queue.sent(claim, telegram_message_id)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until stopped; cancellation leaves the current lease to expire."""

        while not stop.is_set():
            worked = await self.run_once()
            if not worked:
                await _wait_or_stop(stop, self._poll_interval)


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    try:
        async with asyncio.timeout(delay):
            await stop.wait()
    except TimeoutError:
        pass
