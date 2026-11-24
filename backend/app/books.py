import base64
import codecs
import hashlib
import io
import os
import posixpath
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as XML
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import select

from .author_names import normal, sync_author_credits
from .db import DATA, INBOX, MAX_BYTES, ORIGINALS, BookFile, Edition, Library, Work
from .formats import IMAGE_TYPES, calibre_metadata, comic, mobi_kind, parse_html, rendered_cover
from .series_data import membership_labels, metadata_memberships, set_memberships
from .series_metadata import epub_memberships, fb2_memberships, series_metadata

MIME = {
    "epub": "application/epub+zip", "fb2": "application/x-fictionbook+xml",
    "fb2.zip": "application/zip", "pdf": "application/pdf",
    "txt": "text/plain", "mobi": "application/x-mobipocket-ebook",
    "azw3": "application/vnd.amazon.ebook", "djvu": "image/vnd.djvu", "lrf": "application/x-sony-bbeb",
    "cbz": "application/vnd.comicbook+zip", "html": "text/html", "rtf": "application/rtf",
}
Image.MAX_IMAGE_PIXELS = 12_000_000


def search_text(work, editions, series_names=()):
    fields = [work.title, *work.authors, work.series, *series_names, *work.genres, *(work.tags or [])]
    for key, value in (work.custom_fields or {}).items():
        fields.extend((key, value))
    for item in editions:
        fields.extend(getattr(item, key) for key in ("language", "isbn", "publisher", "year", "summary"))
    return normal(" ".join(filter(None, fields)))


def under(root, name):
    root = root.resolve()
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise ValueError("Недопустимый путь")
    result = root
    for part in relative.parts:
        result = result / part
        if result.is_symlink():
            raise ValueError("Символические ссылки не поддерживаются")
    return result


def managed_path(relative):
    if ORIGINALS is not None and relative.startswith("originals/"):
        return under(ORIGINALS, relative.removeprefix("originals/"))
    return under(DATA, relative)


def file_path(file, library):
    if file.indexed:
        return under(under(INBOX, library.folder), file.path)
    return managed_path(file.path)


def source_signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Источник не является обычным файлом")
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev]


def indexed_source_matches(file, path):
    before = source_signature(path)
    if file.index_signature is not None:
        return before == file.index_signature
    with path.open("rb") as stream:
        digest, _ = copy_limited(stream, None)
    return digest == file.sha256 and before == source_signature(path)


def refresh_indexed_source(file, library, relative, signature):
    if not file.indexed or relative is None:
        return
    if file.path != relative:
        try:
            if indexed_source_matches(file, file_path(file, library)):
                return
        except (ValueError, OSError):
            pass
    file.path, file.index_signature = relative, signature


def copy_limited(source, destination):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError("Превышен лимит размера книги")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if not size:
        raise ValueError("Пустой файл")
    return digest.hexdigest(), size


def bounded_zip(z):
    entries = z.infolist()
    if len(entries) > 10000 or sum(x.file_size for x in entries) > 256 * 1024 * 1024:
        raise ValueError("Слишком большой распакованный архив")
    if len({x.filename for x in entries}) != len(entries):
        raise ValueError("Повторяющиеся имена внутри архива")
    for item in entries:
        name = PurePosixPath(item.filename)
        if name.is_absolute() or ".." in name.parts or "\\" in item.filename or item.flag_bits & 1:
            raise ValueError("Неподдерживаемая структура архива")


def zip_read(z, path, limit=8 * 1024 * 1024):
    if z.getinfo(path).file_size > limit:
        raise ValueError("Превышен лимит ресурса внутри архива")
    return z.read(path)


def values(node, name):
    return [" ".join("".join(item.itertext()).split())[:4000] for item in node.findall(f".//{{*}}{name}")]


def first(node, name):
    return next(iter(values(node, name)), "")


def fb2(raw):
    root = XML.fromstring(raw)
    if root.tag.split("}")[-1] != "FictionBook":
        raise ValueError("Ожидался документ FictionBook")
    info = root.find("./{*}description/{*}title-info")
    if info is None:
        return {}, None
    pub = root.find("./{*}description/{*}publish-info")
    cover = info.find("./{*}coverpage/{*}image")
    ref = next((value.removeprefix("#") for key, value in cover.attrib.items() if key.endswith("href")), "") if cover is not None else ""
    binary = next((n for n in root.findall("./{*}binary") if ref and n.get("id") == ref), None)
    author_names = [
        " ".join(filter(None, (first(author, field) for field in ("first-name", "middle-name", "last-name"))))
        or first(author, "nickname") for author in info.findall("./{*}author")
    ]
    return {
        "title": first(info, "book-title"), "authors": list(filter(None, author_names)),
        "language": first(info, "lang"), "summary": first(info, "annotation"), "genres": values(info, "genre"),
        **series_metadata(fb2_memberships(info)),
        **({key: first(pub, key) for key in ("isbn", "publisher", "year")} if pub is not None else {}),
    }, base64.b64decode(binary.text or "") if binary is not None else None


