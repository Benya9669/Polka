import base64
import io
import json
from email.message import Message
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine

from app import books, catalog, db, devices, main, organization, remote, worker
from tests.fixtures import FB2, epub_bytes


@pytest.fixture
def library(tmp_path, monkeypatch):
    database = create_engine("sqlite:///" + (tmp_path / "test.db").as_posix())
    monkeypatch.setattr(db.Session, "kw", {**db.Session.kw, "bind": database})
    db.Base.metadata.create_all(database)
    for module in (books, catalog, devices, main, organization, remote):
        monkeypatch.setattr(module, "DATA", tmp_path / "storage")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for module in (books, catalog, worker):
        monkeypatch.setattr(module, "INBOX", inbox)
    with TestClient(main.app) as client:
        result = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct-password"})
        assert result.status_code == 201, result.text
        response = client.post("/api/v1/libraries", json={"name": "Книги", "mode": "managed"})
        assert response.status_code == 201, response.text
        yield client, response.json()["id"], inbox
    database.dispose()


def upload(client, ident, content=None, name="book.epub"):
    response = client.post(f"/api/v1/libraries/{ident}/upload",
                           files={"file": (name, content if content is not None else epub_bytes())})
    assert response.status_code == 201, response.text
    return response.json()


def test_cover_cache_revalidates_access_and_representation(library):
    admin, ident, _ = library
    image = io.BytesIO()
    Image.new("RGB", (24, 36), "green").save(image, format="PNG")
    fb2 = FB2.replace(b"<lang>ru</lang>", b'<coverpage><image href="#cover"/></coverpage><lang>ru</lang>')
    fb2 = fb2.replace(b"</FictionBook>", b'<binary id="cover" content-type="image/png">' +
                      base64.b64encode(image.getvalue()) + b"</binary></FictionBook>")
    file = upload(admin, ident, fb2, "with-cover.fb2")["file"]
    edition = file["edition_id"]
    web_url = f"/api/v1/editions/{edition}/cover"
    opds_url = f"/opds/covers/{edition}"
    assert admin.patch(f"/api/v1/libraries/{ident}", json={"shared": True}).is_success
    account = {"username": "reader", "password": "another-password"}
    assert admin.post("/api/v1/auth/users", json=account).status_code == 201
    with TestClient(main.app) as reader, TestClient(main.app) as opds:
        assert reader.post("/api/v1/auth/login", json=account).is_success
        credential = reader.post("/api/v1/auth/opds-tokens", json={"label": "Cover test"}).json()
        opds.headers["Authorization"] = "Bearer " + credential["token"]
        cover = reader.get(web_url)
        assert cover.status_code == 200 and cover.headers["content-type"] == "image/jpeg"
        assert cover.headers["cache-control"] == "private, no-cache"
        assert cover.headers["vary"] == "Cookie, Authorization, X-Polka-User"
        etag = cover.headers["etag"]
        cached = {"If-None-Match": f'"other", W/{etag}'}
        for client, path in ((reader, web_url), (opds, opds_url)):
            reused = client.get(path, headers=cached)
            assert reused.status_code == 304 and reused.content == b""
            for header in ("etag", "cache-control", "vary"):
                assert reused.headers[header] == cover.headers[header]

        replacement = io.BytesIO()
        Image.new("RGB", (30, 42), "red").save(replacement, format="PNG")
        assert books.write_cover(replacement.getvalue(), file["sha256"])
        changed = reader.get(web_url, headers=cached)
        assert changed.status_code == 200 and changed.content != cover.content
        assert changed.headers["etag"] != etag
        assert reader.get(web_url, headers={"If-None-Match": "*"}).status_code == 304

        assert admin.patch(f"/api/v1/libraries/{ident}", json={"shared": False}).is_success
        current = {"If-None-Match": changed.headers["etag"]}
        assert reader.get(web_url, headers=current).status_code == 404
        assert opds.get(opds_url, headers=current).status_code == 404
        opds.headers.clear()
        assert opds.get(opds_url, headers=current).status_code == 401


