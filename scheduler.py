import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

import database as db

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

_scheduler = None
_sync_fn = None


def _is_night():
    now = datetime.now(BERLIN)
    return now.hour < 6 or (now.hour == 23 and now.minute >= 59)


def _fast_poll_enabled():
    try:
        return db.get_fast_poll_enabled()
    except Exception:
        return False


def _next_delay_seconds():
    fast = _fast_poll_enabled()
    if _is_night():
        return 300 if fast else 3600
    return 60 if fast else random.randint(30, 240)


def _run_and_reschedule():
    try:
        _sync_fn()
    finally:
        _schedule_next()


def _schedule_next():
    delay = _next_delay_seconds()
    run_at = datetime.now(BERLIN) + timedelta(seconds=delay)
    _scheduler.add_job(
        _run_and_reschedule,
        trigger=DateTrigger(run_date=run_at),
        id="auto_sync",
        replace_existing=True,
    )
    tag = "fast" if _fast_poll_enabled() else "normal"
    logger.info("Next sync (%s) scheduled in %ss", tag, delay)


def start(sync_fn):
    global _scheduler, _sync_fn
    _sync_fn = sync_fn
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.start()
    _schedule_next()
    logger.info("Scheduler started – random 0:30–4:00 min (night: hourly)")
    return _scheduler


def next_run():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None
