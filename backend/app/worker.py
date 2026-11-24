import hashlib
import http.client
import logging
import os
import stat
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select, text, update

from .author_names import names_for
from .books import (MIME, copy_limited, file_path, inspect, normal, refresh_indexed_source, require_source_format, source_signature,
                    store_book, under)
from .conversions import CONVERSION_TARGETS, convert_file
from .db import INBOX, MAX_BYTES, BookFile, Edition, Job, Library, ScanEntry, Session, Source, User, Work, WorkAuthor, engine, now
from .devices import SONY_JOB, build_sony
from .direct_downloads import clean_artifacts, save_artifact
from .job_signals import wait_for_job
from .organization import build_layout
from .remote import fetch, unseal
from .scan_watch import SUFFIXES, ScanWatch
from .series_data import memberships_for, metadata_memberships, path_key, position_key

logger = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


def fingerprint(path):
    with path.open("rb") as stream:
        return copy_limited(stream, None)[0]


def match_isbn(value):
    return "".join(char for char in value.upper() if char.isascii() and char in "0123456789X")


def possible_matches(db, library, metadata, fmt):
    title = metadata.get("title")
    authors = metadata.get("authors")
    isbn = match_isbn(metadata.get("isbn", ""))
    if not title and not isbn:
        return []
    terms = [Work.search.contains(word, autoescape=True) for word in normal(title or "").split()[:4]]
    if isbn and metadata.get("isbn"):
        terms.append(Edition.isbn == metadata["isbn"])
    if not terms:
        return []
    query = (select(Work, Edition, BookFile.format).select_from(Work).join(Edition, Edition.work_id == Work.id)
             .join(BookFile, BookFile.edition_id == Edition.id).where(
        Work.library_id == library.id, Work.trashed_at.is_(None), or_(*terms)
    ).order_by(Work.id.desc()).limit(200))
    matches = {}
    input_title = normal(title or "")
    input_authors = {normal(name) for name in authors or [] if name}
    language = normal(metadata.get("language", ""))
    rows = db.execute(query).all()
    input_volumes = {path_key(entry["path"]): position_key(entry["position"])
                     for entry in metadata_memberships(metadata) if entry["position"]}
    memberships = memberships_for(db, {work.id for work, _, _ in rows}) if rows and input_volumes else {}
    author_links = set()
    if rows and input_authors:
        author_ids = {row.author_id for row in names_for(db, library.id, input_authors).values()}
        if author_ids:
            author_links = set(db.scalars(select(WorkAuthor.work_id).where(WorkAuthor.author_id.in_(author_ids),
                               WorkAuthor.work_id.in_({work.id for work, _, _ in rows}))))
    for work, edition, other_format in rows:
        same_title = bool(input_title and input_title == normal(work.title))
        same_authors = work.id in author_links or bool(input_authors.intersection(normal(name) for name in work.authors))
        other_isbn = match_isbn(edition.isbn)
        same_isbn = len(isbn) in {10, 13} and isbn == other_isbn
        known_volumes = {path_key(entry["path"]): position_key(entry["position"])
                         for entry in memberships.get(work.id, []) if entry["position"]}
        different_volume = any(known_volumes[key] != input_volumes[key] for key in known_volumes.keys() & input_volumes.keys())
        if same_isbn and (same_title or same_authors):
            confidence, reason = 98, "Совпал ISBN и название или имя автора"
        elif same_authors and different_volume:
            confidence, reason = 35, "Одна серия и автор, разные номера томов"
        elif same_title and same_authors and language and edition.language and language != normal(edition.language):
            confidence, reason = 55, "Совпали название и автор, языки издания различаются"
        elif same_title and same_authors and isbn and other_isbn and isbn != other_isbn:
            confidence, reason = 70, "Совпали название и автор, ISBN изданий различаются"
        elif same_title and same_authors and fmt != other_format:
            confidence, reason = 85, "Совпали название и автор, разные форматы"
        elif same_title and same_authors:
            confidence, reason = 80, "Совпали название и автор"
        elif same_title:
            confidence, reason = 42, "Совпало название, авторы различаются или не указаны"
        elif same_authors and input_title and (input_title in normal(work.title) or normal(work.title) in input_title):
            confidence, reason = 40, "Совпал автор и часть названия"
        else:
            continue
        if confidence > matches.get(work.id, {}).get("confidence", 0):
            matches[work.id] = {"work_id": work.id, "confidence": confidence, "reason": reason, "title": work.title}
    return sorted(matches.values(), key=lambda candidate: (-candidate["confidence"], candidate["work_id"]))[:10]


