"""Store original KOReader locators and explicit document bindings."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "koreader_positions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("document", sa.String(32), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("book_files.id"), nullable=True),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("percentage", sa.Float(), nullable=True),
        sa.Column("device", sa.String(120), nullable=True),
        sa.Column("device_id", sa.String(120), nullable=True),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_koreader_positions_file_id", "koreader_positions", ["file_id"])


def downgrade():
    op.drop_index("ix_koreader_positions_file_id", table_name="koreader_positions")
    op.drop_table("koreader_positions")

