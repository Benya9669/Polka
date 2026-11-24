"""Include existing metadata in the expanded search index."""

import unicodedata

import sqlalchemy as sa
from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


work = sa.table("works",
    sa.column("id", sa.Integer), sa.column("title", sa.Text), sa.column("authors", sa.JSON),
    sa.column("series", sa.Text), sa.column("genres", sa.JSON), sa.column("tags", sa.JSON),
    sa.column("custom_fields", sa.JSON), sa.column("search", sa.Text),
)
edition = sa.table("editions",
    sa.column("work_id", sa.Integer), sa.column("language", sa.Text), sa.column("isbn", sa.Text),
    sa.column("publisher", sa.Text), sa.column("year", sa.Text), sa.column("summary", sa.Text),
)


def normalized(parts):
    raw = " ".join(str(value) for value in parts if value)
    return " ".join(unicodedata.normalize("NFKC", raw).casefold().split())


def reindex(include_metadata):
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    after = 0
    update = work.update().where(work.c.id == sa.bindparam("work_id")).values(search=sa.bindparam("term"))
    while rows := connection.execute(sa.select(work).where(work.c.id > after).order_by(work.c.id).limit(200)).mappings().all():
        words = {}
        for item in rows:
            words[item["id"]] = [item["title"], *(item["authors"] or [])]
            if include_metadata:
                words[item["id"]].extend([item["series"], *(item["genres"] or []), *(item["tags"] or [])])
                for key, value in (item["custom_fields"] or {}).items():
                    words[item["id"]].extend((key, value))
        if include_metadata:
            for row in connection.execute(sa.select(edition).where(edition.c.work_id.in_(words))).mappings():
                words[row["work_id"]].extend(row[key] for key in ("language", "isbn", "publisher", "year", "summary"))
        connection.execute(update, [{"work_id": ident, "term": normalized(parts)} for ident, parts in words.items()])
        after = rows[-1]["id"]


def upgrade():
    reindex(True)


def downgrade():
    reindex(False)