def test_global_opds_search_matches_titles_and_aliases_without_exposing_private_books(library, monkeypatch):
    from threading import Event
    from urllib.parse import urlsplit

    from app import opds

    client, private_library, _ = library
    shared_library = client.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()["id"]

    def uploaded_work(ident):
        file = upload(client, ident)["file"]["id"]
        return client.get(f"/api/v1/files/{file}/info").json()["work_id"]

    shared = uploaded_work(shared_library)
    private = uploaded_work(private_library)
    for ident in (shared, private):
        updated = client.put(f"/api/v1/books/{ident}", json={"title": "Полярная ночь", "authors": ["Анна Соколова"]})
        credit, = updated.json()["author_credits"]
        assert client.post(f"/api/v1/authors/{credit['id']}/aliases", json={"name": "Лада Норд"}).is_success
    assert client.post("/api/v1/auth/users", json={"username": "reader", "password": "another-password"}).is_success

    def book(title, author, links):
        return {"metadata": {"title": title, "author": author}, "links": links}

    download = {"href": "/book.epub", "rel": remote.ACQ + "/open-access", "type": "application/epub+zip"}
    buy = {"href": "/buy", "rel": remote.ACQ + "/buy", "type": "text/html"}
    responses = {
        "books.example": [book("ПОЛЯРНАЯ   НОЧЬ", "лада норд", [download]),
                          book("Полярная ночь", "Другой автор", [download])],
        "shop.example": [book("Полярная ночь", "Лада Норд", [buy])],
    }
    slow_started = Event()
    release_slow = Event()
    slow_done = Event()

    def feed(url, source, secret):
        address = urlsplit(url)
        if address.hostname == "slow.example" and not release_slow.is_set():
            slow_started.set()
            try:
                release_slow.wait(5)
                raise ConnectionError("Источник недоступен")
            finally:
                slow_done.set()
        info = {"metadata": {"title": "Test"}, "links": []}
        if address.hostname in responses:
            if address.query:
                info["publications"] = responses[address.hostname]
            else:
                info["links"].append({"rel": "search", "href": "/catalog{?query}", "type": "application/opds+json"})
        return remote.parse_json(json.dumps(info).encode(), url)

    monkeypatch.setattr(opds, "read_catalog", feed)
    monkeypatch.setattr(opds, "SEARCH_TIMEOUT", 0.8)
    for name, host in (("Книги", "books"), ("Магазин", "shop"), ("Архив", "archive"), ("Медленный", "slow")):
        assert client.post("/api/v1/opds/sources", json={"name": name, "url": f"https://{host}.example/catalog"}).is_success

    def search(session):
        response = session.get("/api/v1/opds/search", params={"q": "Полярная ночь"})
        assert response.is_success, response.text
        data = response.json()
        return data["local"]["items"], {entry["source"]["name"]: entry for entry in data["remote"]}

    try:
        local, entries = search(client)
        assert slow_started.is_set() and not slow_done.is_set()
        assert entries["Медленный"]["error"] == "Источник не ответил вовремя"
        assert entries["Архив"]["unsupported"]
        assert {book["id"] for book in local} == {shared, private}
        matched, other = entries["Книги"]["result"]["publications"]
        assert matched["downloads"]
        assert {book["id"] for book in matched["matches"]["local"]} == {shared, private}
        assert [source["name"] for source in matched["matches"]["sources"]] == ["Магазин"]
        assert other["matches"] == {"local": [], "local_total": 0, "sources": []}
        purchase, = entries["Магазин"]["result"]["publications"]
        assert purchase["purchase"] and not purchase["downloads"]
    finally:
        release_slow.set()
        assert slow_done.wait(2)

    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json={"username": "reader", "password": "another-password"}).is_success
        local, entries = search(reader)
        assert [book["id"] for book in local] == [shared]
        assert entries["Книги"]["result"]["publications"][0]["matches"]["local"] == [
            {"id": shared, "title": "Полярная ночь", "library": "Общая"}
        ]


def test_opds_authentication_discovery_and_basic_login(library, monkeypatch):
    client, _, _ = library
    calls = []
    document = json.dumps({
        "title": "Тестовый каталог", "description": "Введите номер читательского билета",
        "authentication": [{"type": "http://opds-spec.org/auth/basic",
                            "labels": {"login": "Номер билета", "password": "Код доступа"}}],
        "links": [{"rel": ["help", "alternate"], "href": "/help"},
                  {"rel": "register", "href": "https://accounts.example/register"},
                  {"rel": "logo", "href": "/logo.png"}],
    }).encode()
    feed = b'{"metadata":{"title":"Books"},"links":[]}'
    auth_feed = b'{"metadata":{"title":"Books"},"links":[{"rel":"http://opds-spec.org/auth/document","href":"/auth"}]}'
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(png, format="PNG")
    expected = "Basic " + base64.b64encode(b"reader:catalog-password").decode()

    class Reply(io.BytesIO):
        def __init__(self, body, status, **headers):
            super().__init__(body)
            self.status = status
            self.headers = Message()
            for name, value in headers.items():
                self.headers[name.replace("_", "-")] = value

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

    class Connection:
        def __init__(self, host, port, ip, scheme):
            self.host = host

        def request(self, method, path, headers):
            self.path = path
            self.headers = headers
            calls.append((self.host, path, headers.get("Authorization")))

        def getresponse(self):
            if self.path == "/public":
                return Reply(feed, 200)
            if self.path == "/feed-link":
                return Reply(auth_feed, 200, Content_Type="application/opds+json")
            if self.path == "/header-link":
                return Reply(b"", 401, Link='</about>; rel="about", <https://accounts.example/auth>; '
                             'rel="alternate http://opds-spec.org/auth/document"; type="application/opds-authentication+json"')
            if self.path == "/auth":
                return Reply(document, 200, Content_Type="application/vnd.opds.authentication.v1.0+json")
            if self.path == "/logo.png":
                return Reply(png.getvalue(), 200, Content_Type="image/png")
            if self.path == "/challenge":
                return Reply(b"", 401, WWW_Authenticate='Bearer realm="Books"')
            if self.path == "/redirect":
                return Reply(b"", 302, Location="https://accounts.example/auth")
            if self.headers.get("Authorization") == expected:
                return Reply(feed, 200, Content_Type="application/opds+json")
            return Reply(document, 401, Content_Type="application/opds-authentication+json; charset=utf-8")

        def close(self):
            pass

    monkeypatch.setattr(remote, "PinnedHTTP", Connection)
    monkeypatch.setattr(remote, "resolve", lambda *_: ["93.184.215.14"])
    endpoint = "/api/v1/opds/sources"

    def probe(path):
        result = client.post(endpoint + "/authentication", json={"url": "https://books.example" + path})
        assert result.status_code == 200, result.text
        return result.json()

    with TestClient(main.app) as guest:
        assert guest.post(endpoint + "/authentication", json={"url": "https://books.example/"}).status_code == 401
    inline = probe("/catalog")
    assert inline["secure"] and inline["same_origin"] and inline["protected"]
    profile = inline["document"]
    assert profile["title"] == "Тестовый каталог"
    assert profile["methods"] == [{"type": "http://opds-spec.org/auth/basic", "kind": "basic",
                                    "login": "Номер билета", "password": "Код доступа"}]
    assert profile["links"]["help"] == "https://books.example/help"
    assert client.get(profile["links"]["logo"]).headers["content-type"] == "image/jpeg"
    assert probe("/header-link")["document"]["links"]["help"] == "https://accounts.example/help"
    assert probe("/feed-link")["document"]["methods"] == profile["methods"]
    assert probe("/challenge")["document"]["methods"][0]["kind"] == "bearer"
    assert probe("/public")["document"] is None
    assert not probe("/redirect")["same_origin"]
    assert all(authorization is None for _, _, authorization in calls)

    payload = {"name": "Protected", "url": "https://books.example/catalog",
               "auth": "basic", "username": "reader", "password": "catalog-password"}
    saved = client.post(endpoint, json=payload)
    assert saved.status_code == 201
    assert saved.json()["authenticated"] and payload["password"] not in saved.text
    ident = saved.json()["id"]
    assert client.post(f"{endpoint}/{ident}/test").json()["title"] == "Books"
    assert calls[-1] == ("books.example", "/catalog", expected)
    assert client.put(f"{endpoint}/{ident}", json={**payload, "url": "https://books.example/redirect"}).is_success
    client.post(f"{endpoint}/{ident}/test")
    assert calls[-2:] == [("books.example", "/redirect", expected), ("accounts.example", "/auth", None)]
    assert client.post(endpoint, json={**payload, "url": "http://books.example/catalog"}).status_code == 422


