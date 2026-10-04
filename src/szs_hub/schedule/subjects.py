"""Stable display aliases; never rewrite source subjects or calendar identity."""

SUBJECT_ALIASES: tuple[tuple[str, str], ...] = (
    ("Безопасность жизнедеятельности", "БЖД"),
    ("Водоснабжение и водоотведение", "ВиВ"),
    ("Информационное моделирование в строительстве", "Инф. моделирование"),
    ("Средства механизации строительства", "Механизация строительства"),
    ("Строительная механика", "Строймеханика"),
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
        "Расписание 3-СУЗСс-3 СПбГАСУ. Одна обновляемая карточка.\n\n"
        "Сокращения предметов:\n" + glossary
        + "\n\nВ календаре названия полные. Остальные предметы не сокращаем."
    )


def bot_short_description() -> str:
    """The two non-obvious acronyms fit directly on the bot's profile page."""
    return "\n".join(
        f"{short} — {full.lower()}"
        for full, short in SUBJECT_ALIASES
        if short in {"БЖД", "ВиВ"}
    )
