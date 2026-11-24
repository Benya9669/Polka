"""Store private collections, favorites and work-level reading state."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reading_states",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("started", sa.DateTime(timezone=True)),
        sa.Column("finished", sa.DateTime(timezone=True)),
        sa.Column("last_read", sa.DateTime(timezone=True)),
        sa.Column("last_file_id", sa.Integer(), sa.ForeignKey("book_files.id", ondelete="SET NULL")),
        sa.Column("percentage", sa.Float()),
        sa.Column("device", sa.String(120)),
    )
    op.create_table(
        "collections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("normalized_name", sa.String(240), nullable=False),
        sa.UniqueConstraint("user_id", "normalized_name"),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])
    op.create_table(
        "collection_items",
        sa.Column("collection_id", sa.Integer(), sa.ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
    )
    op.execute("""
        INSERT INTO reading_states
          (user_id, work_id, status, favorite, started, last_read, last_file_id, percentage, device)
        SELECT user_id, work_id, 'reading', false, started, updated, file_id, percentage, device
        FROM (
            SELECT p.user_id, e.work_id, p.file_id, p.updated, p.percentage, p.device,
                   min(p.updated) OVER (PARTITION BY p.user_id, e.work_id) AS started,
                   row_number() OVER (PARTITION BY p.user_id, e.work_id ORDER BY p.updated DESC, p.file_id DESC) AS newest
            FROM positions AS p
            JOIN book_files AS f ON p.file_id = f.id
            JOIN editions AS e ON f.edition_id = e.id
        ) AS latest
        WHERE newest = 1
    """)


def downgrade():
    op.drop_table("collection_items")
    op.drop_index("ix_collections_user_id", table_name="collections")
    op.drop_table("collections")
    op.drop_table("reading_states")

