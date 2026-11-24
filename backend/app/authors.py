from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select, update

from .auth import Current, DB
from .author_names import add_name, name_key, normal
from .catalog import library_or_404, lock_library, serialize_works, visible_work
from .db import Author, AuthorName, Library, Work, WorkAuthor

router = APIRouter(prefix="/api/v1/authors", tags=["authors"])


class AuthorInput(BaseModel):
    name: str = Field(min_length=1, max_length=1000)


class AliasInput(AuthorInput):
    merge_existing: bool = False


def profile_works(user, author_id):
    credit = select(1).where(WorkAuthor.work_id == Work.id, WorkAuthor.author_id == author_id).correlate(Work).exists()
    return select(Work).join(Library).where(*visible_work(user), credit)


def profile_or_404(db, user, ident, *, write=False):
    author = db.get(Author, ident)
    if not author:
        raise HTTPException(404, "Автор не найден")
    lib = library_or_404(db, user, author.library_id, write=write)
    if write:
        lock_library(db, lib)
        db.refresh(author)
    while author.merged_into_id is not None:
        author = db.get(Author, author.merged_into_id)
    if not write and db.scalar(profile_works(user, author.id).with_only_columns(Work.id).limit(1)) is None:
        raise HTTPException(404, "Автор не найден")
    return author, lib


def author_json(db, author, lib):
    primary = name_key(author.name)
    aliases = db.execute(select(AuthorName.key, AuthorName.name).where(AuthorName.author_id == author.id,
                        AuthorName.key != primary).order_by(AuthorName.normalized))
    return {"id": author.id, "name": author.name, "library_id": lib.id, "library": lib.name,
            "aliases": [{"key": key, "name": name} for key, name in aliases]}


def checked_name(value):
    name = value.strip()
    if not normal(name) or "\0" in name:
        raise HTTPException(422, "Укажите имя автора")
    return name


@router.get("")
def list_authors(db: DB, user: Current, q: str = Query("", max_length=1000), library_id: int | None = None,
                 page: int = Query(1, ge=1, le=10000)):
    query = (select(Author.id, Author.name, Author.library_id, Library.name.label("library"),
                    func.count(distinct(Work.id)).label("books"))
             .join(WorkAuthor, WorkAuthor.author_id == Author.id).join(Work, Work.id == WorkAuthor.work_id)
             .join(Library, Library.id == Work.library_id).where(*visible_work(user)))
    if library_id is not None:
        query = query.where(Author.library_id == library_id)
    for word in normal(q).split():
        query = query.where(select(1).where(AuthorName.author_id == Author.id,
                            AuthorName.normalized.contains(word, autoescape=True)).correlate(Author).exists())
    rows = db.execute(query.group_by(Author.id, Author.name, Author.library_id, Library.name)
                      .order_by(Author.name, Author.id).offset((page - 1) * 30).limit(31)).mappings().all()
    return {"items": rows[:30], "has_next": len(rows) > 30}


@router.get("/{author_id}")
def get_author(author_id: int, db: DB, user: Current, page: int = Query(1, ge=1, le=10000)):
    author, lib = profile_or_404(db, user, author_id)
    works = list(db.scalars(profile_works(user, author.id).order_by(Work.title, Work.id).offset((page - 1) * 30).limit(31)))
    return {**author_json(db, author, lib), "books": serialize_works(db, works[:30]), "has_next": len(works) > 30}


@router.put("/{author_id}")
def rename_author(author_id: int, payload: AuthorInput, db: DB, user: Current):
    author, lib = profile_or_404(db, user, author_id, write=True)
    name = checked_name(payload.name)
    entry = db.get(AuthorName, (lib.id, name_key(name)))
    if entry and entry.author_id != author.id:
        raise HTTPException(409, "Это имя уже связано с другим профилем")
    if entry:
        entry.name = name
    else:
        add_name(db, author, name)
    author.name = name
    db.commit()
    return author_json(db, author, lib)


@router.post("/{author_id}/aliases")
def add_alias(author_id: int, payload: AliasInput, db: DB, user: Current):
    author, lib = profile_or_404(db, user, author_id, write=True)
    name = checked_name(payload.name)
    entry = db.get(AuthorName, (lib.id, name_key(name)))
    if not entry:
        add_name(db, author, name)
    elif entry.author_id != author.id:
        if not payload.merge_existing:
            raise HTTPException(409, "Под этим именем уже есть профиль в библиотеке. Подтвердите объединение профилей.")
        source = db.get(Author, entry.author_id)
        db.execute(update(WorkAuthor).where(WorkAuthor.author_id == source.id).values(author_id=author.id))
        db.execute(update(AuthorName).where(AuthorName.author_id == source.id).values(author_id=author.id))
        source.merged_into_id = author.id
    db.commit()
    return author_json(db, author, lib)


@router.delete("/{author_id}/aliases/{key}", status_code=204)
def delete_alias(author_id: int, key: str, db: DB, user: Current):
    author, lib = profile_or_404(db, user, author_id, write=True)
    entry = db.get(AuthorName, (lib.id, key))
    if not entry or entry.author_id != author.id or key == name_key(author.name):
        raise HTTPException(404, "Псевдоним не найден")
    if db.scalar(select(WorkAuthor.work_id).where(WorkAuthor.author_id == author.id, WorkAuthor.name_key == key).limit(1)) is not None:
        raise HTTPException(409, "Это имя указано в карточках книг. Сначала измените сведения об авторах в этих карточках.")
    db.delete(entry)
    db.commit()
