"""add search_documents FTS5 projection

Revision ID: 8e9d7f42c1a0
Revises: fcefb6533b1c
Create Date: 2026-08-29 13:44:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8e9d7f42c1a0"
down_revision: str | None = "fcefb6533b1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE search_documents_fts USING fts5(
            content,
            metadata,
            content='search_documents',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER search_documents_fts_ai
        AFTER INSERT ON search_documents BEGIN
            INSERT INTO search_documents_fts(rowid, content, metadata)
            VALUES (new.id, new.content, new.metadata);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER search_documents_fts_ad
        AFTER DELETE ON search_documents BEGIN
            INSERT INTO search_documents_fts(search_documents_fts, rowid, content, metadata)
            VALUES ('delete', old.id, old.content, old.metadata);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER search_documents_fts_au
        AFTER UPDATE ON search_documents BEGIN
            INSERT INTO search_documents_fts(search_documents_fts, rowid, content, metadata)
            VALUES ('delete', old.id, old.content, old.metadata);
            INSERT INTO search_documents_fts(rowid, content, metadata)
            VALUES (new.id, new.content, new.metadata);
        END
        """
    )
    op.execute("INSERT INTO search_documents_fts(search_documents_fts) VALUES ('rebuild')")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS search_documents_fts_au")
    op.execute("DROP TRIGGER IF EXISTS search_documents_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS search_documents_fts_ai")
    op.execute("DROP TABLE IF EXISTS search_documents_fts")
