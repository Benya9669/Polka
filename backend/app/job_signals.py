import logging
import os
import time
from functools import lru_cache

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import event

from .db import Session

REDIS_URL = os.getenv("REDIS_URL", "").strip()
WAKE_KEY = "polka:jobs:wake"
PENDING_WAKE = "job_wake_after_commit"
logger = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def redis_client(timeout):
    return Redis.from_url(REDIS_URL, socket_connect_timeout=0.35, socket_timeout=timeout,
                          retry=Retry(NoBackoff(), 0))


def wake_after_commit(db, task):
    if REDIS_URL:
        db.info.setdefault(PENDING_WAKE, []).append(task)


@event.listens_for(Session, "after_commit")
def publish(db):
    if db.in_nested_transaction():
        return
    if not any(task.status == "queued" for task in db.info.pop(PENDING_WAKE, [])):
        return
    try:
        with redis_client(0.35).pipeline() as pipe:
            pipe.lpush(WAKE_KEY, "1")
            pipe.ltrim(WAKE_KEY, 0, 0)
            pipe.execute()
    except (RedisError, ValueError) as exc:
        logger.debug("Не удалось разбудить обработчик через Redis: %s", type(exc).__name__)


@event.listens_for(Session, "after_transaction_end")
def clear_pending_wake(db, transaction):
    if transaction.parent is None:
        db.info.pop(PENDING_WAKE, None)


def wait_for_job(delay):
    if delay <= 0:
        return
    if REDIS_URL:
        try:
            redis_client(4).brpop(WAKE_KEY, timeout=delay)
            return
        except (RedisError, ValueError) as exc:
            logger.debug("Redis недоступен, продолжаем опрос PostgreSQL: %s", type(exc).__name__)
    time.sleep(min(delay, 1))