def incoming_matches(index, metadata, item):
    title = normal(metadata.get("title", ""))
    authors = {normal(author) for author in metadata.get("authors", []) if normal(author)}
    groups = [index.setdefault((title, author), []) for author in sorted(authors)] if title else []
    earlier = {row["path"]: row for group in groups for row in group}
    for group in groups:
        group.append(item)
        del group[:-10]
    matches = []
    positions = {path_key(entry["path"]): position_key(entry["position"])
                 for entry in metadata_memberships(metadata) if entry["position"]}
    for row in earlier.values():
        if any(positions[key] != row["volumes"][key] for key in positions.keys() & row["volumes"].keys()):
            confidence, reason = 35, "Одна серия и автор, разные номера томов"
        elif item["language"] and row["language"] and normal(item["language"]) != normal(row["language"]):
            confidence, reason = 55, "Совпали название и автор, языки издания различаются"
        elif item["isbn"] and row["isbn"] and match_isbn(item["isbn"]) != match_isbn(row["isbn"]):
            confidence, reason = 70, "Совпали название и автор, ISBN изданий различаются"
        elif item["format"] != row["format"]:
            confidence, reason = 85, "Совпали название и автор, разные форматы"
        else:
            confidence, reason = 80, "Совпали название и автор"
        matches.append({"path": row["path"], "title": row["title"], "format": row["format"],
                        "confidence": confidence, "reason": reason})
    item["volumes"] = positions
    return sorted(matches, key=lambda row: (-row["confidence"], row["path"]))[:10]


def preview(db, task, library):
    folder = task.payload["folder"]
    root = under(INBOX, folder)
    if not root.is_dir():
        raise ValueError("Каталог импорта недоступен")
    allowed_formats = set(library.formats or [])
    scheduled_formats = set(task.payload.get("formats", []))
    formats = (allowed_formats & scheduled_formats if allowed_formats and scheduled_formats
               else allowed_formats or scheduled_formats or None)
    skip_all = formats is not None and not formats
    name_filter = formats if scheduled_formats else None
    aliases = {"zip": {"fb2.zip", "epub", "cbz"}, "htm": {"html"}, "xhtml": {"html"},
               "mobi": {"mobi", "azw3"}, "azw3": {"mobi", "azw3"}}
    cached = {entry.location: entry for entry in db.scalars(select(ScanEntry).filter_by(library_id=library.id, folder=folder))}
    known = {entry.sha256: entry for entry in db.execute(
        select(BookFile.sha256, BookFile.indexed, BookFile.path, BookFile.index_signature)
        .where(BookFile.library_id == library.id))}
    items, errors = [], []
    incoming = {}
    seen, visited, walk_errors = set(), set(), []
    found = reused = unreadable = 0
    for parent, folders, names in os.walk(root, followlinks=False, onerror=walk_errors.append):
        folders[:] = sorted(f for f in folders if not (Path(parent) / f).is_symlink())
        for name in sorted(names):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower().lstrip(".")
            if suffix not in SUFFIXES or skip_all or name_filter and name_filter.isdisjoint(aliases.get(suffix, {suffix})):
                continue
            found += 1
            if found > 50000:
                raise ValueError("План ограничен 50 000 файлами. Выберите подкаталог.")
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                path = under(root, relative)
                location = hashlib.sha256(os.fsencode(folder) + b"\0" + os.fsencode(relative)).hexdigest()
                visited.add(location)
                signature = source_signature(path)
                entry = cached.get(location)
                if entry and entry.data.get("metadata_version") == 2 and entry.data["signature"] == signature:
                    digest, fmt, meta = (entry.data[key] for key in ("sha256", "format", "metadata"))
                    reused += 1
                else:
                    digest = fingerprint(path)
                    try:
                        fmt, meta, _ = inspect(path, name)
                    except (ValueError, SyntaxError):
                        unreadable += 1
                        raise
                    if source_signature(path) != signature:
                        raise ValueError("Файл изменился во время сканирования")
                    data = {"signature": signature, "sha256": digest, "format": fmt, "metadata": meta, "metadata_version": 2}
                    if entry:
                        entry.data = data
                    else:
                        db.add(ScanEntry(library_id=library.id, location=location, folder=folder, data=data))
                if formats is not None and fmt not in formats:
                    continue
                existing = known.get(digest)
                duplicate = digest in seen or existing is not None
                seen.add(digest)
                matches = [] if duplicate else possible_matches(db, library, meta, fmt)
                refresh = False
                if existing and existing.indexed:
                    try:
                        current = signature if existing.path == relative else source_signature(file_path(existing, library))
                        refresh = current != existing.index_signature
                    except (ValueError, OSError):
                        refresh = True
                item = {"path": relative, "sha256": digest, "format": fmt, "title": meta.get("title") or name,
                              "duplicate": duplicate, "existing_duplicate": existing is not None,
                              "signature": signature, "refresh_index": refresh,
                              "work_candidates": [item["work_id"] for item in matches],
                              "match_reason": matches[0]["reason"] if matches else None,
                              "confidence": matches[0]["confidence"] if matches else None, "matches": matches,
                              "language": meta.get("language", ""), "isbn": meta.get("isbn", ""),
                              "convert": not duplicate and fmt in {"fb2", "fb2.zip"}}
                if not duplicate:
                    # Keep temporary comparison data out of the persisted plan.
                    item["incoming"] = incoming_matches(incoming, meta, item.copy())
                items.append(item)
            except (ValueError, OSError, SyntaxError) as exc:
                errors.append({"path": relative, "error": str(exc)[:240]})
    for error in walk_errors:
        errors.append({"path": os.path.relpath(error.filename, root), "error": error.strerror or "Каталог недоступен"})
    if not walk_errors and not scheduled_formats:
        for location in cached.keys() - visited:
            db.delete(cached[location])
    return {"found": found, "new_works": sum(not i["duplicate"] for i in items), "unreadable": unreadable,
            "duplicates": sum(i["duplicate"] for i in items),
            "candidates": sum(bool(i["work_candidates"] or i.get("incoming")) for i in items),
            "conversions": sum(i["convert"] for i in items), "reused": reused, "items": items, "errors": errors}


