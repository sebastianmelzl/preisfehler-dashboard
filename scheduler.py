import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


def start(sync_fn, interval_minutes=20):
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        sync_fn,
        trigger="interval",
        minutes=interval_minutes,
        id="auto_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started – syncing every %d minutes", interval_minutes)
    return scheduler
