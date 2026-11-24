import hashlib
import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from .auth import Current, DB, digest
from .books import file_path, indexed_source_matches, source_signature
from .catalog import file_or_404
from .db import BookFile, Credential, KOPosition, KOPositionEvent, Position, PositionEvent, User, now
from .personal import record_reading

MEDIA = "application/vnd.koreader.v1+json"
DOC_PATTERN = r"^[a-fA-F0-9]{32}$"
router = APIRouter(tags=["KOReader"])


def key_id(username, key):
    return digest(username.casefold() + ":" + key.lower())


def client_error(message, status=401):
    return JSONResponse({"code": 2001, "message": message}, status_code=status, media_type=MEDIA)


def identity(request: Request, db: DB):
    username = request.headers.get("x-auth-user", "")
    key = request.headers.get("x-auth-key", "")
    if len(username) > 64 or not re.fullmatch(DOC_PATTERN, key):
        return None
    credential = db.get(Credential, key_id(username, key))
    if not credential or credential.scope != "koreader":
        return None
    user = db.get(User, credential.user_id)
    return user if user and user.username == username.casefold() else None


KOUser = Annotated[User | None, Depends(identity)]


@router.post("/api/v1/devices/koreader/credentials", status_code=201)
def provision(db: DB, user: Current):
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    db.execute(delete(Credential).where(Credential.user_id == user.id, Credential.scope == "koreader"))
    password = secrets.token_urlsafe(24)
    key = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
    db.add(Credential(digest=key_id(user.username, key), user_id=user.id, scope="koreader", label="KOReader"))
    db.commit()
    return {"username": user.username, "password": password}


@router.delete("/api/v1/devices/koreader/credentials", status_code=204)
def revoke(db: DB, user: Current):
    db.execute(delete(Credential).where(Credential.user_id == user.id, Credential.scope == "koreader"))
    db.commit()


@router.post("/koreader/users/create")
def disabled_registration():
    return client_error("Create credentials in the library settings.", 403)


@router.get("/koreader/users/auth")
def authorize(user: KOUser):
    if not user:
        return client_error("Unauthorized")
    return JSONResponse({"authorized": "OK"}, media_type=MEDIA)


class Push(BaseModel):
    document: str = Field(pattern=DOC_PATTERN)
    progress: str = Field(min_length=1, max_length=4000)
    percentage: float = Field(ge=0, le=1)
    device: str = Field(max_length=120)
    device_id: str | None = Field(default=None, max_length=120)


class Binding(BaseModel):
    document: str = Field(pattern=DOC_PATTERN)
    file_id: int


def format_native(row):
    stamp = row.updated
    if stamp is not None and stamp.tzinfo is None:
        from datetime import timezone
        stamp = stamp.replace(tzinfo=timezone.utc)
    return {"document": row.document, "percentage": row.percentage, "progress": row.locator, "device": row.device,
            "device_id": row.device_id, "timestamp": int(stamp.timestamp()) if stamp else 0}


@router.get("/api/v1/devices/koreader/documents")
def documents(db: DB, user: Current):
    rows = db.scalars(select(KOPosition).filter_by(user_id=user.id).order_by(KOPosition.updated.desc()).limit(100))
    return [{**format_native(row), "file_id": row.file_id} for row in rows]


@router.get("/api/v1/devices/koreader/documents/{document}/history")
def document_history(document: Annotated[str, Path(pattern=DOC_PATTERN)], db: DB, user: Current,
                     before: int | None = Query(default=None, gt=0)):
    query = select(KOPositionEvent).filter_by(user_id=user.id, document=document.lower())
    if before is not None:
        query = query.where(KOPositionEvent.id < before)
    rows = list(db.scalars(query.order_by(KOPositionEvent.id.desc()).limit(51)))
    return {"items": [{**format_native(row), "id": row.id, "file_id": row.file_id} for row in rows[:50]],
            "next_before": rows[49].id if len(rows) > 50 else None}