def enqueue_conversion(db, task, stored, duplicate):
    if not duplicate and stored.format in {"fb2", "fb2.zip"}:
        db.add(Job(owner_id=task.owner_id, library_id=task.library_id, kind="conversion", payload={"file_id": stored.id}))


def apply_import(db, task, library):
    plan = db.get(Job, task.payload["preview_id"])
    root = under(INBOX, plan.payload["folder"])
    saved = task.result or {}
    added, duplicates = saved.get("added", 0), saved.get("duplicates", 0)
    attached, skipped = saved.get("attached", 0), saved.get("skipped", 0)
    errors, work_ids = list(saved.get("errors", [])), list(saved.get("work_ids", []))
    file_ids = list(saved.get("file_ids", []))
    choices = task.payload.get("choices", {})
    referenced = {choice["path"] for choice in choices.values() if choice["action"] == "incoming"}
    imported_targets = dict(saved.get("imported_targets", {}))
    items = plan.result["items"]
    start = saved.get("processed", 0)
    for index, item in enumerate(items[start:], start=start):
        choice = choices.get(item["path"], {})
        action = choice.get("action")
        if action == "skip":
            skipped += 1
        else:
            try:
                target = choice.get("work_id")
                if action == "incoming":
                    target = imported_targets.get(choice["path"])
                    if target is None:
                        raise ValueError("Выбранный основной файл не удалось импортировать")
                stored, duplicate = import_item(db, task, library, root, item, target)
                if duplicate:
                    duplicates += 1
                else:
                    work_id = db.get(Edition, stored.edition_id).work_id
                    added += 1
                    file_ids.append(stored.id)
                    attached += target is not None
                    if target is None:
                        work_ids.append(work_id)
                    if item["path"] in referenced:
                        imported_targets[item["path"]] = work_id
            except (OSError, ValueError, SyntaxError) as exc:
                db.rollback()
                errors.append({"path": item["path"], "error": str(exc)[:240]})
        task.result = {"added": added, "duplicates": duplicates, "attached": attached, "skipped": skipped,
                       "errors": errors, "work_ids": work_ids, "file_ids": file_ids,
                       "processed": index + 1, "imported_targets": imported_targets}
        db.commit()
        errors, work_ids = list(errors), list(work_ids)
        file_ids = list(file_ids)
        imported_targets = dict(imported_targets)
    return {"added": added, "duplicates": duplicates, "attached": attached, "skipped": skipped,
            "errors": errors, "work_ids": work_ids, "file_ids": file_ids, "processed": len(items)}