def epub_package(z):
    container = XML.fromstring(zip_read(z, "META-INF/container.xml"))
    rootfile = container.find(".//{*}rootfile")
    if rootfile is None:
        raise ValueError("В EPUB отсутствует пакет OPF")
    opf = rootfile.get("full-path", "")
    return opf, XML.fromstring(zip_read(z, opf))


def epub(z):
    opf, root = epub_package(z)
    info = root.find("./{*}metadata")
    if info is None or root.find("./{*}spine/{*}itemref") is None:
        raise ValueError("В EPUB отсутствуют метаданные или разделы")
    props = {n.get("name") or n.get("property"): n.get("content") or n.text or "" for n in info.findall("./{*}meta")}
    cover_path = next((n.get("href") for n in root.findall("./{*}manifest/{*}item")
                      if "cover-image" in n.get("properties", "").split() or n.get("id") == props.get("cover")), None)
    cover = None
    if cover_path:
        from urllib.parse import unquote, urlsplit
        name = posixpath.normpath(posixpath.join(posixpath.dirname(opf), unquote(urlsplit(cover_path).path)))
        if name in z.namelist():
            cover = zip_read(z, name)
    isbn = next((v for v in values(info, "identifier") if v.lower().startswith("urn:isbn:")), "")
    return {
        "title": first(info, "title"), "authors": values(info, "creator"), "language": first(info, "language"),
        "publisher": first(info, "publisher"), "year": first(info, "date")[:4],
        "summary": first(info, "description"), "genres": values(info, "subject"), "isbn": isbn[9:],
        **series_metadata(epub_memberships(info, props)),
    }, cover


