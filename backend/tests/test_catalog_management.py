import time
from datetime import timedelta
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app import books, db, main, worker
from tests.fixtures import FB2, epub_bytes
from tests.test_app import library, upload


def test_schedule_modes_and_incremental_scan(library, monkeypatch):
    client, ident, inbox = library
    endpoint = f"/api/v1/libraries/{ident}/scan-settings"
    assert client.get(endpoint).json()["mode"] == "off"
    assert client.put(endpoint, json={"mode": "preview", "folder": "../"}).status_code == 422
    assert client.put(endpoint, json={"mode": "preview", "interval_minutes": 0}).status_code == 422
    (inbox / "a.txt").write_bytes(b"first text")
    changed = inbox / "b.txt"
    changed.write_bytes(b"second text")
    (inbox / "other.epub").write_bytes(epub_bytes())
    reads = []
    fingerprint = worker.fingerprint

    def tracked(path):
        reads.append(path.name)
        return fingerprint(path)

    monkeypatch.setattr(worker, "fingerprint", tracked)

    def due():
        with db.Session.begin() as session:
            session.get(db.Library, ident).scan_due = db.now() - timedelta(seconds=1)
        worker.enqueue_due_scans()

    configured = {"mode": "preview", "interval_minutes": 1, "formats": ["txt", "txt"]}
    assert client.put(endpoint, json=configured).json()["formats"] == ["txt"]
    due()
    worker.enqueue_due_scans()
    assert worker.run_one()
    assert not worker.run_one()
    plan, = client.get("/api/v1/imports").json()
    assert plan["result"]["found"] == 2
    assert reads == ["a.txt", "b.txt"]
    assert client.get("/api/v1/books").json()["items"] == []

    changed.write_bytes(b"changed text")
    assert client.put(endpoint, json={**configured, "mode": "apply"}).is_success
    due()
    assert worker.run_one()
    assert worker.run_one()
    imported, current, _ = client.get("/api/v1/imports").json()
    assert current["result"]["reused"] == 1
    assert current["result"]["confirmed"] == imported["id"]
    assert imported["result"]["added"] == 2
    assert reads == ["a.txt", "b.txt", "b.txt"]
    due()
    assert worker.run_one()
    assert not worker.run_one()
    unchanged = client.get("/api/v1/imports").json()[0]["result"]
    assert unchanged["reused"] == 2 and unchanged["automatic"] is True
    assert reads == ["a.txt", "b.txt", "b.txt"]

    changed.write_bytes(b"new version with a different length")
    due()
    assert worker.run_one()
    assert client.put(endpoint, json={"mode": "off"}).json()["next_scan"] is None
    assert worker.run_one()
    assert client.get("/api/v1/imports").json()[0]["status"] == "error"
    assert changed.read_bytes() == b"new version with a different length"


def test_library_formats_limit_new_originals_only(library, monkeypatch):
    client, ident, inbox = library
    original = upload(client, ident, b"original text", "original.txt")["file"]
    endpoint = f"/api/v1/libraries/{ident}"
    (inbox / "book.fb2").write_bytes(FB2)
    text_source = inbox / "rejected.txt"
    text_source.write_bytes(b"second text")
    unrestricted = client.post(endpoint + "/scan", json={}).json()
    assert worker.run_one()

    assert client.patch(endpoint, json={"formats": ["fb2", "fb2"]}).json()["formats"] == ["fb2"]
    assert client.patch(endpoint, json={"formats": ["exe"]}).status_code == 422
    wrong = client.post(endpoint + "/upload", files={"file": ("mislabelled.fb2", epub_bytes())})
    assert wrong.status_code == 422 and "запрещён" in wrong.json()["detail"]
    assert client.get(f"/api/v1/files/{original['id']}").content == b"original text"

    job = client.post(f"/api/v1/imports/{unrestricted['id']}/confirm").json()
    assert worker.run_one()
    report = next(item["result"] for item in client.get("/api/v1/imports").json() if item["id"] == job["id"])
    assert report["added"] == 1
    assert [entry["path"] for entry in report["errors"]] == ["rejected.txt"]
    assert text_source.read_bytes() == b"second text"

    def convert(*args):
        path = args[2] / "derived.epub"
        path.write_bytes(epub_bytes())
        return path

    monkeypatch.setattr(worker, "convert_file", convert)
    assert worker.run_one()
    with db.Session() as session:
        assert session.query(db.BookFile).filter_by(format="epub").one().derived_from is not None
    assert client.post(endpoint + "/scan", json={}).is_success
    assert worker.run_one()
    plan = client.get("/api/v1/imports").json()[0]["result"]
    assert [item["path"] for item in plan["items"]] == ["book.fb2"]

    zipped = inbox / "hidden-extension.epub"
    with ZipFile(zipped, "w") as archive:
        archive.writestr("contained.fb2", FB2)
    assert client.patch(endpoint, json={"formats": ["fb2.zip"]}).is_success
    assert client.post(endpoint + "/scan", json={}).is_success
    assert worker.run_one()
    detected = client.get("/api/v1/imports").json()[0]["result"]["items"]
    assert [(item["path"], item["format"]) for item in detected] == [(zipped.name, "fb2.zip")]

    assert client.put(endpoint + "/scan-settings", json={"mode": "preview", "formats": ["txt"]}).is_success
    with db.Session.begin() as session:
        session.get(db.Library, ident).scan_due = db.now() - timedelta(seconds=1)
    worker.enqueue_due_scans()
    assert worker.run_one()
    assert client.get("/api/v1/imports").json()[0]["result"]["found"] == 0

    assert client.patch(endpoint, json={"formats": []}).json()["formats"] == []
    assert client.post(endpoint + "/upload", files={"file": ("again.txt", b"second text")}).status_code == 201


