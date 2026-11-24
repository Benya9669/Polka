"""Allow standalone OPDS jobs without a target library."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("jobs", "library_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    op.execute(sa.text("DELETE FROM jobs WHERE library_id IS NULL AND kind = 'download'"))
    op.alter_column("jobs", "library_id", existing_type=sa.Integer(), nullable=False)
