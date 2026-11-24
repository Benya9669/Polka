"""Keep references to records combined through a catalog merge."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("works", sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("works.id")))
    op.create_index("ix_works_merged_into_id", "works", ["merged_into_id"])


def downgrade():
    op.drop_index("ix_works_merged_into_id", table_name="works")
    op.drop_column("works", "merged_into_id")