def test_indexed_refresh_handles_replacements_and_renames(library):
    client, _, inbox = library
    ident = client.post("/api/v1/libraries", json={"name": "Индекс", "mode": "indexed"}).json()["id"]
    original = inbox / "source.txt"
    original.write_bytes(b"one")

    def index():
        plan = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
        assert worker.run_one()
        assert client.post(f"/api/v1/imports/{plan['id']}/confirm").is_success
        assert worker.run_one()

    index()
    work, = client.get("/api/v1/books").json()["items"]
    file, = work["editions"][0]["files"]
    endpoint = f"/api/v1/files/{file['id']}"
    assert client.get(endpoint).content == b"one"
    moved = original.rename(inbox / "moved.txt")
    original.write_bytes(b"two")
    assert client.get(endpoint).status_code == 404
    index()
    assert client.get(endpoint).content == b"one"
    works = client.get("/api/v1/books").json()["items"]
    assert len(works) == 2
    assert {moved.read_bytes(), original.read_bytes()} == {b"one", b"two"}


def test_filesystem_event_advances_scheduled_scan(library):
    client, ident, inbox = library
    endpoint = f"/api/v1/libraries/{ident}/scan-settings"
    assert client.put(endpoint, json={"mode": "preview", "watch": True, "interval_minutes": 60}).is_success
    with worker.ScanWatch(inbox) as watch:
        worker.enqueue_due_scans(watch)
        assert not worker.run_one()
        (inbox / "new.txt").write_bytes(b"event driven")
        timeout = time.monotonic() + 8
        while time.monotonic() < timeout:
            worker.enqueue_due_scans(watch)
            if worker.run_one():
                break
            time.sleep(0.2)
        else:
            raise AssertionError("Событие не запустило сканирование")
    plan, = client.get("/api/v1/imports").json()
    assert plan["result"]["items"][0]["path"] == "new.txt"


def work_for(client, file_id):
    return client.get(f"/api/v1/files/{file_id}/info").json()["work_id"]


def titled_epub(title):
    result = BytesIO()
    with ZipFile(BytesIO(epub_bytes())) as template, ZipFile(result, "w") as output:
        for entry in template.infolist():
            data = template.read(entry)
            if entry.filename == "OPS/book.opf":
                data = data.replace("Северная полка".encode(), title.encode())
            output.writestr(entry, data)
    return result.getvalue()