def test_upload_review_matches_fb2_and_filename_only_text(library):
    client, ident, _ = library
    upload(client, ident)
    work, = client.get("/api/v1/books").json()["items"]
    url = f"/api/v1/libraries/{ident}/upload?review_matches=true"
    fb2 = FB2.replace("Снежная книга".encode(), work["title"].encode())
    for name, data in [("other.fb2", fb2), (work["title"] + ".txt", b"plain text")]:
        response = client.post(url, files={"file": (name, data)})
        assert response.status_code == 200
        assert response.json()["matches"][0]["work_id"] == work["id"]
    assert client.post(url, files={"file": ("renamed.epub", epub_bytes())}).json()["duplicate"]
    assert len(client.get("/api/v1/books").json()["items"]) == 1


def test_duplicate_metadata_opds_and_token(library):
    client, ident, _ = library
    item = upload(client, ident)
    assert upload(client, ident)["duplicate"] is True
    assert item["file"]["format"] == "epub"
    works = client.get("/api/v1/books", params={"q": "СОКОЛОВА северная"}).json()["items"]
    assert [work["title"] for work in works] == ["Северная полка"]
    token = client.post("/api/v1/auth/opds-tokens", json={"label": "KOReader"}).json()["token"]
    authorization = "Basic " + base64.b64encode(("admin:" + token).encode()).decode()
    with TestClient(main.app) as guest:
        assert guest.get("/opds/v1").status_code == 401
        headers = {"Authorization": authorization}
        feed = guest.get("/opds/v1?view=all", headers=headers)
        assert feed.headers["content-type"].startswith("application/atom+xml")
        entries = ET.fromstring(feed.content).findall("{*}entry")
        assert len(entries) == 1
        acquisition = entries[0].find(f"{{*}}link[@rel='{remote.ACQ}/open-access']")
        assert acquisition.get("type") == "application/epub+zip"
        assert int(acquisition.get("length")) == len(epub_bytes())
        second = guest.get("/opds/v1", params={"q": "Соколова северная", "page": 2}, headers=headers)
        previous = ET.fromstring(second.content).find("{*}link[@rel='previous']")
        target = urlsplit(previous.get("href"))
        assert len(ET.fromstring(guest.get(target.path + "?" + target.query, headers=headers).content).findall("{*}entry")) == 1
        assert guest.get("/opds/v2", params={"query": "северная"}, headers=headers).json()["publications"]
        assert guest.get(f"/opds/files/{item['file']['id']}", headers=headers).content == epub_bytes()
        token_id = client.get("/api/v1/auth/opds-tokens").json()[0]["id"]
        assert client.delete("/api/v1/auth/opds-tokens/" + token_id).status_code == 204
        assert guest.get("/opds/v1", headers=headers).status_code == 401


def test_opds2_publication_metadata_and_previous_page(library):
    client, ident, _ = library
    file = upload(client, ident)["file"]
    edition = file["edition_id"]
    work = client.get(f"/api/v1/files/{file['id']}/info").json()["work_id"]
    assert client.put(f"/api/v1/books/{work}", json={"title": "Северная полка", "authors": ["Анна Соколова"],
        "genres": ["Фантастика"], "series_memberships": [{"path": ["Цикл", "Подсерия"], "position": "2,5"},
                                                        {"path": ["Дополнения"], "position": "Пролог"}]}).is_success
    assert client.patch(f"/api/v1/editions/{edition}", json={"summary": "Книга о библиотеке", "language": "ru-RU",
        "publisher": "Издательство", "isbn": "978-0-306-40615-7"}).is_success
    page = client.get("/opds/v2", params={"query": "северная полка", "page": 2}).json()
    assert page["metadata"]["currentPage"] == 2 and "publications" not in page
    assert page["navigation"][0]["type"] == "application/opds+json"
    previous, = [link for link in page["links"] if link["rel"] == "previous"]
    destination = urlsplit(previous["href"])
    feed = client.get(destination.path + "?" + destination.query)
    assert feed.headers["content-type"] == "application/opds+json"
    publication, = feed.json()["publications"]
    metadata = publication["metadata"]
    assert metadata["description"] == "Книга о библиотеке"
    assert metadata["publisher"] == "Издательство" and metadata["subject"] == ["Фантастика"]
    assert metadata["altIdentifier"] == ["urn:isbn:9780306406157"]
    first_series, extra = metadata["belongsTo"]["series"]
    assert first_series["name"] == "Цикл / Подсерия" and first_series["position"] == 2.5
    assert extra["name"] == "Дополнения" and "position" not in extra
    details = urlsplit(publication["links"][0]["href"]).path
    assert details == f"/opds/v2/publications/{edition}"
    response = client.get(details)
    assert response.headers["content-type"] == "application/opds-publication+json"
    assert response.json() == publication

    parsed = remote.parse_json(feed.content, str(feed.url))["publications"][0]
    assert parsed["language"] == "ru-RU" and parsed["summary"] == "Книга о библиотеке"
    assert parsed["series_memberships"][0]["position"] == "2.5"
    _, acquisition = parsed["links"]
    assert acquisition["size"] == file["size"] and acquisition["type"] == "application/epub+zip"
    with TestClient(main.app) as guest:
        assert guest.get(details).status_code == 401
    assert client.post(f"/api/v1/books/{work}/trash").status_code == 204
    assert client.get(details).status_code == 404


