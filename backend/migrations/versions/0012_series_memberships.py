"""Create hierarchical series and preserve existing volume labels."""

import hashlib
import json
import re
import unicodedata
from decimal import Decimal

import sqlalchemy as sa
from alembic import context, op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    series = op.create_table("series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("series.id")),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.UniqueConstraint("library_id", "key"),
    )
    ancestors = op.create_table("series_ancestors",
        sa.Column("ancestor_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("descendant_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    )
    memberships = op.create_table("series_memberships",
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("volume_number", sa.Numeric()),
    )
    op.create_index("ix_series_parent_id", "series", ["parent_id"])
    op.create_index("ix_series_ancestors_descendant_id", "series_ancestors", ["descendant_id"])
    op.create_index("ix_series_memberships_series_id", "series_memberships", ["series_id"])
    if not context.is_offline_mode():
        backfill(series, ancestors, memberships)


def backfill(series, ancestors, memberships):
    db = op.get_bind()
    works = sa.table("works", sa.column("id", sa.Integer), sa.column("library_id", sa.Integer),
                     sa.column("series", sa.Text), sa.column("series_index", sa.Text))
    batch_query = sa.select(works).where(works.c.series != "").order_by(works.c.id).limit(500)
    known = {}
    after = 0
    while batch := db.execute(batch_query.where(works.c.id > after)).all():
        inserts = []
        for ident, library_id, raw_name, position in batch:
            name = raw_name.strip()
            normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
            if not normalized:
                continue
            pair = (library_id, normalized)
            if pair not in known:
                encoded = json.dumps([normalized], separators=(",", ":"), ensure_ascii=False).encode()
                branch = db.execute(series.insert().values(library_id=library_id, key=hashlib.sha256(encoded).hexdigest(),
                    name=name, normalized_name=normalized, path=[name]).returning(series.c.id)).scalar_one()
                db.execute(ancestors.insert().values(ancestor_id=branch, descendant_id=branch))
                known[pair] = branch
            label = position.strip()
            number = Decimal(label.replace(",", ".")) if len(label) <= 40 and re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)?", label) else None
            inserts.append({"work_id": ident, "series_id": known[pair], "rank": 0, "position": position, "volume_number": number})
        if inserts:
            db.execute(memberships.insert(), inserts)
        after = batch[-1].id


def downgrade():
    op.drop_table("series_memberships")
    op.drop_table("series_ancestors")
    op.drop_table("series")
