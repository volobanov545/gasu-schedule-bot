"""SQLite FTS5 projection and ranked search repository."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from szs_hub.search.text import build_fts5_query

_CREATE_PROJECTION_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
        content,
        metadata,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_documents_fts_ai
    AFTER INSERT ON search_documents BEGIN
        INSERT INTO search_documents_fts(rowid, content, metadata)
        VALUES (
            new.id,
            new.content,
            COALESCE(
                (
                    SELECT group_concat(CAST(json_tree.atom AS TEXT), ' ')
                    FROM json_tree(new.metadata)
                    WHERE json_tree.atom IS NOT NULL
                ),
                ''
            )
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_documents_fts_ad
    AFTER DELETE ON search_documents BEGIN
        DELETE FROM search_documents_fts WHERE rowid = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_documents_fts_au
    AFTER UPDATE ON search_documents BEGIN
        DELETE FROM search_documents_fts WHERE rowid = old.id;
        INSERT INTO search_documents_fts(rowid, content, metadata)
        VALUES (
            new.id,
            new.content,
            COALESCE(
                (
                    SELECT group_concat(CAST(json_tree.atom AS TEXT), ' ')
                    FROM json_tree(new.metadata)
                    WHERE json_tree.atom IS NOT NULL
                ),
                ''
            )
        );
    END
    """,
    "DELETE FROM search_documents_fts",
    """
    INSERT INTO search_documents_fts(rowid, content, metadata)
    SELECT
        documents.id,
        documents.content,
        COALESCE(
            (
                SELECT group_concat(CAST(json_tree.atom AS TEXT), ' ')
                FROM json_tree(documents.metadata)
                WHERE json_tree.atom IS NOT NULL
            ),
            ''
        )
    FROM search_documents AS documents
    """,
)

_DROP_PROJECTION_STATEMENTS = (
    "DROP TRIGGER IF EXISTS search_documents_fts_au",
    "DROP TRIGGER IF EXISTS search_documents_fts_ad",
    "DROP TRIGGER IF EXISTS search_documents_fts_ai",
    "DROP TABLE IF EXISTS search_documents_fts",
)


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: int
    source_type: str
    source_id: int
    content: str
    metadata: dict[str, Any] | None
    score: float
    snippet: str


def build_safe_match_query(query: str) -> str:
    """Convert user text into a bounded, literal FTS5 AND query.

    FTS syntax is never accepted from the caller. Only Unicode word tokens are kept,
    quoted, and passed as a bound parameter, so malformed input cannot alter SQL or
    produce an invalid MATCH expression.
    """

    # The shared builder accepts only normalized word tokens, quotes every term,
    # and adds safe Russian stems/prefixes. Caller-provided FTS operators never
    # survive into the MATCH expression.
    try:
        return build_fts5_query(query)
    except ValueError as exc:
        raise ValueError("search query must contain at least one word") from exc


async def install_search_projection(connection: AsyncConnection) -> None:
    """Install triggers and rebuild the disposable FTS projection."""

    for statement in _CREATE_PROJECTION_STATEMENTS:
        await connection.execute(text(statement))


async def uninstall_search_projection(connection: AsyncConnection) -> None:
    """Remove projection objects before dropping the source table."""

    for statement in _DROP_PROJECTION_STATEMENTS:
        await connection.execute(text(statement))


class SearchRepository:
    """Bound-parameter FTS5 search ordered by SQLite BM25 relevance."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self,
        query: str,
        *,
        source_type: str | None = None,
        source_ids: Sequence[int] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[SearchHit, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("search limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("search offset must be non-negative")
        match_query = build_safe_match_query(query)
        filtered_source_ids = tuple(source_ids) if source_ids is not None else None
        if filtered_source_ids == ():
            return ()

        clauses = ["search_documents_fts MATCH :match_query"]
        parameters: dict[str, Any] = {
            "match_query": match_query,
            "limit": limit,
            "offset": offset,
        }
        if source_type is not None:
            clauses.append("documents.source_type = :source_type")
            parameters["source_type"] = source_type
        if filtered_source_ids is not None:
            clauses.append("documents.source_id IN :source_ids")
            parameters["source_ids"] = filtered_source_ids

        statement = text(
            """
            SELECT
                documents.id AS document_id,
                documents.source_type,
                documents.source_id,
                documents.content,
                documents.metadata,
                bm25(search_documents_fts, 1.0, 0.25) AS score,
                snippet(search_documents_fts, 0, '[', ']', ' … ', 18) AS snippet
            FROM search_documents_fts
            JOIN search_documents AS documents ON documents.id = search_documents_fts.rowid
            WHERE
            """
            + " AND ".join(clauses)
            + " ORDER BY score ASC, documents.id ASC LIMIT :limit OFFSET :offset"
        )
        if filtered_source_ids is not None:
            statement = statement.bindparams(bindparam("source_ids", expanding=True))

        rows = (await self._session.execute(statement, parameters)).mappings()
        return tuple(
            SearchHit(
                document_id=int(row["document_id"]),
                source_type=str(row["source_type"]),
                source_id=int(row["source_id"]),
                content=str(row["content"]),
                metadata=_decode_metadata(row["metadata"]),
                score=float(row["score"]),
                snippet=str(row["snippet"]),
            )
            for row in rows
        )

    async def rebuild(self) -> None:
        """Recreate the complete index from the canonical content table."""

        await self._session.execute(text("DELETE FROM search_documents_fts"))
        await self._session.execute(
            text(
                """
                INSERT INTO search_documents_fts(rowid, content, metadata)
                SELECT
                    documents.id,
                    documents.content,
                    COALESCE(
                        (
                            SELECT group_concat(CAST(json_tree.atom AS TEXT), ' ')
                            FROM json_tree(documents.metadata)
                            WHERE json_tree.atom IS NOT NULL
                        ),
                        ''
                    )
                FROM search_documents AS documents
                """
            )
        )

    async def optimize(self) -> None:
        """Merge FTS segments after a large import."""

        await self._session.execute(
            text("INSERT INTO search_documents_fts(search_documents_fts) VALUES ('optimize')")
        )


def _decode_metadata(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None
