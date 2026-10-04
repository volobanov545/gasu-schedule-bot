"""Stable display aliases; never rewrite source subjects or calendar identity."""

from collections.abc import Iterable
from html import escape

SUBJECT_ALIASES: tuple[tuple[str, str], ...] = (
    ("Безопасность жизнедеятельности", "БЖД"),
    ("Водоснабжение и водоотведение", "ВиВ"),
    ("Информационное моделирование в строительстве", "ИМС"),
    ("Механика грунтов", "Мех. грунтов"),
    ("Проектный менеджмент", "ПМ"),
    ("Средства механизации строительства", "СМС"),
    ("Строительная механика", "Строймех."),
    ("Учебная практика/Ознакомительная практика", "Ознак. практика"),
)

_ALIASES = {full.casefold(): short for full, short in SUBJECT_ALIASES}
_REFERENCE_NAMES = {
    full.casefold(): f"subject-{index}" for index, (full, _) in enumerate(SUBJECT_ALIASES)
}


def subject_reference_name(value: str) -> str | None:
    """Stable internal footnote target, never a URL or callback to the bot."""
    return _REFERENCE_NAMES.get(" ".join(value.split()).casefold())


def subject_reference_definitions(subjects: Iterable[str]) -> str:
    """Define each used abbreviation once, including those in collapsed days."""
    used = {subject_reference_name(subject) for subject in subjects}
    entries = "".join(
        f'<p><b>{escape(short)}</b> — '
        f'<tg-reference name="{_REFERENCE_NAMES[full.casefold()]}">{escape(full)}</tg-reference>'
        "</p>"
        for full, short in SUBJECT_ALIASES
        if _REFERENCE_NAMES[full.casefold()] in used
    )
    if not entries:
        return ""
    return '<details><summary>Сокращения</summary>' + entries + "</details>"


def subject_label(value: str) -> str:
    """Only exact known names are shortened, not substrings or unknown courses."""
    clean = " ".join(value.split())
    return _ALIASES.get(clean.casefold(), clean)


def bot_description() -> str:
    """Keep the complete glossary within Telegram's 512-character limit."""
    glossary = "\n".join(f"{short} — {full}" for full, short in SUBJECT_ALIASES)
    return (
        "Расписание 3-СУЗСс-3 СПбГАСУ.\n\nСокращения предметов:\n" + glossary
        + "\n\nВ календаре названия полные."
    )


def bot_short_description() -> str:
    """Brief meanings in the profile; the description contains exact full names."""
    return (
        "БЖД — безопасность; ВиВ — вода/стоки; ИМС — инфомоделирование; "
        "ПМ — проектный менеджмент; СМС — механизация."
    )
