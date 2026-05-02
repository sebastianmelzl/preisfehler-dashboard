import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = None


def start(sync_fn, interval_minutes=20):
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        sync_fn,
        trigger="interval",
        minutes=interval_minutes,
        id="auto_sync",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started – syncing every %d minutes", interval_minutes)
    return _scheduler


def reschedule(interval_minutes):
    if _scheduler is None:
        return
    _scheduler.reschedule_job("auto_sync", trigger="interval", minutes=interval_minutes)
    logger.info("Scheduler rescheduled – syncing every %d minutes", interval_minutes)


def next_run():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None