def import_item(db, task, library, root, item, target):
    library = db.get(Library, library.id, with_for_update=True, populate_existing=True)
    require_source_format(library, item["format"])
    path = under(root, item["path"])
    signature = source_signature(path)
    relative = item["path"] if library.mode == "indexed" else None
    stored = None
    if signature == item.get("signature"):
        stored = db.scalar(select(BookFile).filter_by(library_id=library.id, sha256=item["sha256"]))
    if stored:
        if target is not None and db.get(Edition, stored.edition_id).work_id != target:
            raise ValueError("Этот файл уже относится к другому произведению")
        if stored.indexed and (item.get("refresh_index") or stored.path == relative):
            refresh_indexed_source(stored, library, relative, signature)
        return stored, True
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / path.name
        with path.open("rb") as source, snapshot.open("wb") as output:
            sha, _ = copy_limited(source, output)
        if sha != item["sha256"] or signature != source_signature(path):
            raise ValueError("Файл изменился после предварительного просмотра")
        stored, duplicate = store_book(db, library, snapshot, path.name, indexed=relative,
                                       index_signature=signature if relative is not None else None, into_work_id=target)
        enqueue_conversion(db, task, stored, duplicate)
        return stored, duplicate


def conversion(db, task, library):
    ident = task.payload["file_id"]
    existing = db.scalar(select(BookFile).filter_by(derived_from=ident))
    if existing:
        download_read_link(db, task, existing.id)
        return {"file_id": existing.id, "duplicate": True}
    original = db.get(BookFile, ident)
    if not original or original.library_id != library.id:
        raise ValueError("Исходный файл не найден")
    if db.get(Work, db.get(Edition, original.edition_id).work_id).trashed_at is not None:
        raise ValueError("Произведение находится в корзине")
    with tempfile.TemporaryDirectory() as directory:
        output = convert_file(original, library, Path(directory))
        name = Path(original.name.removesuffix(".zip")).stem + output.suffix
        book, duplicate = store_book(db, library, output, name, derived=original)
        download_read_link(db, task, book.id)
        return {"file_id": book.id, "duplicate": duplicate}


def download_read_link(db, task, file_id):
    parent = task.payload.get("download_id")
    if parent is not None:
        job = db.get(Job, parent, with_for_update=True)
        if job and job.status == "done" and job.result.get("conversion_id") == task.id:
            job.result = {**job.result, "read_file_id": file_id}


def report_download(task_id, result):
    with Session.begin() as control:
        row = control.get(Job, task_id, with_for_update=True)
        if row.status != "running":
            raise Cancelled()
        row.result = result
        row.heartbeat = now()


