"""Persist scan schedules, file fingerprints and indexed source identity."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("libraries", sa.Column("scan_settings", sa.JSON()))
    op.add_column("libraries", sa.Column("scan_due", sa.DateTime(timezone=True)))
    op.add_column("libraries", sa.Column("scan_owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.create_index("ix_libraries_scan_due", "libraries", ["scan_due"])
    op.add_column("book_files", sa.Column("index_signature", sa.JSON()))
    op.create_table(
        "scan_entries",
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("location", sa.String(64), primary_key=True),
        sa.Column("folder", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("scan_entries")
    op.drop_column("book_files", "index_signature")
    op.drop_index("ix_libraries_scan_due", table_name="libraries")
    op.drop_column("libraries", "scan_owner_id")
    op.drop_column("libraries", "scan_due")
    op.drop_column("libraries", "scan_settings")
