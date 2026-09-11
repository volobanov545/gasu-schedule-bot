"""Public SPbGASU group-schedule client and weekly-template parser."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time as monotonic_time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from szs_hub.domain.schedule import Lesson, normalize_text

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

_WEEKDAY_INDEX = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
}
_NUMBER_WEEK = re.compile(r"window\.NUMBER_WEEK\s*=\s*['\"]?(\d+)['\"]?\s*;")
_GROUPS = re.compile(r"window\.GROUPS\s*=\s*(\[[\s\S]*?\])\s*;")
_LESSON_TYPE = re.compile(r"\s*\((л\.|пр\.|лаб\.|сем\.)\)\s*$", re.IGNORECASE)
_SCRIPT_SRC = re.compile(r"<script[^>]+src=['\"]([^'\"]+)['\"]", re.IGNORECASE)
_CONTRACT_SNIPPET = re.compile(
    r".{0,260}(?:ajax\.php|SERACH|SEARCH|FILTER|quick_search|"
    r"\$\.ajax|fetch\s*\(|XMLHttpRequest|url\s*:).{0,700}",
    re.IGNORECASE,
)

DEFAULT_BELL_SCHEDULE: Mapping[int, tuple[time, time]] = {
    1: (time(9, 0), time(10, 30)),
    2: (time(10, 45), time(12, 15)),
    3: (time(12, 30), time(14, 0)),
    4: (time(15, 0), time(16, 30)),
    5: (time(16, 45), time(18, 15)),
    6: (time(18, 30), time(20, 0)),
    7: (time(20, 15), time(21, 45)),
}


class SpbGasuError(RuntimeError):
    """Safe base error for the public timetable source."""


class SpbGasuUnavailableError(SpbGasuError):
    pass


class SpbGasuProtocolError(SpbGasuError):
    pass


class SpbGasuGroupNotFoundError(SpbGasuError):
    pass


class WeekParity(StrEnum):
    NUMERATOR = "числ."
    DENOMINATOR = "знам."


@dataclass(frozen=True, slots=True)
class WeeklyLesson:
    weekday: int
    slot: int
    parity: WeekParity
    subject: str
    group: str | None
    auditorium: str | None
    professor: str | None
    source_date: str | None
    occurrence: int = 0


@dataclass(frozen=True, slots=True)
class WeeklySchedule:
    group_key: str
    lessons: tuple[WeeklyLesson, ...]


@dataclass(frozen=True, slots=True)
class SchedulePageBootstrap:
    current_week_number: int
    groups: tuple[str, ...]


class SpbGasuClient:
    """Rate-bounded client that can only make the known valid group request."""

    _RETRYABLE = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        base_url: str = "https://rasp.spbgasu.ru",
        cache_seconds: float = 600,
        timeout_seconds: float = 20,
        max_response_bytes: int = 2_000_000,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic_time.monotonic,
    ) -> None:
        if cache_seconds < 300:
            raise ValueError("SPbGASU cache must be at least five minutes")
        self._base_url = base_url.rstrip("/")
        self._cache_seconds = cache_seconds
        self._max_response_bytes = max_response_bytes
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, WeeklySchedule]] = {}
        self._bootstrap_cache: tuple[float, SchedulePageBootstrap] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_group(self, group_key: str) -> WeeklySchedule:
        cleaned = group_key.strip()
        if not cleaned:
            raise ValueError("SPbGASU group key cannot be empty")
        cached = self._cache.get(cleaned)
        now = self._clock()
        if cached and now - cached[0] < self._cache_seconds:
            return cached[1]

        async with self._lock:
            cached = self._cache.get(cleaned)
            now = self._clock()
            if cached and now - cached[0] < self._cache_seconds:
                return cached[1]
            payload = await self._request_group(cleaned)
            schedule = parse_weekly_schedule(payload, group_key=cleaned)
            if not schedule.lessons:
                raise SpbGasuProtocolError(
                    "SPbGASU weekly template parsed as empty; key schema="
                    f"{_payload_key_schema(payload)}; public dates="
                    f"{_payload_public_dates(payload)}; containers="
                    f"{_payload_container_schema(payload)}"
                )
            self._cache[cleaned] = (self._clock(), schedule)
            return schedule

    async def probe_group_schema(self, group_key: str) -> str:
        """Fetch once and expose structure/public dates without lesson or person values."""

        cleaned = group_key.strip()
        if not cleaned:
            raise ValueError("SPbGASU group key cannot be empty")
        html = await self._request_bootstrap_page()
        bootstrap = parse_schedule_page_bootstrap(html)
        payload = await self._request_group(cleaned)
        normalized_groups = {group.casefold(): group for group in bootstrap.groups}
        candidates = difflib.get_close_matches(
            cleaned.casefold(),
            normalized_groups,
            n=12,
            cutoff=0.35,
        )
        return (
            f"group exact={cleaned.casefold() in normalized_groups}; "
            "public group candidates="
            f"{json.dumps([normalized_groups[item] for item in candidates], ensure_ascii=False)}; "
            f"key schema={_payload_key_schema(payload)}; "
            f"public dates={_payload_public_dates(payload)}; "
            f"containers={_payload_container_schema(payload)}; "
            f"public contract={await self._probe_public_contract(html)}"
        )

    async def _probe_public_contract(self, html: str) -> str:
        """Read public first-party scripts and return short request-contract snippets."""

        base_host = urlsplit(self._base_url).netloc
        documents: list[tuple[str, str]] = [("page", html)]
        for raw_src in _SCRIPT_SRC.findall(html)[:20]:
            url = urljoin(f"{self._base_url}/", raw_src)
            if urlsplit(url).netloc != base_host:
                continue
            try:
                response = await self._client.get(
                    url,
                    headers={"Accept": "text/javascript", "User-Agent": "SZS-Hub/0.1"},
                )
            except (httpx.TimeoutException, httpx.NetworkError):
                continue
            if response.is_success and len(response.content) <= self._max_response_bytes:
                documents.append((urlsplit(url).path, response.text))

        snippets = [f"scripts={[source for source, _ in documents[1:]]}"]
        contract_sources = {
            "/local/templates/rasp/script.js",
            "/local/templates/rasp/js/script.js",
            "/local/templates/rasp/asset/js/main.js",
        }
        for source, document in documents:
            if source not in contract_sources:
                continue
            for match in _CONTRACT_SNIPPET.finditer(document):
                compact = " ".join(match.group(0).split())
                snippets.append(f"{source}: {compact[:500]}")
                if len(snippets) >= 20:
                    return json.dumps(snippets, ensure_ascii=False)[:8_000]
        return json.dumps(snippets, ensure_ascii=False)[:8_000]

    async def fetch_bootstrap(self) -> SchedulePageBootstrap:
        """Read the page's academic week reference without a student session."""

        now = self._clock()
        if self._bootstrap_cache and now - self._bootstrap_cache[0] < self._cache_seconds:
            return self._bootstrap_cache[1]
        async with self._lock:
            now = self._clock()
            if self._bootstrap_cache and now - self._bootstrap_cache[0] < self._cache_seconds:
                return self._bootstrap_cache[1]
            html = await self._request_bootstrap_page()
            bootstrap = parse_schedule_page_bootstrap(html)
            self._bootstrap_cache = (self._clock(), bootstrap)
            return bootstrap

    async def _request_group(self, group_key: str) -> object:
        params = {
            "SEARCH": group_key,
            "SERACH": group_key,
            "FILTER": "GROUPS",
            "GROUP": "",
            "SELECT": "*",
        }
        for attempt in range(self._max_retries + 1):
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/local/components/gasu/raspisanie.csv/ajax.php",
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "SZS-Hub/0.1"},
                ) as response:
                    retry_delay = _retry_delay(response, attempt)
                    if response.status_code not in self._RETRYABLE:
                        if response.is_error:
                            raise SpbGasuProtocolError(
                                "SPbGASU rejected schedule request "
                                f"(HTTP {response.status_code})"
                            )
                        body = await _bounded_body(
                            response,
                            limit=self._max_response_bytes,
                            error="SPbGASU response exceeds the safe size limit",
                        )
                        try:
                            payload: object = json.loads(body)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise SpbGasuProtocolError(
                                "SPbGASU returned non-JSON schedule data"
                            ) from exc
                        if payload == []:
                            raise SpbGasuGroupNotFoundError("SPbGASU group was not found")
                        return payload
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self._max_retries:
                    raise SpbGasuUnavailableError("SPbGASU schedule request failed") from exc
                await self._sleep(0.5 * (2**attempt))
                continue

            if attempt >= self._max_retries:
                raise SpbGasuUnavailableError(
                    f"SPbGASU schedule unavailable (HTTP {response.status_code})"
                )
            await self._sleep(retry_delay)

        raise AssertionError("SPbGASU retry loop exhausted unexpectedly")

    async def _request_bootstrap_page(self) -> str:
        for attempt in range(self._max_retries + 1):
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/",
                    headers={"Accept": "text/html", "User-Agent": "SZS-Hub/0.1"},
                ) as response:
                    retry_delay = _retry_delay(response, attempt)
                    if response.status_code not in self._RETRYABLE:
                        if response.is_error:
                            raise SpbGasuProtocolError(
                                "SPbGASU rejected bootstrap request "
                                f"(HTTP {response.status_code})"
                            )
                        body = await _bounded_body(
                            response,
                            limit=self._max_response_bytes,
                            error="SPbGASU bootstrap exceeds the safe size limit",
                        )
                        try:
                            return body.decode(response.encoding or "utf-8")
                        except UnicodeDecodeError as exc:
                            raise SpbGasuProtocolError(
                                "SPbGASU bootstrap has invalid text encoding"
                            ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self._max_retries:
                    raise SpbGasuUnavailableError("SPbGASU bootstrap request failed") from exc
                await self._sleep(0.5 * (2**attempt))
                continue
            if attempt >= self._max_retries:
                raise SpbGasuUnavailableError(
                    f"SPbGASU bootstrap unavailable (HTTP {response.status_code})"
                )
            await self._sleep(retry_delay)
        raise AssertionError("SPbGASU bootstrap retry loop exhausted unexpectedly")


async def _bounded_body(response: httpx.Response, *, limit: int, error: str) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65_536):
        if len(content) + len(chunk) > limit:
            raise SpbGasuProtocolError(error)
        content.extend(chunk)
    return bytes(content)


