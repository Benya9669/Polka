import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

from app import worker
from tests.fixtures import epub_bytes
from tests.test_app import library  # noqa: F401


def test_interrupted_download_retries_and_can_be_cancelled_during_inspection(library, monkeypatch):
    client, library_id, _ = library
    contents = epub_bytes()
    complete_transfer = Event()
    send_body = Event()
    inspecting = Event()
    finish_inspection = Event()
    requests = []
    feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Test source</title>
      <entry><id>test-book</id><title>Test book</title>
      <link rel="http://opds-spec.org/acquisition/open-access" href="book.epub" type="application/epub+zip"/>
      </entry></feed>'''

    class Catalog(BaseHTTPRequestHandler):
        def do_GET(self):
            is_book = self.path == "/book.epub"
            expected = contents if is_book else feed
            self.send_response(200)
            self.send_header("Content-Length", str(len(expected)))
            self.end_headers()
            if is_book:
                requests.append(self.headers.get("Range"))
                if not complete_transfer.is_set():
                    expected = expected[:len(expected) // 2]
                else:
                    assert send_body.wait(10)
            self.wfile.write(expected)

        def log_message(self, *_):
            pass

    inspect = worker.inspect

    def paused_inspect(path, name):
        inspecting.set()
        assert finish_inspection.wait(10)
        return inspect(path, name)

    monkeypatch.setenv("OPDS_PRIVATE_ALLOW", "127.0.0.1=127.0.0.1")
    monkeypatch.setattr(worker, "inspect", paused_inspect)
    with ThreadingHTTPServer(("127.0.0.1", 0), Catalog) as server, ThreadPoolExecutor(max_workers=1) as pool:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = "/api/v1/opds"
            source = client.post(endpoint + "/sources", json={"name": "Test source", "url": f"http://127.0.0.1:{server.server_port}/catalog"})
            assert source.status_code == 201, source.text
            source_id = source.json()["id"]
            catalog = client.post(f"{endpoint}/sources/{source_id}/browse", json={}).json()
            link = catalog["publications"][0]["downloads"][0]["link"]
            queued = client.post(endpoint + "/downloads", json={"source_id": source_id, "library_id": library_id, "link": link})
            assert queued.status_code == 202, queued.text
            job_url = f"{endpoint}/downloads/{queued.json()['id']}"

            for attempt, expected_status in enumerate(("queued", "queued", "error"), 1):
                assert worker.run_one()
                job = client.get(job_url).json()
                assert (job["status"], job["attempt"], job["error"]) == (expected_status, attempt, "Загрузка прервалась")
                assert client.get("/api/v1/books").json()["items"] == []

            complete_transfer.set()
            retry = client.post(job_url + "/retry").json()
            assert (retry["status"], retry["attempt"], retry["error"]) == ("queued", 0, None)
            running = pool.submit(worker.run_one)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                transfer = client.get(job_url).json()["result"]
                if transfer.get("total") == len(contents) and transfer.get("bytes") == 0:
                    break
                time.sleep(0.05)
            assert transfer["stage"] == "downloading" and transfer["total"] == len(contents) and transfer["bytes"] == 0
            send_body.set()
            assert inspecting.wait(10)
            processing = client.get(job_url).json()
            assert (processing["status"], processing["result"]["stage"]) == ("running", "processing")
            assert processing["result"]["bytes"] == len(contents)
            assert client.post(job_url + "/cancel").json()["status"] == "cancelled"
            finish_inspection.set()
            assert running.result(timeout=10)
            assert client.get(job_url).json()["status"] == "cancelled"
            assert client.get("/api/v1/books").json()["items"] == []

            assert client.post(job_url + "/retry").json()["status"] == "queued"
            assert worker.run_one()
            finished = client.get(job_url).json()
            assert finished["status"] == "done", finished
            assert finished["result"]["sha256"] == hashlib.sha256(contents).hexdigest()
            assert client.get(f"/api/v1/files/{finished['result']['file_id']}").content == contents
            assert len(client.get("/api/v1/books").json()["items"]) == 1
            assert requests == [None] * 5
        finally:
            send_body.set()
            finish_inspection.set()
            server.shutdown()
            thread.join()
