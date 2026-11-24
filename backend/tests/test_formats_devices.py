from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import books, devices, main, worker
from app.conversions import convert_file
from tests.fixtures import epub_bytes
from tests.test_app import library, upload


def rendered_comic():
    buffer = BytesIO()
    Image.new("RGB", (320, 480), "#31524c").save(buffer, format="PNG")
    target = BytesIO()
    with ZipFile(target, "w") as archive:
        archive.writestr("001.png", buffer.getvalue())
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>Тестовый комикс</Title><Writer>Анна</Writer><Number>7</Number></ComicInfo>")
    return target.getvalue()


def test_comic_cover_and_html_attachment(library):
    client, ident, _ = library
    original = rendered_comic()
    comic = upload(client, ident, original, "comic.cbz")["file"]
    assert comic["format"] == "cbz"
    info = client.get("/api/v1/files/" + str(comic["id"]) + "/info").json()
    book = client.get("/api/v1/books/" + str(info["work_id"])).json()
    assert book["title"] == "Тестовый комикс"
    assert book["series_index"] == "7"
    assert client.get(book["editions"][0]["cover"]).headers["content-type"] == "image/jpeg"
    assert client.get("/api/v1/files/" + str(comic["id"])).content == original

    html = upload(client, ident, "<html><title>Страница</title><body><p>Содержание</p></body></html>".encode(), "page.htm")
    assert html["file"]["format"] == "html"
    resource = client.get("/api/v1/files/" + str(html["file"]["id"]))
    assert resource.headers["content-disposition"].startswith("attachment;")
    assert client.get("/api/v1/files/" + str(html["file"]["id"]), params={"inline": True}).status_code == 415


def test_html_conversion_copy_omits_untrusted_markup(tmp_path):
    from app.formats import local_html

    source = tmp_path / "original.html"
    text = '''<!doctype html><title>Книга</title><p onclick="handle()">Текст
      <img src="https://example.test/image"><script>fetch('/private')</script>
      <style>@import url(https://example.test/theme)</style><a href="file:///etc/passwd">Ссылка</a></p>'''
    source.write_text(text, encoding="utf-8")
    target = tmp_path / "sanitized.html"
    local_html(source, target)
    converted = target.read_text(encoding="utf-8")
    for value in ("onclick", "https:", "file:", "fetch(", "<style", "<script"):
        assert value not in converted
    assert "<title>Книга</title>" in converted
    assert "Текст" in converted and "Ссылка" in converted
    assert source.read_text(encoding="utf-8") == text


def test_sony_package_and_progress_use_personal_access(library, monkeypatch):
    author, _, _ = library
    shared = author.post("/api/v1/libraries", json={"name": "Общая Sony", "mode": "managed", "shared": True}).json()
    file = upload(author, shared["id"])["file"]
    file_id = file["id"]

    def fake_convert(book, lib, root, *, sony, title, authors):
        assert sony and title == "Северная полка"
        result = root / "test.epub"
        result.write_bytes(epub_bytes())
        return result

    monkeypatch.setattr(devices, "convert_file", fake_convert)
    credentials = {"username": "sonyreader", "password": "reader-password"}
    assert author.post("/api/v1/auth/users", json={**credentials, "read_only": True}).is_success
    with TestClient(main.app) as reader:
        assert reader.post("/api/v1/auth/login", json=credentials).is_success
        base = "/api/v1/devices/sony"
        preview = reader.post(base + "/preview", json={"file_ids": [file_id]}).json()
        assert preview["files"][0]["format"] == "epub"
        result = reader.post(base + "/exports", json={"file_ids": [file_id]})
        assert result.status_code == 202
        ident = result.json()["id"]
        assert reader.post(base + "/exports", json={"file_ids": [file_id]}).json()["id"] == ident
        assert worker.run_one()
        path = f"{base}/exports/{ident}/archive"
        archive = reader.get(path)
        assert archive.status_code == 200
        assert author.get(path).status_code == 404
        with ZipFile(BytesIO(archive.content)) as bundle:
            package = json.loads(bundle.read("manifest.json"))
            entry = package["files"][0]
            assert entry["sha256"] == sha256(bundle.read(entry["filename"])).hexdigest()

        assert reader.put(f"{base}/progress/{file_id}", json={"page": 17, "percentage": 0.42}).is_success
        client_progress = reader.get(f"/api/v1/reading/progress/{file_id}").json()
        assert client_progress["version"] == 0 and client_progress["sony"]["page"] == 17
        assert "sony" not in author.get(f"/api/v1/reading/progress/{file_id}").json()
        assert reader.put(f"{base}/progress/{file_id}", json={"finished": True}).is_success
        assert reader.get("/api/v1/reading/shelf?view=finished").json()["items"][0]["state"]["device"] == "Sony PRS-600"
        work_id = reader.get(f"/api/v1/files/{file_id}/info").json()["work_id"]
        assert author.post(f"/api/v1/books/{work_id}/trash").status_code == 204
        assert reader.get(path).status_code == 404


