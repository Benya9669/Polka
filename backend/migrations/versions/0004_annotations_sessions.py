"""Persist annotations and browser reading sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("book_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_annotations_user_id", "annotations", ["user_id"])
    op.create_index("ix_annotations_file_id", "annotations", ["file_id"])
    op.create_table(
        "reading_sessions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(32), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("book_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opened", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
    )
    op.create_index("ix_reading_sessions_file_id", "reading_sessions", ["file_id"])


def downgrade():
    op.drop_index("ix_reading_sessions_file_id", table_name="reading_sessions")
    op.drop_table("reading_sessions")
    op.drop_index("ix_annotations_file_id", table_name="annotations")
    op.drop_index("ix_annotations_user_id", table_name="annotations")
    op.drop_table("annotations")

