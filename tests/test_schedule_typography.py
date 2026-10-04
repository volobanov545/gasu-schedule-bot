"""Typography is a rendering concern, not a change to schedule identity."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from szs_hub.domain.schedule import Lesson
from szs_hub.schedule.ci import ScheduleEnvelope, render_rich_digest
from szs_hub.schedule.typography import location_text, table_text, teacher_text


@pytest.mark.parametrize(("source", "expected"), [
    ("Информационное моделирование в строительстве",
     "Информационное моделирование в\u00a0строительстве"),
    ("Водоснабжение и водоотведение", "Водоснабжение и\u00a0водоотведение"),
    ("в ОченьДлинноеНазваниеПредмета", "в ОченьДлинноеНазваниеПредмета"),
    ("  Геодезия\n  и\t геология ", "Геодезия и\u00a0геология"),
    ("<Предмет & расчёт>", "&lt;Предмет &amp; расчёт&gt;"),
])
def test_subject_binds_only_short_units(source, expected) -> None:
    assert table_text(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("Шиманова А.А.", "Шиманова\u00a0А.\u00a0А."),
    ("Иванов И. И.", "Иванов\u00a0И.\u00a0И."),
    ("Мейке У.Н.Беляева А.И.", "Мейке\u00a0У.\u00a0Н. Беляева\u00a0А.\u00a0И."),
    ("Иванов Иван Иванович", "Иванов Иван Иванович"),
    ("Иванов-Сидоров И.И.; Петров П.П.", "Иванов-Сидоров\u00a0И.\u00a0И.; Петров\u00a0П.\u00a0П."),
    ("Оченьдлиннаясоставнаяфамилия И.И.", "Оченьдлиннаясоставнаяфамилия И.\u00a0И."),
    ("<Иванов> & Петров", "&lt;Иванов&gt; &amp; Петров"),
])
def test_teacher_initials_are_readable_without_inventing_names(source, expected) -> None:
    assert teacher_text(source) == expected


def test_short_location_is_one_unit_but_long_value_can_wrap() -> None:
    assert location_text("Ауд.", "142*") == "Ауд.\u00a0<b>142*</b>"
    assert location_text("корп.", "К305/К") == "корп.\u00a0<b>К305/К</b>"
    assert location_text("Ауд.", "Большой конференц-зал") == "Ауд. <b>Большой конференц-зал</b>"
    assert location_text("Ауд.", "<701>") == "Ауд.\u00a0<b>&lt;701&gt;</b>"
    assert location_text("Ауд.", " ") == "Ауд.\u00a0<b>—</b>"


def test_rendering_has_semantic_alignment_and_does_not_mutate_source() -> None:
    msk = timezone(timedelta(hours=3))
    entry = Lesson(date(2026, 10, 6), time(9), time(10, 30), "Моделирование в строительстве")
    entry = replace(entry, teacher="Иванов И.И.", room="701", building="С", lesson_type="Практика")
    original = entry
    data = ScheduleEnvelope("3-СУЗСс-3", datetime(2026, 10, 4, 12, tzinfo=msk),
                            date(2026, 9, 28), date(2026, 10, 11), (entry,))
    html = render_rich_digest(data, local_now=data.fetched_at)
    assert '<td align="center" valign="middle">' in html
    assert '<td align="left" valign="top">' in html
    assert "<i>Практика</i>" in html
    assert "Иванов\u00a0И.\u00a0И." in html
    assert data.lessons[0] == original
    assert "style=" not in html and "<mark" not in html and "<th" not in html
