"""Index normalized work titles for OPDS search matching."""

import hashlib
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("works", sa.Column("title_key", sa.String(64)))
    db = op.get_bind()
    works = sa.table("works", sa.column("id", sa.Integer), sa.column("title", sa.Text), sa.column("title_key", sa.String(64)))
    update = works.update().where(works.c.id == sa.bindparam("work_id")).values(title_key=sa.bindparam("key"))
    after = 0
    while batch := db.execute(sa.select(works.c.id, works.c.title).where(works.c.id > after).order_by(works.c.id).limit(500)).all():
        keys = []
        for ident, title in batch:
            normalized = " ".join(unicodedata.normalize("NFKC", title).casefold().split())
            keys.append({"work_id": ident, "key": hashlib.sha256(normalized.encode("utf-8")).hexdigest()})
        db.execute(update, keys)
        after = batch[-1].id
    op.alter_column("works", "title_key", existing_type=sa.String(64), nullable=False)
    op.create_index("ix_works_title_key", "works", ["title_key"])


def downgrade():
    op.drop_index("ix_works_title_key", table_name="works")
    op.drop_column("works", "title_key")
