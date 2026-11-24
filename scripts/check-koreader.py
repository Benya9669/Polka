"""Exercise Polka over HTTP using the network code from an official KOReader release."""

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import subprocess
import tarfile
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from xml.etree import ElementTree

from pypdf import PdfWriter

BASE = "http://app:8080"
VERSION = "2026.07.1"
SHA256 = "299aadb28147a25e9432ced1214ea444a4184393b5ae97cf42402c8a61b1a1b0"
CLIENT = r"""
dofile("setupkoenv.lua")
-- Select the upstream client's synchronous transport for this headless run.
package.loaded["ui/uimanager"] = {}
package.loaded["device"] = {model = "desktop-smoke"}
local KOSync = dofile("plugins/kosync.koplugin/KOSyncClient.lua")
local api = KOSync:new{
    custom_url = "http://app:8080/koreader",
    service_spec = "plugins/kosync.koplugin/api.json",
}
local name = os.getenv("CHECK_KO_USER")
local key = require("ffi/sha2").md5(os.getenv("CHECK_KO_PASSWORD"))
local document = assert(require("util").partialMD5(arg[2]))
local mode = arg[1]
local authorized, response = api:authorize(name, key)
if mode == "revoked" then
    assert(not authorized, "Revoked credentials still worked")
    print("PASS revocation")
    return
end
assert(authorized and response.authorized == "OK", "KOReader authorization failed")
if mode == "send" then
    local sent
    api:update_progress(name, key, document, nil, 2, 0.5, "KOReader A", "test-device-a", function(ok, status, body)
        sent = ok and status == 200 and body.document == document and body.timestamp > 0
    end)
    assert(sent, "KOReader update was rejected")
else
    local received
    api:get_progress(name, key, document, function(ok, body)
        if not ok or not body then return end
        if mode == "isolated" then
            received = next(body) == nil
        elseif mode == "web" then
            received = body.progress == "3" and body.device_id == "polka-web" and body.percentage == 0.75
        else
            received = body.progress == "2" and body.device_id == "test-device-a" and body.percentage == 0.5
        end
    end)
    assert(received, "KOReader did not receive the expected progress")
end
print("PASS " .. mode .. " " .. document)
"""


def browser():
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(path, method="GET", data=None, headers=None):
        extra = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            extra["Content-Type"] = "application/json"
        with opener.open(Request(BASE + path, data=data, headers=extra, method=method), timeout=10) as reply:
            content = reply.read()
            return json.loads(content) if content and reply.headers.get_content_type() == "application/json" else content

    return request


def check(root, username, password):
    admin = browser()
    payload = {"username": username, "password": password}
    if admin("/api/v1/auth/status")["setup"]:
        admin("/api/v1/auth/setup", "POST", payload)
    else:
        admin("/api/v1/auth/login", "POST", payload)
    suffix = secrets.token_hex(5)
    library = admin("/api/v1/libraries", "POST", {"name": "KOReader smoke " + suffix, "mode": "managed", "shared": True})
    pdf = io.BytesIO()
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(300, 200)
    writer.add_metadata({"/Subject": "Polka KOReader sample. " * 900})
    writer.write(pdf)
    content = pdf.getvalue()
    boundary = "polka-" + suffix
    part = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="ko-{suffix}.pdf"\r\n'
            'Content-Type: application/pdf\r\n\r\n').encode()
    file_id = admin(f"/api/v1/libraries/{library['id']}/upload", "POST",
                    part + content + f"\r\n--{boundary}--\r\n".encode(),
                    {"Content-Type": f"multipart/form-data; boundary={boundary}"})["file"]["id"]
    token = admin("/api/v1/auth/opds-tokens", "POST", {"label": "KOReader smoke"})["token"]
    headers = {"Authorization": "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()}
    atom = ElementTree.fromstring(admin(f"/opds/v1?view=all&q=ko-{suffix}", headers=headers))
    choices = [link.get("href") for link in atom.findall(".//{*}entry/{*}link")
               if link.get("rel", "").startswith("http://opds-spec.org/acquisition")]
    href, = choices
    path = root / "smoke.pdf"
    path.write_bytes(admin(urlsplit(href).path, headers=headers))
    assert path.read_bytes() == content, "Downloaded OPDS file differs from the original"
    script = root / "smoke.lua"
    script.write_text(CLIENT, encoding="utf-8")

    def run(mode, credentials):
        env = {**os.environ, "CHECK_KO_USER": credentials["username"], "CHECK_KO_PASSWORD": credentials["password"]}
        result = subprocess.run(["./luajit", str(script), mode, str(path)], cwd=root, env=env,
                                text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip().splitlines()[-1]

    user = {"username": "ko-sender-" + suffix, "password": secrets.token_urlsafe(20)}
    other = {"username": "ko-other-" + suffix, "password": secrets.token_urlsafe(20)}
    sessions = []
    for account in (user, other):
        admin("/api/v1/auth/users", "POST", {**account, "read_only": True})
        session = browser()
        session("/api/v1/auth/login", "POST", account)
        sessions.append((session, session("/api/v1/devices/koreader/credentials", "POST", {})))
    sender, credential = sessions[0]
    sent = run("send", credential)
    digest = sent.rsplit(" ", 1)[-1]
    print(sent)
    print(run("receive", credential))
    print(run("isolated", sessions[1][1]))
    match_path = f"/api/v1/devices/koreader/files/{file_id}/match"
    assert sessions[1][0](match_path)["document"] is None
    assert sender(match_path) == {"document": digest, "linked_file_id": None}
    print("PASS document fingerprint")
    history = f"/api/v1/devices/koreader/documents/{digest}/history"
    initial_event, = sender(history)["items"]
    assert initial_event["progress"] == "2" and initial_event["device_id"] == "test-device-a"
    assert initial_event["file_id"] is None and sessions[1][0](history)["items"] == []
    print("PASS native history")
    sender("/api/v1/devices/koreader/documents", "PUT", {"document": digest, "file_id": file_id})
    position = f"/api/v1/reading/progress/{file_id}"
    assert sender(position)["koreader"]["locator"] == "2"
    sender(position, "PUT", {"version": 0, "locator": "3", "percentage": 0.75, "device": "Polka"})
    print(run("web", credential))
    sender("/api/v1/devices/koreader/credentials", "DELETE")
    print(run("revoked", credential))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    with args.archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise SystemExit(f"Expected the verified Linux x86_64 KOReader {VERSION} archive")
    with TemporaryDirectory() as directory, tarfile.open(args.archive) as release:
        release.extractall(directory, filter="data")
        check(Path(directory) / "lib/koreader", os.environ["POLKA_SMOKE_ADMIN"], os.environ["POLKA_SMOKE_PASSWORD"])
