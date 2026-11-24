import io
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import Admin, Current, DB, OpdsUser
from .author_names import catalog_search, linked_author, name_key
from .books import MIME
from .catalog import (author_facet, file_or_404, job_json, label_facet, library_or_404, matches_label, queue,
                      serialize_works, serve_book, serve_cover, visible_work, work_or_404)
from .db import APP_URL, Author, AuthorName, Edition, Job, Library, Series, Source, Work, now
from .direct_downloads import artifact_path
from .job_signals import wake_after_commit
from .opds_auth import discover
from .opds_search import mark_matches
from .remote import ACQ, ATOM, DOWNLOAD_RELS, fetch, origin, read_catalog, read_limited, seal, search_url, unseal
from .series_data import in_named_series, volume_number
from .series_routes import pages, visible_series, volume_query

router = APIRouter(tags=["OPDS"])
API = "/api/v1/opds"
ATOM_TYPE = "application/atom+xml;profile=opds-catalog"
OPEN_SEARCH = "application/opensearchdescription+xml"
OPDS_JSON = "application/opds+json"
OPDS_PUBLICATION = "application/opds-publication+json"
NAV_VIEWS = {"home", "authors", "series", "series-branch", "genres"}
SEARCH_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="opds")
SEARCH_TIMEOUT = 10
ET.register_namespace("", ATOM)
ET.register_namespace("dcterms", "http://purl.org/dc/terms/")


def atom(tag):
    return f"{{{ATOM}}}{tag}"


def node(parent, tag, value=None, **attrs):
    item = ET.SubElement(parent, atom(tag), attrs)
    item.text = value
    return item


def link(parent, href, rel, kind, title=None, *, length=None):
    attributes = {"title": title} if title else {}
    if length is not None:
        attributes["length"] = str(length)
    return node(parent, "link", href=href, rel=rel, type=kind, **attributes)


def url(path, **params):
    return APP_URL + path + ("?" + urlencode(params) if params else "")


def branch_for(db, user, value):
    if value.isascii() and value.isdecimal() and len(value) <= 10 and int(value) < 2**31:
        return db.scalar(visible_series(user).where(Series.id == int(value)))
    return None


def page_content(db, user, view, value, q, page):
    base = select(Work).join(Library).where(*visible_work(user))
    if view == "home":
        return [("Все книги", "all", ""), ("Новые поступления", "recent", ""), ("Авторы", "authors", ""),
                ("Серии", "series", ""), ("Жанры", "genres", "")], [], False
    if view == "authors":
        names = list(db.scalars(author_facet(user).order_by(Author.name).offset((page-1)*30).limit(31)))
        return [(entry, "author", entry) for entry in names[:30]], [], len(names) > 30
    if view == "series" or view == "series-branch":
        branch = branch_for(db, user, value) if view == "series-branch" else None
        if view == "series-branch" and branch is None:
            return [], [], False
        siblings = visible_series(user).where(Series.parent_id == (branch.id if branch else None))
        nodes, more = pages(db, siblings.order_by(Series.normalized_name, Series.id), page)
        navigation = [(item.name, "series-branch", str(item.id)) for item in nodes]
        if branch and page == 1 and db.scalar(volume_query(user, branch.id).with_only_columns(Work.id).limit(1)) is not None:
            navigation.insert(0, ("Тома серии", "series-volumes", str(branch.id)))
        return navigation, [], more
    if view == "series-volumes":
        branch = branch_for(db, user, value)
        if branch is None:
            return [], [], False
        rows, more = pages(db, volume_query(user, branch.id), page)
        return [], serialize_works(db, rows), more
    if view == "genres":
        ordered = label_facet(db, user, Work.genres)
        return [(entry, "genre", entry) for entry in ordered[(page-1)*30:page*30]], [], len(ordered) > page*30
    if view == "genre":
        base = base.where(matches_label(db, Work.genres, value))
    if view == "series-item":
        base = base.where(in_named_series(value))
    if view == "author":
        base = base.where(linked_author(AuthorName.key == name_key(value)))
    base = base.where(*catalog_search(q))
    order = Work.title.asc() if view == "all" and not q else Work.id.desc()
    rows = list(db.scalars(base.order_by(order, Work.id).offset((page-1)*30).limit(31)))
    return [], serialize_works(db, rows[:30]), len(rows) > 30


