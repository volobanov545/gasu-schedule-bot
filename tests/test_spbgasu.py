from __future__ import annotations

from datetime import date, time

import httpx
import pytest

from szs_hub.schedule.spbgasu import (
    SpbGasuClient,
    SpbGasuGroupNotFoundError,
    SpbGasuProtocolError,
    WeekParity,
    materialize_week,
    parity_for_week,
    parse_schedule_page_bootstrap,
    parse_weekly_schedule,
)


def payload() -> dict[str, object]:
    return {
        "R": {
            "Понедельник": {
                "1": {
                    "числ.": {
                        "DATE": "",
                        "LESSON": "Железобетонные конструкции (пр.)",
                        "GROUP": "СЗС-3",
                        "AUDITORIUM": "407/1",
                        "PROFESSOR": "Иванов А. А.",
                    },
                    "знам.": {
                        "DATE": "",
                        "LESSON": "Геодезия (л.)",
                        "GROUP": "СЗС-3",
                        "AUDITORIUM": "312",
                        "PROFESSOR": "Петров П. П.",
                    },
                }
            }
        }
    }


def test_response_wrapper_and_root_shape_are_supported() -> None:
    wrapped = parse_weekly_schedule(payload(), group_key="СЗС-3")
    root = parse_weekly_schedule(payload()["R"], group_key="СЗС-3")

    assert wrapped == root
    assert len(wrapped.lessons) == 2


def test_week_materialization_uses_official_bell_slots() -> None:
    schedule = parse_weekly_schedule(payload(), group_key="СЗС-3")
    lessons = materialize_week(
        schedule,
        monday=date(2026, 8, 31),
        parity=WeekParity.NUMERATOR,
    )

    assert len(lessons) == 1
    assert lessons[0].starts_at == time(9, 0)
    assert lessons[0].ends_at == time(10, 30)
    assert lessons[0].lesson_type == "Практика"
    assert lessons[0].room == "407"
    assert lessons[0].building == "1"


def test_bootstrap_extracts_week_number_and_groups() -> None:
    html = """
    <script>
      window.NUMBER_WEEK = '5';
      window.GROUPS = ["СЗС-3", {"NAME": "1-СЗС-3"}];
    </script>
    """

    result = parse_schedule_page_bootstrap(html)

    assert result.current_week_number == 5
    assert result.groups == ("СЗС-3", "1-СЗС-3")


def test_parity_is_derived_from_official_current_week_number() -> None:
    assert (
        parity_for_week(
            current_week_number=5,
            current_monday=date(2026, 8, 31),
            target_monday=date(2026, 9, 7),
        )
        is WeekParity.DENOMINATOR
    )


@pytest.mark.asyncio
async def test_client_uses_exact_typo_and_caches() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["SERACH"] == "СЗС-3"
        assert request.url.params["FILTER"] == "GROUPS"
        assert request.url.params["GROUP"] == ""
        assert request.url.params["SELECT"] == "*"
        assert "SEARCH" not in request.url.params
        return httpx.Response(200, json=payload())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    first = await client.fetch_group("СЗС-3")
    second = await client.fetch_group("СЗС-3")

    assert first == second
    assert len(requests) == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_client_fetches_and_caches_public_week_bootstrap() -> None:
    requests: list[httpx.Request] = []
    html = """
    <script>
      window.NUMBER_WEEK = 7;
      window.GROUPS = ["СЗС-3"];
    </script>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/"
        return httpx.Response(200, text=html)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    first = await client.fetch_bootstrap()
    second = await client.fetch_bootstrap()

    assert first == second
    assert first.current_week_number == 7
    assert len(requests) == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_unknown_group_is_explicit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    with pytest.raises(SpbGasuGroupNotFoundError):
        await client.fetch_group("ZZZ-NONEXISTENT-000")
    await http.aclose()


@pytest.mark.asyncio
async def test_oversized_or_malformed_response_fails_closed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300, max_response_bytes=100)

    with pytest.raises(SpbGasuProtocolError, match="size limit"):
        await client.fetch_group("СЗС-3")
    await http.aclose()
