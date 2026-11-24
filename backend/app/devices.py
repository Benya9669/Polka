import json
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import Current, DB
from .books import copy_limited, save_atomic, under
from .catalog import file_or_404, job_json, queue
from .conversions import SONY_TARGETS, convert_file
from .db import DATA, EXPORTS, Edition, Job, PositionEvent, SonyPosition, Work, now
from .personal import lock_reader, record_reading

router = APIRouter(prefix="/api/v1/devices/sony", tags=["Sony PRS-600"])
SONY_JOB = "sony_export"
PACKAGE_LIMIT = 500 * 1024 * 1024


class ExportInput(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=25)


class SonyInput(BaseModel):
    percentage: float | None = Field(default=None, ge=0, le=1)
    page: int | None = Field(default=None, ge=1, le=100000)
    finished: bool = False


def selected_files(db, user, ids):
    entries = []
    for file_id in dict.fromkeys(ids):
        file, library = file_or_404(db, user, file_id)
        target = SONY_TARGETS.get(file.format)
        if target is None:
            raise HTTPException(422, "Неподдерживаемый формат экспорта")
        work = db.get(Work, db.get(Edition, file.edition_id).work_id)
        entries.append((file, library, work, target))
    return entries


def plan_entry(file, work, target):
    return {"file_id": file.id, "work_id": work.id, "title": work.title, "authors": work.authors,
            "source_format": file.format, "format": target, "filename": f"book-{file.id}.{target}"}


def package_path(task):
    relative = f"sony/{task.owner_id}/{task.id}.zip"
    return under(EXPORTS, relative) if EXPORTS is not None else under(DATA, f"exports/{relative}")


@router.post("/preview")
def preview_export(payload: ExportInput, db: DB, user: Current):
    return {"profile": "sony-prs-600", "files": [plan_entry(file, work, target)
                                               for file, _, work, target in selected_files(db, user, payload.file_ids)]}


@router.post("/exports", status_code=202)
def create_export(payload: ExportInput, db: DB, user: Current):
    entries = selected_files(db, user, payload.file_ids)
    file_ids = [entry[0].id for entry in entries]
    lock_reader(db, user)
    existing = db.scalars(select(Job).where(Job.owner_id == user.id, Job.kind == SONY_JOB,
                                           Job.status.in_(("queued", "running")))).all()
    for item in existing:
        if item.payload.get("file_ids") == file_ids:
            return job_json(item)
    task = queue(db, user, entries[0][1], SONY_JOB, {"file_ids": file_ids})
    db.commit()
    return job_json(task)


@router.get("/exports")
def exports(db: DB, user: Current):
    entries = db.scalars(select(Job).filter_by(owner_id=user.id, kind=SONY_JOB).order_by(Job.id.desc()).limit(30))
    return [job_json(task) for task in entries]


@router.get("/exports/{task_id}/archive")
def export_archive(task_id: int, db: DB, user: Current):
    task = db.get(Job, task_id)
    if not task or task.owner_id != user.id or task.kind != SONY_JOB or task.status != "done":
        raise HTTPException(404, "Архив экспорта не найден")
    selected_files(db, user, task.payload["file_ids"])
    archive = package_path(task)
    if not archive.is_file():
        raise HTTPException(404, "Архив экспорта недоступен")
    return FileResponse(archive, filename=f"polka-sony-{task.id}.zip", media_type="application/zip",
                        headers={"Cache-Control": "private, no-store"})


def build_sony(db, task, user):
    try:
        entries = selected_files(db, user, task.payload["file_ids"])
    except HTTPException as exc:
        raise ValueError("Один из файлов пакета недоступен") from exc

    manifest = {"version": 1, "profile": "sony-prs-600", "files": []}
    size = 0

    def write_archive(target):
        nonlocal size
        with ZipFile(target, "w", compression=ZIP_STORED) as archive:
            for file, library, work, output_format in entries:
                entry = plan_entry(file, work, output_format)
                with tempfile.TemporaryDirectory() as directory:
                    output = convert_file(file, library, Path(directory), sony=True,
                                          title=work.title, authors=work.authors)
                    with output.open("rb") as data:
                        checksum, count = copy_limited(data, None)
                    size += count
                    if size > PACKAGE_LIMIT:
                        raise ValueError("Размер пакета превышает 500 МиБ")
                    archive.write(output, entry["filename"])
                manifest["files"].append({**entry, "size": count, "sha256": checksum})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    save_atomic(package_path(task), write_archive)
    return {"files": len(entries), "bytes": size, "titles": [entry["title"] for entry in manifest["files"]]}


def describe_sony(value):
    return {"file_id": value.file_id, "percentage": value.percentage, "page": value.page,
            "finished": value.finished, "updated": value.updated}


@router.get("/progress")
def sony_progress(db: DB, user: Current):
    rows = db.scalars(select(SonyPosition).filter_by(user_id=user.id).order_by(SonyPosition.updated.desc()).limit(50))
    result = []
    for row in rows:
        try:
            file, _ = file_or_404(db, user, row.file_id)
        except HTTPException:
            continue
        work = db.get(Work, db.get(Edition, file.edition_id).work_id)
        result.append({**describe_sony(row), "title": work.title, "work_id": work.id, "format": file.format})
    return result


@router.put("/progress/{file_id}")
def save_sony_progress(file_id: int, payload: SonyInput, db: DB, user: Current):
    file, _ = file_or_404(db, user, file_id)
    if payload.page is None and payload.percentage is None and not payload.finished:
        raise HTTPException(422, "Укажите страницу, процент или отметьте книгу прочитанной")
    lock_reader(db, user)
    row = db.get(SonyPosition, (user.id, file.id))
    if row is None:
        row = SonyPosition(user_id=user.id, file_id=file.id)
        db.add(row)
    timestamp = now()
    row.percentage = 1.0 if payload.finished else payload.percentage
    row.page, row.finished, row.updated = payload.page, payload.finished, timestamp
    state = record_reading(db, user.id, file, timestamp, row.percentage, "Sony PRS-600")
    if row.finished:
        state.status, state.finished = "finished", timestamp
    elif state.status == "finished":
        state.status = "reading"
    db.add(PositionEvent(user_id=user.id, file_id=file.id, payload={
        "updated": timestamp.isoformat(), "device": "Sony PRS-600",
        "percentage": row.percentage, "page": row.page, "kind": "sony",
    }))
    db.commit()
    return describe_sony(row)