def inspect(path, name):
    hint = Path(name.lower()).suffix
    with path.open("rb") as f:
        head = f.read(4096)
    try:
        if head.startswith(b"PK\x03\x04"):
            with ZipFile(path) as z:
                bounded_zip(z)
                if "mimetype" in z.namelist() and zip_read(z, "mimetype", 256) == b"application/epub+zip":
                    return "epub", *epub(z)
                files = [n for n in z.namelist() if not n.endswith("/")]
                if len(files) == 1 and files[0].lower().endswith(".fb2"):
                    return "fb2.zip", *fb2(zip_read(z, files[0], 32 * 1024 * 1024))
                if hint == ".cbz" or any(Path(item).suffix.lower() in IMAGE_TYPES for item in files):
                    return "cbz", *comic(z)
            raise ValueError("Архив должен содержать EPUB, FB2 или страницы CBZ")
        if head.startswith(b"%PDF-"):
            with path.open("rb") as f:
                reader = PdfReader(f)
                if reader.is_encrypted or not len(reader.pages):
                    raise ValueError("PDF зашифрован или не содержит страниц")
                meta = reader.metadata
                metadata = {"title": meta.title or "", "authors": [meta.author] if meta.author else []} if meta else {}
            return "pdf", metadata, rendered_cover(path, "pdf")
        if head[60:68] == b"BOOKMOBI":
            kind = mobi_kind(path, head)
            return kind, *calibre_metadata(path, kind)
        if head.startswith(b"L\x00R\x00F\x00"):
            objects = int.from_bytes(head[16:24], "little")
            index = int.from_bytes(head[24:32], "little")
            if index < 0x4e or not objects or index + objects * 16 > path.stat().st_size:
                raise ValueError("Повреждена таблица объектов LRF")
            return "lrf", *calibre_metadata(path, "lrf")
        if head.startswith(b"AT&TFORM") and head[12:16] in {b"DJVU", b"DJVM"}:
            if 12 + int.from_bytes(head[8:12], "big") > path.stat().st_size:
                raise ValueError("Повреждён контейнер DjVu")
            return "djvu", {}, rendered_cover(path, "djvu")
        if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{\\rtf"):
            return "rtf", *calibre_metadata(path, "rtf")
        if hint in {".html", ".htm", ".xhtml"}:
            html = parse_html(path)
            return "html", {"title": "".join(html.title).strip()[:1000]}, None
        if name.lower().endswith(".fb2") or b"FictionBook" in head:
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Превышен лимит FB2")
            return "fb2", *fb2(path.read_bytes())
        if name.lower().endswith(".txt") and b"\x00" not in head:
            try:
                codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=False)
            except UnicodeDecodeError:
                head.decode("cp1251", errors="strict")
            return "txt", {}, None
    except (BadZipFile, KeyError, PdfReadError, SyntaxError, UnicodeError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Книга повреждена или имеет неподдерживаемую структуру") from exc
    raise ValueError("Формат книги не поддерживается")


def write_cover(raw, checksum):
    if not raw or len(raw) > 8 * 1024 * 1024:
        return None
    relative = f"covers/{checksum}.jpg"
    path = DATA / relative
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((480, 720))
            save_atomic(path, lambda temp: image.convert("RGB").save(temp, format="JPEG", quality=85))
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return relative


def save_atomic(path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
    try:
        write(temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def require_source_format(library, fmt):
    if library.formats and fmt not in library.formats:
        raise ValueError("Формат файла запрещён настройками библиотеки")


def store_book(db, library, path, name, *, indexed=None, index_signature=None, provenance=None, derived=None,
               into_work_id=None, into_edition_id=None, inspected=None):
    with path.open("rb") as source:
        checksum, size = copy_limited(source, None)
    locked = db.get(Library, library.id, with_for_update=True, populate_existing=True)
    target_work = db.get(Work, into_work_id, populate_existing=True) if into_work_id is not None else None
    if into_work_id is not None and (not target_work or target_work.library_id != locked.id or target_work.trashed_at is not None):
        raise ValueError("Произведение недоступно")
    target_edition = db.get(Edition, into_edition_id, populate_existing=True) if into_edition_id is not None else None
    if into_edition_id is not None and (not target_edition or not target_work or target_edition.work_id != target_work.id):
        raise ValueError("Издание недоступно")
    if derived is not None:
        parent_edition = db.get(Edition, derived.edition_id)
        parent_work = db.get(Work, parent_edition.work_id, populate_existing=True)
        if parent_work.trashed_at is not None:
            raise ValueError("Произведение находится в корзине")
    duplicate = db.scalar(select(BookFile).filter_by(library_id=locked.id, sha256=checksum))
    if duplicate:
        if derived is None:
            require_source_format(locked, duplicate.format)
        duplicate_edition = db.get(Edition, duplicate.edition_id)
        if target_work and duplicate_edition.work_id != target_work.id:
            raise ValueError("Этот файл уже относится к другому произведению")
        if target_edition and duplicate_edition.id != target_edition.id:
            raise ValueError("Этот файл уже входит в другое издание")
        refresh_indexed_source(duplicate, library, indexed, index_signature)
        return duplicate, True
    fmt, meta, raw_cover = inspected if inspected is not None else inspect(path, name)
    if derived is None:
        require_source_format(locked, fmt)
    if indexed is None:
        relative = f"{'cache' if derived else 'originals'}/{checksum[:2]}/{checksum}.{fmt}"
        save_atomic(managed_path(relative), lambda temp: shutil.copyfile(path, temp))
    else:
        relative = indexed
    if derived:
        edition_id = derived.edition_id
    elif target_edition:
        edition_id = target_edition.id
        if not target_edition.cover:
            target_edition.cover = write_cover(raw_cover, checksum)
    else:
        if target_work:
            work = target_work
        else:
            title = meta.get("title") or name.removesuffix(".zip").rsplit(".", 1)[0]
            authors = list(filter(None, meta.get("authors", [])))
            work = Work(library_id=library.id, title=title, authors=authors, search=normal(" ".join([title, *authors])),
                        genres=meta.get("genres", []))
            db.add(work)
            db.flush()
            sync_author_credits(db, work)
            set_memberships(db, work, metadata_memberships(meta), standalone_position=meta.get("series_index", ""))
        edition = Edition(work_id=work.id, cover=write_cover(raw_cover, checksum),
                          metadata_source="embedded" if any(meta.values()) else "filename",
                          **{k: meta.get(k, "") for k in ("language", "isbn", "publisher", "year", "summary")})
        db.add(edition)
        db.flush()
        editions = db.scalars(select(Edition).where(Edition.work_id == work.id)) if target_work else [edition]
        work.search = search_text(work, editions, membership_labels(db, work.id))
        edition_id = edition.id
    file = BookFile(library_id=library.id, edition_id=edition_id, sha256=checksum, name=name, path=relative,
                    format=fmt, size=size, indexed=indexed is not None, provenance=provenance, index_signature=index_signature,
                    derived_from=derived.id if derived else None)
    db.add(file)
    db.flush()
    return file, False

