import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = None


def start(sync_fn, min_minutes=2, max_minutes=4):
    global _scheduler
    jitter = (max_minutes - min_minutes) * 60
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        sync_fn,
        trigger="interval",
        minutes=min_minutes,
        jitter=jitter,
        id="auto_sync",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started – syncing every %d–%d minutes", min_minutes, max_minutes)
    return _scheduler


def reschedule(min_minutes, max_minutes):
    if _scheduler is None:
        return
    jitter = (max_minutes - min_minutes) * 60
    _scheduler.reschedule_job("auto_sync", trigger="interval", minutes=min_minutes, jitter=jitter)
    logger.info("Scheduler rescheduled – syncing every %d–%d minutes", min_minutes, max_minutes)


def next_run():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None
