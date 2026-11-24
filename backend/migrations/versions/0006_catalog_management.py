"""Add managed library layouts, trash and editable catalog fields."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("libraries", sa.Column("path_template", sa.Text(), nullable=False, server_default="{author}/{title}.{format}"))
    op.add_column("libraries", sa.Column("active_layout_id", sa.Integer()))
    op.add_column("works", sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("works", sa.Column("custom_fields", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("works", sa.Column("trashed_at", sa.DateTime(timezone=True)))
    op.add_column("editions", sa.Column("metadata_source", sa.String(20), nullable=False, server_default="embedded"))


def downgrade():
    op.drop_column("editions", "metadata_source")
    op.drop_column("works", "trashed_at")
    op.drop_column("works", "custom_fields")
    op.drop_column("works", "tags")
    op.drop_column("libraries", "active_layout_id")
    op.drop_column("libraries", "path_template")