def test_indexed_import_preview_confirmation_and_immutable_source(library):
    client, _, inbox = library
    file = inbox / "Книга.fb2"
    file.write_bytes(FB2)
    response = client.post("/api/v1/libraries", json={"name": "Диск", "mode": "indexed"})
    ident = response.json()["id"]
    assert client.get("/api/v1/books").json()["items"] == []
    scan = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    jobs = client.get("/api/v1/imports").json()
    assert jobs[0]["result"]["conversions"] == 1
    assert client.get("/api/v1/books").json()["items"] == []
    endpoint = f"/api/v1/imports/{scan['id']}/confirm"
    first = client.post(endpoint).json()["id"]
    assert client.post(endpoint).json()["id"] == first
    assert worker.run_one()
    books_result = client.get("/api/v1/books").json()["items"]
    assert books_result[0]["title"] == "Снежная книга"
    assert books_result[0]["editions"][0]["files"][0]["indexed"]
    assert file.read_bytes() == FB2


def test_progress_conflicts_and_user_isolation(library):
    client, ident, _ = library
    file_id = upload(client, ident)["file"]["id"]
    path = f"/api/v1/reading/progress/{file_id}"
    assert client.get(path).json() == {"version": 0}
    first = {"version": 0, "locator": "epubcfi(/6/2!/4/2)", "percentage": 0.4}
    assert client.put(path, json=first).json()["version"] == 1
    with TestClient(main.app) as browser:
        assert browser.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"}).status_code == 200
        assert browser.get(path).json()["locator"] == first["locator"]
        conflict = browser.put(path, json={**first, "locator": "epubcfi(/6/4!/4/4)"})
        assert conflict.status_code == 409
        assert conflict.json()["current"]["version"] == 1
        client.post("/api/v1/auth/users", json={"username": "reader", "password": "another-password"})
        browser.post("/api/v1/auth/login", json={"username": "reader", "password": "another-password"})
        assert browser.get(path).status_code == 404


def test_reading_chapter_and_approximate_position_between_files(library):
    from pypdf import PdfWriter

    client, _, _ = library
    shared = client.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()["id"]
    epub = upload(client, shared)["file"]["id"]
    work = client.get(f"/api/v1/files/{epub}/info").json()["work_id"]
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=300, height=200)
    pdf = io.BytesIO()
    writer.write(pdf)
    attached = client.post(f"/api/v1/books/{work}/editions/upload", files={"file": ("book.pdf", pdf.getvalue())})
    assert attached.is_success, attached.text
    pdf_id = attached.json()["file"]["id"]
    epub_path = f"/api/v1/reading/progress/{epub}"
    pdf_path = f"/api/v1/reading/progress/{pdf_id}"
    assert client.get(pdf_path).json() == {"version": 0}
    point = {"version": 0, "locator": "epubcfi(/6/2!/4/2)", "percentage": 0.55, "chapter": "  Глава\n2 "}
    saved = client.put(epub_path, json=point).json()
    assert saved["chapter"] == "Глава 2" and saved["locator_type"] == "epub_cfi"
    assert client.get("/api/v1/reading/history").json()[0]["chapter"] == "Глава 2"
    assert client.get("/api/v1/reading/home").json()["reading"][0]["state"]["chapter"] == "Глава 2"
    old_client = {key: value for key, value in point.items() if key != "chapter"}
    assert client.put(epub_path, json={**old_client, "version": 1}).json()["chapter"] == "Глава 2"
    suggested = client.get(pdf_path).json()
    assert suggested["version"] == 0 and "locator" not in suggested
    assert suggested["alternate"] == {"file_id": epub, "format": "epub", "percentage": 0.55,
                                      "updated": suggested["alternate"]["updated"]}

    credentials = {"username": "reader", "password": "another-password"}
    assert client.post("/api/v1/auth/users", json=credentials).is_success
    with TestClient(main.app) as other:
        assert other.post("/api/v1/auth/login", json=credentials).is_success
        assert other.get(pdf_path).json() == {"version": 0}
    accepted = client.put(pdf_path, json={"version": 0, "locator": "3", "percentage": 0.75}).json()
    assert accepted["locator_type"] == "pdf_page" and accepted["chapter"] is None
    assert "alternate" not in client.get(pdf_path).json()
    exact = client.get(epub_path).json()
    assert exact["chapter"] == "Глава 2" and exact["locator"] == point["locator"]
    assert exact["alternate"]["file_id"] == pdf_id and exact["alternate"]["percentage"] == 0.75


