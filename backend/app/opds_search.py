from collections import defaultdict
from itertools import batched

from sqlalchemy import select

from .catalog import visible_work
from .db import AuthorName, Library, Work, WorkAuthor
from .text_keys import normal, normalized_key


def publication_keys(book):
    if not normal(book["title"]):
        return ()
    title = normalized_key(book["title"])
    return tuple((title, normalized_key(author)) for author in book["authors"] if normal(author))


def mark_matches(db, user, remote):
    publications = []
    by_key = defaultdict(dict)
    for entry in remote:
        source = {key: entry["source"][key] for key in ("id", "name")}
        for book in entry.get("result", {}).get("publications", []):
            keys = publication_keys(book)
            publications.append((source["id"], book, keys))
            for key in keys:
                by_key[key][source["id"]] = source

    local = defaultdict(dict)
    titles = sorted({title for title, _ in by_key})
    for batch in batched(titles, 300):
        query = (select(Work.title_key, AuthorName.key, Work.id, Work.title, Library.name).select_from(Work)
                 .join(Library, Library.id == Work.library_id)
                 .join(WorkAuthor, WorkAuthor.work_id == Work.id)
                 .join(AuthorName, AuthorName.author_id == WorkAuthor.author_id)
                 .where(Work.title_key.in_(batch), Work.merged_into_id.is_(None), *visible_work(user)))
        for title_key, author_key, work_id, title, library_name in db.execute(query):
            key = (title_key, author_key)
            if key in by_key:
                local[key][work_id] = {"id": work_id, "title": title, "library": library_name}

    for source_id, book, keys in publications:
        books = {ident: work for key in keys for ident, work in local[key].items()}
        sources = {ident: source for key in keys for ident, source in by_key[key].items() if ident != source_id}
        book["matches"] = {"local": [books[ident] for ident in sorted(books, reverse=True)[:5]], "local_total": len(books),
                           "sources": [sources[ident] for ident in sorted(sources)[:5]]}
