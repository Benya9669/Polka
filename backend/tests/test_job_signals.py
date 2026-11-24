from contextlib import nullcontext
from unittest.mock import Mock

from sqlalchemy import select

from app import db, job_signals
from app.catalog import queue
from tests.test_app import library  # noqa: F401


def test_wakeup_is_sent_only_after_a_job_is_committed(library, monkeypatch):
    _, library_id, _ = library
    pipeline = Mock()
    redis = Mock()
    redis.pipeline.return_value = nullcontext(pipeline)
    monkeypatch.setattr(job_signals, "REDIS_URL", "redis://unused")
    monkeypatch.setattr(job_signals, "redis_client", lambda _: redis)

    def verify_published_jobs():
        with db.Session() as reader:
            queued = select(db.Job.kind).where(db.Job.status == "queued").order_by(db.Job.id)
            assert list(reader.scalars(queued)) == ["scan", "conversion"]

    pipeline.execute.side_effect = verify_published_jobs
    with db.Session() as session:
        collection = session.get(db.Library, library_id)
        owner = session.get(db.User, collection.owner_id)
        queue(session, owner, collection, "discarded", {})
        session.rollback()
        queue(session, owner, collection, "merge", {}).status = "done"
        session.commit()
        pipeline.execute.assert_not_called()

        queue(session, owner, collection, "scan", {})
        queue(session, owner, collection, "conversion", {})
        pipeline.execute.assert_not_called()
        session.commit()

    pipeline.lpush.assert_called_once_with(job_signals.WAKE_KEY, "1")
    pipeline.ltrim.assert_called_once_with(job_signals.WAKE_KEY, 0, 0)
    pipeline.execute.assert_called_once_with()
