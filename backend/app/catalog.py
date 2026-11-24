import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import cast, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from .auth import Current, DB
from .author_names import catalog_search, linked_author, name_key, sync_author_credits
from .books import MIME, copy_limited, file_path, indexed_source_matches, inspect, normal, require_source_format, search_text, store_book, under
from .conversions import CONVERSION_TARGETS
from .db import (DATA, INBOX, Author, AuthorName, BookFile, Edition, Job, Library, Series, SeriesAncestor,
                 SeriesMembership, User, Work, WorkAuthor, now)
from .job_signals import wake_after_commit
from .merges import move_related_records
from .organization import layout_root, parse_template, plan_layout
from .series_data import in_named_series, legacy_memberships, membership_labels, memberships_for, path_key, set_memberships

router = APIRouter(prefix="/api/v1", tags=["catalog"])


def allowed(user):
    return True if user.admin else or_(Library.owner_id == user.id, Library.shared.is_(True))


def visible_work(user):
    return (allowed(user), Work.trashed_at.is_(None))


def library_or_404(db, user, ident, write=False):
    lib = db.scalar(select(Library).where(Library.id == ident, allowed(user)))
    if not lib or (write and (user.read_only or not (user.admin or lib.owner_id == user.id))):
        raise HTTPException(404, "Библиотека не найдена")
    return lib


def file_or_404(db, user, ident):
    pair = db.execute(select(BookFile, Library).join(Library, Library.id == BookFile.library_id)
                      .join(Edition, Edition.id == BookFile.edition_id).join(Work, Work.id == Edition.work_id)
                      .where(BookFile.id == ident, *visible_work(user))).first()
    if not pair:
        raise HTTPException(404, "Файл не найден")
    return pair


def resolve_work(db, ident, *, for_update=False):
    work = db.get(Work, ident, with_for_update=for_update, populate_existing=for_update)
    while work and work.merged_into_id is not None:
        work = db.get(Work, work.merged_into_id, with_for_update=for_update, populate_existing=for_update)
    return work


def work_or_404(db, user, ident, write=False, include_trash=False, *, follow_merge=False, for_update=False):
    work = (resolve_work(db, ident, for_update=for_update) if follow_merge else
            db.get(Work, ident, with_for_update=for_update, populate_existing=for_update))
    if not work or work.merged_into_id is not None or (work.trashed_at is not None and not include_trash):
        raise HTTPException(404, "Книга не найдена")
    library_or_404(db, user, work.library_id, write)
    return work


def lock_library(db, lib):
    return db.get(Library, lib.id, with_for_update=True, populate_existing=True)


def lock_work(db, user, ident, include_trash=False):
    work = work_or_404(db, user, ident, write=True, include_trash=include_trash)
    lib = lock_library(db, db.get(Library, work.library_id))
    db.refresh(work)
    if work.merged_into_id is not None or (work.trashed_at is not None and not include_trash):
        raise HTTPException(404, "Книга не найдена")
    return work, lib