def parse_schedule_page_bootstrap(html: str) -> SchedulePageBootstrap:
    week_match = _NUMBER_WEEK.search(html)
    group_match = _GROUPS.search(html)
    if not week_match or not group_match:
        raise SpbGasuProtocolError("SPbGASU page bootstrap variables are missing")
    try:
        raw_groups = json.loads(group_match.group(1))
    except json.JSONDecodeError as exc:
        raise SpbGasuProtocolError("SPbGASU group bootstrap is malformed") from exc
    if not isinstance(raw_groups, list):
        raise SpbGasuProtocolError("SPbGASU group bootstrap is not an array")
    groups = tuple(_bootstrap_group_name(item) for item in raw_groups)
    return SchedulePageBootstrap(
        int(week_match.group(1)),
        tuple(group for group in groups if group),
    )


def parse_weekly_schedule(payload: object, *, group_key: str) -> WeeklySchedule:
    root = _mapping(payload, "schedule response")
    wrapped = root.get("R")
    schedule_root = _mapping(wrapped, "schedule response R") if wrapped is not None else root
    lessons: list[WeeklyLesson] = []

    for raw_day, raw_slots in schedule_root.items():
        weekday = _weekday(raw_day)
        slots = _mapping(raw_slots, f"day {raw_day}")
        for raw_slot, raw_parities in slots.items():
            slot = _positive_int(raw_slot, "lesson slot")
            parities = _mapping(raw_parities, f"slot {raw_slot}")
            for parity in WeekParity:
                raw_entries = parities.get(parity.value)
                if raw_entries in (None, "", []):
                    continue
                entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
                for occurrence, entry in enumerate(entries):
                    values = _mapping(entry, "lesson entry")
                    subject = _string(values.get("LESSON"))
                    if not subject:
                        continue
                    lessons.append(
                        WeeklyLesson(
                            weekday=weekday,
                            slot=slot,
                            parity=parity,
                            subject=subject,
                            group=_string(values.get("GROUP")),
                            auditorium=_string(values.get("AUDITORIUM")),
                            professor=_string(values.get("PROFESSOR")),
                            source_date=_string(values.get("DATE")),
                            occurrence=occurrence,
                        )
                    )
    return WeeklySchedule(group_key, tuple(lessons))


