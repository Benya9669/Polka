import hashlib
import json
import re
from decimal import Decimal

from sqlalchemy import delete, select

from .author_names import normal
from .db import Series, SeriesAncestor, SeriesMembership, Work


def path_key(path):
    canonical = json.dumps([normal(name) for name in path], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def volume_number(position):
    if len(position) <= 40 and re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)?", position):
        return Decimal(position.replace(",", "."))
    return None


def position_key(position):
    number = volume_number(position)
    return normal(position) if number is None else number


def legacy_memberships(series, position):
    return [{"path": [series], "position": position}] if series.strip() else []


def metadata_memberships(metadata):
    return metadata.get("series_memberships") or legacy_memberships(metadata.get("series", ""), metadata.get("series_index", ""))


def memberships_for(db, work_ids):
    query = (select(SeriesMembership.work_id, Series.id, Series.path, SeriesMembership.position)
             .join(Series, Series.id == SeriesMembership.series_id).where(SeriesMembership.work_id.in_(work_ids))
             .order_by(SeriesMembership.rank))
    result = {}
    for work_id, series_id, path, position in db.execute(query):
        result.setdefault(work_id, []).append({"id": series_id, "path": path, "position": position})
    return result


def membership_labels(db, work_id):
    return [name for entry in memberships_for(db, [work_id]).get(work_id, []) for name in entry["path"]]


def ensure_series(db, library_id, path):
    parent = None
    ancestors = []
    for level, name in enumerate(path, start=1):
        key = path_key(path[:level])
        branch = db.scalar(select(Series).filter_by(library_id=library_id, key=key))
        if branch is None:
            branch = Series(library_id=library_id, key=key, name=name, normalized_name=normal(name),
                            parent_id=parent.id if parent else None, path=[*(parent.path if parent else []), name])
            db.add(branch)
            db.flush()
            db.add_all(SeriesAncestor(ancestor_id=ident, descendant_id=branch.id) for ident in [*ancestors, branch.id])
        parent = branch
        ancestors.append(branch.id)
    return parent


def set_memberships(db, work, memberships, *, standalone_position=""):
    db.execute(delete(SeriesMembership).where(SeriesMembership.work_id == work.id))
    work.series, work.series_index = "", standalone_position.strip()
    seen = set()
    for entry in memberships:
        path = [name.strip() for name in entry["path"] if normal(name)]
        if not path:
            continue
        branch = ensure_series(db, work.library_id, path)
        if branch.id in seen:
            continue
        position = entry["position"].strip()
        if not seen:
            work.series, work.series_index = branch.path[0], position
        db.add(SeriesMembership(work_id=work.id, series_id=branch.id, rank=len(seen), position=position,
                                volume_number=volume_number(position)))
        seen.add(branch.id)


def in_named_series(name):
    return (select(1).select_from(SeriesMembership).join(SeriesAncestor, SeriesAncestor.descendant_id == SeriesMembership.series_id)
            .join(Series, Series.id == SeriesAncestor.ancestor_id)
            .where(SeriesMembership.work_id == Work.id, Series.normalized_name == normal(name)).correlate(Work).exists())
