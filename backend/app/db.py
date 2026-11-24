import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .text_keys import normalized_key

DATA = Path(os.getenv("LIBRARY_PATH", "/data"))
INBOX = Path(os.getenv("IMPORT_PATH", "/inbox"))
ORIGINALS = Path(os.environ["ORIGINALS_PATH"]) if os.getenv("ORIGINALS_PATH") else None
EXPORTS = Path(os.environ["EXPORT_PATH"]) if os.getenv("EXPORT_PATH") else None
MAX_BYTES = int(os.getenv("MAX_BOOK_BYTES", str(100 * 1024 * 1024)))
APP_URL = os.getenv("APP_URL", "http://localhost:8080").rstrip("/")
engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+psycopg://ebook:ebook@db/ebook"), pool_pre_ping=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password: Mapped[str] = mapped_column(Text)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)


class Credential(Base):
    __tablename__ = "credentials"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(12))
    label: Mapped[str] = mapped_column(String(80), default="")
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Library(Base):
    __tablename__ = "libraries"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(12))
    folder: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    formats: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    path_template: Mapped[str] = mapped_column(Text, default="{author}/{title}.{format}")
    active_layout_id: Mapped[int | None] = mapped_column(Integer)
    scan_settings: Mapped[dict | None] = mapped_column(JSON)
    scan_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scan_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ScanEntry(Base):
    __tablename__ = "scan_entries"
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True)
    location: Mapped[str] = mapped_column(String(64), primary_key=True)
    folder: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)


class Work(Base):
    __tablename__ = "works"
    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    title_key: Mapped[str] = mapped_column(String(64), index=True)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    series: Mapped[str] = mapped_column(Text, default="")
    series_index: Mapped[str] = mapped_column(Text, default="")
    genres: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    search: Mapped[str] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), index=True)


@event.listens_for(Work.title, "set")
def sync_title_key(work, title, old_value, initiator):
    work.title_key = normalized_key(title)


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))


class AuthorName(Base):
    __tablename__ = "author_names"
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    normalized: Mapped[str] = mapped_column(Text)


class WorkAuthor(Base):
    __tablename__ = "work_authors"
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    credit: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id", ondelete="CASCADE"), index=True)
    name_key: Mapped[str] = mapped_column(String(64))


class Series(Base):
    __tablename__ = "series"
    __table_args__ = (UniqueConstraint("library_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    path: Mapped[list] = mapped_column(JSON)


class SeriesAncestor(Base):
    __tablename__ = "series_ancestors"
    ancestor_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), primary_key=True)
    descendant_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), primary_key=True, index=True)


class SeriesMembership(Base):
    __tablename__ = "series_memberships"
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), primary_key=True, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    position: Mapped[str] = mapped_column(Text)
    volume_number: Mapped[Decimal | None] = mapped_column(Numeric())


class Edition(Base):
    __tablename__ = "editions"
    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    language: Mapped[str] = mapped_column(Text, default="")
    isbn: Mapped[str] = mapped_column(Text, default="")
    publisher: Mapped[str] = mapped_column(Text, default="")
    year: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    cover: Mapped[str | None] = mapped_column(Text)
    metadata_source: Mapped[str] = mapped_column(String(20), default="embedded")


class BookFile(Base):
    __tablename__ = "book_files"
    __table_args__ = (UniqueConstraint("library_id", "sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id"), index=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(12))
    size: Mapped[int] = mapped_column(Integer)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    derived_from: Mapped[int | None] = mapped_column(ForeignKey("book_files.id"), unique=True)
    provenance: Mapped[dict | None] = mapped_column(JSON)
    index_signature: Mapped[list | None] = mapped_column(JSON)


class Position(Base):
    __tablename__ = "positions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id"), primary_key=True)
    locator: Mapped[str] = mapped_column(Text)
    chapter: Mapped[str | None] = mapped_column(String(500))
    percentage: Mapped[float] = mapped_column(Float)
    device: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PositionEvent(Base):
    __tablename__ = "position_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReadingState(Base):
    __tablename__ = "reading_states"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="unread")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    started: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_file_id: Mapped[int | None] = mapped_column(ForeignKey("book_files.id", ondelete="SET NULL"))
    percentage: Mapped[float | None] = mapped_column(Float)
    device: Mapped[str | None] = mapped_column(String(120))
    chapter: Mapped[str | None] = mapped_column(String(500))


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("user_id", "normalized_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(240))


class CollectionItem(Base):
    __tablename__ = "collection_items"
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)


class KOPosition(Base):
    __tablename__ = "koreader_positions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    document: Mapped[str] = mapped_column(String(32), primary_key=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("book_files.id"), index=True)
    locator: Mapped[str | None] = mapped_column(Text)
    percentage: Mapped[float | None] = mapped_column(Float)
    device: Mapped[str | None] = mapped_column(String(120))
    device_id: Mapped[str | None] = mapped_column(String(120))
    updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KOPositionEvent(Base):
    __tablename__ = "koreader_position_events"
    __table_args__ = (Index("ix_koreader_position_events_document", "user_id", "document", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    document: Mapped[str] = mapped_column(String(32))
    file_id: Mapped[int | None] = mapped_column(ForeignKey("book_files.id", ondelete="SET NULL"))
    locator: Mapped[str] = mapped_column(Text)
    percentage: Mapped[float | None] = mapped_column(Float)
    device: Mapped[str | None] = mapped_column(String(120))
    device_id: Mapped[str | None] = mapped_column(String(120))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SonyPosition(Base):
    __tablename__ = "sony_positions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id", ondelete="CASCADE"), primary_key=True)
    percentage: Mapped[float | None] = mapped_column(Float)
    page: Mapped[int | None] = mapped_column(Integer)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Bookmark(Base):
    __tablename__ = "bookmarks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id"), index=True)
    locator: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(200))


class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id", ondelete="CASCADE"), index=True)
    locator: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReadingSession(Base):
    __tablename__ = "reading_sessions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id", ondelete="CASCADE"), index=True)
    opened: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    sequence: Mapped[int] = mapped_column(Integer, default=0)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    secret: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