def test_opds_atom_import_through_mock_server(library, monkeypatch):
    from contextlib import contextmanager
    from io import BytesIO
    client, ident, _ = library
    root = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Mock</title>
      <link rel="search" href="search.xml" type="application/opensearchdescription+xml"/>
      <link rel="http://opds-spec.org/sort/popular" href="popular" type="application/atom+xml"/>
      <entry><title>Section</title><link rel="subsection" href="section" type="application/atom+xml"/></entry></feed>"""
    entry = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog"
      xmlns:thr="http://purl.org/syndication/thread/1.0"><title>Books</title>
      <link rel="previous" href="root" type="application/atom+xml"/>
      <link rel="http://opds-spec.org/facet" opds:facetGroup="Language" opds:activeFacet="true" title="All" href="section"/>
      <link rel="http://opds-spec.org/facet" opds:facetGroup="Language" title="English" href="english" thr:count="1"/>
      <entry><id>book-1</id><title>Book</title><author><name>Reader</name></author>
      <link rel="http://opds-spec.org/acquisition/open-access" href="book.epub" type="application/epub+zip"/>
      <link rel="http://opds-spec.org/acquisition/buy" href="store"/></entry></feed>"""
    search = b"""<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
      <Url type="application/atom+xml" template="section?q={searchTerms}"/></OpenSearchDescription>"""

    class Response(BytesIO):
        def getheader(self, name, default=None):
            return default

    @contextmanager
    def mock_fetch(url, source, credentials):
        name = url.rsplit("/", 1)[-1].split("?")[0]
        feeds = {"root": root, "search.xml": search, "section": entry, "book.epub": epub_bytes(),
                 "popular": entry.replace(b"<title>Books", b"<title>Popular books"),
                 "english": entry.replace(b"<title>Books", b"<title>English books")}
        yield Response(feeds[name]), url

    monkeypatch.setattr(remote, "fetch", mock_fetch)
    monkeypatch.setattr(worker, "fetch", mock_fetch)
    sid = client.post("/api/v1/opds/sources", json={"name": "Mock", "url": "https://catalog.example/root"}).json()["id"]
    prefix = f"/api/v1/opds/sources/{sid}"
    test = client.post(prefix + "/test").json()
    assert test["version"] == "1.x"
    home = client.post(prefix + "/browse", json={}).json()
    sorting, = home["facets"]
    popular, = sorting["options"]
    assert sorting["sorting"] and popular["sort"] == "popular"
    assert client.post(prefix + "/browse", json={"link": popular["link"]}).json()["title"] == "Popular books"
    nav = home["navigation"][0]["link"]
    nested = client.post(prefix + "/browse", json={"link": nav}).json()
    language, = nested["facets"]
    all_books, english = language["options"]
    assert language["title"] == "Language" and all_books["active"]
    assert english["count"] == 1 and not english["active"]
    assert client.post(prefix + "/browse", json={"link": english["link"]}).json()["title"] == "English books"
    assert client.post(prefix + "/browse", json={"link": nested["previous"]}).json()["title"] == "Mock"
    results = client.get(prefix + "/search?q=Book").json()
    assert nested["publications"][0]["title"] == results["publications"][0]["title"]
    downloads = results["publications"][0]["downloads"]
    assert len(downloads) == 1
    payload = {"source_id": sid, "library_id": ident, "link": downloads[0]["link"]}
    for duplicate in (False, True):
        task = client.post("/api/v1/opds/downloads", json=payload).json()
        assert worker.run_one()
        completed = client.get(f"/api/v1/opds/downloads/{task['id']}").json()
        assert completed["status"] == "done", completed
        assert completed["result"]["duplicate"] is duplicate

    strict = client.post("/api/v1/libraries", json={"name": "Только FB2", "mode": "managed", "formats": ["fb2"]}).json()["id"]
    denied = client.post("/api/v1/opds/downloads", json={**payload, "library_id": strict}).json()
    assert worker.run_one()
    report = client.get(f"/api/v1/opds/downloads/{denied['id']}").json()
    assert report["status"] == "error" and "запрещён" in report["error"]
    assert client.get("/api/v1/books", params={"library_id": strict}).json()["items"] == []


def test_opds_download_attaches_edition_and_retains_public_metadata(library, monkeypatch):
    import hashlib
    import json
    from contextlib import contextmanager
    from io import BytesIO

    client, ident, _ = library
    existing = upload(client, ident)["file"]["id"]
    target = client.get(f"/api/v1/files/{existing}/info").json()["work_id"]
    fb2 = FB2.replace("Снежная книга".encode(), "Северная полка".encode())
    acquisition = "https://origin.example/book.fb2?token=temporary"
    feed = json.dumps({"metadata":{"title":{"en":"Source","ru-RU":"Источник"}},"links":[],"publications":[{
        "metadata":{"identifier":"https://origin.example/id?token=temporary",
                    "title":{"en":"Northern shelf","ru":"Северная полка"},
                    "description":{"ja":"本","en":"Book description"},
                    "author":{"name":{"en":"Anna Sokolova","ru":"Анна Соколова"}},"language":["ru"],
                    "publisher":[{"name":{"ru":"Издатель каталога"}}], "altIdentifier":["urn:isbn:9780306406157"],
                    "belongsTo":{"series":{"name":{"ru":"Цикл источника"},"position":2.5}}},
        "links":[{"href":acquisition,"rel":"download","type":"application/x-fictionbook+xml","length":len(fb2)},
                 {"href":"/store","rel":["acquisition", "buy"],"type":"application/x-fictionbook+xml"}],
    }]}).encode()

    class Response(BytesIO):
        def getheader(self, name, default=None):
            return default

    @contextmanager
    def fetch(url, *_):
        yield Response(fb2 if url == acquisition else feed), url

    def convert(_, __, directory):
        output = directory / "converted.epub"
        output.write_bytes(epub_bytes() + b"converted")
        return output

    monkeypatch.setattr(remote,"fetch",fetch)
    monkeypatch.setattr(worker,"fetch",fetch)
    monkeypatch.setattr(worker,"convert_file",convert)
    source = client.post("/api/v1/opds/sources", json={"name":"Проба","url":"https://origin.example/catalog"}).json()["id"]
    catalogue = client.post(f"/api/v1/opds/sources/{source}/browse", json={}).json()
    assert catalogue["title"] == "Источник"
    publication, = catalogue["publications"]
    assert publication["title"] == "Северная полка" and publication["summary"] == "Book description"
    assert publication["series_memberships"] == [{"path":["Цикл источника"],"position":"2.5"}]
    assert publication["purchase"] is True
    link, = publication["downloads"]
    assert link["size"] == len(fb2) and "temporary" not in link["link"]
    request = {"source_id":source,"library_id":ident,"link":link["link"],"read_after":True}
    preview = client.post("/api/v1/opds/downloads/preview",json=request)
    assert preview.is_success, preview.text
    assert preview.json()["matches"][0]["work_id"] == target
    assert client.post("/api/v1/opds/downloads",json={**request,"work_id":target + 1000}).status_code == 409
    job = client.post("/api/v1/opds/downloads",json={**request,"work_id":target}).json()["id"]
    assert worker.run_one()
    task = f"/api/v1/opds/downloads/{job}"
    report = client.get(task).json()["result"]
    assert report["sha256"] == hashlib.sha256(fb2).hexdigest()
    assert report["provenance"]["identifier"] == "" and report["provenance"]["source"] == "Проба"
    assert report["provenance"]["metadata"]["authors"] == ["Анна Соколова"]
    assert report["provenance"]["metadata"]["publisher"] == "Издатель каталога"
    assert report["provenance"]["metadata"]["isbn"] == "9780306406157"
    editions = client.get(f"/api/v1/books/{target}").json()["editions"]
    assert any(edition["isbn"] == "9780306406157" and edition["publisher"] == "Издатель каталога" for edition in editions)
    assert report["conversion_id"] and report["read_file_id"] is None
    assert worker.run_one()
    read_file = client.get(task).json()["result"]["read_file_id"]
    info = client.get(f"/api/v1/files/{read_file}/info").json()
    assert info["work_id"] == target and info["format"] == "epub"
    assert len(client.get(f"/api/v1/books/{target}").json()["editions"]) == 2
    with db.Session() as session:
        assert "link" not in session.get(db.Job,job).payload


