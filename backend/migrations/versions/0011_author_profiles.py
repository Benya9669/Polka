"""Backfill author profiles from the credits stored on each work."""

import hashlib
import unicodedata

import sqlalchemy as sa
from alembic import context, op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    authors = op.create_table("authors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("authors.id")),
    )
    names = op.create_table("author_names",
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized", sa.Text(), nullable=False),
    )
    credits = op.create_table("work_authors",
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("credit", sa.Integer(), primary_key=True),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("authors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name_key", sa.String(64), nullable=False),
    )
    op.create_index("ix_authors_library_id", "authors", ["library_id"])
    op.create_index("ix_author_names_author_id", "author_names", ["author_id"])
    op.create_index("ix_work_authors_author_id", "work_authors", ["author_id"])
    if not context.is_offline_mode():
        backfill(authors, names, credits)


def backfill(authors, names, credits):
    db = op.get_bind()
    works = sa.table("works", sa.column("id", sa.Integer), sa.column("library_id", sa.Integer), sa.column("authors", sa.JSON))
    known = {}
    after = 0
    while batch := db.execute(sa.select(works).where(works.c.id > after).order_by(works.c.id).limit(500)).all():
        pending = {}
        bindings = []
        for work_id, library_id, values in batch:
            for credit, value in enumerate(values or []):
                normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
                if not normalized:
                    continue
                key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                pair = (library_id, key)
                if pair not in known:
                    pending.setdefault(pair, {"library_id": library_id, "key": key, "name": value.strip(), "normalized": normalized})
                bindings.append((work_id, credit, pair))
        if pending:
            created = db.execute(authors.insert().returning(authors.c.id, sort_by_parameter_order=True),
                [{"library_id": entry["library_id"], "name": entry["name"]} for entry in pending.values()]).scalars().all()
            known.update(zip(pending, created, strict=True))
            db.execute(names.insert(), [{**entry, "author_id": known[pair]} for pair, entry in pending.items()])
        if bindings:
            db.execute(credits.insert(), [{"work_id": ident, "credit": credit, "name_key": pair[1], "author_id": known[pair]}
                                          for ident, credit, pair in bindings])
        after = batch[-1].id


def downgrade():
    op.drop_table("work_authors")
    op.drop_table("author_names")
    op.drop_table("authors")
