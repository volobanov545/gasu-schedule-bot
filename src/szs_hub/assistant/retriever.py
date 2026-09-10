"""Russian-first FTS retrieval adapter for the private group assistant."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from szs_hub.assistant.service import RetrievedMessage
from szs_hub.search.text import tokenize
from szs_hub.storage.fts import SearchHit, SearchRepository

_STOPWORDS = {
    "а",
    "без",
    "был",
    "была",
    "были",
    "было",
    "в",
    "вы",
    "где",
    "для",
    "и",
    "из",
    "как",
    "когда",
    "кто",
    "ли",
    "мы",
    "на",
    "наш",
    "не",
    "о",
    "об",
    "обсуждали",
    "по",
    "про",
    "с",
    "со",
    "что",
    "это",
}


class SQLiteArchiveRetriever:
    """Combine strict multi-term matches with bounded per-term recall."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone: str = "Europe/Moscow",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._timezone = _load_timezone(timezone)
        self._now = now or (lambda: datetime.now(self._timezone))

    async def retrieve(self, *, question: str, limit: int) -> tuple[RetrievedMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("retrieval limit must be between 1 and 100")
        terms = tuple(
            dict.fromkeys(
                token for token in tokenize(question) if token not in _STOPWORDS and len(token) > 1
            )
        )[:8]
        if not terms:
            return ()

        queries = [" ".join(terms), *terms]
        accumulated: dict[int, SearchHit] = {}
        match_counts: dict[int, int] = defaultdict(int)
        async with self._sessions() as session:
            repository = SearchRepository(session)
            for index, query in enumerate(dict.fromkeys(queries)):
                hits = await repository.search(
                    query,
                    source_type="message",
                    limit=min(100, limit * 3),
                )
                for hit in hits:
                    accumulated.setdefault(hit.source_id, hit)
                    match_counts[hit.source_id] += 4 if index == 0 else 1

        scoped = [
            hit
            for hit in accumulated.values()
            if _within_requested_time_scope(hit, question=question, now=self._now())
        ]
        ordered = sorted(
            scoped,
            key=lambda hit: (
                -match_counts[hit.source_id],
                hit.score,
                -hit.source_id,
            ),
        )[:limit]
        converted: list[RetrievedMessage] = []
        for hit in ordered:
            item = _to_retrieved(
                hit,
                score=float(match_counts[hit.source_id]) - hit.score,
            )
            if item is not None:
                converted.append(item)
        return tuple(converted)


def _to_retrieved(hit: SearchHit, *, score: float) -> RetrievedMessage | None:
    metadata = hit.metadata or {}
    chat_id = metadata.get("chat_id")
    message_id = metadata.get("message_id")
    sent_at = metadata.get("sent_at")
    if (
        not isinstance(chat_id, int)
        or not isinstance(message_id, int)
        or not isinstance(sent_at, str)
    ):
        return None
    try:
        parsed = datetime.fromisoformat(sent_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    sender = metadata.get("sender_display_name")
    topic_id = metadata.get("topic_id")
    link = metadata.get("telegram_link")
    return RetrievedMessage(
        chat_id=chat_id,
        message_id=message_id,
        sent_at=parsed,
        sender_display_name=sender if isinstance(sender, str) else None,
        text=hit.content,
        score=score,
        topic_id=topic_id if isinstance(topic_id, int) else None,
        telegram_link=link if isinstance(link, str) else None,
    )


def _within_requested_time_scope(hit: SearchHit, *, question: str, now: datetime) -> bool:
    tokens = set(tokenize(question))
    if "вчера" not in tokens and "сегодня" not in tokens:
        return True
    metadata = hit.metadata or {}
    sent_at = metadata.get("sent_at")
    if not isinstance(sent_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(sent_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    local_day = parsed.astimezone(now.tzinfo).date()
    if "сегодня" in tokens:
        return local_day == now.date()
    return local_day == (now - timedelta(days=1)).date()


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows does not ship the IANA database. Moscow has had a fixed UTC+3
        # offset since 2014; keep local development usable while deployments still
        # install system tzdata. Other zones fail rather than silently mis-time data.
        if name == "Europe/Moscow":
            return fixed_timezone(timedelta(hours=3), name="Europe/Moscow")
        raise
