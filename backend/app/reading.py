from datetime import timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import Current, DB
from .catalog import file_or_404, visible_work
from .db import (Annotation, BookFile, Bookmark, Edition, KOPosition, Library, Position, PositionEvent,
                 ReadingSession, ReadingState, SonyPosition, User, Work, now)
from .personal import lock_reader, record_reading

router = APIRouter(prefix="/api/v1/reading", tags=["reading"])


class PositionInput(BaseModel):
    locator: str = Field(min_length=1, max_length=4000)
    percentage: float = Field(ge=0, le=1)
    chapter: str | None = Field(default=None, max_length=500)
    device: str = Field(default="Браузер", max_length=80)
    version: int = Field(ge=0)


class BookmarkInput(BaseModel):
    locator: str = Field(min_length=1, max_length=4000)
    label: str = Field(min_length=1, max_length=200)


def position_json(value, fmt):
    if not value:
        return {"version": 0}
    return {"version": value.version, "locator": value.locator, "percentage": value.percentage, "chapter": value.chapter,
            "locator_type": "epub_cfi" if fmt == "epub" else "pdf_page",
            "device": value.device, "updated": value.updated.isoformat()}


def valid_position(fmt, locator):
    if fmt == "epub" and locator.startswith("epubcfi(") and locator.endswith(")"):
        return
    if fmt == "pdf" and locator.isascii() and locator.isdecimal() and 0 < int(locator) < 1000000:
        return
    raise HTTPException(422, "Недопустимая позиция для этого формата")