def test_import_plan_choices_and_safe_undo(library, monkeypatch):
    client, ident, inbox = library
    monkeypatch.setattr(worker, "enqueue_conversion", lambda *args: None)
    known = work_for(client, upload(client, ident)["file"]["id"])
    files = {
        "a.fb2": FB2.replace("Снежная книга".encode(), "Северная полка".encode()),
        "b.epub": titled_epub("Новый цикл"),
        "c.fb2": FB2.replace("Снежная книга".encode(), "Новый цикл".encode()),
        "omit.txt": b"skip this",
    }
    for name, data in files.items():
        (inbox / name).write_bytes(data)
    preview = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    plan = client.get("/api/v1/imports").json()[0]["result"]
    assert plan["candidates"] == 2
    items = {item["path"]: item for item in plan["items"]}
    assert items["a.fb2"]["matches"][0]["work_id"] == known
    assert items["c.fb2"]["incoming"][0]["path"] == "b.epub"
    endpoint = f"/api/v1/imports/{preview['id']}/confirm"
    payload = {"choices": {"a.fb2": {"action": "work", "work_id": known},
                           "c.fb2": {"action": "incoming", "path": "b.epub"},
                           "omit.txt": {"action": "skip"}}}
    assert client.post(endpoint, json={"choices":{"b.epub":{"action":"incoming","path":"c.fb2"}}}).status_code == 422
    assert client.post(endpoint, json={"choices":{"c.fb2":payload["choices"]["c.fb2"],"b.epub":{"action":"skip"}}}).status_code == 422
    task = client.post(endpoint, json=payload)
    assert task.is_success, task.text
    assert client.post(endpoint, json=payload).json()["id"] == task.json()["id"]
    assert client.post(endpoint, json={"choices": {}}).status_code == 409
    assert worker.run_one()
    report = next(job["result"] for job in client.get("/api/v1/imports").json() if job["id"] == task.json()["id"])
    assert (report["added"], report["attached"], report["skipped"], report["errors"]) == (3, 2, 1, [])
    added, = report["work_ids"]
    for work in (known, added):
        item = client.get(f"/api/v1/books/{work}").json()
        assert {entry["files"][0]["format"] for entry in item["editions"]} == {"epub", "fb2"}
    assert client.post(f"/api/v1/imports/{task.json()['id']}/undo").status_code == 204
    assert client.get(f"/api/v1/books/{added}").status_code == 404
    assert len(client.get(f"/api/v1/books/{known}").json()["editions"]) == 2
    assert {name: (inbox / name).read_bytes() for name in files} == files

    (inbox / "another.txt").write_bytes(b"new book")
    later = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()["id"]
    assert worker.run_one()
    imported = client.post(f"/api/v1/imports/{later}/confirm", json={"choices":{"omit.txt":{"action":"skip"}}}).json()["id"]
    assert worker.run_one()
    new_work, = next(job["result"]["work_ids"] for job in client.get("/api/v1/imports").json() if job["id"] == imported)
    assert client.post(f"/api/v1/books/{new_work}/editions/upload", files={"file":("extra.txt", b"other file")}).is_success
    assert client.post(f"/api/v1/imports/{imported}/undo").status_code == 409