@pytest.mark.skipif(shutil.which("ebook-convert") is None, reason="Requires Calibre")
def test_lrf_import_matches_existing_epub_and_skips_identical_copy(library, tmp_path):
    client, ident, inbox = library
    original = epub_bytes()
    upload(client, ident, original)
    work, = client.get("/api/v1/books").json()["items"]
    source = tmp_path / "source.epub"
    source.write_bytes(original)
    lrf = inbox / "a.LRF"
    subprocess.run(["ebook-convert", str(source), str(lrf.with_suffix(".lrf"))], check=True, timeout=90)
    lrf.with_suffix(".lrf").rename(lrf)
    raw = lrf.read_bytes()
    (inbox / "b.lrf").write_bytes(raw)
    scan = client.post(f"/api/v1/libraries/{ident}/scan", json={}).json()
    assert worker.run_one()
    first, copy = client.get("/api/v1/imports").json()[0]["result"]["items"]
    assert first["format"] == "lrf" and first["work_candidates"] == [work["id"]]
    assert first["matches"][0]["reason"] == "Совпали название и автор, разные форматы"
    assert copy["duplicate"]
    decision = {"choices": {lrf.name: {"action": "work", "work_id": work["id"]}}}
    assert client.post(f"/api/v1/imports/{scan['id']}/confirm", json=decision).is_success
    assert worker.run_one()
    card, = client.get("/api/v1/books").json()["items"]
    assert card["id"] == work["id"]
    assert sorted(f["format"] for e in card["editions"] for f in e["files"]) == ["epub", "lrf"]
    assert upload(client, ident, raw, "renamed.lrf")["duplicate"]
    output = tmp_path / "converted.epub"
    subprocess.run(["ebook-convert", str(lrf), str(output)], check=True, timeout=90)
    assert books.inspect(output, output.name)[0] == "epub"


@pytest.mark.skipif(any(shutil.which(tool) is None for tool in ("ebook-convert", "ebook-meta", "c44", "ddjvu", "pdftoppm")),
                    reason="Requires conversion programs installed in the application image")
def test_real_format_conversion_in_application_image(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "DATA", tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("Catalog fixture. A sentence for EPUB conversion.", encoding="utf-8")
    subprocess.run(["ebook-convert", str(source), str(tmp_path / "portable.azw3"), "--title", "KF8 Test"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["ebook-convert", str(source), str(tmp_path / "portable.mobi"), "--title", "MOBI Test"],
                   check=True, capture_output=True, timeout=60)
    ppm = tmp_path / "page.ppm"
    Image.new("RGB", (320, 480), "#31524c").save(ppm)
    subprocess.run(["c44", str(ppm), str(tmp_path / "scan.djvu")], check=True, capture_output=True, timeout=60)
    (tmp_path / "page.html").write_text("<html><title>HTML Test</title><p>Local section.</p></html>", encoding="utf-8")
    (tmp_path / "page.rtf").write_bytes(rb"{\rtf1\ansi{\info{\title RTF Test}} A readable paragraph.}")
    (tmp_path / "comic.cbz").write_bytes(rendered_comic())
    library = SimpleNamespace(folder="")
    inputs = {"portable.azw3": "azw3", "portable.mobi": "mobi", "scan.djvu": "djvu",
              "page.html": "html", "page.rtf": "rtf", "comic.cbz": "cbz"}
    for index, (name, expected) in enumerate(inputs.items()):
        file = Path(tmp_path / name)
        raw = file.read_bytes()
        fmt, _, cover = books.inspect(file, name)
        assert fmt == expected, (name, fmt)
        if fmt in {"cbz", "djvu"}:
            assert cover
        original = SimpleNamespace(indexed=False, path=name, format=fmt, sha256=sha256(raw).hexdigest(), size=len(raw))
        work = tmp_path / f"converted-{index}"
        work.mkdir()
        output = convert_file(original, library, work)
        assert books.inspect(output, output.name)[0] == ("pdf" if fmt == "djvu" else "epub")
        assert file.read_bytes() == raw

