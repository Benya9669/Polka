"""Measure the real catalog APIs inside a dedicated Polka Compose project."""

import argparse
import base64
import json
import os
import secrets
import statistics
from http.cookiejar import CookieJar
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from sqlalchemy import func, select, text

from app.db import Library, Session, User, Work

NAME = "Polka benchmark"
USERNAME = "catalog-benchmark"
BASE = "http://127.0.0.1:8080"


def client():
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def fetch(path, *, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(BASE + path, data=body, headers={"Content-Type": "application/json", **(headers or {})})
        with opener.open(request, timeout=30) as reply:
            return json.load(reply)

    return fetch


def run(size, samples):
    if os.getenv("APP_URL") != "http://localhost:18081":
        raise SystemExit("Run only in the benchmark project configured for localhost:18081")
    fetch = client()
    with Session() as db:
        accounts = list(db.scalars(select(User)))
        library = db.scalar(select(Library).where(Library.name == NAME))
        count = db.scalar(select(func.count()).select_from(Work))
        if db.bind.dialect.name != "postgresql" or (accounts and (
            len(accounts) != 1 or accounts[0].username != USERNAME or not library or count != size
        )):
            raise SystemExit("The benchmark needs an empty database or its own existing fixture")
        password = secrets.token_urlsafe(24)
        if library:
            from app.auth import passwords

            accounts[0].password = passwords.hash(password)
            db.commit()
            fetch("/api/v1/auth/login", payload={"username": USERNAME, "password": password})
        else:
            fetch("/api/v1/auth/setup", payload={"username": USERNAME, "password": password,
                                                 "setup_key": os.getenv("SETUP_KEY", "")})
            library_id = fetch("/api/v1/libraries", payload={"name": NAME, "mode": "managed"})["id"]
            seed(db, library_id, size)
            db.commit()
            library = db.get(Library, library_id)
        ident = library.id

    token = fetch("/api/v1/auth/opds-tokens", payload={"label": NAME})["token"]
    authorization = "Basic " + base64.b64encode(f"{USERNAME}:{token}".encode()).decode()

    def route(path, **params):
        return path + "?" + urlencode(params)

    endpoints = {
        "latest": (route("/api/v1/books", library_id=ident), 30),
        "deep_page": (route("/api/v1/books", library_id=ident, page=(size - 30) // 30), 30),
        "selective_title": (route("/api/v1/books", library_id=ident, q="edition-00017", sort="title"), 1),
        "pen_name": (route("/api/v1/books", library_id=ident, q="astral-pen-00421"), size // 1500),
        "mixed_terms": (route("/api/v1/books", library_id=ident, q="expedition astral-pen-00421"), size // 1500),
        "no_matches": (route("/api/v1/books", library_id=ident, q="unfindable-314159 expedition"), 0),
        "facets": (route("/api/v1/books/facets", library_id=ident), 1500),
        "opds_genre": (route("/opds/v2", view="genre", value="Adventure", page=50), 30),
    }
    output = {}
    for name, (path, expected) in endpoints.items():
        intervals = []
        for iteration in range(samples + 1):
            start = perf_counter()
            data = fetch(path, headers={"Authorization": authorization})
            elapsed = (perf_counter() - start) * 1000
            items = data.get("items", data.get("publications", data.get("authors", [])))
            if len(items) != expected:
                raise RuntimeError(f"{name}: got {len(items)} results, expected {expected}")
            if iteration:
                intervals.append(elapsed)
        output[name] = {"median_ms": round(statistics.median(intervals), 1), "max_ms": round(max(intervals), 1)}
    print(json.dumps({"works": size, "samples": samples, "requests": output}, indent=2))


def seed(db, library, size):
    statements = [
        """INSERT INTO authors (library_id, name)
           SELECT :library, 'Author ' || lpad(n::text, 5, '0') FROM generate_series(1, 1500) AS n""",
        """INSERT INTO author_names (library_id, key, author_id, name, normalized)
           SELECT library_id, encode(sha256(convert_to(lower(label), 'UTF8')), 'hex'), id, label, lower(label)
           FROM authors CROSS JOIN LATERAL (VALUES (name), ('Astral-pen-' || right(name, 5))) AS names(label)
           WHERE library_id = :library""",
        """INSERT INTO works (library_id, title, title_key, authors, series, series_index, genres, tags, custom_fields, search, created)
           SELECT :library, title, encode(sha256(convert_to(lower(title), 'UTF8')), 'hex'),
                  json_build_array(author), '', '', json_build_array(CASE WHEN n % 20 = 0 THEN 'History' ELSE 'Adventure' END),
                  json_build_array('Tag ' || (n % 40)), '{}', lower(title || ' ' || author || ' expedition in a distant galaxy'), now()
           FROM generate_series(1, :size) AS n
           CROSS JOIN LATERAL (VALUES ('Edition-' || lpad(n::text, 5, '0'), 'Author ' || lpad(((n - 1) % 1500 + 1)::text, 5, '0')))
                AS metadata(title, author)""",
        """INSERT INTO work_authors (work_id, credit, author_id, name_key)
           SELECT works.id, 0, author_names.author_id, author_names.key FROM works
           JOIN author_names ON author_names.library_id = works.library_id AND author_names.name = works.authors->>0
           WHERE works.library_id = :library""",
        """INSERT INTO editions (work_id, language, isbn, publisher, year, summary, metadata_source)
           SELECT id, 'en', '', 'Polka Test Press', '2026', 'An expedition in a distant galaxy.', 'embedded'
           FROM works WHERE library_id = :library""",
        """INSERT INTO book_files (library_id, edition_id, sha256, name, path, format, size, indexed)
           SELECT :library, editions.id, works.title_key, works.title || '.epub',
                  'benchmark/' || works.title_key || '.epub', 'epub', 32768, false
           FROM editions JOIN works ON works.id = editions.work_id WHERE works.library_id = :library""",
        "ANALYZE",
    ]
    for statement in statements:
        db.execute(text(statement), {"library": library, "size": size})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=30000, choices=(15000, 30000, 45000))
    parser.add_argument("--samples", type=int, default=12, choices=range(3, 31))
    args = parser.parse_args()
    run(args.size, args.samples)
