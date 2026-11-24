from datetime import datetime, timezone

from sqlalchemy import select, update

from .db import CollectionItem, Edition, ReadingState


def timestamp(value):
    return value.replace(tzinfo=timezone.utc) if value else datetime.min.replace(tzinfo=timezone.utc)


def combine_reading_state(target, source):
    target.favorite = target.favorite or source.favorite
    if source.started and (not target.started or timestamp(source.started) < timestamp(target.started)):
        target.started = source.started
    if timestamp(source.last_read) > timestamp(target.last_read):
        target.last_read, target.last_file_id = source.last_read, source.last_file_id
        target.percentage, target.device = source.percentage, source.device
        target.chapter = source.chapter
    if (target.status == "unread" or
            source.status == "finished" and timestamp(source.finished) > timestamp(target.finished)):
        target.status = source.status
    if timestamp(source.finished) > timestamp(target.finished):
        target.finished = source.finished


def move_related_records(db, target_id, source_id):
    db.execute(update(Edition).where(Edition.work_id == source_id).values(work_id=target_id))

    states = list(db.scalars(select(ReadingState).where(ReadingState.work_id.in_([target_id, source_id]))))
    targets = {row.user_id: row for row in states if row.work_id == target_id}
    for source in states:
        if source.work_id == source_id:
            target = targets.get(source.user_id)
            if target:
                combine_reading_state(target, source)
                db.delete(source)
            else:
                source.work_id = target_id

    memberships = list(db.scalars(select(CollectionItem).where(CollectionItem.work_id.in_([target_id, source_id]))
                                  .order_by(CollectionItem.collection_id, CollectionItem.work_id).with_for_update()))
    collections = {item.collection_id for item in memberships if item.work_id == target_id}
    for item in memberships:
        if item.work_id == source_id:
            if item.collection_id in collections:
                db.delete(item)
            else:
                item.work_id = target_id
