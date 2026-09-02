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
    """Adaptive cadence: run tight while syncs succeed, back off the moment
    mydealz starts throttling us (403 / empty), cool right down if it keeps
    up. `consecutive_empty` counts both empty results and failed fetches."""
    fast = _fast_poll_enabled()
    try:
        empty = db.get_sync_status().get("consecutive_empty") or 0
    except Exception:
        empty = 0

    if empty >= 3:
        return random.randint(1200, 2400)   # 20–40 min hard cooldown
    if empty >= 1:
        return random.randint(300, 600)     # one bad sync → ease off to 5–10 min

    if _is_night():
        return 600 if fast else 3600
    return 75 if fast else random.randint(75, 150)   # healthy: ~1.5–2.5 min


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