def test_hierarchical_series_import_and_navigation(library):
    client, private, _ = library
    shared = client.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()["id"]
    prefix = ["Северный цикл", "Вторая эпоха"]

    def book_for(ident, data, name):
        work = work_for(client, upload(client, ident, data, name)["file"]["id"])
        return client.get(f"/api/v1/books/{work}").json()

    fb2 = FB2.replace(b"</title-info>", ('<sequence name="Северный цикл"><sequence name="Вторая эпоха" number="10"/></sequence>'
        '<sequence name="Антология" number="5"/></title-info>').encode())
    last = book_for(shared, fb2, "beyond.fb2")
    assert [(entry["path"], entry["position"]) for entry in last["series_memberships"]] == [(prefix, "10"), (["Антология"], "5")]

    packed = BytesIO()
    with ZipFile(BytesIO(epub_bytes())) as original, ZipFile(packed, "w") as output:
        for entry in original.infolist():
            content = original.read(entry)
            if entry.filename == "OPS/book.opf":
                content = content.replace(b"</metadata>", ('<meta property="belongs-to-collection" id="sub">Вторая эпоха</meta>'
                    '<meta property="belongs-to-collection" refines="#sub" id="root">Северный цикл</meta>'
                    '<meta property="group-position" refines="#root">99</meta>'
                    '<meta property="group-position" refines="#sub">2.5</meta></metadata>').encode())
            output.writestr(entry, content)
    half = book_for(shared, packed.getvalue(), "winter.epub")
    branch = last["series_memberships"][0]["id"]
    assert half["series_memberships"] == [{"id": branch, "path": prefix, "position": "2.5"}]

    start = book_for(shared, b"one", "first.txt")
    updated = client.put(f"/api/v1/books/{start['id']}", json={"title":"Первый том", "authors":[],
        "series_memberships":[{"path":["СЕВЕРНЫЙ ЦИКЛ", "Вторая эпоха"], "position":"2"}]}).json()
    assert updated["series_memberships"] == [{"id":branch, "path":prefix, "position":"2"}]
    assert client.put(f"/api/v1/books/{half['id']}", json={"title":half["title"], "authors":half["authors"],
        "series":"Северный цикл", "series_index":"2,5"}).json()["series_memberships"][0]["path"] == prefix

    order = [start["id"], half["id"], last["id"]]
    detail = client.get(f"/api/v1/series/{branch}").json()
    assert [book["id"] for book in detail["books"]] == order
    root = client.get(f"/api/v1/series/{detail['parent_id']}").json()
    assert [child["id"] for child in root["children"]] == [branch] and root["books"] == []
    assert sorted(book["id"] for book in client.get("/api/v1/books", params={"series":prefix[0]}).json()["items"]) == sorted(order)
    assert sorted(book["id"] for book in client.get("/api/v1/books", params={"q":"Вторая эпоха"}).json()["items"]) == sorted(order)
    assert client.get("/api/v1/books", params={"series":"Антология"}).json()["items"][0]["id"] == last["id"]

    versions = ["/opds/v1", "/opds/v2"]
    for version in versions:
        listing = client.get(version, params={"view":"series-branch", "value":root["id"]})
        assert "Вторая эпоха".encode() in listing.content
    nav = client.get("/opds/v2", params={"view":"series-branch", "value":branch}).json()["navigation"]
    link, = nav
    volumes = client.get("/opds/v2", params={k: v[0] for k, v in parse_qs(urlsplit(link["href"]).query).items()}).json()
    assert [entry["metadata"]["title"] for entry in volumes["publications"]] == ["Первый том", half["title"], last["title"]]
    atom = client.get("/opds/v1", params={"view":"series-volumes", "value":branch})
    assert atom.content.index("Первый том".encode()) < atom.content.index("Северная полка".encode()) < atom.content.index("Снежная книга".encode())

    layout = f"/api/v1/libraries/{shared}/layout"
    assert client.put(layout, json={"template":"{series}/{subseries}/{series_index} - {title}.{format}"}).is_success
    plan = client.post(layout + "/plans").json()["id"]
    paths = client.get(layout + f"/plans/{plan}").json()["items"]
    assert any(item["path"] == "Северный цикл/Вторая эпоха/2 - Первый том.txt" for item in paths)

    hidden = book_for(private, packed.getvalue(), "winter.epub")["series_memberships"][0]["id"]
    assert hidden != branch
    credentials = {"username":"reader", "password":"another-password"}
    assert client.post("/api/v1/auth/users", json=credentials).is_success
    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json=credentials).is_success
        assert reader.get(f"/api/v1/series/{hidden}").status_code == 404
        assert not reader.get("/opds/v2", params={"view":"series-volumes", "value":hidden}).json().get("publications")