def feed_title(db, user, view, value):
    if view in {"series-branch", "series-volumes"}:
        branch = branch_for(db, user, value)
        return " / ".join(branch.path) if branch else "Серия"
    return value or "Электронная библиотека"


def file_url(file):
    return url(f"/opds/files/{file['id']}")


def cover_url(edition):
    return url(f"/opds/covers/{edition['id']}")


def publication(book, edition):
    href = url(f"/opds/v2/publications/{edition['id']}")
    metadata = {"@type": "http://schema.org/Book", "title": book["title"],
                "identifier": url(f"/api/v1/books/{book['id']}") + f"#edition-{edition['id']}"}
    fields = {"author": book["authors"], "subject": book["genres"], "description": edition["summary"],
              "language": edition["language"], "publisher": edition["publisher"]}
    metadata.update({key: value for key, value in fields.items() if value})
    isbn = re.sub(r"[-\s]", "", edition["isbn"]).upper()
    if re.fullmatch(r"[0-9]{13}|[0-9]{9}[0-9X]", isbn):
        metadata["altIdentifier"] = [f"urn:isbn:{isbn}"]

    series = []
    for membership in book["series_memberships"]:
        item = {"name": " / ".join(membership["path"])}
        position = volume_number(membership["position"])
        if position is not None:
            item["position"] = int(position) if position == position.to_integral_value() else float(position)
        if (ident := membership.get("id")) is not None:
            item["links"] = [{"href": url("/opds/v2", view="series-branch", value=ident), "type": OPDS_JSON}]
        series.append(item)
    if series:
        metadata["belongsTo"] = {"series": series}

    links = [{"rel": "self", "href": href, "type": OPDS_PUBLICATION}]
    links.extend({"rel": ACQ, "href": file_url(file), "type": MIME[file["format"]],
                  "properties": {"length": file["size"]}} for file in edition["files"])
    result = {"metadata": metadata, "links": links}
    if edition["cover"]:
        result["images"] = [{"href": cover_url(edition), "type": "image/jpeg"}]
    return result


@router.get("/opds/v1")
def atom_catalog(db: DB, user: OpdsUser, view: str = "home", value: str = "", q: str = "",
                 page: int = Query(1, ge=1, le=10000)):
    path = "/opds/v1"
    if q and view == "home":
        view = "all"
    nav, books, has_next = page_content(db, user, view, value, q, page)
    nav_feed = view in NAV_VIEWS
    media = ATOM_TYPE + (";kind=navigation" if nav_feed else ";kind=acquisition")
    self_url = url(path, view=view, value=value, q=q, page=page)
    root = ET.Element(atom("feed"))
    node(root, "id", self_url)
    node(root, "title", feed_title(db, user, view, value))
    from .db import now
    stamp = now().isoformat()
    node(root, "updated", stamp)
    node(node(root, "author"), "name", "Электронная библиотека")
    link(root, self_url, "self", media)
    link(root, url(path), "start", ATOM_TYPE + ";kind=navigation")
    link(root, url("/opds/search.xml"), "search", OPEN_SEARCH)
    if has_next:
        link(root, url(path, view=view, value=value, q=q, page=page+1), "next", media)
    if page > 1:
        link(root, url(path, view=view, value=value, q=q, page=page-1), "previous", media)
    for title, target, key in nav:
        entry = node(root, "entry")
        href = url(path, view=target, value=key)
        node(entry, "id", href)
        node(entry, "title", title)
        node(entry, "updated", stamp)
        kind = "navigation" if target in NAV_VIEWS else "acquisition"
        link(entry, href, "subsection", ATOM_TYPE + ";kind=" + kind)
    for book in books:
        for edition in book["editions"]:
            if not edition["files"]:
                continue
            entry = node(root, "entry")
            node(entry, "id", url(f"/api/v1/books/{book['id']}") + f"#edition-{edition['id']}")
            node(entry, "title", book["title"])
            created = book["created"].isoformat()
            node(entry, "updated", created if "+" in created else created + "Z")
            for author in book["authors"]:
                node(node(entry, "author"), "name", author)
            if edition["summary"]:
                node(entry, "summary", edition["summary"], type="text")
            if edition["language"]:
                ET.SubElement(entry, "{http://purl.org/dc/terms/}language").text = edition["language"]
            for genre in book["genres"]:
                node(entry, "category", term=genre)
            if edition["cover"]:
                for rel in ("image", "image/thumbnail"):
                    link(entry, cover_url(edition), f"http://opds-spec.org/{rel}", "image/jpeg")
            for file in edition["files"]:
                link(entry, file_url(file), ACQ + "/open-access", MIME[file["format"]], length=file["size"])
    return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True), media_type=media)


