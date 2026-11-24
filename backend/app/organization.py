import json
import re
import unicodedata
from pathlib import PurePosixPath
from string import Formatter

from sqlalchemy import select

from .books import copy_limited, file_path, save_atomic, under
from .db import DATA, BookFile, Edition, Job, Library, Work
from .series_data import memberships_for

FIELDS = {"author", "title", "series", "subseries", "series_index", "year", "language", "format"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def parse_template(template):
    if "\\" in template or not template or len(template) > 280:
        raise ValueError("Укажите шаблон из относительных частей, разделённых /")
    parts = template.split("/")
    if len(parts) > 6 or any(part.strip() in {"", ".", ".."} for part in parts):
        raise ValueError("Шаблон должен содержать от одной до шести частей без переходов в другие папки")
    parsed = []
    try:
        for part in parts:
            tokens = list(Formatter().parse(part))
            if any(spec or conversion or field not in FIELDS
                   for _, field, spec, conversion in tokens if field is not None):
                raise ValueError
            if any(INVALID_FILENAME.search(literal) for literal, _, _, _ in tokens):
                raise ValueError
            parsed.append(tokens)
    except ValueError:
        raise ValueError("Используйте допустимые поля шаблона без модификаторов и специальных символов") from None
    return parsed


def safe_component(value):
    value = INVALID_FILENAME.sub("_", unicodedata.normalize("NFC", value)).strip(" .")
    if DEVICE.match(value):
        value = "_" + value
    return value or "Без названия"


def fit(value, limit):
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(" .") or "_"


def destination(parsed, file, edition, work, occupied, subseries=""):
    values = {"author": ", ".join(work.authors) or "Неизвестный автор", "title": work.title,
              "series": work.series or "Без серии", "subseries": subseries,
              "series_index": work.series_index, "year": edition.year,
              "language": edition.language, "format": file.format}
    components = [safe_component("".join(literal + (values[field] if field else "")
                                        for literal, field, _, _ in tokens)) for tokens in parsed]
    suffix = "." + file.format
    filename = components.pop()
    stem = filename[:-len(suffix)] if filename.casefold().endswith(suffix) else filename
    # Bound the total UTF-8 path while preserving the full file extension.
    directories = [fit(element, 34 if len(components) > 3 else 48) for element in components]
    budget = min(100, 248 - sum(len(part.encode("utf-8")) + 1 for part in directories))
    lookup_directories = tuple(unicodedata.normalize("NFC", item).casefold() for item in directories)
    attempt = 0
    while True:
        variation = "" if not attempt else "-" + file.sha256[:12] + (f"-{attempt}" if attempt > 1 else "")
        leaf = fit(stem, budget - len(suffix) - len(variation)) + variation + suffix
        key = (*lookup_directories, leaf.casefold())
        if key not in occupied:
            occupied.add(key)
            return PurePosixPath(*directories, leaf).as_posix()
        attempt += 1


def plan_layout(db, library, template=None):
    template = library.path_template if template is None else template
    parsed = parse_template(template)
    rows = db.execute(select(BookFile, Edition, Work).select_from(BookFile)
                      .join(Edition, BookFile.edition_id == Edition.id).join(Work, Edition.work_id == Work.id)
                      .where(BookFile.library_id == library.id, BookFile.indexed.is_(False), Work.trashed_at.is_(None))
                      .order_by(BookFile.id)).all()
    path_map = memberships_for(db, {work.id for _, _, work in rows}) if "{subseries}" in template else {}
    subseries = {ident: " · ".join(memberships[0]["path"][1:]) for ident, memberships in path_map.items()}
    occupied = set()
    return [{"file_id": file.id, "sha256": file.sha256, "size": file.size,
             "path": destination(parsed, file, edition, work, occupied, subseries.get(work.id, ""))}
            for file, edition, work in rows]


def layout_root(library_id, job_id):
    return under(DATA, f"organized/{library_id}/{job_id}")


def build_layout(db, task, library):
    lock = db.get(Library, library.id, with_for_update=True, populate_existing=True)
    plan = db.get(Job, task.payload["preview_id"])
    if plan is None or plan.kind != "layout_plan" or plan.library_id != library.id:
        raise ValueError("План раскладки не найден")
    items = plan_layout(db, library)
    if plan.result["template"] != library.path_template or plan.result["items"] != items:
        raise ValueError("Состав библиотеки или шаблон изменился. Создайте новый предварительный план.")
    target = layout_root(library.id, task.id)
    target.mkdir(parents=True, exist_ok=True)
    for item in items:
        file = db.get(BookFile, item["file_id"])
        source = file_path(file, library)

        def verified_copy(temporary):
            with source.open("rb") as stream, temporary.open("wb") as output:
                checksum, size = copy_limited(stream, output)
            if checksum != item["sha256"] or size != item["size"]:
                raise ValueError("Контрольная сумма оригинала не совпала")

        save_atomic(under(target, "books/" + item["path"]), verified_copy)
    manifest = {"library_id": library.id, "layout_id": task.id, "files": items}
    save_atomic(under(target, "manifest.json"), lambda path: path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    ))
    parent = lock.active_layout_id
    lock.active_layout_id = task.id
    return {"previous": parent, "files": len(items), "location": f"organized/{library.id}/{task.id}/books"}