def test_author_profiles_aliases_and_visibility(library):
    client, private_id, inbox = library
    shared = client.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()["id"]
    original = work_for(client, upload(client, shared)["file"]["id"])
    other = work_for(client, upload(client, shared, b"second book", "next.txt")["file"]["id"])
    secret = work_for(client, upload(client, private_id)["file"]["id"])

    def edit(ident, title, names):
        response = client.put(f"/api/v1/books/{ident}", json={"title": title, "authors": names})
        assert response.is_success, response.text
        return response.json()["author_credits"]

    credit, = client.get(f"/api/v1/books/{original}").json()["author_credits"]
    profile = credit["id"]
    secondary = edit(other, "Вторая книга", ["Лада Норд", "Иван Волков"])
    assert [item["credited_as"] for item in secondary] == ["Лада Норд", "Иван Волков"]
    old, collaborator = [item["id"] for item in secondary]
    secret_credit, = edit(secret, "Частная книга", ["Анна Соколова"])
    assert secret_credit["id"] != profile

    path = f"/api/v1/authors/{profile}"
    assert client.post(path + "/aliases", json={"name": "  лада  НОРД "}).status_code == 409
    assert client.post(path + "/aliases", json={"name": "  лада  НОРД ", "merge_existing": True}).is_success
    assert client.get(f"/api/v1/authors/{old}").json()["id"] == profile
    canonical = client.put(path, json={"name": "Анна Ветрова"}).json()
    assert {alias["name"] for alias in canonical["aliases"]} == {"Анна Соколова", "Лада Норд"}
    assert client.post(path + "/aliases", json={"name": "Helen Wind"}).is_success
    found = client.get("/api/v1/authors", params={"q": "helen", "library_id": shared})
    assert found.is_success, found.text
    assert [(row["id"], row["books"]) for row in found.json()["items"]] == [(profile, 2)]
    for field, value in (("q", "Helen Wind"), ("q", "Норд"), ("author", "Анна Ветрова")):
        books_found = client.get("/api/v1/books", params={field: value, "library_id": shared}).json()["items"]
        assert {book["id"] for book in books_found} == {original, other}
    authors = client.get("/api/v1/books/facets", params={"library_id": shared}).json()["authors"]
    assert authors == ["Анна Ветрова", "Иван Волков"]
    assert [item["id"] for item in client.get(f"/api/v1/books/{other}").json()["author_credits"]] == [profile, collaborator]

    opds = client.get("/opds/v2", params={"view":"author", "value":"Лада Норд"}).json()["publications"]
    assert {item["metadata"]["title"] for item in opds} == {"Северная полка", "Вторая книга"}
    assert [item["title"] for item in client.get("/opds/v2", params={"view":"authors"}).json()["navigation"]].count("Анна Ветрова") == 1
    assert "Вторая книга".encode() in client.get("/opds/v1", params={"q":"Helen Wind"}).content

    contents = (FB2.decode().replace("Анна", "Лада").replace("Соколова", "Норд")
                .replace("Снежная книга", "Северная полка")).encode()
    (inbox / "alias.fb2").write_bytes(contents)
    scan = client.post(f"/api/v1/libraries/{shared}/scan", json={}).json()
    assert worker.run_one()
    jobs = client.get("/api/v1/imports").json()
    match = next(job for job in jobs if job["id"] == scan["id"])["result"]["items"][0]["matches"][0]
    assert match["confidence"] >= 80 and match["work_id"] == original
    third = work_for(client, upload(client, shared, contents, "alias.fb2")["file"]["id"])
    assert client.get(f"/api/v1/books/{third}").json()["author_credits"][0]["id"] == profile
    aliases = {alias["name"]: alias["key"] for alias in client.get(path).json()["aliases"]}
    assert client.delete(path + "/aliases/" + aliases["Лада Норд"]).status_code == 409
    assert client.delete(path + "/aliases/" + aliases["Helen Wind"]).status_code == 204

    credentials = {"username": "reader", "password": "another-password", "read_only": True}
    assert client.post("/api/v1/auth/users", json=credentials).is_success
    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json=credentials).is_success
        assert reader.get(path).is_success
        assert reader.post(path + "/aliases", json={"name":"Запрещено"}).status_code == 404
        assert reader.get(f"/api/v1/authors/{secret_credit['id']}").status_code == 404
        assert all(entry["library_id"] == shared for entry in reader.get("/api/v1/authors").json()["items"])
        assert {row["id"] for row in reader.get("/api/v1/books", params={"q":"Соколова"}).json()["items"]} == {original, other, third}


