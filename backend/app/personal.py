import json
from datetime import timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .auth import Current, DB
from .books import normal
from .catalog import allowed, resolve_work, serialize_works, visible_work, work_or_404
from .db import (
    Annotation, BookFile, Bookmark, Collection, CollectionItem, Edition, KOPosition, KOPositionEvent, Library, Position,
    PositionEvent, ReadingSession, ReadingState, SonyPosition, User, Work, now,
)

router = APIRouter(prefix="/api/v1", tags=["personal library"])
Status = Literal["unread", "planned", "reading", "paused", "finished", "abandoned", "rereading"]
IN_PROGRESS = ("reading", "rereading")


def lock_reader(db, user):
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()


def state_json(row):
    if not row:
        return {"status": "unread", "favorite": False, "started": None, "finished": None,
                "last_read": None, "last_file_id": None, "percentage": None, "device": None, "chapter": None}
    return {key: getattr(row, key) for key in ("status", "favorite", "started", "finished", "last_read",
                                               "last_file_id", "percentage", "device", "chapter")}


def state_for(db, user_id, work_id):
    row = db.get(ReadingState, (user_id, work_id))
    if row is None:
        row = ReadingState(user_id=user_id, work_id=work_id, status="unread", favorite=False)
        db.add(row)
    return row


def record_reading(db, user_id, file, stamp, percentage, device, chapter=None):
    edition = db.get(Edition, file.edition_id)
    work = resolve_work(db, edition.work_id, for_update=True)
    row = state_for(db, user_id, work.id)
    if row.last_read is not None and row.last_read.replace(tzinfo=timezone.utc) > stamp.replace(tzinfo=timezone.utc):
        return row
    row.started = row.started or stamp
    row.last_read, row.last_file_id, row.percentage, row.device = stamp, file.id, percentage, device
    row.chapter = chapter
    if row.status in ("unread", "planned"):
        row.status = "reading"
    return row


class StateInput(BaseModel):
    status: Status | None = None
    favorite: bool | None = None


@router.get("/reading/states/{work_id}")
def get_state(work_id: int, db: DB, user: Current):
    work = work_or_404(db, user, work_id, follow_merge=True)
    return state_json(db.get(ReadingState, (user.id, work.id)))


@router.patch("/reading/states/{work_id}")
def change_state(work_id: int, payload: StateInput, db: DB, user: Current):
    lock_reader(db, user)
    work = work_or_404(db, user, work_id, follow_merge=True, for_update=True)
    state = state_for(db, user.id, work.id)
    if payload.favorite is not None:
        state.favorite = payload.favorite
    if payload.status is not None and state.status != payload.status:
        stamp = now()
        if payload.status in (*IN_PROGRESS, "finished"):
            state.started = stamp if payload.status == "rereading" else state.started or stamp
        if payload.status == "finished":
            state.finished = stamp
        state.status = payload.status
    db.commit()
    return state_json(state)


def visible_books(user):
    return select(Work).join(Library).where(*visible_work(user))


def state_books(db, user, query):
    rows = db.execute(query.add_columns(ReadingState).outerjoin(
        ReadingState, (ReadingState.work_id == Work.id) & (ReadingState.user_id == user.id)
    )).all()
    books = serialize_works(db, [work for work, _ in rows])
    return [{"book": book, "state": state_json(row)} for book, (_, row) in zip(books, rows)]


@router.get("/reading/shelf")
def shelf(db: DB, user: Current, view: Literal["favorites", "planned", "reading", "finished"] = "reading",
          page: int = Query(1, ge=1, le=10000)):
    conditions = {
        "favorites": ReadingState.favorite.is_(True),
        "planned": ReadingState.status == "planned",
        "reading": ReadingState.status.in_(IN_PROGRESS),
        "finished": ReadingState.status == "finished",
    }
    query = visible_books(user).where(conditions[view]).order_by(ReadingState.last_read.desc().nulls_last(), Work.id.desc())
    result = state_books(db, user, query.offset((page - 1) * 30).limit(31))
    return {"items": result[:30], "has_next": len(result) > 30}


@router.get("/reading/home")
def home(db: DB, user: Current):
    stamp = now()
    month_start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    state_query = select(ReadingState).join(Work).join(Library).where(*visible_work(user), ReadingState.user_id == user.id)

    def count(*criteria):
        return db.scalar(state_query.with_only_columns(func.count()).where(*criteria))

    return {
        "reading": state_books(db, user, visible_books(user).where(
            ReadingState.status.in_(IN_PROGRESS), ReadingState.last_file_id.is_not(None)
        ).order_by(ReadingState.last_read.desc()).limit(6)),
        "recent": serialize_works(db, list(db.scalars(visible_books(user).order_by(Work.id.desc()).limit(8)))),
        "statistics": {
            "finished": count(ReadingState.status == "finished"),
            "reading": count(ReadingState.status.in_(IN_PROGRESS)),
            "finished_this_month": count(ReadingState.finished >= month_start),
            "seconds": db.scalar(select(func.coalesce(func.sum(ReadingSession.seconds), 0)).join(
                BookFile, ReadingSession.file_id == BookFile.id
            ).join(Library, Library.id == BookFile.library_id).where(ReadingSession.user_id == user.id, allowed(user))),
        },
    }


class CollectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def find_collection(db, user, ident):
    row = db.get(Collection, ident)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "Коллекция не найдена")
    return row


def named_collection(db, user, payload, row=None):
    name = payload.name.strip()
    key = normal(name)
    if not key or len(key) > 240:
        raise HTTPException(422, "Укажите название коллекции")
    existing = db.scalar(select(Collection).filter_by(user_id=user.id, normalized_name=key))
    if existing and (row is None or row.id != existing.id):
        raise HTTPException(409, "Коллекция с этим названием уже существует")
    if row is None:
        row = Collection(user_id=user.id)
        db.add(row)
    row.name, row.normalized_name = name, key
    db.commit()
    return collection_json(row)


def collection_json(row):
    return {"id": row.id, "name": row.name}


@router.get("/collections")
def collections(db: DB, user: Current, work_id: int | None = None):
    if work_id is not None:
        work_id = work_or_404(db, user, work_id, follow_merge=True).id
    rows = db.execute(
        select(Collection, CollectionItem.work_id).filter(Collection.user_id == user.id)
        .outerjoin(CollectionItem, (CollectionItem.collection_id == Collection.id) & (CollectionItem.work_id == work_id))
        .order_by(Collection.name, Collection.id)
    )
    return [{**collection_json(row), "contains": item is not None} for row, item in rows]


@router.post("/collections", status_code=201)
def create_collection(payload: CollectionInput, db: DB, user: Current):
    lock_reader(db, user)
    return named_collection(db, user, payload)


@router.patch("/collections/{collection_id}")
def rename_collection(collection_id: int, payload: CollectionInput, db: DB, user: Current):
    lock_reader(db, user)
    return named_collection(db, user, payload, find_collection(db, user, collection_id))


@router.delete("/collections/{collection_id}", status_code=204)
def remove_collection(collection_id: int, db: DB, user: Current):
    lock_reader(db, user)
    row = find_collection(db, user, collection_id)
    db.execute(delete(CollectionItem).where(CollectionItem.collection_id == row.id))
    db.delete(row)
    db.commit()


@router.get("/collections/{collection_id}")
def collection_detail(collection_id: int, db: DB, user: Current, page: int = Query(1, ge=1, le=10000)):
    row = find_collection(db, user, collection_id)
    query = visible_books(user).join(CollectionItem, CollectionItem.work_id == Work.id).where(
        CollectionItem.collection_id == row.id
    ).order_by(Work.id.desc())
    works = list(db.scalars(query.offset((page - 1) * 30).limit(31)))
    return {**collection_json(row), "items": serialize_works(db, works[:30]), "has_next": len(works) > 30}


@router.put("/collections/{collection_id}/books/{work_id}", status_code=204)
def add_to_collection(collection_id: int, work_id: int, db: DB, user: Current):
    lock_reader(db, user)
    find_collection(db, user, collection_id)
    work_id = work_or_404(db, user, work_id, follow_merge=True, for_update=True).id
    if not db.get(CollectionItem, (collection_id, work_id)):
        db.add(CollectionItem(collection_id=collection_id, work_id=work_id))
    db.commit()


@router.delete("/collections/{collection_id}/books/{work_id}", status_code=204)
def remove_from_collection(collection_id: int, work_id: int, db: DB, user: Current):
    lock_reader(db, user)
    find_collection(db, user, collection_id)
    work = resolve_work(db, work_id, for_update=True)
    item = db.get(CollectionItem, (collection_id, work.id)) if work else None
    if item:
        db.delete(item)
    db.commit()


@router.get("/reading/export")
def export_personal(db: DB, user: Current):
    mappings = {
        "states": ReadingState, "collections": Collection, "positions": Position,
        "history": PositionEvent, "bookmarks": Bookmark, "koreader_positions": KOPosition, "koreader_history": KOPositionEvent,
        "annotations": Annotation, "sessions": ReadingSession, "sony_positions": SonyPosition,
    }
    result = {"version": 1, "exported_at": now(), "username": user.username}
    for key, model in mappings.items():
        result[key] = [{column.key: getattr(row, column.key) for column in model.__table__.columns
                        if column.key not in {"user_id", "normalized_name"}}
                       for row in db.scalars(select(model).filter_by(user_id=user.id))]
    members = db.scalars(select(CollectionItem).join(Collection).where(Collection.user_id == user.id))
    result["collection_items"] = [{"collection_id": item.collection_id, "work_id": item.work_id} for item in members]
    return Response(json.dumps(jsonable_encoder(result), ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="polka-reading.json"', "Cache-Control": "no-store"})