@router.get("/opds/search.xml")
def opensearch():
    import html
    pattern = html.escape(url("/opds/v1") + "?view=all&q={searchTerms}", quote=True)
    return Response(f'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
                    f'<ShortName>EBook</ShortName><Description>Поиск книг</Description>'
                    f'<Url type="{ATOM_TYPE}" template="{pattern}"/></OpenSearchDescription>', media_type=OPEN_SEARCH)


@router.get("/opds/v2")
def json_catalog(db: DB, user: OpdsUser, view: str = "home", value: str = "", q: str = "", query: str = "",
                 page: int = Query(1, ge=1, le=10000)):
    path = "/opds/v2"
    q = q or query
    if q and view == "home":
        view = "all"
    nav, books, has_next = page_content(db, user, view, value, q, page)
    links = [{"rel": "self", "href": url(path, view=view, value=value, q=q, page=page), "type": OPDS_JSON},
             {"rel": "start", "href": url(path), "type": OPDS_JSON},
             {"rel": "search", "href": url(path) + "{?query}", "type": OPDS_JSON, "templated": True}]
    if has_next:
        links.append({"rel": "next", "href": url(path, view=view, value=value, q=q, page=page+1), "type": OPDS_JSON})
    if page > 1:
        links.append({"rel": "previous", "href": url(path, view=view, value=value, q=q, page=page-1), "type": OPDS_JSON})
    publications = [publication(book, edition) for book in books for edition in book["editions"] if edition["files"]]
    result = {"metadata": {"title": feed_title(db, user, view, value), "itemsPerPage": 30, "currentPage": page},
              "links": links}
    if publications:
        result["publications"] = publications
    if nav:
        result["navigation"] = [{"href": url(path, view=target, value=key), "title": name, "type": OPDS_JSON}
                                for name, target, key in nav]
    elif not publications:
        result["navigation"] = [{"href": url(path), "title": "В начало каталога", "type": OPDS_JSON}]
    return JSONResponse(result, media_type=OPDS_JSON)


@router.get("/opds/v2/publications/{edition_id}")
def publication_detail(edition_id: int, db: DB, user: OpdsUser):
    edition = db.get(Edition, edition_id)
    if not edition:
        raise HTTPException(404, "Издание не найдено")
    book = serialize_works(db, [work_or_404(db, user, edition.work_id)])[0]
    selected = next((item for item in book["editions"] if item["id"] == edition_id and item["files"]), None)
    if not selected:
        raise HTTPException(404, "Издание не найдено")
    return JSONResponse(publication(book, selected), media_type=OPDS_PUBLICATION)


