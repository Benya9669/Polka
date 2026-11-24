"""Persist EPUB chapter labels with saved reading positions."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("positions", "reading_states"):
        op.add_column(table, sa.Column("chapter", sa.String(500)))


def downgrade():
    for table in ("reading_states", "positions"):
        op.drop_column(table, "chapter")