def download(db, task, library):
    source = db.get(Source, task.payload["source_id"])
    if not source or not source.enabled:
        raise ValueError("Источник отключён или удалён")
    data = unseal(task.payload["link"])
    secret = unseal(source.secret) if source.secret else None
    title = (data.get("title") or "Книга").replace("/", "_").replace("\\", "_")
    title = "".join(c for c in title if ord(c) >= 32 and ord(c) != 127)[:180]
    media = data.get("media_type", "").split(";", 1)[0].strip().lower()
    hint = next((fmt for fmt, kind in MIME.items() if kind == media), "")
    delivery = task.payload.get("delivery", "library")
    progress = {"title": title, "source": source.name, "format": hint or media, "delivery": delivery}
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source"
        with fetch(data["url"], source.url, secret) as (response, _), path.open("wb") as output:
            declared = response.getheader("Content-Length")
            total = int(declared) if declared and declared.isdigit() else None
            if total is not None and total > MAX_BYTES:
                raise ValueError("Превышен лимит размера книги")
            transfer = {**progress, "stage": "downloading", "total": total if total is not None else data.get("size")}
            report_download(task.id, {**transfer, "bytes": 0})
            start, reported, count = time.monotonic(), 0.0, 0
            while True:
                try:
                    chunk = response.read1(128 * 1024)
                except (OSError, http.client.HTTPException) as exc:
                    raise ConnectionError("Загрузка прервалась") from exc
                if not chunk:
                    break
                count += len(chunk)
                elapsed = time.monotonic() - start
                if count > MAX_BYTES or elapsed > 300:
                    raise ValueError("Превышен лимит загрузки")
                output.write(chunk)
                digest.update(chunk)
                if elapsed - reported > 0.75:
                    report_download(task.id, {**transfer, "bytes": count, "speed": round(count / elapsed)})
                    reported = elapsed
            if total is not None and total != count:
                raise ConnectionError("Загрузка прервалась")
            if not count:
                raise ValueError("Источник вернул пустой файл")
        report_download(task.id, {**progress, "stage": "processing", "bytes": count, "total": count})
        fmt, embedded, cover = inspect(path, title + ("." + hint if hint else ""))
        db.refresh(task, with_for_update=True)
        if task.status != "running":
            raise Cancelled()
        original_metadata = data.get("metadata") or {"title": data.get("title", "")}
        provenance = {"source": source.name, "source_id": source.id, "identifier": data.get("identifier"), "title": title,
                      "downloaded_at": now().isoformat(), "format": fmt, "metadata": original_metadata, "media_type": media}
        if delivery == "file":
            expires = save_artifact(task.id, path)
            task.payload = {key: value for key, value in task.payload.items() if key != "link"}
            db.flush()
            return {"delivery": "file", "title": title, "filename": title + "." + fmt, "format": fmt,
                    "bytes": count, "expires_at": expires.isoformat(), "source": source.name,
                    "sha256": digest.hexdigest(), "provenance": provenance}
        metadata = {**original_metadata, **{key: value for key, value in embedded.items() if value}}
        if embedded.get("series") and not embedded.get("series_memberships"):
            metadata.pop("series_memberships", None)
        matches = possible_matches(db, library, metadata, fmt)
        target = task.payload.get("work_id")
        if target is not None and not any(row["work_id"] == target for row in matches):
            raise ValueError("Метаданные файла не совпали с выбранной карточкой")
        stored, duplicate = store_book(db, library, path, title + "." + fmt, provenance=provenance,
                                       into_work_id=target, inspected=(fmt, metadata, cover))
        read_file, conversion_id = (stored.id if fmt in {"epub", "pdf"} else None), None
        auto_convert = not duplicate and fmt in {"fb2", "fb2.zip"}
        if fmt in CONVERSION_TARGETS and (task.payload.get("read_after") or auto_convert):
            prior = db.scalar(select(BookFile.id).filter_by(derived_from=stored.id))
            if prior is not None:
                read_file = prior
            else:
                step = Job(owner_id=task.owner_id, library_id=library.id, kind="conversion",
                           payload={"file_id": stored.id, "download_id": task.id})
                db.add(step)
                db.flush()
                conversion_id = step.id
        task.payload = {key: value for key, value in task.payload.items() if key != "link"}
        db.flush()
        return {"file_id": stored.id, "duplicate": duplicate, "title": title, "bytes": stored.size, "format": stored.format,
                "source": source.name, "sha256": stored.sha256, "provenance": provenance, "matches": matches,
                "read_file_id": read_file, "conversion_id": conversion_id}


HANDLERS = {"scan": preview, "import": apply_import, "download": download, "conversion": conversion,
            "layout": build_layout}


def enqueue_due_scans(watch=None):
    stamp = now()
    with Session.begin() as db:
        if watch is not None:
            settings = {ident: plan for ident, plan in db.execute(
                select(Library.id, Library.scan_settings).where(Library.scan_due.is_not(None))) if plan and plan.get("watch")}
            changed = watch.changed({plan["folder"] for plan in settings.values()})
            for ident, plan in settings.items():
                if plan["folder"] in changed:
                    library = db.get(Library, ident, with_for_update=True)
                    if library and library.scan_due is not None and library.scan_settings.get("token") == plan["token"]:
                        library.scan_due = stamp
            db.flush()
        libraries = db.scalars(select(Library).where(Library.scan_due <= stamp).with_for_update(skip_locked=True))
        for library in libraries:
            settings = library.scan_settings or {}
            owner = db.get(User, library.scan_owner_id) if library.scan_owner_id else None
            if not owner or not owner.admin or owner.read_only or settings.get("mode") not in {"preview", "apply"}:
                library.scan_due = None
                continue
            pending = db.scalar(select(Job.id).where(Job.library_id == library.id, Job.kind.in_(["scan", "import"]),
                                                     Job.status.in_(["queued", "running"])).limit(1))
            if pending is None:
                library.scan_due = stamp + timedelta(minutes=settings["interval_minutes"])
                payload = {"folder": settings["folder"], "formats": settings["formats"], "schedule": settings["token"]}
                db.add(Job(owner_id=owner.id, library_id=library.id, kind="scan", payload=payload))


