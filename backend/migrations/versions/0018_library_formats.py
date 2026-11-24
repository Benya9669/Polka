"""Configure the source formats accepted by each library."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("libraries", sa.Column("formats", sa.JSON, nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("libraries", "formats")
