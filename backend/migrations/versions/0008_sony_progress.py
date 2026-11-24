"""Store manually reported Sony reading locations."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sony_positions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("book_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("percentage", sa.Float()),
        sa.Column("page", sa.Integer()),
        sa.Column("finished", sa.Boolean(), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("sony_positions")