def test_merge_preserves_editions_private_data_and_old_links(library):
    admin, _, _ = library
    lib_id = admin.post("/api/v1/libraries", json={"name": "Общая", "mode": "managed", "shared": True}).json()["id"]
    target_file = upload(admin, lib_id, b"First", "target.txt")["file"]["id"]
    original = epub_bytes()
    source_file = upload(admin, lib_id, original, "source.epub")["file"]["id"]
    target, source = [work_for(admin, ident) for ident in (target_file, source_file)]
    source_detail = admin.get(f"/api/v1/books/{source}").json()
    edition_id = source_detail["editions"][0]["id"]
    endpoint = f"/api/v1/books/{target}/merge"
    find = {"source_id": source}
    draft = admin.post(endpoint + "/preview", json=find).json()
    assert draft["source"]["id"] == source
    assert draft["target"]["id"] == target
    assert admin.patch(f"/api/v1/editions/{edition_id}", json={"publisher": "Другое издание"}).is_success
    assert admin.post(endpoint, json={**find, "revision": draft["revision"]}).status_code == 409
    draft = admin.post(endpoint + "/preview", json=find).json()
    assert draft["source"]["editions"][0]["publisher"] == "Другое издание"

    credentials = {"username": "borrower", "password": "another-password"}
    assert admin.post("/api/v1/auth/users", json={**credentials, "read_only": True}).is_success
    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json=credentials).is_success
        assert reader.post(endpoint + "/preview", json=find).status_code == 404
        path = f"/api/v1/reading/progress/{source_file}"
        position = {"version": 0, "locator": "epubcfi(/6/2!/4/2)", "percentage": 0.4, "device": "Планшет"}
        assert reader.put(path, json=position).is_success
        assert reader.patch(f"/api/v1/reading/states/{source}", json={"status": "finished", "favorite": True}).is_success
        assert reader.patch(f"/api/v1/reading/states/{target}", json={"status": "planned"}).is_success
        annotation_path = f"/api/v1/reading/annotations/{source_file}"
        assert reader.post(annotation_path, json={"locator": position["locator"], "note": "Перечитать"}).is_success
        collection = reader.post("/api/v1/collections", json={"name": "Подборка"}).json()["id"]
        collection_path = f"/api/v1/collections/{collection}"
        for ident in (source, target):
            assert reader.put(f"{collection_path}/books/{ident}").status_code == 204
        confirmation = {**find, "revision": draft["revision"]}
        for _ in range(2):
            response = admin.post(endpoint, json=confirmation)
            assert response.is_success, response.text
            assert len(response.json()["editions"]) == 2
        assert reader.get(path).json()["locator"] == position["locator"]
        assert reader.get(annotation_path).json()[0]["note"] == "Перечитать"
        assert [item["id"] for item in reader.get(collection_path).json()["items"]] == [target]
        state = reader.get(f"/api/v1/reading/states/{target}").json()
        assert state["favorite"] and state["status"] == "finished" and state["last_file_id"] == source_file
        assert admin.get(f"/api/v1/reading/states/{target}").json()["status"] == "unread"
        assert reader.put(path, json={**position, "version": 1, "percentage": 0.5}).is_success
        assert reader.get(f"/api/v1/reading/states/{target}").json()["percentage"] == 0.5

    assert work_for(admin, source_file) == target
    assert admin.get(f"/api/v1/files/{source_file}").content == original
    assert admin.get(f"/api/v1/books/{source}").json()["id"] == target
    assert admin.post(f"/api/v1/books/{source}/restore").status_code == 404
    assert admin.get("/api/v1/books/trash").json()["items"] == []
    assert [row["id"] for row in admin.get("/api/v1/books").json()["items"]] == [target]
    merge_job, = [job for job in admin.get("/api/v1/imports").json() if job["kind"] == "merge"]
    assert merge_job["result"]["source_id"] == source

    final = work_for(admin, upload(admin, lib_id, b"Third", "third.txt")["file"]["id"])
    endpoint = f"/api/v1/books/{final}/merge"
    plan = admin.post(endpoint + "/preview", json={"source_id": target}).json()
    assert admin.post(endpoint, json={"source_id": target, "revision": plan["revision"]}).is_success
    assert admin.get(f"/api/v1/books/{source}").json()["id"] == final
    assert work_for(admin, source_file) == final


def test_metadata_facets_batch_and_reversible_trash(library):
    client, library_id, _ = library
    file_id = upload(client, library_id)["file"]["id"]
    info = client.get(f"/api/v1/files/{file_id}/info").json()
    ident = info["work_id"]
    edition_id = info["edition_id"]
    assert client.patch(f"/api/v1/editions/{edition_id}", json={
        "isbn": "978-5-1234-5678-9", "publisher": "Северное издательство",
    }).json()["editions"][0]["metadata_source"] == "manual"
    batch = client.patch("/api/v1/books/batch", json={
        "ids":[ident], "series":"Морские истории", "genres":["Фантастика"], "add_tags":["любимое"],
    })
    assert batch.json() == {"updated": 1}
    facets = client.get("/api/v1/books/facets", params={"library_id":library_id}).json()
    assert facets["genres"] == ["Фантастика"]
    assert facets["tags"] == ["любимое"]
    query = client.get("/api/v1/books", params={"author":"Анна Соколова", "genre":"Фантастика", "q":"северное"})
    assert query.json()["items"][0]["id"] == ident
    file_path = f"/api/v1/files/{file_id}"
    data = client.get(file_path).content
    assert client.patch(f"/api/v1/reading/states/{ident}", json={"favorite":True}).is_success
    for _ in range(2):
        assert client.post(f"/api/v1/books/{ident}/trash").status_code == 204
    assert client.get(file_path).status_code == 404
    assert client.get(f"/opds/files/{file_id}").status_code == 404
    assert not client.get("/api/v1/books").json()["items"]
    assert not client.get("/api/v1/reading/shelf?view=favorites").json()["items"]
    assert client.get("/api/v1/books/trash").json()["items"][0]["id"] == ident
    assert client.post(f"/api/v1/books/{ident}/restore").status_code == 204
    assert client.get(file_path).content == data
    assert client.get(f"/api/v1/reading/states/{ident}").json()["favorite"] is True