@router.get("/opds/files/{file_id}")
def opds_file(file_id: int, db: DB, user: OpdsUser):
    return serve_book(*file_or_404(db, user, file_id))


@router.get("/opds/covers/{edition_id}")
def opds_cover(edition_id: int, db: DB, user: OpdsUser, request: Request):
    return serve_cover(db, user, edition_id, request)


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=5000)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    auth: str | None = Field(default=None, pattern="^(none|basic|bearer)$")
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=1200)


class BrowseInput(BaseModel):
    link: str | None = Field(default=None, max_length=12000)


class AuthenticationInput(BaseModel):
    url: str = Field(min_length=1, max_length=5000)


class DownloadInput(BaseModel):
    source_id: int
    delivery: Literal["library", "file"] = "library"
    library_id: int | None = Field(default=None, gt=0)
    link: str = Field(max_length=64000)
    work_id: int | None = Field(default=None, gt=0)
    read_after: bool = False


def source_json(source):
    parts = urlsplit(source.url)
    return {"id": source.id, "name": source.name, "description": source.description, "enabled": source.enabled,
            "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), "authenticated": bool(source.secret)}


def source_or_404(db, ident, include_disabled=False):
    source = db.get(Source, ident)
    if not source or not (source.enabled or include_disabled):
        raise HTTPException(404, "Источник недоступен")
    return source


def save_source(source, payload):
    try:
        scheme, _, _ = origin(payload.url)
        if source.secret and payload.auth is None and origin(source.url) != origin(payload.url):
            raise ValueError("Повторно укажите авторизацию при смене адреса сервера")
        if payload.auth in {"basic", "bearer"}:
            if scheme != "https" or not payload.password or (payload.auth == "basic" and
                    (not payload.username or ":" in payload.username)):
                raise ValueError("Для авторизации укажите HTTPS и действительные учётные данные")
            source.secret = seal({"kind": payload.auth, "username": payload.username, "password": payload.password})
        elif payload.auth == "none":
            source.secret = None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    source.url, source.name, source.description, source.enabled = payload.url, payload.name, payload.description, payload.enabled


@router.get(API + "/sources")
def sources(db: DB, user: Current):
    query = select(Source).order_by(Source.id)
    if not user.admin:
        query = query.where(Source.enabled.is_(True))
    return [source_json(s) for s in db.scalars(query)]


@router.post(API + "/sources/authentication")
def discover_source_auth(payload: AuthenticationInput, user: Admin):
    try:
        origin(payload.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        result = discover(payload.url)
    except (OSError, ValueError, SyntaxError, RecursionError) as exc:
        detail = str(exc) if type(exc) in (ValueError, ConnectionError) else "Некорректный или недоступный ответ OPDS"
        raise HTTPException(502, detail) from None
    if result["document"]:
        links = result["document"]["links"]
        if logo := links.get("logo"):
            key = seal({"action": "auth-logo", "url": logo})
            links["logo"] = API + "/sources/authentication/logo?" + urlencode({"key": key})
    return result


@router.post(API + "/sources", status_code=201)
def add_source(payload: SourceInput, db: DB, user: Admin):
    source = Source()
    save_source(source, payload)
    db.add(source)
    db.commit()
    return source_json(source)


@router.put(API + "/sources/{source_id}")
def edit_source(source_id: int, payload: SourceInput, db: DB, user: Admin):
    source = source_or_404(db, source_id, True)
    save_source(source, payload)
    db.commit()
    return source_json(source)


@router.delete(API + "/sources/{source_id}", status_code=204)
def remove_source(source_id: int, db: DB, user: Admin):
    db.delete(source_or_404(db, source_id, True))
    db.commit()


def capability(source, link, action, depth, **meta):
    return seal({"source": source.id, "url": link["url"], "action": action, "depth": depth, **meta})


def get_capability(source, key, action):
    data = unseal(key, ttl=3600)
    if data["source"] != source.id or data["action"] != action or data["depth"] > 20:
        raise ValueError("Ссылка недоступна или превышена глубина каталога")
    return data


def plain(value, limit=500):
    return value[:limit] if isinstance(value, str) else ""


def publication_metadata(pub):
    return {"title": plain(pub["title"]), "authors": [value for author in pub["authors"][:20] if (value := plain(author, 150))],
            "summary": plain(pub["summary"], 2000), "language": plain(pub.get("language"), 40),
            "publisher": plain(pub.get("publisher")), "isbn": plain(pub.get("isbn"), 60),
            "series_memberships": pub.get("series_memberships", [])}


def publication_identifier(value):
    identifier = plain(value)
    try:
        parts = urlsplit(identifier)
    except ValueError:
        return ""
    if parts.scheme in {"http", "https"}:
        if parts.username or parts.password or parts.query or parts.fragment:
            return ""
    return identifier


def downloadable(link):
    relations = {rel for rel in link["rels"] if isinstance(rel, str)}
    other_acquisition = {"buy", "borrow", "subscribe", "preview"}
    return bool(relations & DOWNLOAD_RELS) and not (relations & other_acquisition) and not any(
        rel.startswith(ACQ + "/") and rel not in DOWNLOAD_RELS for rel in relations)


def browser_feed(source, key=None, query=None):
    try:
        secret = unseal(source.secret) if source.secret else None
        cap = get_capability(source, key, "browse") if key else {"url": source.url, "depth": 0}
        depth = cap["depth"]
        data = read_catalog(cap["url"], source.url, secret)
        search = next((l for l in data["links"] if "search" in l["rels"]), None)
        can_search = search is not None
        if query:
            if not search:
                raise HTTPException(422, "Источник не предоставляет поиск")
            data = read_catalog(search_url(search, query, source.url, secret), source.url, secret)
        next_link = next((l for l in data["links"] if "next" in l["rels"]), None)
        previous = next((l for l in data["links"] if "previous" in l["rels"] or "prev" in l["rels"]), None)
        entries = []
        for pub in data["publications"]:
            metadata = publication_metadata(pub)
            identifier = publication_identifier(pub["id"])
            downloads = [{"type": plain(link["type"], 120), "size": link["size"],
                          "link": capability(source, link, "download", depth, title=metadata["title"], identifier=identifier,
                                             media_type=plain(link["type"], 120), size=link["size"], metadata=metadata)}
                         for link in pub["links"] if downloadable(link)]
            image = next(iter(pub.get("images", [])), None)
            cover = API + f"/sources/{source.id}/cover?" + urlencode({"key": capability(source, image, "cover", depth)}) if image else None
            entries.append({**metadata, "cover": cover, "downloads": downloads,
                            "purchase": any("buy" in link["rels"] or ACQ + "/buy" in link["rels"] for link in pub["links"])})
        return {"title": plain(data["title"]), "version": data["version"], "search": can_search,
                "navigation": [{"title": plain(l["title"]), "link": capability(source, l, "browse", depth+1)}
                               for l in data["navigation"]] if depth < 20 else [],
                "facets": [
                    {"title": plain(group["title"], 160), "sorting": group.get("sorting", False),
                     "options": [{"title": plain(option["title"], 160), "active": option.get("active", False),
                                  "count": option.get("count"), "sort": option.get("sort"),
                                  "link": capability(source, option, "browse", depth)} for option in group["links"][:50]]}
                    for group in data.get("facets", [])[:20]
                ],
                "next": capability(source, next_link, "browse", depth) if next_link else None,
                "previous": capability(source, previous, "browse", depth) if previous else None,
                "publications": entries}
    except PermissionError as exc:
        raise HTTPException(502, str(exc)) from None
    except (ValueError, SyntaxError, ConnectionError, TimeoutError, RecursionError) as exc:
        message = str(exc) if type(exc) in (ValueError, ConnectionError) else "Некорректный или недоступный ответ OPDS"
        raise HTTPException(502, message) from None


@router.post(API + "/sources/{source_id}/test")
def test_source(source_id: int, db: DB, user: Admin):
    data = browser_feed(source_or_404(db, source_id, True))
    return {k: data[k] for k in ("title", "version", "search")}


@router.post(API + "/sources/{source_id}/browse")
def browse(source_id: int, payload: BrowseInput, db: DB, user: Current):
    return browser_feed(source_or_404(db, source_id), payload.link)


@router.get(API + "/sources/{source_id}/search")
def search_source(source_id: int, db: DB, user: Current, q: str = Query(min_length=1, max_length=300)):
    return browser_feed(source_or_404(db, source_id), query=q)


@router.get(API + "/sources/{source_id}/cover", include_in_schema=False)
def remote_cover(source_id: int, db: DB, user: Current, key: str = Query(max_length=12000)):
    source = source_or_404(db, source_id)
    try:
        data = get_capability(source, key, "cover")
        secret = unseal(source.secret) if source.secret else None
        return render_remote_image(data["url"], source.url, secret)
    except (ValueError, OSError, Image.DecompressionBombError):
        raise HTTPException(404, "Обложка недоступна") from None


@router.get(API + "/sources/authentication/logo", include_in_schema=False)
def authentication_logo(user: Admin, key: str = Query(max_length=12000)):
    try:
        data = unseal(key, ttl=3600)
        if data.get("action") != "auth-logo":
            raise ValueError("Неверная ссылка")
        return render_remote_image(data["url"], data["url"])
    except (ValueError, OSError, Image.DecompressionBombError):
        raise HTTPException(404, "Логотип недоступен") from None


def render_remote_image(url, source_url, secret=None):
    with fetch(url, source_url, secret) as (response, _):
        raw = read_limited(response, 4 * 1024 * 1024)
    output = io.BytesIO()
    with Image.open(io.BytesIO(raw)) as image:
        image.thumbnail((240, 360))
        image.convert("RGB").save(output, format="JPEG")
    return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get(API + "/search")
def global_search(db: DB, user: Current, q: str = Query(min_length=1, max_length=300)):
    from .catalog import books
    local = books(db, user, q, page=1, limit=30)
    catalogues = [(source_json(source), SimpleNamespace(id=source.id, url=source.url, secret=source.secret))
                  for source in db.scalars(select(Source).filter_by(enabled=True).order_by(Source.id))]
    db.rollback()

    def search(source):
        try:
            return {"result": browser_feed(source, query=q)}
        except HTTPException as exc:
            if exc.status_code == 422:
                return {"unsupported": True}
            return {"error": exc.detail}

    pending = [(summary, SEARCH_POOL.submit(search, source)) for summary, source in catalogues]
    done, timed_out = wait([future for _, future in pending], timeout=SEARCH_TIMEOUT)
    for future in timed_out:
        future.cancel()
    remote = [{"source": summary, **(future.result() if future in done else
               {"error": "Источник не ответил вовремя"})} for summary, future in pending]
    mark_matches(db, user, remote)
    return {"local": local, "remote": remote}


def download_parameters(payload, db, user):
    if user.read_only:
        raise HTTPException(403, "Для загрузки требуется право записи")
    source = source_or_404(db, payload.source_id)
    lib = None
    if payload.delivery == "file":
        if payload.library_id is not None or payload.work_id is not None or payload.read_after:
            raise HTTPException(422, "Для скачивания файла укажите только источник и ссылку")
    else:
        if payload.library_id is None:
            raise HTTPException(422, "Выберите управляемую библиотеку")
        lib = library_or_404(db, user, payload.library_id, write=True)
        if lib.mode != "managed":
            raise HTTPException(409, "Выберите управляемую библиотеку")
    try:
        item = get_capability(source, payload.link, "download")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return source, lib, item


def download_candidates(db, library, item):
    from .worker import possible_matches
    media = item.get("media_type", "").split(";", 1)[0].strip().lower()
    fmt = next((fmt for fmt, kind in MIME.items() if kind == media), "")
    metadata = item.get("metadata") or {"title": item["title"]}
    return fmt, possible_matches(db, library, metadata, fmt) if library else []


@router.post(API + "/downloads/preview")
def preview_download(payload: DownloadInput, db: DB, user: Current):
    source, lib, item = download_parameters(payload, db, user)
    fmt, matches = download_candidates(db, lib, item)
    return {"title": item["title"], "source": source.name, "format": fmt, "size": item.get("size"), "matches": matches}


@router.post(API + "/downloads", status_code=202)
def start_download(payload: DownloadInput, db: DB, user: Current):
    source, lib, item = download_parameters(payload, db, user)
    fmt, matches = download_candidates(db, lib, item) if payload.work_id is not None else ("", [])
    if payload.work_id is not None and not any(row["work_id"] == payload.work_id for row in matches):
        raise HTTPException(409, "Выбранная карточка больше не подходит. Обновите список совпадений.")
    task = queue(db, user, lib, "download", {"source_id": source.id, "link": seal(item), "title": item["title"],
                                            "delivery": payload.delivery, "work_id": payload.work_id, "read_after": payload.read_after})
    task.result = {"title": item["title"], "source": source.name, "format": fmt or item.get("media_type"),
                   "total": item.get("size"), "delivery": payload.delivery}
    db.commit()
    return job_json(task)


@router.get(API + "/downloads")
def downloads(db: DB, user: Current):
    return [job_json(j) for j in db.scalars(select(Job).filter_by(owner_id=user.id, kind="download")
                                          .order_by(Job.id.desc()).limit(100))]


def download_task(db, user, ident, lock=False):
    stmt = select(Job).filter_by(id=ident, owner_id=user.id, kind="download")
    task = db.scalar(stmt.with_for_update() if lock else stmt)
    if not task:
        raise HTTPException(404, "Загрузка не найдена")
    return task


@router.get(API + "/downloads/{task_id}")
def download_detail(task_id: int, db: DB, user: Current):
    return job_json(download_task(db, user, task_id))


@router.get(API + "/downloads/{task_id}/file")
def downloaded_file(task_id: int, db: DB, user: Current):
    task = download_task(db, user, task_id)
    if task.payload.get("delivery") != "file" or task.library_id is not None:
        raise HTTPException(404, "Файл не найден")
    result = task.result or {}
    if task.status != "done":
        raise HTTPException(409, "Файл ещё не готов")
    path = artifact_path(task.id)
    if datetime.fromisoformat(result["expires_at"]) <= now() or not path.is_file():
        raise HTTPException(410, "Срок хранения файла истёк. Скачайте книгу снова из каталога.")
    return FileResponse(path, filename=result["filename"], media_type=MIME[result["format"]],
                        headers={"Cache-Control": "private, no-store"})


@router.post(API + "/downloads/{task_id}/cancel")
def cancel_download(task_id: int, db: DB, user: Current):
    task = download_task(db, user, task_id, lock=True)
    if task.status not in {"queued", "running"}:
        raise HTTPException(409, "Загрузка уже завершена")
    task.status = "cancelled"
    db.commit()
    return job_json(task)


@router.post(API + "/downloads/{task_id}/retry")
def retry_download(task_id: int, db: DB, user: Current):
    if user.read_only:
        raise HTTPException(403, "Для загрузки требуется право записи")
    task = download_task(db, user, task_id, lock=True)
    if task.status not in {"error", "cancelled"}:
        raise HTTPException(409, "Загрузку сейчас нельзя повторить")
    task.result = {key: value for key, value in (task.result or {}).items()
                   if key in {"title", "source", "format", "total", "delivery"}}
    task.status, task.error, task.attempt = "queued", None, 0
    wake_after_commit(db, task)
    db.commit()
    return job_json(task)

