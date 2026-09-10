"""Deterministic Russian normalization for an SQLite FTS5/BM25 baseline."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

import snowballstemmer  # type: ignore[import-untyped]

_TOKEN = re.compile(r"[0-9a-zа-я]+", re.IGNORECASE)
_RUSSIAN_STEMMER = snowballstemmer.stemmer("russian")


def tokenize(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return tuple(_TOKEN.findall(normalized))


def stem_token(token: str) -> str:
    if len(token) <= 3 or not any("а" <= character <= "я" for character in token):
        return token
    stems = _RUSSIAN_STEMMER.stemWords([token])
    return stems[0] if stems else token


def prepare_index_text(text: str) -> str:
    """Index both surface forms and stems so quoted source text remains untouched."""

    tokens = tokenize(text)
    terms: list[str] = []
    for token in tokens:
        terms.append(token)
        stem = stem_token(token)
        if stem != token:
            terms.append(stem)
    return " ".join(terms)


def build_fts5_query(
    text: str,
    *,
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Build an injection-safe MATCH expression with alias and stem expansion."""

    tokens = tokenize(text)
    if not tokens:
        raise ValueError("search query has no searchable terms")

    normalized_aliases = _normalize_aliases(aliases or {})
    groups: list[tuple[str, ...]] = []
    consumed: set[int] = set()

    whole_query = " ".join(tokens)
    whole_aliases = normalized_aliases.get(whole_query)
    if whole_aliases:
        groups.append(_expanded_terms(tokens + whole_aliases))
        consumed.update(range(len(tokens)))

    for index, token in enumerate(tokens):
        if index in consumed:
            continue
        alias_tokens = normalized_aliases.get(token, ())
        groups.append(_expanded_terms((token, *alias_tokens)))

    return " AND ".join(_render_group(group) for group in groups)


def _normalize_aliases(aliases: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for key, values in aliases.items():
        key_tokens = tokenize(key)
        if not key_tokens:
            continue
        expanded: list[str] = []
        for value in values:
            expanded.extend(tokenize(value))
        normalized[" ".join(key_tokens)] = tuple(expanded)
    return normalized


def _expanded_terms(tokens: Sequence[str]) -> tuple[str, ...]:
    unique: dict[str, None] = {}
    for token in tokens:
        if not token:
            continue
        unique[token] = None
        unique[stem_token(token)] = None
    return tuple(unique)


def _render_group(group: Sequence[str]) -> str:
    clauses = [f'"{term}"*' for term in group]
    if len(clauses) == 1:
        return clauses[0]
    return f"({' OR '.join(clauses)})"