def materialize_week(
    schedule: WeeklySchedule,
    *,
    monday: date,
    parity: WeekParity,
    bell_schedule: Mapping[int, tuple[time, time]] = DEFAULT_BELL_SCHEDULE,
) -> tuple[Lesson, ...]:
    if monday.weekday() != 0:
        raise ValueError("schedule week anchor must be a Monday")
    lessons: list[Lesson] = []
    for raw in schedule.lessons:
        if raw.parity is not parity:
            continue
        times = bell_schedule.get(raw.slot)
        if not times:
            raise SpbGasuProtocolError(f"unknown SPbGASU lesson slot: {raw.slot}")
        subject, lesson_type = _split_lesson_type(raw.subject)
        room, building = _split_auditorium(raw.auditorium)
        source = (
            f"{schedule.group_key}|{raw.weekday}|{raw.slot}|{raw.parity}|"
            f"{normalize_text(raw.group)}|{raw.occurrence}"
        )
        lessons.append(
            Lesson(
                day=monday + timedelta(days=raw.weekday),
                starts_at=times[0],
                ends_at=times[1],
                subject=subject,
                lesson_type=lesson_type,
                teacher=raw.professor,
                room=room,
                building=building,
                subgroup=raw.group,
                source_id=f"spbgasu:{sha256(source.encode()).hexdigest()[:20]}",
            )
        )
    return tuple(sorted(lessons, key=lambda lesson: (lesson.day, lesson.starts_at, lesson.subject)))


