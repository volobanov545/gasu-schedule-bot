from __future__ import annotations

from datetime import date, time
from urllib.parse import parse_qs

import httpx
import pytest

from szs_hub.schedule.spbgasu import (
    SpbGasuClient,
    SpbGasuGroupNotFoundError,
    SpbGasuProtocolError,
    WeeklyLesson,
    WeeklySchedule,
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


def component_html() -> str:
    return """
    <div class="owl-carousel">
      <div class="item" data-hash="week_2">
        <div class="time week_today">Неделя №2: Знаменатель</div>
        <div class="days">
          <div class="week_day"><div>ПТ</div><div class="date">11.09.2026</div></div>
          <div class="lessons">
            <div class="lesson">
              <div class="day_name"><b>2 пара</b><br>10:45-12:15</div>
              <div class="lesson_block">
                <div><span class="lesson-name">Геодезия (л.)</span></div>
                <div>3-СУЗСс-3</div><div>407/1</div><div>Иванов И. И.</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """


def test_response_wrapper_and_root_shape_are_supported() -> None:
    wrapped = parse_weekly_schedule(payload(), group_key="СЗС-3")
    root = parse_weekly_schedule(payload()["R"], group_key="СЗС-3")

    assert wrapped == root
    assert len(wrapped.lessons) == 2


def test_empty_week_is_a_valid_schedule() -> None:
    schedule = parse_weekly_schedule({"R": {}}, group_key="СЗС-3")

    assert schedule.group_key == "СЗС-3"
    assert schedule.lessons == ()


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


def test_component_html_uses_published_dates_instead_of_week_template() -> None:
    schedule = parse_weekly_schedule(component_html(), group_key="3-СУЗСс-3")

    lessons = materialize_week(
        schedule,
        monday=date(2026, 9, 7),
        parity=WeekParity.DENOMINATOR,
    )

    assert len(lessons) == 1
    assert lessons[0].day == date(2026, 9, 11)
    assert lessons[0].starts_at == time(10, 45)
    assert lessons[0].subject == "Геодезия"
    assert lessons[0].room == "407"


@pytest.mark.parametrize(
    "malformed_day",
    [
        """
        <div class="days">
          <div class="week_day"><div>СБ</div><div class="date">12/09/2026</div></div>
          <div class="lessons"><div class="lesson">
            <div class="day_name"><b>3 пара</b></div>
            <div class="lesson_block">
              <div><span class="lesson-name">Механика (л.)</span></div>
              <div>3-СУЗСс-3</div><div>101/1</div><div>Петров П. П.</div>
            </div>
          </div></div>
        </div>
        """,
        """
        <div class="days">
          <div class="week_day"><div>СБ</div><div class="date">12.09.2026</div></div>
          <div class="lessons"><div class="lesson">
            <div class="day_name"><b>третья пара</b></div>
            <div class="lesson_block">
              <div><span class="lesson-name">Механика (л.)</span></div>
              <div>3-СУЗСс-3</div><div>101/1</div><div>Петров П. П.</div>
            </div>
          </div></div>
        </div>
        """,
        """
        <div class="days">
          <div class="week_day"><div>СБ</div><div class="date">12.09.2026</div></div>
          <div class="lessons"><div class="lesson">
            <div class="day_name"><b>3 пара</b></div>
            <div class="lesson_block">
              <div>Механика (л.)</div>
              <div>3-СУЗСс-3</div><div>101/1</div><div>Петров П. П.</div>
            </div>
          </div></div>
        </div>
        """,
    ],
    ids=("invalid-date", "invalid-slot", "missing-subject"),
)
def test_component_html_fails_closed_on_partially_malformed_schedule(
    malformed_day: str,
) -> None:
    html = component_html().replace(
        "      </div>\n    </div>",
        f"{malformed_day}\n      </div>\n    </div>",
    )

    with pytest.raises(SpbGasuProtocolError):
        parse_weekly_schedule(html, group_key="3-СУЗСс-3")


def test_nonempty_invalid_source_date_fails_closed() -> None:
    schedule = WeeklySchedule(
        group_key="3-СУЗСс-3",
        lessons=(
            WeeklyLesson(
                weekday=4,
                slot=2,
                parity=WeekParity.DENOMINATOR,
                subject="Геодезия (л.)",
                group="3-СУЗСс-3",
                auditorium="407/1",
                professor=None,
                source_date="11.09.26",
            ),
        ),
    )

    with pytest.raises(SpbGasuProtocolError):
        materialize_week(
            schedule,
            monday=date(2026, 9, 7),
            parity=WeekParity.DENOMINATOR,
        )


def test_component_html_supports_two_weeks_and_multiple_lesson_blocks() -> None:
    html = """
    <div class="owl-carousel">
      <div class="item" data-hash="week_2">
        <div class="time">Неделя №2: Знаменатель</div>
        <div class="predmets"><div class="days">
          <div class="week_day"><div>ПТ</div><div class="date">11.09.2026</div></div>
          <div class="lessons"><div class="lesson">
            <div class="day_name"><b>2 пара</b><br>10:45-12:15</div>
            <div class="lesson_block">
              <div><span class="lesson-name">Геодезия (л.)</span></div>
              <div>Подгруппа 1</div><div>407/1</div><div>Иванов И. И.</div>
            </div>
            <div class="lesson_block">
              <div><span class="lesson-name">Геодезия (пр.)</span></div>
              <div>Подгруппа 2</div><div>408/1</div><div>Петров П. П.</div>
            </div>
          </div></div>
        </div></div>
      </div>
      <div class="item" data-hash="week_3">
        <div class="time">Неделя №3: Числитель</div>
        <div class="predmets"><div class="days">
          <div class="week_day"><div>ПН</div><div class="date">14.09.2026</div></div>
          <div class="lessons"><div class="lesson">
            <div class="day_name"><b>1 пара</b><br>09:00-10:30</div>
            <div class="lesson_block">
              <div><span class="lesson-name">Механика (л.)</span></div>
              <div>3-СУЗСс-3</div><div>101/2</div><div>Сидоров С. С.</div>
            </div>
          </div></div>
        </div></div>
      </div>
    </div>
    """
    schedule = parse_weekly_schedule(html, group_key="3-СУЗСс-3")

    first_week = materialize_week(
        schedule,
        monday=date(2026, 9, 7),
        parity=WeekParity.DENOMINATOR,
    )
    second_week = materialize_week(
        schedule,
        monday=date(2026, 9, 14),
        parity=WeekParity.NUMERATOR,
    )

    assert len(first_week) == 2
    assert {lesson.subgroup for lesson in first_week} == {"Подгруппа 1", "Подгруппа 2"}
    assert len({lesson.source_id for lesson in first_week}) == 2
    assert [(lesson.day, lesson.subject) for lesson in second_week] == [
        (date(2026, 9, 14), "Механика")
    ]


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
async def test_client_uses_component_api_refreshes_csrf_and_caches() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/bitrix/services/main/ajax.php"
        assert request.url.params["mode"] == "class"
        assert request.url.params["c"] == "gasu:raspisanie.csv"
        assert request.url.params["action"] == "getRasp"
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        assert form["search_params[SEARCH]"] == ["СЗС-3"]
        assert form["search_params[FILTER]"] == ["GROUPS"]
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "status": "error",
                    "data": None,
                    "errors": [
                        {
                            "code": "invalid_csrf",
                            "message": "Invalid csrf token",
                            "customData": {"csrf": "fresh-token"},
                        }
                    ],
                },
            )
        assert form["sessid"] == ["fresh-token"]
        return httpx.Response(
            200,
            json={"status": "success", "data": {"html": component_html()}, "errors": []},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    first = await client.fetch_group("СЗС-3")
    second = await client.fetch_group("СЗС-3")

    assert first == second
    assert len(requests) == 2
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
        return httpx.Response(
            200,
            json={"status": "success", "data": {"html": ""}, "errors": []},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    with pytest.raises(SpbGasuGroupNotFoundError):
        await client.fetch_group("ZZZ-NONEXISTENT-000")
    await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_empty_template_with_key_only_diagnostics() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"html": '<div class="days"><div class="date">01.09.2026</div></div>'},
                "errors": [],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SpbGasuClient(client=http, cache_seconds=300)

    with pytest.raises(SpbGasuProtocolError, match="parsed as empty") as error:
        await client.fetch_group("СЗС-3")
    assert "hidden" not in str(error.value)
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
