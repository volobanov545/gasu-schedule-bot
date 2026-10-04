"""Contract tests for the selected Telegram-native day-plan design."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser

import pytest

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ci import ScheduleEnvelope, render_digest_fallback, render_rich_digest

MSK = timezone(timedelta(hours=3), "Europe/Moscow")


def lesson(day: date, **values) -> Lesson:
    return replace(Lesson(day, time(9), time(10, 30), "Геодезия"), **values)


def envelope(*lessons: Lesson) -> ScheduleEnvelope:
    return ScheduleEnvelope(
        "3-СУЗСс-3", datetime(2026, 10, 4, 11, 56, tzinfo=MSK),
        date(2026, 9, 28), date(2026, 10, 11), lessons,
    )


def test_sunday_opens_tuesday_and_omits_expired_week() -> None:
    data = envelope(
        lesson(date(2026, 10, 3), subject="Прошедшая суббота"),
        lesson(date(2026, 10, 6), subject="Ближайшая лекция"),
        lesson(date(2026, 10, 7), subject="Среда"),
    )
    html = render_rich_digest(data, local_now=datetime(2026, 10, 4, 12, tzinfo=MSK))
    assert "Вс, 4 октября — без пар" in html
    assert "Следующий учебный день: Вт, 6 октября" in html
    assert "Прошедшая суббота" not in html
    assert html.index("Ближайшая лекция") < html.index("<details")
    assert "<summary>Остальные дни недели</summary>" in html
    assert "Следующая неделя" not in html  # Never show a dead disclosure.
    assert "Завтра" not in html


@pytest.mark.parametrize("hour,minute,label", [
    (8, 59, "Далее"), (9, 0, "Сейчас"), (10, 29, "Сейчас"),
    (10, 30, "Следующий учебный день: Ср, 7 октября"),
])
def test_day_plan_changes_exactly_at_lesson_boundaries(hour, minute, label) -> None:
    html = render_rich_digest(
        envelope(lesson(date(2026, 10, 6)), lesson(date(2026, 10, 7))),
        local_now=datetime(2026, 10, 6, hour, minute, tzinfo=MSK),
    )
    assert label in html
    assert "<mark" not in html and "<th" not in html


def test_relative_time_uses_moscow_timestamp_not_naive_or_utc_clock() -> None:
    start = datetime(2026, 10, 6, 9, tzinfo=MSK)
    html = render_rich_digest(
        envelope(lesson(start.date())), local_now=start - timedelta(minutes=15),
    )
    assert f'<tg-time unix="{int(start.timestamp())}" format="r">' in html
    assert "Через 15 мин" in html
    assert "09:00" in html


def test_parallel_subgroups_have_both_teachers_but_count_as_one_slot() -> None:
    day = date(2026, 10, 6)
    data = envelope(
        lesson(day, teacher="Иванов Иван Иванович", subgroup="1", room="701"),
        lesson(day, teacher="Петров Пётр Петрович", subgroup="2", room="702"),
    )
    html = render_rich_digest(data, local_now=datetime(2026, 10, 4, 12, tzinfo=MSK))
    assert "1 пара · 09:00–10:30" in html
    assert "Иванов Иван Иванович" in html and "Петров Пётр Петрович" in html
    assert "Подгруппа: 1" in html and "Подгруппа: 2" in html
    assert "701" in html and "702" in html


def test_empty_and_expired_horizon_do_not_claim_infinite_holidays() -> None:
    html = render_rich_digest(envelope(), local_now=datetime(2026, 10, 4, 12, tzinfo=MSK))
    assert "до 11 октября" in html
    assert "<table" not in html and "<details" not in html
    expired = render_rich_digest(
        envelope(), local_now=datetime(2026, 10, 12, 12, tzinfo=MSK),
    )
    assert "На сегодня нет опубликованных данных" in expired
    assert "без пар" not in expired


def test_fallback_also_opens_the_next_teaching_day() -> None:
    text = render_digest_fallback(
        envelope(lesson(date(2026, 10, 6), subject="Безопасность жизнедеятельности")),
        local_now=datetime(2026, 10, 4, 12, tzinfo=MSK),
    )
    assert "Ближайший учебный день" in text
    assert "6 октября" in text and "Безопасность жизнедеятельности" in text


def test_source_values_are_escaped_and_every_table_row_has_only_two_cells() -> None:
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.row_cells = []

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.row_cells.append(0)
            if tag in {"td", "th"}:
                self.row_cells[-1] += 1

    data = envelope(lesson(
        date(2026, 10, 6), subject="<Длинный предмет & расчёт>", teacher="<ФИО>",
        room="<701>", building="<С>", subgroup="<1>",
    ))
    html = render_rich_digest(data, local_now=datetime(2026, 10, 4, 12, tzinfo=MSK))
    parser = TableParser()
    parser.feed(html)
    assert parser.row_cells == [2]
    for expected in ("&lt;Длинный предмет &amp; расчёт&gt;", "&lt;ФИО&gt;", "&lt;701&gt;"):
        assert expected in html


def test_next_week_disclosure_is_real_and_does_not_duplicate_primary_day() -> None:
    data = envelope(lesson(date(2026, 10, 2)), lesson(date(2026, 10, 6)))
    html = render_rich_digest(data, local_now=datetime(2026, 10, 2, 8, tzinfo=MSK))
    assert "<summary>Следующая неделя</summary>" in html
    assert html.count("<h2>Сегодня · Пятница, 2 октября</h2>") == 1
