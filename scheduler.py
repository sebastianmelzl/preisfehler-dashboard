import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = None


def start(sync_fn):
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        sync_fn,
        trigger="interval",
        minutes=2,
        jitter=120,
        id="auto_sync",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started – syncing every 2–4 minutes (random)")
    return _scheduler


def next_run():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("auto_sync")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None