def serve_book(file, library, inline=False):
    try:
        path = file_path(file, library)
        available = indexed_source_matches(file, path) if file.indexed else path.is_file()
    except (OSError, ValueError):
        raise HTTPException(404, "Файл недоступен") from None
    if not available:
        raise HTTPException(404, "Файл недоступен")
    media = MIME[file.format]
    if inline and file.format not in {"epub", "pdf"}:
        raise HTTPException(415, "Для чтения требуется EPUB или PDF")
    return FileResponse(path, media_type=media, filename=file.name,
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


def serialize_file(file):
    return {key: getattr(file, key) for key in ("id", "edition_id", "name", "format", "size", "sha256", "indexed", "derived_from")}


def serialize_works(db, works):
    if not works:
        return []
    ids = [work.id for work in works]
    memberships = memberships_for(db, ids)
    credits = {}
    for work_id, position, author_id, name in db.execute(select(WorkAuthor.work_id, WorkAuthor.credit, Author.id, Author.name)
            .join(Author, Author.id == WorkAuthor.author_id).where(WorkAuthor.work_id.in_(ids)).order_by(WorkAuthor.credit)):
        credits.setdefault(work_id, {})[position] = {"id": author_id, "name": name}
    editions = list(db.scalars(select(Edition).where(Edition.work_id.in_(ids)).order_by(Edition.id)))
    files = db.scalars(select(BookFile).where(BookFile.edition_id.in_([item.id for item in editions])).order_by(BookFile.id))
    file_map = {}
    for file in files:
        file_map.setdefault(file.edition_id, []).append(serialize_file(file))
    edition_map = {}
    for edition in editions:
        item = {key: getattr(edition, key) for key in ("id", "language", "isbn", "publisher", "year", "summary", "metadata_source")}
        item["cover"] = f"/api/v1/editions/{edition.id}/cover" if edition.cover else None
        item["files"] = file_map.get(edition.id, [])
        edition_map.setdefault(edition.work_id, []).append(item)
    return [{**{key: getattr(work, key) for key in ("id", "title", "authors", "series", "series_index", "genres",
                                                    "tags", "custom_fields", "library_id", "created", "trashed_at")},
             "author_credits": [{"credited_as": name, **credits.get(work.id, {}).get(position, {"id": None, "name": name})}
                                for position, name in enumerate(work.authors)],
             "series_memberships": memberships.get(work.id, []),
             "editions": edition_map.get(work.id, [])}
            for work in works]


BookFormat = Literal["epub", "fb2", "fb2.zip", "pdf", "txt", "mobi", "azw3", "lrf", "djvu", "cbz", "html", "rtf"]
FormatList = Annotated[list[BookFormat], Field(max_length=len(MIME))]


class LibraryInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    mode: Literal["managed", "indexed"]
    folder: str = Field(default="", max_length=500)
    shared: bool = False
    formats: FormatList = Field(default_factory=list)


class LibraryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    description: str | None = Field(default=None, max_length=2000)
    shared: bool | None = None
    formats: FormatList | None = None


class MembershipInput(BaseModel):
    path: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(min_length=1, max_length=16)
    position: str = Field(default="", max_length=40)


class WorkInput(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    authors: list[str] = Field(max_length=30)
    series: str = Field(default="", max_length=500)
    series_index: str = Field(default="", max_length=40)
    series_memberships: list[MembershipInput] | None = Field(default=None, max_length=64)
    genres: list[str] = Field(default_factory=list, max_length=30)
    tags: list[str] | None = Field(default=None, max_length=40)
    custom_fields: dict[str, str] | None = Field(default=None, max_length=20)


class EditionInput(BaseModel):
    language: str | None = Field(default=None, max_length=40)
    isbn: str | None = Field(default=None, max_length=60)
    publisher: str | None = Field(default=None, max_length=500)
    year: str | None = Field(default=None, max_length=25)
    summary: str | None = Field(default=None, max_length=20000)


class BatchInput(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    series: str | None = Field(default=None, max_length=500)
    genres: list[str] | None = Field(default=None, max_length=30)
    add_tags: list[str] = Field(default_factory=list, max_length=40)
    remove_tags: list[str] = Field(default_factory=list, max_length=40)


class MergeInput(BaseModel):
    source_id: int = Field(gt=0)


class MergeConfirmation(MergeInput):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class LayoutInput(BaseModel):
    template: str = Field(min_length=1, max_length=280)


class ScanInput(BaseModel):
    folder: str = Field(default="", max_length=500)


class SkipFile(BaseModel):
    action: Literal["skip"]


class AttachWork(BaseModel):
    action: Literal["work"]
    work_id: int = Field(gt=0)


class AttachIncoming(BaseModel):
    action: Literal["incoming"]
    path: str


class ImportConfirmation(BaseModel):
    choices: dict[str, Annotated[SkipFile | AttachWork | AttachIncoming, Field(discriminator="action")]] = Field(default_factory=dict)


class ScanSettings(ScanInput):
    mode: Literal["off", "preview", "apply"] = "off"
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    watch: bool = False
    formats: FormatList = Field(default_factory=list)


def lib_json(lib):
    return {"id": lib.id, "name": lib.name, "mode": lib.mode, "folder": lib.folder, "description": lib.description,
            "shared": lib.shared, "owner_id": lib.owner_id, "formats": lib.formats, "path_template": lib.path_template,
            "active_layout_id": lib.active_layout_id}


@router.get("/libraries")
def libraries(db: DB, user: Current):
    return [lib_json(lib) for lib in db.scalars(select(Library).where(allowed(user)).order_by(Library.id))]


@router.post("/libraries", status_code=201)
def create_library(payload: LibraryInput, db: DB, user: Current):
    if user.read_only:
        raise HTTPException(403, "Учётная запись имеет доступ только для чтения")
    if not user.admin and (payload.mode == "indexed" or payload.shared):
        raise HTTPException(403, "Требуются права администратора")
    if payload.mode == "indexed":
        try:
            if not under(INBOX, payload.folder).is_dir():
                raise ValueError("Каталог не найден")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    data = payload.model_dump()
    data["formats"] = sorted(set(payload.formats))
    if payload.mode == "managed":
        data["folder"] = ""
    lib = Library(**data, owner_id=user.id)
    db.add(lib)
    db.commit()
    return lib_json(lib)


@router.patch("/libraries/{library_id}")
def edit_library(library_id: int, payload: LibraryUpdate, db: DB, user: Current):
    lib = library_or_404(db, user, library_id, write=True)
    changes = payload.model_dump(exclude_none=True)
    if "shared" in changes and not user.admin:
        raise HTTPException(403, "Требуются права администратора")
    if "formats" in changes:
        changes["formats"] = sorted(set(changes["formats"]))
    for field, value in changes.items():
        setattr(lib, field, value)
    db.commit()
    return lib_json(lib)


@router.get("/books")
def books(db: DB, user: Current, q: Annotated[str, Query(max_length=300)] = "", library_id: int | None = None,
          page: Annotated[int, Query(ge=1, le=10000)] = 1, limit: Annotated[int, Query(ge=1, le=100)] = 30,
          sort: Literal["recent", "title", "oldest"] = "recent",
          series: str | None = None, genre: str | None = None, author: str | None = None, tag: str | None = None):
    query = select(Work).join(Library).where(*visible_work(user))
    if library_id is not None:
        query = query.where(Work.library_id == library_id)
    query = query.where(*catalog_search(q))
    if author is not None:
        query = query.where(linked_author(AuthorName.key == name_key(author)))
    if series is not None:
        query = query.where(in_named_series(series))
    for column, value in ((Work.genres, genre), (Work.tags, tag)):
        if value is not None:
            query = query.where(matches_label(db, column, value))
    order = {"recent": Work.id.desc(), "oldest": Work.id.asc(), "title": Work.title.asc()}[sort]
    rows = list(db.scalars(query.order_by(order, Work.id).offset((page - 1) * limit).limit(limit + 1)))
    return {"items": serialize_works(db, rows[:limit]), "has_next": len(rows) > limit}


@router.get("/books/facets")
def book_facets(db: DB, user: Current, library_id: int | None = None):
    series_query = (select(Series.name).join(SeriesAncestor, SeriesAncestor.ancestor_id == Series.id)
                    .join(SeriesMembership, SeriesMembership.series_id == SeriesAncestor.descendant_id)
                    .join(Work, Work.id == SeriesMembership.work_id).join(Library, Library.id == Work.library_id)
                    .where(*visible_work(user)).distinct())
    if library_id is not None:
        series_query = series_query.where(Work.library_id == library_id)
    return {"authors": sorted(db.scalars(author_facet(user, library_id)), key=normal),
            "series": sorted(db.scalars(series_query), key=normal),
            "genres": label_facet(db, user, Work.genres, library_id),
            "tags": label_facet(db, user, Work.tags, library_id)}


def matches_label(db, column, value):
    if db.bind.dialect.name == "postgresql":
        return cast(column, JSONB).contains([value])
    items = func.json_each(column).table_valued("value")
    return select(1).select_from(items).where(items.c.value == value).exists()


def label_facet(db, user, column, library_id=None):
    if db.bind.dialect.name == "postgresql":
        items = func.jsonb_array_elements_text(cast(column, JSONB)).table_valued("value")
    else:
        items = func.json_each(column).table_valued("value")
    query = (select(items.c.value).select_from(Work).join(Library, Work.library_id == Library.id)
             .join(items, true()).where(*visible_work(user), items.c.value.is_not(None), items.c.value != "").distinct())
    if library_id is not None:
        query = query.where(Work.library_id == library_id)
    return sorted(db.scalars(query), key=normal)


def author_facet(user, library_id=None):
    query = (select(Author.name).join(WorkAuthor, WorkAuthor.author_id == Author.id)
             .join(Work, Work.id == WorkAuthor.work_id).join(Library, Library.id == Work.library_id)
             .where(*visible_work(user)).distinct())
    return query.where(Work.library_id == library_id) if library_id is not None else query


@router.get("/books/trash")
def trash(db: DB, user: Current, page: int = Query(1, ge=1, le=10000)):
    if user.read_only:
        raise HTTPException(403, "Корзина доступна владельцу библиотеки")
    query = select(Work).join(Library).where(Work.trashed_at.is_not(None), Work.merged_into_id.is_(None), Library.mode == "managed")
    if not user.admin:
        query = query.where(Library.owner_id == user.id)
    rows = list(db.scalars(query.order_by(Work.trashed_at.desc(), Work.id.desc()).offset((page - 1) * 30).limit(31)))
    return {"items": serialize_works(db, rows[:30]), "has_next": len(rows) > 30}


def clean_labels(values):
    cleaned = {}
    for value in values:
        title = value.strip()
        key = normal(title)
        if len(title) > 200:
            raise HTTPException(422, "Метка слишком длинная")
        if key:
            cleaned.setdefault(key, title)
    return list(cleaned.values())


def index_work(db, work):
    editions = db.scalars(select(Edition).where(Edition.work_id == work.id))
    work.search = search_text(work, editions, membership_labels(db, work.id))


def update_primary_series(db, work, name, position):
    existing = memberships_for(db, [work.id]).get(work.id, [])
    if work.series == name and existing:
        first = {**existing[0], "position": position}
        set_memberships(db, work, [first, *existing[1:]])
    else:
        set_memberships(db, work, [*legacy_memberships(name, position), *existing[1:]], standalone_position=position)


@router.patch("/books/batch")
def bulk_metadata(payload: BatchInput, db: DB, user: Current):
    works = [work_or_404(db, user, ident, write=True) for ident in dict.fromkeys(payload.ids)]
    for lib_id in sorted({work.library_id for work in works}):
        lock_library(db, db.get(Library, lib_id))
    excluded = {normal(label) for label in clean_labels(payload.remove_tags)}
    for work in works:
        db.refresh(work)
        if work.trashed_at is not None:
            raise HTTPException(409, "Книга перемещена в корзину")
        if payload.series is not None and work.series != payload.series:
            update_primary_series(db, work, payload.series, "")
        if payload.genres is not None:
            work.genres = clean_labels(payload.genres)
        work.tags = [tag for tag in clean_labels([*work.tags, *payload.add_tags]) if normal(tag) not in excluded]
        if len(work.tags) > 40:
            raise HTTPException(422, "У произведения может быть до 40 меток")
        index_work(db, work)
    db.commit()
    return {"updated": len(works)}


@router.get("/books/{work_id}")
def book(work_id: int, db: DB, user: Current):
    return serialize_works(db, [work_or_404(db, user, work_id, follow_merge=True)])[0]


def merge_plan(db, target, source):
    books = serialize_works(db, [target, source])
    material = json.dumps(jsonable_encoder(books), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {"target": books[0], "source": books[1], "revision": hashlib.sha256(material).hexdigest()}


def merge_pair(db, user, target_id, source_id):
    if target_id == source_id:
        raise HTTPException(422, "Выберите другую карточку")
    target = work_or_404(db, user, target_id, write=True)
    source = work_or_404(db, user, source_id, write=True)
    if source.library_id != target.library_id:
        raise HTTPException(422, "Карточки должны быть в одной библиотеке")
    return target, source


@router.post("/books/{work_id}/merge/preview")
def preview_merge(work_id: int, payload: MergeInput, db: DB, user: Current):
    return merge_plan(db, *merge_pair(db, user, work_id, payload.source_id))


@router.post("/books/{work_id}/merge")
def merge_books(work_id: int, payload: MergeConfirmation, db: DB, user: Current):
    if work_id == payload.source_id:
        raise HTTPException(422, "Выберите другую карточку")
    target = work_or_404(db, user, work_id, write=True)
    db.execute(select(User.id).where(User.id == user.id).with_for_update(read=True, key_share=True)).scalar_one()
    lib = lock_library(db, db.get(Library, target.library_id))
    copies = {row.id: row for row in db.scalars(
        select(Work).where(Work.library_id == lib.id, Work.id.in_([work_id, payload.source_id]))
        .order_by(Work.id).with_for_update().execution_options(populate_existing=True))}
    target, source = copies.get(work_id), copies.get(payload.source_id)
    if not target or target.trashed_at is not None or not source:
        raise HTTPException(404, "Карточка недоступна")
    if source.merged_into_id == target.id:
        return serialize_works(db, [target])[0]
    if source.trashed_at is not None or merge_plan(db, target, source)["revision"] != payload.revision:
        raise HTTPException(409, "Карточки изменились. Сформируйте новый предпросмотр.")
    move_related_records(db, target.id, source.id)
    target.tags = clean_labels([*target.tags, *source.tags])
    target.genres = clean_labels([*target.genres, *source.genres])
    target.custom_fields = {**source.custom_fields, **target.custom_fields}
    branches = memberships_for(db, [target.id, source.id])
    if branches.get(source.id):
        set_memberships(db, target, [*branches.get(target.id, []), *branches[source.id]])
    source.merged_into_id = target.id
    source.trashed_at = now()
    index_work(db, target)
    log = queue(db, user, lib, "merge", {"source_id": source.id, "target_id": target.id})
    log.status = "done"
    log.result = {"source_id": source.id, "target_id": target.id, "source_title": source.title, "target_title": target.title}
    db.commit()
    return serialize_works(db, [target])[0]


@router.put("/books/{work_id}")
def edit_book(work_id: int, payload: WorkInput, db: DB, user: Current):
    work, _ = lock_work(db, user, work_id)
    fields = payload.model_dump(exclude_none=True)
    memberships = fields.pop("series_memberships", None)
    series, position = fields.pop("series"), fields.pop("series_index")
    if "series" not in payload.model_fields_set:
        series = work.series
    if "series_index" not in payload.model_fields_set:
        position = work.series_index
    for key in ("authors", "genres", "tags"):
        if key in fields:
            fields[key] = clean_labels(fields[key])
    if fields.get("custom_fields") and any(not key.strip() or len(key) > 100 or len(value) > 2000
                                           for key, value in fields["custom_fields"].items()):
        raise HTTPException(422, "Проверьте названия и значения дополнительных полей")
    for key, value in fields.items():
        setattr(work, key, value)
    if memberships is not None:
        if any(not all(normal(part) and "\0" not in part for part in item["path"]) or "\0" in item["position"] for item in memberships):
            raise HTTPException(422, "Укажите названия всех уровней серии")
        if len({path_key(item["path"]) for item in memberships}) != len(memberships):
            raise HTTPException(422, "У книги может быть один номер тома в каждой серии")
        standalone = position if "series_index" in payload.model_fields_set or not work.series else ""
        set_memberships(db, work, memberships, standalone_position=standalone)
    elif (series, position) != (work.series, work.series_index):
        update_primary_series(db, work, series, position)
    sync_author_credits(db, work)
    index_work(db, work)
    db.commit()
    return serialize_works(db, [work])[0]


@router.patch("/editions/{edition_id}")
def edit_edition(edition_id: int, payload: EditionInput, db: DB, user: Current):
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(404, "Издание не найдено")
    work, _ = lock_work(db, user, edition.work_id)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(edition, key, value.strip())
    edition.metadata_source = "manual"
    index_work(db, work)
    db.commit()
    return serialize_works(db, [work])[0]


@router.post("/books/{work_id}/trash", status_code=204)
@router.post("/books/{work_id}/restore", status_code=204)
def change_trash(work_id: int, db: DB, user: Current, request: Request):
    work, lib = lock_work(db, user, work_id, include_trash=True)
    if lib.mode != "managed":
        raise HTTPException(409, "Корзина доступна для управляемой библиотеки")
    if request.url.path.endswith("/trash"):
        work.trashed_at = work.trashed_at or now()
    else:
        work.trashed_at = None
    db.commit()


@router.get("/files/{file_id}")
def download(file_id: int, db: DB, user: Current, inline: bool = False):
    return serve_book(*file_or_404(db, user, file_id), inline=inline)


@router.get("/files/{file_id}/info")
def file_info(file_id: int, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    edition = db.get(Edition, file.edition_id)
    work = db.get(Work, edition.work_id)
    return {**serialize_file(file), "title": work.title, "work_id": work.id, "provenance": file.provenance}


@router.get("/editions/{edition_id}/cover")
def cover(edition_id: int, db: DB, user: Current, request: Request):
    return serve_cover(db, user, edition_id, request)


def serve_cover(db, user, edition_id, request):
    edition = db.get(Edition, edition_id)
    if not edition or not edition.cover:
        raise HTTPException(404, "Обложка не найдена")
    work_or_404(db, user, edition.work_id)
    path = under(DATA, edition.cover)
    try:
        metadata = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        raise HTTPException(404, "Обложка не найдена") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise HTTPException(404, "Обложка не найдена")
    version = f"{edition.cover}:{metadata.st_mtime_ns}:{metadata.st_size}"
    etag = '"' + hashlib.sha256(version.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Authorization, X-Polka-User"}
    validators = {tag.strip() for value in request.headers.getlist("if-none-match")
                  for tag in value.split(",")}
    if etag in validators or f"W/{etag}" in validators or validators == {"*"}:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers, stat_result=metadata)


def queue(db, user, lib, kind, payload):
    task = Job(owner_id=user.id, library_id=lib.id if lib else None, kind=kind, payload=payload)
    db.add(task)
    db.flush()
    wake_after_commit(db, task)
    return task


def managed_library(db, user, ident):
    lib = library_or_404(db, user, ident, write=True)
    if lib.mode != "managed":
        raise HTTPException(409, "Раскладка доступна для управляемой библиотеки")
    return lib


def layout_job(db, lib, ident, kind):
    task = db.get(Job, ident)
    if not task or task.library_id != lib.id or task.kind != kind or task.status != "done":
        raise HTTPException(404, "План раскладки не найден")
    return task


def layout_snapshots(db, lib):
    return {task.id: task for task in db.scalars(
        select(Job).filter_by(library_id=lib.id, kind="layout", status="done").order_by(Job.id.desc())
    )}


def protected_layouts(lib, snapshots):
    protected = set()
    current = lib.active_layout_id
    while current is not None:
        if current in protected or current not in snapshots or "previous" not in snapshots[current].result:
            raise HTTPException(409, "История раскладки повреждена")
        protected.add(current)
        current = snapshots[current].result["previous"]
    return protected


@router.get("/libraries/{library_id}/layout")
def library_layout(library_id: int, db: DB, user: Current):
    lib = managed_library(db, user, library_id)
    snapshots = layout_snapshots(db, lib)
    protected = protected_layouts(lib, snapshots)
    active = snapshots.get(lib.active_layout_id)
    removable = []
    for ident, task in snapshots.items():
        if ident in protected:
            continue
        try:
            if layout_root(lib.id, ident).is_dir():
                removable.append({"id": ident, "files": task.result["files"], "location": task.result["location"]})
        except ValueError:
            continue
    last = db.scalar(select(Job).filter_by(library_id=lib.id, kind="layout_plan").order_by(Job.id.desc()).limit(1))
    pending_id = last.result.get("confirmed") if last else None
    current = db.get(Job, pending_id) if pending_id is not None else None
    return {"template": lib.path_template, "active": job_json(active) if active else None,
            "last_plan": last.id if last else None, "job": job_json(current) if current else None,
            "removable_snapshots": removable}


@router.put("/libraries/{library_id}/layout")
def save_layout(library_id: int, payload: LayoutInput, db: DB, user: Current):
    lib = lock_library(db, managed_library(db, user, library_id))
    try:
        parse_template(payload.template)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    lib.path_template = payload.template
    db.commit()
    return {"template": lib.path_template}


@router.post("/libraries/{library_id}/layout/plans", status_code=201)
def preview_layout(library_id: int, db: DB, user: Current):
    lib = lock_library(db, managed_library(db, user, library_id))
    items = plan_layout(db, lib)
    if not items:
        raise HTTPException(409, "В библиотеке пока нет файлов для раскладки")
    task = queue(db, user, lib, "layout_plan", {})
    task.status = "done"
    task.result = {"template": lib.path_template, "items": items}
    db.commit()
    return {"id": task.id, "files": len(items)}


@router.get("/libraries/{library_id}/layout/plans/{plan_id}")
def get_layout_plan(library_id: int, plan_id: int, db: DB, user: Current, page: int = Query(1, ge=1)):
    lib = managed_library(db, user, library_id)
    task = layout_job(db, lib, plan_id, "layout_plan")
    items = task.result["items"]
    return {"id": task.id, "template": task.result["template"], "files": len(items),
            "job": task.result.get("confirmed"), "items": items[(page - 1) * 30:page * 30],
            "has_next": len(items) > page * 30}


@router.post("/libraries/{library_id}/layout/plans/{plan_id}/apply", status_code=202)
def apply_layout(library_id: int, plan_id: int, db: DB, user: Current):
    lib = lock_library(db, managed_library(db, user, library_id))
    plan = layout_job(db, lib, plan_id, "layout_plan")
    if confirmed := plan.result.get("confirmed"):
        task = db.get(Job, confirmed)
        if task.status != "error":
            return job_json(task)
    task = queue(db, user, lib, "layout", {"preview_id": plan.id})
    plan.result = {**plan.result, "confirmed": task.id}
    db.commit()
    return job_json(task)


@router.post("/libraries/{library_id}/layout/undo", status_code=204)
def undo_layout(library_id: int, db: DB, user: Current, expected: int):
    lib = lock_library(db, managed_library(db, user, library_id))
    if lib.active_layout_id != expected:
        raise HTTPException(409, "Активная раскладка уже изменилась")
    task = layout_job(db, lib, expected, "layout")
    previous = task.result["previous"]
    if previous is not None:
        layout_job(db, lib, previous, "layout")
    action = queue(db, user, lib, "layout_undo", {"layout_id": expected})
    action.status, action.result = "done", {"active_layout_id": previous}
    lib.active_layout_id = previous
    db.commit()


@router.delete("/libraries/{library_id}/layout/snapshots/{snapshot_id}", status_code=204)
def delete_layout_snapshot(library_id: int, snapshot_id: int, db: DB, user: Current):
    lib = lock_library(db, managed_library(db, user, library_id))
    snapshots = layout_snapshots(db, lib)
    if snapshot_id not in snapshots:
        raise HTTPException(404, "Снимок раскладки не найден")
    if snapshot_id in protected_layouts(lib, snapshots):
        raise HTTPException(409, "Снимок нужен для текущей раскладки или её отмены")
    try:
        directory = layout_root(lib.id, snapshot_id)
        if directory.exists():
            shutil.rmtree(directory)
    except ValueError:
        raise HTTPException(409, "Недопустимый путь к снимку раскладки") from None
    except OSError:
        raise HTTPException(500, "Не удалось удалить снимок раскладки") from None
    db.commit()


@router.get("/libraries/{library_id}/layout/manifest")
def layout_manifest(library_id: int, db: DB, user: Current):
    lib = managed_library(db, user, library_id)
    if lib.active_layout_id is None:
        raise HTTPException(404, "Раскладка ещё не создана")
    manifest = under(layout_root(lib.id, lib.active_layout_id), "manifest.json")
    if not manifest.is_file():
        raise HTTPException(404, "Манифест недоступен")
    return FileResponse(manifest, media_type="application/json", filename="manifest.json",
                        headers={"Cache-Control": "private, no-store"})


def job_json(task):
    summary = {key: getattr(task, key) for key in ("id", "kind", "status", "library_id", "result", "error", "created", "attempt")}
    if task.kind == "layout_plan" and task.result:
        summary["result"] = {key: value for key, value in task.result.items() if key != "items"}
    return summary


@router.post("/libraries/{library_id}/upload", status_code=201)
def upload(library_id: int, db: DB, user: Current, file: Annotated[UploadFile, File()], review_lrf: bool = False):
    lib = library_or_404(db, user, library_id, write=True)
    return save_upload(db, user, lib, file, review_lrf=review_lrf)


@router.post("/books/{work_id}/editions/upload", status_code=201)
def upload_edition(work_id: int, db: DB, user: Current, file: Annotated[UploadFile, File()], edition_id: int | None = None):
    work = work_or_404(db, user, work_id, write=True)
    lib = library_or_404(db, user, work.library_id, write=True)
    return save_upload(db, user, lib, file, into_work_id=work.id, into_edition_id=edition_id)


def save_upload(db, user, lib, file, *, into_work_id=None, into_edition_id=None, review_lrf=False):
    if lib.mode != "managed":
        raise HTTPException(409, "Выберите управляемую библиотеку")
    name = (file.filename or "Книга").replace("\\", "/").rsplit("/", 1)[-1][:240]
    if not name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise HTTPException(422, "Недопустимое имя файла")
    fd, temporary = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as target:
            checksum, _ = copy_limited(file.file, target)
        path = Path(temporary)
        inspected = None
        if review_lrf:
            with path.open("rb") as source:
                is_lrf = source.read(6) == b"L\x00R\x00F\x00"
            if is_lrf:
                from .worker import possible_matches
                lib = lock_library(db, lib)
                require_source_format(lib, "lrf")
                if not db.scalar(select(BookFile.id).filter_by(library_id=lib.id, sha256=checksum)):
                    inspected = inspect(path, name)
                    matches = possible_matches(db, lib, inspected[1], "lrf")
                    if matches:
                        return JSONResponse({"matches": matches})
        stored, duplicate = store_book(db, lib, path, name, inspected=inspected,
                                        into_work_id=into_work_id, into_edition_id=into_edition_id)
        if not duplicate and stored.format in {"fb2", "fb2.zip"}:
            queue(db, user, lib, "conversion", {"file_id": stored.id})
        db.commit()
        return {"file": serialize_file(stored), "duplicate": duplicate}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    finally:
        Path(temporary).unlink(missing_ok=True)


@router.post("/libraries/{library_id}/scan", status_code=202)
def scan(library_id: int, payload: ScanInput, db: DB, user: Current):
    lib = library_or_404(db, user, library_id, write=True)
    if not user.admin:
        raise HTTPException(403, "Сканирование доступно администратору")
    relative = lib.folder if lib.mode == "indexed" else payload.folder
    try:
        if not under(INBOX, relative).is_dir():
            raise ValueError("Каталог импорта не найден")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    task = queue(db, user, lib, "scan", {"folder": relative})
    db.commit()
    return job_json(task)


def scan_settings_json(lib):
    return {**ScanSettings.model_validate(lib.scan_settings or {}).model_dump(), "next_scan": lib.scan_due}


@router.get("/libraries/{library_id}/scan-settings")
def get_scan_settings(library_id: int, db: DB, user: Current):
    lib = library_or_404(db, user, library_id, write=True)
    if not user.admin:
        raise HTTPException(403, "Сканирование доступно администратору")
    return scan_settings_json(lib)


@router.put("/libraries/{library_id}/scan-settings")
def set_scan_settings(library_id: int, payload: ScanSettings, db: DB, user: Current):
    lib = lock_library(db, library_or_404(db, user, library_id, write=True))
    if not user.admin:
        raise HTTPException(403, "Сканирование доступно администратору")
    folder = lib.folder if lib.mode == "indexed" else payload.folder
    if payload.mode != "off":
        try:
            if not under(INBOX, folder).is_dir():
                raise ValueError("Каталог импорта не найден")
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
    lib.scan_settings = {**payload.model_dump(), "folder": folder, "formats": sorted(set(payload.formats)),
                         "token": uuid4().hex}
    lib.scan_owner_id = user.id if payload.mode != "off" else None
    lib.scan_due = now() + timedelta(minutes=payload.interval_minutes) if lib.scan_owner_id else None
    db.commit()
    return scan_settings_json(lib)


@router.post("/imports/{task_id}/confirm", status_code=202)
def confirm(task_id: int, db: DB, user: Current, payload: ImportConfirmation | None = None):
    if not user.admin or user.read_only:
        raise HTTPException(403, "Импорт из каталога доступен администратору")
    preview = db.execute(select(Job).where(Job.id == task_id).with_for_update()).scalar_one_or_none()
    if not preview or preview.owner_id != user.id or preview.kind != "scan" or preview.status != "done":
        raise HTTPException(404, "План импорта не найден")
    choices = payload.model_dump()["choices"] if payload else {}
    if preview.result.get("confirmed"):
        existing = db.get(Job, preview.result["confirmed"])
        if payload is not None and choices != existing.payload.get("choices", {}):
            raise HTTPException(409, "План уже подтверждён с другими решениями")
        return job_json(existing)
    lib = library_or_404(db, user, preview.library_id, write=True)
    items = {item["path"]: item for item in preview.result["items"]}
    for path, choice in choices.items():
        item = items.get(path)
        if item is None:
            raise HTTPException(422, "Файл отсутствует в плане")
        if choice["action"] == "skip":
            continue
        if item["duplicate"]:
            raise HTTPException(422, "Точный дубликат уже учтён в плане")
        if choice["action"] == "work" and choice["work_id"] not in item.get("work_candidates", []):
            raise HTTPException(422, "Карточка отсутствует среди совпадений")
        if choice["action"] == "incoming":
            target = choice["path"]
            if target not in {row["path"] for row in item.get("incoming", [])} or choices.get(target, {}).get("action") == "skip":
                raise HTTPException(422, "Выберите основной файл из плана, который будет импортирован")
    task = queue(db, user, lib, "import", {"preview_id": preview.id, "choices": choices})
    preview.result = {**preview.result, "confirmed": task.id}
    db.commit()
    return job_json(task)


@router.get("/imports")
def operations(db: DB, user: Current):
    return [job_json(j) for j in db.scalars(select(Job).filter_by(owner_id=user.id).order_by(Job.id.desc()).limit(100))]


@router.post("/imports/{task_id}/undo", status_code=204)
def undo_import(task_id: int, db: DB, user: Current):
    task = db.get(Job, task_id)
    if not task or task.owner_id != user.id or task.kind != "import" or task.status != "done":
        raise HTTPException(404, "Импорт не найден")
    lib = lock_library(db, managed_library(db, user, task.library_id))
    db.refresh(task, with_for_update=True)
    if task.result.get("undo_id"):
        return
    if not task.result.get("work_ids"):
        raise HTTPException(409, "У этого импорта нет новых записей для отмены")
    ids = task.result["work_ids"]
    merged = db.scalar(select(Work.id).where(Work.library_id == lib.id, or_(Work.merged_into_id.in_(ids),
                       Work.id.in_(ids) & Work.merged_into_id.is_not(None))).limit(1))
    if merged is not None:
        raise HTTPException(409, "В этом импорте есть объединённые карточки")
    imported = set(task.result.get("file_ids", []))
    if imported:
        files = db.execute(select(BookFile.id, BookFile.derived_from).join(Edition, Edition.id == BookFile.edition_id)
                           .where(Edition.work_id.in_(ids)))
        if any(file_id not in imported and original_id not in imported for file_id, original_id in files):
            raise HTTPException(409, "В карточки этого импорта позже добавили другие файлы")
    rows = db.scalars(select(Work).where(Work.library_id == lib.id, Work.id.in_(ids), Work.trashed_at.is_(None)))
    stamp = now()
    moved = 0
    for work in rows:
        work.trashed_at = stamp
        moved += 1
    action = queue(db, user, lib, "import_undo", {"import_id": task.id})
    action.status, action.result = "done", {"trashed": moved}
    task.result = {**task.result, "undo_id": action.id}
    db.commit()


@router.post("/conversions/{file_id}", status_code=202)
def convert(file_id: int, db: DB, user: Current):
    file, lib = file_or_404(db, user, file_id)
    library_or_404(db, user, lib.id, write=True)
    if file.format not in CONVERSION_TARGETS:
        raise HTTPException(422, "Для этого формата конвертация недоступна")
    task = queue(db, user, lib, "conversion", {"file_id": file.id})
    db.commit()
    return job_json(task)

