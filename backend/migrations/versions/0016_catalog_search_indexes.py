"""Index substring searches and catalog labels on PostgreSQL."""

from alembic import context, op
from sqlalchemy.exc import DBAPIError

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def add_trigram_indexes():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_works_search_trgm ON works USING gin (search gin_trgm_ops)")
    op.execute("CREATE INDEX ix_author_names_normalized_trgm ON author_names USING gin (normalized gin_trgm_ops)")


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("CREATE INDEX ix_works_genres_jsonb ON works USING gin ((genres::jsonb))")
    op.execute("CREATE INDEX ix_works_tags_jsonb ON works USING gin ((tags::jsonb))")
    if context.is_offline_mode():
        add_trigram_indexes()
        return
    try:
        with op.get_bind().begin_nested():
            add_trigram_indexes()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"42501", "0A000", "58P01"}:
            raise


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        for index in ("ix_works_search_trgm", "ix_author_names_normalized_trgm",
                      "ix_works_genres_jsonb", "ix_works_tags_jsonb"):
            op.execute(f"DROP INDEX IF EXISTS {index}")
