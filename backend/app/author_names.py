from sqlalchemy import delete, or_, select

from .db import Author, AuthorName, Work, WorkAuthor
from .text_keys import normal, normalized_key as name_key


def names_for(db, library_id, names):
    keys = {name_key(value) for value in names if normal(value)}
    return {entry.key: entry for entry in db.scalars(select(AuthorName).where(
        AuthorName.library_id == library_id, AuthorName.key.in_(keys)))} if keys else {}


def add_name(db, author, name):
    entry = AuthorName(library_id=author.library_id, key=name_key(name), author_id=author.id,
                       name=name, normalized=normal(name))
    db.add(entry)
    return entry


def sync_author_credits(db, work):
    db.execute(delete(WorkAuthor).where(WorkAuthor.work_id == work.id))
    names = names_for(db, work.library_id, work.authors)
    for credit, name in enumerate(work.authors):
        if not normal(name):
            continue
        key = name_key(name)
        if key not in names:
            author = Author(library_id=work.library_id, name=name.strip())
            db.add(author)
            db.flush()
            names[key] = add_name(db, author, name.strip())
        db.add(WorkAuthor(work_id=work.id, credit=credit, author_id=names[key].author_id, name_key=key))


def linked_author(predicate):
    return (select(1).select_from(WorkAuthor).join(AuthorName, AuthorName.author_id == WorkAuthor.author_id)
            .where(WorkAuthor.work_id == Work.id, predicate).correlate(Work).exists())


def catalog_search(query):
    words = sorted(dict.fromkeys(normal(query).split()), key=len, reverse=True)
    if not words:
        return []
    longest, *remaining = words
    metadata = select(Work.id).where(Work.search.contains(longest, autoescape=True)).correlate(None)
    aliases = (select(WorkAuthor.work_id).join(AuthorName, AuthorName.author_id == WorkAuthor.author_id)
               .where(AuthorName.normalized.contains(longest, autoescape=True)))
    return [Work.id.in_(metadata.union_all(aliases))] + [
        or_(Work.search.contains(word, autoescape=True), linked_author(AuthorName.normalized.contains(word, autoescape=True)))
        for word in remaining
    ]
