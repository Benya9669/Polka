from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from .auth import Current, DB
from .author_names import normal
from .catalog import serialize_works, visible_work
from .db import Library, Series, SeriesAncestor, SeriesMembership, Work

router = APIRouter(prefix="/api/v1/series", tags=["series"])


def visible_series(user):
    books = (select(1).select_from(SeriesAncestor)
             .join(SeriesMembership, SeriesMembership.series_id == SeriesAncestor.descendant_id)
             .join(Work, Work.id == SeriesMembership.work_id).join(Library, Library.id == Work.library_id)
             .where(SeriesAncestor.ancestor_id == Series.id, *visible_work(user)).correlate(Series).exists())
    return select(Series).where(books)


def volume_query(user, series_id):
    return (select(Work).join(Library).join(SeriesMembership, SeriesMembership.work_id == Work.id)
            .where(SeriesMembership.series_id == series_id, *visible_work(user))
            .order_by(SeriesMembership.volume_number.asc().nulls_last(), SeriesMembership.position, Work.title, Work.id))


def pages(db, query, page):
    rows = list(db.scalars(query.offset((page - 1) * 30).limit(31)))
    return rows[:30], len(rows) > 30


def series_json(branch):
    return {key: getattr(branch, key) for key in ("id", "name", "path", "library_id", "parent_id")}


@router.get("")
def list_series(db: DB, user: Current, q: str = Query("", max_length=500), library_id: int | None = None,
                parent_id: int | None = None, page: int = Query(1, ge=1, le=10000)):
    query = visible_series(user)
    if parent_id is not None or not normal(q):
        query = query.where(Series.parent_id == parent_id)
    if library_id is not None:
        query = query.where(Series.library_id == library_id)
    for word in normal(q).split():
        query = query.where(Series.normalized_name.contains(word, autoescape=True))
    rows, more = pages(db, query.order_by(Series.normalized_name, Series.id), page)
    return {"items": [series_json(branch) for branch in rows], "has_next": more}


@router.get("/{series_id}")
def series_detail(series_id: int, db: DB, user: Current, page: int = Query(1, ge=1, le=10000),
                  children_page: int = Query(1, ge=1, le=10000)):
    branch = db.scalar(visible_series(user).where(Series.id == series_id))
    if not branch:
        raise HTTPException(404, "Серия не найдена")
    children, children_next = pages(db, visible_series(user).where(Series.parent_id == branch.id)
                                   .order_by(Series.normalized_name, Series.id), children_page)
    books, books_next = pages(db, volume_query(user, branch.id), page)
    return {**series_json(branch), "children": [series_json(child) for child in children], "children_next": children_next,
            "books": serialize_works(db, books), "has_next": books_next}