def run_one():
    with Session.begin() as db:
        task = db.scalar(select(Job).filter_by(status="queued").order_by(Job.id).limit(1).with_for_update(skip_locked=True))
        if not task:
            return False
        task.status, task.heartbeat, task.attempt = "running", now(), task.attempt + 1
        if task.kind == "download":
            task.result = {key: value for key, value in (task.result or {}).items()
                           if key in {"title", "source", "format", "total", "delivery"}}
            task.result["stage"] = "downloading"
            task.error = None
        ident = task.id
    with Session() as db:
        task = db.get(Job, ident)
        try:
            user = db.get(User, task.owner_id)
            library = db.get(Library, task.library_id) if task.library_id is not None else None
            standalone = task.kind == "download" and task.payload.get("delivery") == "file" and task.library_id is None
            if user and task.kind == SONY_JOB:
                result = build_sony(db, task, user)
            elif not user or user.read_only or (not standalone and (not library or (not user.admin and library.owner_id != user.id))):
                raise ValueError("Права на выполнение задачи изменились")
            else:
                if task.kind in {"scan", "import"} and not user.admin:
                    raise ValueError("Права на выполнение задачи изменились")
                token = task.payload.get("schedule")
                if token and (library.scan_settings or {}).get("token") != token:
                    raise ValueError("Расписание сканирования изменилось")
                result = HANDLERS[task.kind](db, task, library)
            db.refresh(task, with_for_update=True)
            if task.status == "running":
                if task.kind == "scan" and task.payload.get("schedule"):
                    db.refresh(library, with_for_update=True)
                    settings = library.scan_settings or {}
                    if (settings.get("mode") == "apply" and settings.get("token") == task.payload["schedule"]
                            and any(not item["duplicate"] or item["refresh_index"] for item in result["items"])):
                        following = Job(owner_id=task.owner_id, library_id=library.id, kind="import",
                                        payload={"preview_id": task.id, "schedule": settings["token"]})
                        db.add(following)
                        db.flush()
                        result["confirmed"] = following.id
                    elif settings.get("mode") == "apply" and settings.get("token") == task.payload["schedule"]:
                        result["automatic"] = True
                        result["items"] = []
                task.status, task.result, task.error = "done", result, None
                db.commit()
        except Cancelled:
            db.rollback()
        except Exception as exc:
            db.rollback()
            task = db.get(Job, ident, with_for_update=True)
            if task.status == "running":
                transient = isinstance(exc, (ConnectionError, TimeoutError)) and task.attempt < 3
                task.status = "queued" if transient else "error"
                task.error = str(exc)[:350] if type(exc) in (ValueError, ConnectionError, PermissionError) else "Не удалось обработать задачу"
                db.commit()
                logger.warning("Job %s: %s (%s)", ident, task.status, type(exc).__name__)
                if transient:
                    time.sleep(2)
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    with engine.connect() as lock, ScanWatch(INBOX) as watch:
        if engine.dialect.name == "postgresql" and not lock.scalar(text("SELECT pg_try_advisory_lock(73459182)")):
            raise RuntimeError("Уже запущен обработчик библиотеки")
        with Session.begin() as db:
            db.execute(update(Job).where(Job.status == "running").values(status="queued"))
        check_due = cleanup_due = 0.0
        while True:
            if time.monotonic() >= cleanup_due:
                try:
                    clean_artifacts()
                except OSError as exc:
                    logger.warning("Не удалось очистить временные загрузки OPDS: %s", type(exc).__name__)
                cleanup_due = time.monotonic() + 600
            if time.monotonic() >= check_due:
                enqueue_due_scans(watch)
                check_due = time.monotonic() + 5
            if not run_one():
                wait_for_job(min(3, check_due - time.monotonic()))


if __name__ == "__main__":
    main()