def test_download_file_without_import_requires_owner_and_expires(library, monkeypatch):
    import hashlib
    import os
    from contextlib import contextmanager
    from datetime import timedelta
    from io import BytesIO
    from app import direct_downloads

    client, library_id, inbox = library
    monkeypatch.setattr(direct_downloads, "DATA", inbox.parent / "artifacts")
    contents = epub_bytes()
    feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Mock</title><entry><id>urn:test:direct</id><title>Sample</title>
      <link rel="http://opds-spec.org/acquisition/open-access" href="book.epub?token=short-lived" type="application/epub+zip"/>
      </entry></feed>'''

    class Response(BytesIO):
        def getheader(self, name, default=None):
            return str(len(self.getbuffer())) if name == "Content-Length" else default

    @contextmanager
    def fetch(url, *_):
        yield Response(contents if "/book.epub" in url else feed), url

    monkeypatch.setattr(remote, "fetch", fetch)
    monkeypatch.setattr(worker, "fetch", fetch)
    source = client.post("/api/v1/opds/sources", json={"name":"Mock", "url":"https://catalog.example/catalog"}).json()["id"]
    publication = client.post(f"/api/v1/opds/sources/{source}/browse", json={}).json()["publications"][0]
    data = {"source_id":source, "delivery":"file", "link":publication["downloads"][0]["link"]}
    assert client.post("/api/v1/opds/downloads", json={**data,"library_id":library_id}).status_code == 422
    queued = client.post("/api/v1/opds/downloads", json=data)
    assert queued.status_code == 202 and queued.json()["library_id"] is None
    task = queued.json()["id"]
    endpoint = f"/api/v1/opds/downloads/{task}/file"
    assert client.get(endpoint).status_code == 409
    assert worker.run_one()
    report = client.get(f"/api/v1/opds/downloads/{task}").json()["result"]
    assert report["sha256"] == hashlib.sha256(contents).hexdigest() and report["delivery"] == "file"
    assert "short-lived" not in str(report)
    response = client.get(endpoint)
    assert response.content == contents and response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-type"] == "application/epub+zip"
    partial = client.get(endpoint, headers={"Range":"bytes=0-3"})
    assert partial.status_code == 206 and partial.content == contents[:4]
    assert client.get("/api/v1/books").json()["items"] == []

    credentials = {"username":"other", "password":"password-for-another-user"}
    assert client.post("/api/v1/auth/users", json=credentials).is_success
    with TestClient(main.app) as other:
        assert other.post("/api/v1/auth/login", json=credentials).is_success
        assert other.get(endpoint).status_code == 404
        other_job = other.post("/api/v1/opds/downloads", json=data)
        assert other_job.status_code == 202
        assert other.post(f"/api/v1/opds/downloads/{other_job.json()['id']}/cancel").is_success
        assert not direct_downloads.artifact_path(other_job.json()["id"]).exists()

    with db.Session.begin() as session:
        job = session.get(db.Job, task)
        assert "link" not in job.payload
        job.result = {**job.result, "expires_at":(db.now() - timedelta(minutes=1)).isoformat()}
    assert client.get(endpoint).status_code == 410
    path = direct_downloads.artifact_path(task)
    old = (db.now() - timedelta(hours=26)).timestamp()
    os.utime(path, (old,old))
    direct_downloads.clean_artifacts()
    assert not path.exists()


def test_private_address_requires_explicit_host_and_ip(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 80))])
    monkeypatch.delenv("OPDS_PRIVATE_ALLOW", raising=False)
    with pytest.raises(ValueError):
        remote.resolve("catalog.example", 80)
    monkeypatch.setenv("OPDS_PRIVATE_ALLOW", "catalog.example=127.0.0.1")
    assert remote.resolve("catalog.example", 80) == ["127.0.0.1"]


def test_preview_web_rejects_cross_origin(library):
    client, ident, _ = library
    response = client.post(f"/api/v1/libraries/{ident}/scan", json={}, headers={"Origin": "https://foreign.example"})
    assert response.status_code == 403


def test_koreader_link_and_native_position(library):
    import hashlib
    client, ident, _ = library
    file_id = upload(client, ident)["file"]["id"]
    credentials = client.post("/api/v1/devices/koreader/credentials").json()
    key = hashlib.md5(credentials["password"].encode(), usedforsecurity=False).hexdigest()
    headers = {"x-auth-user": credentials["username"], "x-auth-key": key}
    document = "a" * 32
    assert client.get("/koreader/users/auth", headers=headers).json() == {"authorized": "OK"}
    assert client.get(f"/koreader/syncs/progress/{document}", headers=headers).json() == {}
    payload = {"document": document, "progress": "/body/DocFragment[2]/body/p[2]",
               "percentage": 0.41, "device": "KOReader test", "device_id": "test-device"}
    assert client.put("/koreader/syncs/progress", json=payload, headers=headers).status_code == 200
    other_position = {**payload, "progress": "/body/DocFragment[3]/body/p[4]/text().7", "device_id": "second-device",
                      "percentage": 0.73}
    second = client.put("/koreader/syncs/progress", json=other_position, headers=headers)
    assert second.is_success
    assert client.put("/koreader/syncs/progress", json=other_position, headers=headers).json() == second.json()
    history_url = f"/api/v1/devices/koreader/documents/{document}/history"
    history = client.get(history_url).json()
    first, earlier = history["items"]
    assert history["next_before"] is None
    assert (first["progress"], first["device_id"], first["file_id"]) == (other_position["progress"], "second-device", None)
    assert (earlier["progress"], earlier["device_id"]) == (payload["progress"], "test-device")
    assert client.get(history_url, params={"before": first["id"]}).json()["items"] == [earlier]
    progress_path = f"/api/v1/reading/progress/{file_id}"
    assert client.get(progress_path).json() == {"version": 0}
    assert client.put("/api/v1/devices/koreader/documents", json={"document": document, "file_id": file_id}).is_success
    assert client.get(progress_path).json()["koreader"]["percentage"] == 0.73
    linked_event = client.get("/api/v1/reading/history").json()[0]
    assert linked_event["file_id"] == file_id and linked_event["locator"] == other_position["progress"]
    assert client.get(history_url).json() == history
    assert client.put(progress_path, json={"version": 0, "locator": "epubcfi(/6/4!/4/2:5)", "percentage": 0.5}).is_success
    reply = client.get(f"/koreader/syncs/progress/{document}", headers=headers).json()
    assert reply["progress"] == other_position["progress"]
    exported = client.get("/api/v1/reading/export").json()["koreader_history"]
    assert {event["locator"] for event in exported} == {payload["progress"], other_position["progress"]}
    alternate = {"username": "ko-other", "password": "another-test-password"}
    assert client.post("/api/v1/auth/users", json=alternate).is_success
    with TestClient(main.app) as other:
        assert other.post("/api/v1/auth/login", json=alternate).is_success
        assert other.get(history_url).json()["items"] == []
    assert client.delete("/api/v1/devices/koreader/credentials").status_code == 204
    assert client.get("/koreader/users/auth", headers=headers).status_code == 401


def test_opds_json_over_http_and_redirect_rechecking(monkeypatch):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    content = json.dumps({"metadata": {"title": "OPDS JSON"},
                          "links": [{"rel": "self", "href": "/catalog", "type": "application/opds+json"}],
                          "publications": [{"metadata": {"title": "Книга"}, "links": [
                              {"rel": remote.ACQ, "href": "/book.epub", "type": "application/epub+zip"}]}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/catalog")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/opds+json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPDS_PRIVATE_ALLOW", "127.0.0.1=127.0.0.1")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        parsed = remote.read_catalog(base + "/catalog", base, None)
        assert parsed["version"] == "2.0"
        assert parsed["publications"][0]["links"][0]["url"] == base + "/book.epub"
        with pytest.raises(ValueError, match="Адрес источника запрещён"):
            remote.read_catalog(base + "/redirect", base, None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_private_shelf_collections_and_export(library):
    client, _, _ = library
    shared = client.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()
    file_id = upload(client, shared["id"])["file"]["id"]
    work_id = client.get(f"/api/v1/files/{file_id}/info").json()["work_id"]
    collection = client.post("/api/v1/collections", json={"name": "Лучшее"}).json()
    item_path = f"/api/v1/collections/{collection['id']}/books/{work_id}"
    assert client.put(item_path).status_code == 204
    assert client.put(item_path).status_code == 204
    assert client.post("/api/v1/collections", json={"name": " лучшее "}).status_code == 409
    assert client.get(f"/api/v1/collections/{collection['id']}").json()["items"][0]["id"] == work_id
    state_path = f"/api/v1/reading/states/{work_id}"
    assert client.patch(state_path, json={"status": "planned", "favorite": True}).is_success
    assert client.get("/api/v1/reading/shelf?view=planned").json()["items"][0]["book"]["id"] == work_id
    response = client.put(f"/api/v1/reading/progress/{file_id}", json={
        "version": 0, "locator": "epubcfi(/6/2!/4/2)", "percentage": 0.4, "device": "Планшет",
    })
    assert response.is_success
    reading = client.get("/api/v1/reading/home").json()
    assert reading["reading"][0]["state"]["status"] == "reading"
    assert reading["reading"][0]["state"]["device"] == "Планшет"
    assert reading["statistics"]["reading"] == 1
    assert client.patch(state_path, json={"status": "finished"}).json()["finished"]
    assert client.get("/api/v1/reading/home").json()["statistics"]["finished_this_month"] == 1
    assert client.get("/api/v1/reading/export").json()["collection_items"] == [
        {"collection_id": collection["id"], "work_id": work_id},
    ]

    assert client.post("/api/v1/auth/users", json={"username": "reader", "password": "another-password"}).is_success
    with TestClient(main.app) as other:
        assert other.post("/api/v1/auth/login", json={"username": "reader", "password": "another-password"}).is_success
        assert other.get(state_path).json()["status"] == "unread"
        assert other.get("/api/v1/reading/shelf?view=favorites").json()["items"] == []
        assert other.get(f"/api/v1/collections/{collection['id']}").status_code == 404
        assert other.put(item_path).status_code == 404
        assert other.get("/api/v1/reading/export").json()["states"] == []

    assert client.delete(f"/api/v1/collections/{collection['id']}").status_code == 204
    assert client.get("/api/v1/reading/export").json()["collection_items"] == []
    assert client.get(f"/api/v1/books/{work_id}").is_success


def test_notes_and_idempotent_reading_sessions(library, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import reading

    client, ident, _ = library
    file_id = upload(client, ident)["file"]["id"]
    annotation_path = f"/api/v1/reading/annotations/{file_id}"
    payload = {"locator": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "quote": "Текст", "note": "Перечитать"}
    row = client.post(annotation_path, json=payload).json()
    assert client.post(annotation_path, json=payload).json()["id"] == row["id"]
    edit = annotation_path + "/" + str(row["id"])
    assert client.patch(edit, json={"note": "Заметка изменена"}).json()["note"] == "Заметка изменена"
    assert client.get("/api/v1/reading/export").json()["annotations"][0]["quote"] == "Текст"
    assert client.delete(edit).status_code == 204
    assert client.get(annotation_path).json() == []

    clock = datetime(2026, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(reading, "now", lambda: clock)
    path = f"/api/v1/reading/sessions/{file_id}"
    session = {"session_id": "1" * 32, "sequence": 0, "active_seconds": 0}
    assert client.put(path, json=session).status_code == 204
    clock += timedelta(seconds=20)
    tick = {**session, "sequence": 1, "active_seconds": 40}
    assert client.put(path, json=tick).status_code == 204
    assert client.put(path, json=tick).status_code == 204
    assert client.get("/api/v1/reading/sessions").json()[0]["seconds"] == 20
    assert client.get("/api/v1/reading/home").json()["statistics"]["seconds"] == 20
    assert client.put(path, json={**session, "session_id": "2" * 32}).status_code == 204
    assert client.get("/api/v1/reading/sessions?limit=1").json()[0]["seconds"] == 0
    assert client.get("/api/v1/reading/sessions?limit=1&offset=1").json()[0]["seconds"] == 20
    assert client.get("/api/v1/reading/sessions?limit=1&offset=2").json() == []


def test_library_settings_update_visibility_without_reimport(library):
    admin, library_id, _ = library
    original = upload(admin, library_id)["file"]
    file_path = f"/api/v1/files/{original['id']}"
    progress_path = f"/api/v1/reading/progress/{original['id']}"
    endpoint = f"/api/v1/libraries/{library_id}"
    credentials = {"username": "library-reader", "password": "library-reader-password"}
    assert admin.post("/api/v1/auth/users", json=credentials).is_success
    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json=credentials).is_success
        token = reader.post("/api/v1/auth/opds-tokens", json={"label": "Reader"}).json()["token"]
        opds_headers = {"Authorization": "Bearer " + token}
        acquisition = f"/opds/files/{original['id']}"
        assert reader.get(file_path).status_code == reader.get(acquisition, headers=opds_headers).status_code == 404

        changes = {"name": "  Читальный зал  ", "description": "Пробная общая библиотека", "shared": True}
        updated = admin.patch(endpoint, json=changes)
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Читальный зал" and updated.json()["description"] == changes["description"]
        assert reader.get("/api/v1/books", params={"library_id": library_id}).json()["items"]
        assert reader.get(file_path).content == reader.get(acquisition, headers=opds_headers).content == epub_bytes()
        assert reader.patch(endpoint, json={"name": "Чужая правка"}).status_code == 404
        point = {"version": 0, "locator": "epubcfi(/6/2!/4/2:1)", "percentage": 0.25}
        assert reader.put(progress_path, json=point).is_success

        own = reader.post("/api/v1/libraries", json={"name": "Моя", "mode": "managed"}).json()["id"]
        own_url = f"/api/v1/libraries/{own}"
        assert reader.patch(own_url, json={"description": "Личная библиотека"}).json()["description"] == "Личная библиотека"
        assert reader.patch(own_url, json={"shared": True}).status_code == 403
        assert admin.patch(endpoint, json={"folder": "other"}).status_code == 422
        assert admin.patch(endpoint, json={"name": "   "}).status_code == 422

        assert admin.patch(endpoint, json={"shared": False}).json()["shared"] is False
        assert reader.get("/api/v1/books", params={"library_id": library_id}).json()["items"] == []
        assert reader.get(file_path).status_code == reader.get(acquisition, headers=opds_headers).status_code == 404
        assert reader.get(progress_path).status_code == 404
        assert admin.get(file_path).content == epub_bytes()
        assert admin.patch(endpoint, json={"shared": True}).is_success
        assert reader.get(progress_path).json()["locator"] == point["locator"]


def test_roles_password_rotation_and_session_revocation(library):
    admin, _, _ = library
    password = "another-password"
    credentials = {"username": "reader", "password": password}
    account = admin.post("/api/v1/auth/users", json={**credentials, "read_only": True}).json()
    with TestClient(main.app) as client, TestClient(main.app) as other:
        login = "/api/v1/auth/login"
        assert client.post(login, json=credentials).is_success
        assert other.post(login, json=credentials).is_success
        assert client.post("/api/v1/libraries", json={"name": "Книги", "mode": "managed"}).status_code == 403
        sessions = client.get("/api/v1/auth/sessions").json()
        other_id = next(row["id"] for row in sessions if not row["current"])
        assert client.delete("/api/v1/auth/sessions/" + other_id).status_code == 204
        assert other.get("/api/v1/auth/me").status_code == 401

        new_password = "changed-password"
        change = client.put("/api/v1/auth/password", json={"current_password": password, "password": new_password})
        assert change.is_success and client.get("/api/v1/auth/me").is_success
        assert other.post(login, json=credentials).status_code == 401
        assert other.post(login, json={**credentials, "password": new_password}).is_success

        user_path = f"/api/v1/auth/users/{account['id']}"
        assert admin.patch(user_path + "/role", json={"role": "member"}).is_success
        new_library = client.post("/api/v1/libraries", json={"name": "Мои книги", "mode": "managed"})
        assert new_library.is_success
        assert admin.patch(user_path + "/role", json={"role": "reader"}).is_success
        assert client.post(f"/api/v1/libraries/{new_library.json()['id']}/upload",
                           files={"file": ("book.epub", epub_bytes())}).status_code == 404
        assert admin.put(user_path + "/password", json={"password": "reset-password"}).status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
        assert other.get("/api/v1/auth/me").status_code == 401

