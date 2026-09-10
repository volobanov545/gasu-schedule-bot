from __future__ import annotations

import pytest

from szs_hub.search.text import build_fts5_query, prepare_index_text, tokenize


def test_russian_tokens_are_normalized_and_stemmed_for_index() -> None:
    assert tokenize("ЖЕЛЕЗОБЕТОННЫЕ, конструкции!") == (
        "железобетонные",
        "конструкции",
    )
    prepared = prepare_index_text("Железобетонные конструкции")
    assert "железобетон" in prepared
    assert "конструкц" in prepared


def test_discipline_alias_expands_abbreviation() -> None:
    query = build_fts5_query(
        "ЖБК",
        aliases={"жбк": ("железобетонные конструкции", "железобетон")},
    )

    assert '"жбк"*' in query
    assert '"железобетон"*' in query
    assert " OR " in query


def test_multiple_words_remain_conjunctive() -> None:
    query = build_fts5_query("методичка геодезия")

    assert " AND " in query
    assert '"методичк"*' in query
    assert '"геодез"*' in query


def test_fts_operators_and_quotes_cannot_be_injected() -> None:
    query = build_fts5_query('методичка" OR * NOT {evil}')

    assert "evil" in query
    assert '" OR *' not in query
    assert "{" not in query


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="no searchable terms"):
        build_fts5_query("... !!!")