def test_layout_preview_collision_rejection_and_undo(library):
    client, ident, _ = library
    first = upload(client, ident, b"one", "first.txt")["file"]["id"]
    second = upload(client, ident, b"two", "second.txt")["file"]["id"]
    for file in (first, second):
        item = client.put(f"/api/v1/books/{work_for(client, file)}", json={
            "title":"Книга / Том I", "authors":["CON"],
        })
        assert item.is_success, item.text
    base = f"/api/v1/libraries/{ident}/layout"
    assert client.put(base, json={"template":"../{title}.{format}"}).status_code == 422
    assert client.put(base, json={"template":"{author}/{title}.{format}"}).is_success
    dry_run = client.post(base + "/plans").json()
    plan_endpoint = base + f"/plans/{dry_run['id']}"
    plan = client.get(plan_endpoint).json()
    paths = [row["path"] for row in plan["items"]]
    assert len(paths) == len(set(paths)) == 2
    assert all(path.startswith("_CON/Книга _ Том I") and path.endswith(".txt") for path in paths)
    assert client.get(base + "/manifest").status_code == 404
    result = client.post(plan_endpoint + "/apply").json()
    assert client.post(plan_endpoint + "/apply").json()["id"] == result["id"]
    assert worker.run_one()
    manifest = client.get(base + "/manifest").json()
    assert manifest["layout_id"] == result["id"]
    storage = books.DATA / "organized" / str(ident) / str(result["id"]) / "books"
    assert {(storage / path).read_bytes() for path in paths} == {b"one", b"two"}
    assert client.put(base, json={"template":"{title}.{format}"}).is_success
    second_plan = client.post(base + "/plans").json()
    second_run = client.post(base + f"/plans/{second_plan['id']}/apply").json()
    assert worker.run_one()
    assert client.get(base).json()["active"]["id"] == second_run["id"]
    assert client.get(base).json()["removable_snapshots"] == []
    protected = base + f"/snapshots/{result['id']}"
    detached = base + f"/snapshots/{second_run['id']}"
    assert client.delete(protected).status_code == 409
    assert client.delete(detached).status_code == 409
    assert client.delete(plan_endpoint.replace("/plans/", "/snapshots/")).status_code == 404
    assert client.post(base + f"/undo?expected={result['id']}").status_code == 409
    assert client.post(base + f"/undo?expected={second_run['id']}").status_code == 204
    assert client.get(base + "/manifest").json()["layout_id"] == result["id"]
    third_plan = client.post(base + "/plans").json()
    third_run = client.post(base + f"/plans/{third_plan['id']}/apply").json()
    assert worker.run_one()
    layout = client.get(base).json()
    assert layout["active"]["id"] == third_run["id"]
    assert [row["id"] for row in layout["removable_snapshots"]] == [second_run["id"]]
    detached_root = storage.parent.parent / str(second_run["id"])
    assert (detached_root / "manifest.json").is_file()
    assert client.delete(detached).status_code == 204
    assert client.delete(detached).status_code == 204
    assert not detached_root.exists()
    assert client.get(base).json()["removable_snapshots"] == []
    assert client.delete(protected).status_code == 409
    assert client.post(base + f"/undo?expected={third_run['id']}").status_code == 204
    assert client.get(base + "/manifest").json()["layout_id"] == result["id"]
    assert {(storage / path).read_bytes() for path in paths} == {b"one", b"two"}
    assert client.get(f"/api/v1/files/{first}").content == b"one"


def test_managed_import_undo_keeps_sources(library):
    client, ident, inbox = library
    original = inbox / "Импорт.txt"
    original.write_bytes(b"independent source")
    scan = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    task = client.post(f"/api/v1/imports/{scan['id']}/confirm").json()
    assert worker.run_one()
    report = next(job for job in client.get("/api/v1/imports").json() if job["id"] == task["id"])
    assert report["result"]["added"] == 1
    assert len(report["result"]["work_ids"]) == 1
    for _ in range(2):
        assert client.post(f"/api/v1/imports/{task['id']}/undo").status_code == 204
    assert original.read_bytes() == b"independent source"
    assert not client.get("/api/v1/books").json()["items"]
    assert len(client.get("/api/v1/books/trash").json()["items"]) == 1


