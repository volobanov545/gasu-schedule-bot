"""Stable display aliases; never rewrite source subjects or calendar identity."""

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