def append_event(db, row):
    if row.file_id:
        db.add(PositionEvent(user_id=row.user_id, file_id=row.file_id, payload={
            "percentage": row.percentage, "device": row.device, "updated": row.updated.isoformat(), "kind": "koreader",
            "document": row.document, "locator": row.locator, "device_id": row.device_id}))
        record_reading(db, row.user_id, db.get(BookFile, row.file_id), row.updated, row.percentage, row.device)


def file_document(file, library):
    try:
        path = file_path(file, library)
        signature = source_signature(path)
        if signature[0] != file.size or (file.indexed and not indexed_source_matches(file, path)):
            raise ValueError("File changed")
        checksum = hashlib.md5(usedforsecurity=False)
        # KOReader samples the first KiB, then one KiB at each power-of-four offset.
        with path.open("rb") as stream:
            for offset in (0, *(1024 << (2 * power) for power in range(11))):
                stream.seek(offset)
                chunk = stream.read(1024)
                if not chunk:
                    break
                checksum.update(chunk)
        if source_signature(path) != signature:
            raise ValueError("File changed")
    except (OSError, ValueError):
        raise HTTPException(404, "Файл недоступен") from None
    return checksum.hexdigest()


@router.get("/api/v1/devices/koreader/files/{file_id}/match")
def match_file(file_id: int, db: DB, user: Current):
    file, library = file_or_404(db, user, file_id)
    row = db.get(KOPosition, (user.id, file_document(file, library)))
    if not row or row.updated is None:
        return {"document": None, "linked_file_id": None}
    return {"document": row.document, "linked_file_id": row.file_id}


@router.put("/api/v1/devices/koreader/documents")
def bind_file(payload: Binding, db: DB, user: Current):
    file_or_404(db, user, payload.file_id)
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    document = payload.document.lower()
    row = db.get(KOPosition, (user.id, document))
    if row is None:
        row = KOPosition(user_id=user.id, document=document, file_id=payload.file_id)
        db.add(row)
    else:
        changed = row.file_id != payload.file_id
        row.file_id = payload.file_id
        if changed and row.updated:
            append_event(db, row)
    db.commit()
    return {"document": document, "file_id": payload.file_id}


@router.get("/koreader/syncs/progress/{document}")
def pull(document: str, db: DB, user: KOUser):
    if not user:
        return client_error("Unauthorized")
    row = db.get(KOPosition, (user.id, document.lower())) if re.fullmatch(DOC_PATTERN, document) else None
    if not row:
        return JSONResponse({}, media_type=MEDIA)
    if row.file_id:
        try:
            file, _ = file_or_404(db, user, row.file_id)
        except HTTPException:
            return JSONResponse({}, media_type=MEDIA)
        if file.format == "pdf":
            web = db.get(Position, (user.id, row.file_id))
            if web and (not row.updated or web.updated > row.updated):
                return JSONResponse({"document": row.document, "percentage": web.percentage, "progress": web.locator,
                                     "device": web.device, "device_id": "polka-web",
                                     "timestamp": int(web.updated.timestamp())}, media_type=MEDIA)
    return JSONResponse(format_native(row) if row.updated else {}, media_type=MEDIA)


@router.put("/koreader/syncs/progress")
def push(payload: Push, db: DB, user: KOUser):
    if not user:
        return client_error("Unauthorized")
    document = payload.document.lower()
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    row = db.get(KOPosition, (user.id, document))
    if row is None:
        row = KOPosition(user_id=user.id, document=document)
        db.add(row)
    fields = (payload.progress, payload.percentage, payload.device, payload.device_id)
    if fields != (row.locator, row.percentage, row.device, row.device_id):
        row.locator, row.percentage, row.device, row.device_id = fields
        row.updated = now()
        db.add(KOPositionEvent(user_id=user.id, document=document, file_id=row.file_id, locator=row.locator,
                               percentage=row.percentage, device=row.device, device_id=row.device_id, updated=row.updated))
        append_event(db, row)
        db.commit()
    return JSONResponse({"document": document, "timestamp": int(row.updated.timestamp())}, media_type=MEDIA)