@router.get("/progress/{file_id}")
def get_progress(file_id: int, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    position = db.get(Position, (user.id, file_id))
    result = position_json(position, file.format)
    native = db.scalar(select(KOPosition).where(KOPosition.user_id == user.id, KOPosition.file_id.in_([file_id, file.derived_from]),
                                              KOPosition.updated.is_not(None)).order_by(KOPosition.updated.desc()).limit(1))
    sony = db.scalar(select(SonyPosition).where(SonyPosition.user_id == user.id, SonyPosition.file_id.in_([file_id, file.derived_from]))
                     .order_by(SonyPosition.updated.desc()).limit(1))
    edition = db.get(Edition, file.edition_id)
    state = db.get(ReadingState, (user.id, edition.work_id))
    if (file.format in {"epub", "pdf"} and state and state.percentage is not None and state.last_file_id != file_id
            and state.last_read and all(state.last_read > candidate.updated for candidate in (position, native, sony) if candidate)):
        source = db.scalar(select(BookFile).join(Edition).where(
            BookFile.id == state.last_file_id, Edition.work_id == edition.work_id))
        if source:
            result["alternate"] = {"file_id": source.id, "format": source.format, "percentage": state.percentage,
                                   "updated": state.last_read.isoformat()}
            return result
    if sony and (not position or sony.updated > position.updated) and (not native or sony.updated > native.updated):
        result["sony"] = {"percentage": sony.percentage, "page": sony.page, "finished": sony.finished,
                          "exact_page": file.format == "pdf" and file.id == sony.file_id}
        return result
    if native and (not position or native.updated > position.updated):
        result["koreader"] = {"percentage": native.percentage, "device": native.device, "locator": native.locator}
    return result


@router.put("/progress/{file_id}")
def put_progress(file_id: int, payload: PositionInput, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    valid_position(file.format, payload.locator)
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    value = db.get(Position, (user.id, file_id))
    version = value.version if value else 0
    if payload.version != version:
        if value and value.locator == payload.locator:
            return position_json(value, file.format)
        return JSONResponse({"detail": "На другом устройстве сохранена другая позиция", "current": position_json(value, file.format)},
                            status_code=409)
    if value is None:
        value = Position(user_id=user.id, file_id=file_id)
        db.add(value)
    if value.locator != payload.locator or "chapter" in payload.model_fields_set:
        chapter = " ".join((payload.chapter or "").replace("\0", "").split())
        value.chapter = chapter or None if file.format == "epub" else None
    value.version, value.locator, value.percentage = version + 1, payload.locator, payload.percentage
    value.device, value.updated = payload.device, now()
    response = position_json(value, file.format)
    db.add(PositionEvent(user_id=user.id, file_id=file_id, payload=response))
    record_reading(db, user.id, file, value.updated, value.percentage, value.device, value.chapter)
    db.commit()
    return response


@router.get("/history")
def history(db: DB, user: Current):
    events = db.scalars(select(PositionEvent).join(BookFile).join(Edition).join(Work).join(Library)
                        .where(PositionEvent.user_id == user.id, *visible_work(user))
                        .order_by(PositionEvent.id.desc()).limit(50))
    return [{"file_id": e.file_id, **e.payload} for e in events]


def bookmark_json(row):
    return {"id": row.id, "locator": row.locator, "label": row.label}


@router.get("/bookmarks/{file_id}")
def bookmarks(file_id: int, db: DB, user: Current):
    file_or_404(db, user, file_id)
    return [bookmark_json(row) for row in db.scalars(select(Bookmark).filter_by(user_id=user.id, file_id=file_id))]


@router.post("/bookmarks/{file_id}", status_code=201)
def add_bookmark(file_id: int, payload: BookmarkInput, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    valid_position(file.format, payload.locator)
    row = Bookmark(user_id=user.id, file_id=file_id, **payload.model_dump())
    db.add(row)
    db.commit()
    return bookmark_json(row)


@router.delete("/bookmarks/{file_id}/{bookmark_id}", status_code=204)
def delete_bookmark(file_id: int, bookmark_id: int, db: DB, user: Current):
    file_or_404(db, user, file_id)
    row = db.get(Bookmark, bookmark_id)
    if not row or row.user_id != user.id or row.file_id != file_id:
        raise HTTPException(404, "Закладка не найдена")
    db.delete(row)
    db.commit()


class AnnotationInput(BaseModel):
    locator: str = Field(min_length=1, max_length=4000)
    quote: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=10000)


class NoteInput(BaseModel):
    note: str = Field(max_length=10000)


def annotation_json(row):
    return {key: getattr(row, key) for key in ("id", "locator", "quote", "note", "created", "updated")}


@router.get("/annotations/{file_id}")
def annotations(file_id: int, db: DB, user: Current):
    file_or_404(db, user, file_id)
    rows = db.scalars(select(Annotation).filter_by(user_id=user.id, file_id=file_id).order_by(Annotation.id.desc()))
    return [annotation_json(row) for row in rows]


@router.post("/annotations/{file_id}", status_code=201)
def add_annotation(file_id: int, payload: AnnotationInput, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    valid_position(file.format, payload.locator)
    quote, note = payload.quote.strip(), payload.note.strip()
    if not quote and not note:
        raise HTTPException(422, "Добавьте текст заметки или выделение")
    if quote and file.format != "epub":
        raise HTTPException(422, "Выделение текста доступно для EPUB")
    lock_reader(db, user)
    exists = db.scalar(select(Annotation).filter_by(user_id=user.id, file_id=file_id, locator=payload.locator, quote=quote, note=note))
    if exists:
        return annotation_json(exists)
    row = Annotation(user_id=user.id, file_id=file_id, locator=payload.locator, quote=quote, note=note)
    db.add(row)
    db.commit()
    return annotation_json(row)


def annotation_or_404(db, user, file_id, annotation_id):
    file_or_404(db, user, file_id)
    row = db.scalar(select(Annotation).filter_by(id=annotation_id, user_id=user.id, file_id=file_id))
    if not row:
        raise HTTPException(404, "Заметка не найдена")
    return row


@router.patch("/annotations/{file_id}/{annotation_id}")
def edit_annotation(file_id: int, annotation_id: int, payload: NoteInput, db: DB, user: Current):
    row = annotation_or_404(db, user, file_id, annotation_id)
    note = payload.note.strip()
    if not note and not row.quote:
        raise HTTPException(422, "Введите текст заметки")
    row.note, row.updated = note, now()
    db.commit()
    return annotation_json(row)


@router.delete("/annotations/{file_id}/{annotation_id}", status_code=204)
def delete_annotation(file_id: int, annotation_id: int, db: DB, user: Current):
    row = annotation_or_404(db, user, file_id, annotation_id)
    db.delete(row)
    db.commit()


class SessionInput(BaseModel):
    session_id: str = Field(pattern="^[a-f0-9]{32}$")
    sequence: int = Field(ge=0, le=1_000_000)
    active_seconds: int = Field(ge=0, le=45)


@router.put("/sessions/{file_id}", status_code=204)
def track_session(file_id: int, payload: SessionInput, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    if file.format not in {"epub", "pdf"}:
        raise HTTPException(422, "Сеансы доступны для EPUB и PDF")
    lock_reader(db, user)
    row = db.get(ReadingSession, (user.id, payload.session_id))
    stamp = now()
    if row is None:
        db.add(ReadingSession(user_id=user.id, session_id=payload.session_id, file_id=file_id,
                              opened=stamp, updated=stamp, seconds=0, sequence=payload.sequence))
    elif row.file_id != file_id:
        raise HTTPException(409, "Сеанс относится к другой книге")
    elif payload.sequence > row.sequence:
        server_seconds = max(0, round((stamp - row.updated.replace(tzinfo=timezone.utc)).total_seconds()))
        row.seconds += min(payload.active_seconds, server_seconds)
        row.updated, row.sequence = stamp, payload.sequence
    db.commit()


@router.get("/sessions")
def sessions(db: DB, user: Current, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1)):
    records = db.execute(select(ReadingSession, Work).join(BookFile, BookFile.id == ReadingSession.file_id)
                         .join(Edition, Edition.id == BookFile.edition_id).join(Work, Work.id == Edition.work_id)
                         .join(Library, Library.id == Work.library_id)
                         .where(ReadingSession.user_id == user.id, *visible_work(user))
                         .order_by(ReadingSession.opened.desc(), ReadingSession.session_id.desc()).offset(offset).limit(limit))
    return [{"file_id": row.file_id, "work_id": work.id, "title": work.title, "opened": row.opened,
             "updated": row.updated, "seconds": row.seconds} for row, work in records]

