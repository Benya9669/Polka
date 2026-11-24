"""Preserve the original locator and device for each KOReader update."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "koreader_position_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", sa.String(32), nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("book_files.id", ondelete="SET NULL")),
        sa.Column("locator", sa.Text, nullable=False),
        sa.Column("percentage", sa.Float),
        sa.Column("device", sa.String(120)),
        sa.Column("device_id", sa.String(120)),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        INSERT INTO koreader_position_events (user_id, document, file_id, locator, percentage, device, device_id, updated)
        SELECT user_id, document, file_id, locator, percentage, device, device_id, updated
        FROM koreader_positions WHERE locator IS NOT NULL AND updated IS NOT NULL
    """)
    op.create_index("ix_koreader_position_events_document", "koreader_position_events", ["user_id", "document", "id"])


def downgrade():
    op.drop_table("koreader_position_events")
