"""Aliases affect presentation only; the profile explains the same dictionary."""

import json
from datetime import UTC, date, datetime, time

import httpx
import pytest

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.bot_profile import sync_bot_profile
from szs_hub.schedule.calendar import render_icalendar
from szs_hub.schedule.ci import (
    ScheduleEnvelope,
    decode_schedule_envelope,
    encode_schedule_envelope,
    render_digest_fallback,
    render_rich_digest,
)
from szs_hub.schedule.subjects import (
    SUBJECT_ALIASES,
    bot_description,
    bot_short_description,
    subject_label,
    subject_reference_definitions,
    subject_reference_name,
)


@pytest.mark.parametrize(("full", "short"), SUBJECT_ALIASES)
def test_known_subject_alias_is_stable_and_case_whitespace_tolerant(full, short) -> None:
    assert subject_label(full) == short
    assert subject_label("  " + full.upper().replace(" ", "\n ") + "  ") == short
    assert subject_label(full + " (спецкурс)") == full + " (спецкурс)"
    assert f"{short} — {full}" in bot_description()


def test_unknown_subjects_not_guessed_and_glossary_fits_telegram_limits() -> None:
    assert subject_label("Инженерная геология") == "Инженерная геология"
    assert subject_label("<Новый & предмет>") == "<Новый & предмет>"
    assert len({short.casefold() for _, short in SUBJECT_ALIASES}) == len(SUBJECT_ALIASES)
    assert len(bot_description()) <= 512
    assert len(bot_short_description()) <= 120
    for acronym in ("БЖД", "ВиВ", "ИМС", "ПМ", "СМС"):
        assert f"{acronym} — " in bot_short_description()
    assert subject_label("Строительная механика") == "Строймех."
    assert subject_label("Средства механизации строительства") == "СМС"
    assert subject_label("Проектный менеджмент") == "ПМ"


def test_aliases_render_without_changing_snapshot_or_phone_calendar() -> None:
    day = date(2026, 10, 6)
    lessons = tuple(
        Lesson(day, time(9 + index), time(10 + index), full)
        for index, (full, _) in enumerate(SUBJECT_ALIASES)
    )
    now = datetime(2026, 10, 6, 5, tzinfo=UTC)
    envelope = ScheduleEnvelope("3-СУЗСс-3", now, date(2026, 10, 5), date(2026, 10, 18), lessons)
    encoded_before = encode_schedule_envelope(envelope)
    rich = render_rich_digest(envelope, local_now=now)
    fallback = render_digest_fallback(envelope, local_now=now)
    for full, short in SUBJECT_ALIASES:
        reference = subject_reference_name(full)
        assert f'<b><a href="#{reference}">{short}</a></b>' in rich
        assert rich.count(f'<tg-reference name="{reference}">{full}</tg-reference>') == 1
        assert short in fallback
    assert "Полные названия" not in rich and "Сокращения предметов" not in rich
    assert encode_schedule_envelope(envelope) == encoded_before
    assert decode_schedule_envelope(encoded_before).lessons == lessons
    calendar = render_icalendar(envelope).replace("\r\n ", "")
    for full, _ in SUBJECT_ALIASES:
        assert f"SUMMARY:{full}" in calendar


def test_reference_targets_are_unique_and_deduplicated_for_repeated_lessons() -> None:
    full = "Проектный менеджмент"
    reference = subject_reference_name(full)
    definitions = subject_reference_definitions([full, full.upper(), "  " + full + "  "])
    assert definitions == f'<tg-reference name="{reference}">{full}</tg-reference>'
    assert subject_reference_name("  ПРОЕКТНЫЙ\nМЕНЕДЖМЕНТ ") == reference
    assert subject_reference_name(full + " (спецкурс)") is None
    assert subject_reference_definitions(["Неизвестный предмет"]) == ""
    names = [subject_reference_name(name) for name, _ in SUBJECT_ALIASES]
    assert len(set(names)) == len(SUBJECT_ALIASES)


def test_references_cover_collapsed_days_without_shortening_unknown_subjects() -> None:
    now = datetime(2026, 10, 6, 5, tzinfo=UTC)
    lessons = (
        Lesson(date(2026, 10, 6), time(9), time(10), "Безопасность жизнедеятельности"),
        Lesson(date(2026, 10, 7), time(9), time(10), "Проектный менеджмент"),
        Lesson(date(2026, 10, 13), time(9), time(10), "Средства механизации строительства"),
        Lesson(date(2026, 10, 14), time(9), time(10), "<Новый & предмет>"),
    )
    envelope = ScheduleEnvelope("3-СУЗСс-3", now, date(2026, 10, 5), date(2026, 10, 18), lessons)
    html = render_rich_digest(envelope, local_now=now)
    for lesson in lessons[:3]:
        reference = subject_reference_name(lesson.subject)
        assert f'href="#{reference}"' in html
        assert html.count(f'<tg-reference name="{reference}">') == 1
    assert "<b>&lt;Новый &amp; предмет&gt;</b>" in html
    assert "callback_data" not in html
    assert "Полные названия" not in html
    assert "<tg-reference" not in render_digest_fallback(envelope, local_now=now)


@pytest.mark.asyncio
async def test_profile_sync_updates_and_verifies_both_languages_then_is_noop() -> None:
    stored: dict[tuple[str, str], str] = {}
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        field = "short_description" if "Short" in method else "description"
        key = (params["language_code"], field)
        if method.startswith("set"):
            stored[key] = params[field]
            writes.append(method)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": True, "result": {field: stored.get(key, "")}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await sync_bot_profile("fixture-value", client)
        assert len(writes) == 4
        await sync_bot_profile("fixture-value", client)
        assert len(writes) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "transport", "not_saved"])
async def test_profile_sync_errors_are_safe_and_dont_claim_unverified_success(failure) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "rejected":
            return httpx.Response(403, json={"ok": False, "description": "fixture-value"})
        if "/setMy" in request.url.path:
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": True, "result": {"description": ""}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as caught:
            await sync_bot_profile("fixture-value", client)
        assert "fixture-value" not in str(caught.value)