def parity_for_week(
    *,
    current_week_number: int,
    current_monday: date,
    target_monday: date,
) -> WeekParity:
    if current_monday.weekday() != 0 or target_monday.weekday() != 0:
        raise ValueError("week anchors must be Mondays")
    delta_days = (target_monday - current_monday).days
    if delta_days % 7:
        raise ValueError("target week must be an exact week offset")
    target_number = current_week_number + delta_days // 7
    return WeekParity.NUMERATOR if target_number % 2 == 1 else WeekParity.DENOMINATOR


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(min(30.0, max(0.0, float(value))))
        except ValueError:
            pass
    return float(0.5 * (2**attempt))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpbGasuProtocolError(f"{label} is not an object")
    return value


def _payload_key_schema(value: object) -> str:
    """Return key counts only, so parser drift is debuggable without logging values."""

    counts: dict[str, int] = {}
    pending = [value]
    visited = 0
    while pending and visited < 10_000:
        item = pending.pop()
        visited += 1
        if isinstance(item, Mapping):
            for key, child in item.items():
                label = str(key)[:100]
                counts[label] = counts.get(label, 0) + 1
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item[:1_000])
    return json.dumps(dict(sorted(counts.items())), ensure_ascii=False)[:2_000]


def _payload_public_dates(value: object) -> str:
    """Return only public timetable DATE fields, never lesson or person values."""

    dates: set[str] = set()
    pending = [value]
    while pending and len(dates) < 100:
        item = pending.pop()
        if isinstance(item, Mapping):
            raw_date = item.get("DATE")
            if isinstance(raw_date, str) and raw_date.strip():
                dates.add(raw_date.strip()[:40])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item[:1_000])
    return json.dumps(sorted(dates), ensure_ascii=False)[:2_000]


def _payload_container_schema(value: object) -> str:
    """Describe container types by redacted path so API shape drift is visible."""

    shapes: dict[str, set[str]] = {}
    pending: list[tuple[str, object]] = [("$", value)]
    visited = 0
    while pending and visited < 10_000:
        path, item = pending.pop()
        visited += 1
        if isinstance(item, Mapping):
            shapes.setdefault(path, set()).add(f"object[{len(item)}]")
            for key, child in item.items():
                segment = str(key) if path in ("$", "$.R") else "*"
                pending.append((f"{path}.{segment}", child))
        elif isinstance(item, list):
            shapes.setdefault(path, set()).add(f"list[{len(item)}]")
            pending.extend((f"{path}[]", child) for child in item[:1_000])
    compact = {path: sorted(kinds) for path, kinds in sorted(shapes.items())}
    return json.dumps(compact, ensure_ascii=False)[:2_000]


def _weekday(value: object) -> int:
    normalized = normalize_text(str(value)).rstrip(":")
    if normalized not in _WEEKDAY_INDEX:
        raise SpbGasuProtocolError(f"unknown SPbGASU weekday: {value}")
    return _WEEKDAY_INDEX[normalized]


def _positive_int(value: object, label: str) -> int:
    try:
        result = int(str(value))
    except ValueError as exc:
        raise SpbGasuProtocolError(f"invalid {label}") from exc
    if result <= 0:
        raise SpbGasuProtocolError(f"invalid {label}")
    return result


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bootstrap_group_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("NAME", "name", "VALUE", "value", "ID", "id"):
            result = _string(value.get(key))
            if result:
                return result
    return ""


def _split_lesson_type(subject: str) -> tuple[str, str | None]:
    match = _LESSON_TYPE.search(subject)
    if not match:
        return subject.strip(), None
    labels = {"л.": "Лекция", "пр.": "Практика", "лаб.": "Лабораторная", "сем.": "Семинар"}
    return subject[: match.start()].strip(), labels[match.group(1).casefold()]


def _split_auditorium(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [part.strip() for part in value.split("/", maxsplit=1)]
    return (parts[0] or None, parts[1] or None) if len(parts) == 2 else (parts[0] or None, None)