def test_import_preview_distinguishes_exact_and_metadata_matches(library):
    client, ident, inbox = library
    file_id = upload(client, ident)["file"]["id"]
    (inbox / "same.epub").write_bytes(epub_bytes())
    (inbox / "similar.epub").write_bytes(epub_bytes() + b"changed bytes")
    (inbox / "twin.epub").write_bytes(epub_bytes() + b"changed bytes")
    (inbox / "broken.fb2").write_bytes(b"<FictionBook><broken")
    planned = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    report = next(job for job in client.get("/api/v1/imports").json() if job["id"] == planned["id"])
    result = report["result"]
    assert (result["found"], result["new_works"], result["duplicates"], result["unreadable"]) == (4, 1, 2, 1)
    assert [error["path"] for error in result["errors"]] == ["broken.fb2"]
    assert len(client.get("/api/v1/books").json()["items"]) == 1
    items = {item["path"]: item for item in result["items"]}
    assert items["same.epub"]["duplicate"] and items["same.epub"]["existing_duplicate"]
    assert items["twin.epub"]["duplicate"] and not items["twin.epub"]["existing_duplicate"]
    assert not items["similar.epub"]["duplicate"]
    assert items["similar.epub"]["matches"][0] == {
        "work_id": work_for(client, file_id), "confidence": 80,
        "reason": "Совпали название и автор", "title": "Северная полка",
    }
    queued = client.post(f"/api/v1/imports/{planned['id']}/confirm", json={"choices": {"similar.epub": {"action": "skip"}}})
    assert queued.status_code == 202, queued.text
    assert worker.run_one()
    added = next(job["result"] for job in client.get("/api/v1/imports").json() if job["id"] == queued.json()["id"])
    assert (added["added"], added["duplicates"], added["skipped"]) == (1, 1, 1)


def test_attach_format_and_separate_edition_without_new_work(library):
    client, ident, _ = library
    original = upload(client, ident)["file"]
    work_id = work_for(client, original["id"])
    path = f"/api/v1/books/{work_id}/editions/upload"
    first = client.post(path, params={"edition_id": original["edition_id"]},
                        files={"file": ("format.txt", b"other format")})
    assert first.status_code == 201, first.text
    assert first.json()["file"]["edition_id"] == original["edition_id"]
    second = client.post(path, files={"file": ("edition.txt", b"new edition")})
    assert second.status_code == 201, second.text
    assert second.json()["file"]["edition_id"] != original["edition_id"]
    listing = client.get("/api/v1/books").json()["items"]
    assert len(listing) == 1 and len(listing[0]["editions"]) == 2
    existing = upload(client, ident, b"different work", "different.txt")["file"]
    rejected = client.post(path, files={"file": ("duplicate.txt", b"different work")})
    assert rejected.status_code == 422
    assert work_for(client, existing["id"]) != work_id


def test_import_one_hundred_epub_and_fb2_files(library):
    client, ident, inbox = library
    for number in range(50):
        suffix = str(number).encode()
        (inbox / f"epub-{number:02}.epub").write_bytes(titled_epub(f"Рукопись {number}"))
        (inbox / f"fb2-{number:02}.fb2").write_bytes(FB2.replace(b"</book-title>", b" " + suffix + b"</book-title>"))
    originals = {path.name: path.read_bytes() for path in inbox.iterdir()}
    scan = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    imported = client.post(f"/api/v1/imports/{scan['id']}/confirm").json()
    assert worker.run_one()
    with db.Session.begin() as session:
        result = session.get(db.Job, imported["id"]).result
        assert result["added"] == 100 and result["errors"] == []
        conversions = session.query(db.Job).filter_by(kind="conversion", status="queued")
        assert conversions.count() == 50
        conversions.update({"status":"done"})  # Conversion itself is exercised separately.
    second = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()["id"]
    assert worker.run_one()
    repeated = client.get("/api/v1/imports").json()[0]["result"]
    assert repeated["found"] == repeated["duplicates"] == 100
    assert repeated["conversions"] == 0
    second_import = client.post(f"/api/v1/imports/{second}/confirm").json()["id"]
    assert worker.run_one()
    report = next(job["result"] for job in client.get("/api/v1/imports").json() if job["id"] == second_import)
    assert (report["processed"],report["added"],report["duplicates"],report["errors"]) == (100,0,100,[])
    listing = client.get("/api/v1/books", params={"library_id":ident,"limit":100}).json()
    assert len(listing["items"]) == 100 and not listing["has_next"]
    assert {path.name: path.read_bytes() for path in inbox.iterdir()} == originals

