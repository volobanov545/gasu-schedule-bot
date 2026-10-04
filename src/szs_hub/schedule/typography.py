"""Small display-only Russian typography rules for native Telegram tables.

Do not guess screen width, abbreviate source data or insert hard line breaks
inside names. Only short semantic units are kept together; full names and
long location values must still be able to wrap in the user's client.
"""

import re
from html import escape

_NBSP = "\N{NO-BREAK SPACE}"
_SHORT_LINK = re.compile(r"\b([вскоуиаВСКОУИА]) ([^\W\d_][\w-]*)")
_INITIALS = re.compile(r"(?<!\w)([А-ЯЁA-Z])\.\s*([А-ЯЁA-Z])\.")
_SURNAME_INITIALS = re.compile(r"([А-ЯЁA-Z][а-яёa-z-]+) ([А-ЯЁA-Z]\.\u00a0[А-ЯЁA-Z]\.)")


def table_text(value: str) -> str:
    """Escape source text, allowing normal wrapping except for short links."""
    text = " ".join(value.split())
    text = _SHORT_LINK.sub(
        lambda match: (
            match[1] + _NBSP + match[2]
            if len(match[2]) <= 18
            else match[0]
        ),
        text,
    )
    return escape(text)


def teacher_text(value: str) -> str:
    """Space real initials consistently; never expand or shorten a name."""
    text = " ".join(value.split())
    # The source sometimes concatenates two teachers: 'У.Н.Беляева А.И.'.
    # A space permits wrapping between them without guessing a separator/name.
    text = re.sub(r"(?<=[.])(?=[А-ЯЁA-Z][а-яёa-z])", " ", text)
    text = _INITIALS.sub(lambda match: match[1] + "." + _NBSP + match[2] + ".", text)
    text = _SURNAME_INITIALS.sub(
        lambda match: (
            match[1] + _NBSP + match[2]
            if len(match[1]) + len(match[2]) <= 24
            else match[0]
        ),
        text,
    )
    return escape(text)


def location_text(label: str, value: str) -> str:
    """Bind compact room/building labels, but let long descriptions wrap."""
    clean = " ".join(value.split()) or "—"
    gap = _NBSP if len(label) + len(clean) <= 16 else " "
    return f"{escape(label)}{gap}<b>{escape(clean)}</b>"
