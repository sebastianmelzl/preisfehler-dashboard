import os
import logging
import statistics
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class _ConnWrapper:
    """Shim so call sites can use conn.execute() like sqlite3."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield _ConnWrapper(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                thread_id        TEXT PRIMARY KEY,
                title            TEXT,
                title_slug       TEXT,
                price            REAL,
                next_best        REAL,
                discount_pct     INTEGER,
                merchant         TEXT,
                category         TEXT,
                temperature      REAL,
                is_expired       INTEGER DEFAULT 0,
                is_hot           INTEGER DEFAULT 0,
                published_at     INTEGER,
                discovered_at    TEXT,
                notified         INTEGER DEFAULT 0,
                url              TEXT,
                source           TEXT DEFAULT 'preisfehler',
                manually_expired INTEGER DEFAULT 0,
                link_host        TEXT DEFAULT '',
                shop_url         TEXT DEFAULT '',
                keyword_notified     INTEGER DEFAULT 0,
                sync_seq             INTEGER DEFAULT 0,
                temp_spike_notified  INTEGER DEFAULT 0,
                temp_updated_at      INTEGER DEFAULT 0,
                temp_rate            REAL    DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id                    INTEGER PRIMARY KEY DEFAULT 1,
                is_running            INTEGER DEFAULT 0,
                last_sync             TEXT,
                deals_found           INTEGER DEFAULT 0,
                deals_new             INTEGER DEFAULT 0,
                message               TEXT DEFAULT '',
                sync_interval_minutes INTEGER DEFAULT NULL,
                sync_interval_max     INTEGER DEFAULT NULL,
                consecutive_empty     INTEGER DEFAULT 0,
                sync_seq              INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO sync_status (id) VALUES (1) ON CONFLICT DO NOTHING")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id         SERIAL PRIMARY KEY,
                keyword    TEXT NOT NULL,
                active     INTEGER DEFAULT 1,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id                SERIAL PRIMARY KEY,
                synced_at         TEXT,
                preisfehler_found INTEGER DEFAULT 0,
                preisfehler_new   INTEGER DEFAULT 0,
                new_deals_found   INTEGER DEFAULT 0,
                new_deals_new     INTEGER DEFAULT 0,
                message           TEXT DEFAULT ''
            )
        """)
        # Upgrade columns for existing databases
        for col_sql in [
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'preisfehler'",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS manually_expired INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS link_host TEXT DEFAULT ''",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS shop_url TEXT DEFAULT ''",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS keyword_notified INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS sync_seq INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS temp_spike_notified INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS temp_updated_at INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN IF NOT EXISTS temp_rate REAL DEFAULT 0",
            "ALTER TABLE sync_status ADD COLUMN IF NOT EXISTS sync_interval_minutes INTEGER DEFAULT NULL",
            "ALTER TABLE sync_status ADD COLUMN IF NOT EXISTS sync_interval_max INTEGER DEFAULT NULL",
            "ALTER TABLE sync_status ADD COLUMN IF NOT EXISTS consecutive_empty INTEGER DEFAULT 0",
            "ALTER TABLE sync_status ADD COLUMN IF NOT EXISTS sync_seq INTEGER DEFAULT 0",
        ]:
            conn.execute(col_sql)
    logger.info("Database initialised")


def next_sync_seq():
    with get_conn() as conn:
        conn.execute("UPDATE sync_status SET sync_seq = sync_seq + 1 WHERE id=1")
        row = conn.execute("SELECT sync_seq FROM sync_status WHERE id=1").fetchone()
    return row["sync_seq"] if row else 1


def _detect_spikes(candidates):
    """Identify disproportionate temperature risers relative to the current batch.

    For each candidate we compute °/min since the last recorded update.
    A deal is a spike if its rate is >= SPIKE_FACTOR × median of all positive
    rates in this batch AND above an absolute floor, so a single slow-moving
    batch doesn't trigger false positives.

    candidates: list of dicts with keys old_temp, new_temp, dt_seconds + full deal fields.
    Returns the subset that are spiking.
    """
    MIN_DT = 20        # seconds – ignore comparisons that are too fresh
    MIN_RATE = 5.0     # °/min absolute floor to even be considered
    SPIKE_FACTOR = 3.0 # must be N× the median positive rate of the whole batch

    rated = []
    for c in candidates:
        if c["dt_seconds"] < MIN_DT:
            continue
        rate = (c["new_temp"] - c["old_temp"]) / (c["dt_seconds"] / 60.0)
        if rate > 0:
            rated.append({**c, "rate": rate})

    if not rated:
        return []

    all_rates = [r["rate"] for r in rated]

    if len(all_rates) == 1:
        # Only one heating deal — flag only if rate is notable on its own
        return [r for r in rated if r["rate"] >= MIN_RATE * 4]

    median_rate = statistics.median(all_rates)
    threshold = max(MIN_RATE, SPIKE_FACTOR * median_rate)
    return [r for r in rated if r["rate"] >= threshold]


def upsert_deals(deals_data, source="preisfehler", sync_seq=0):
    """Insert new deals, update existing ones.
    Returns (new_ids, spike_deals) where spike_deals are dynamically detected
    temperature spikes among source='new' deals."""
    new_ids = []
    candidates = []   # new-source deals eligible for spike detection
    now = int(time.time())

    with get_conn() as conn:
        for d in deals_data:
            existing = conn.execute(
                """SELECT thread_id, source, manually_expired, temperature,
                          temp_spike_notified, temp_updated_at
                   FROM deals WHERE thread_id = %s""",
                (d["thread_id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO deals
                       (thread_id, title, title_slug, price, next_best, discount_pct,
                        merchant, category, temperature, is_expired, is_hot,
                        published_at, discovered_at, notified, url, source,
                        link_host, shop_url, sync_seq, temp_updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s)""",
                    (
                        d["thread_id"], d["title"], d["title_slug"],
                        d["price"], d["next_best"], d["discount_pct"],
                        d["merchant"], d["category"], d["temperature"],
                        d["is_expired"], d["is_hot"], d["published_at"],
                        datetime.utcnow().isoformat(), d["url"], source,
                        d.get("link_host", ""), d.get("shop_url", ""), sync_seq, now,
                    ),
                )
                new_ids.append(d["thread_id"])
            else:
                # Never downgrade a preisfehler deal to a normal deal
                if existing["source"] == "preisfehler" and source == "new":
                    continue

                new_temp = d["temperature"] or 0
                old_temp = existing["temperature"] or 0
                last_updated = existing["temp_updated_at"] or 0
                dt_seconds = (now - last_updated) if last_updated > 0 else 0

                # Rate in °/min — stored for every source='new' deal so the
                # dashboard can do live spike detection via the median.
                rate = ((new_temp - old_temp) / (dt_seconds / 60.0)
                        if source == "new" and dt_seconds > 0 else 0.0)

                # Collect spike candidates for Telegram notifications
                if (source == "new"
                        and not existing["temp_spike_notified"]
                        and new_temp > old_temp
                        and dt_seconds > 0):
                    candidates.append({
                        **d,
                        "old_temp": old_temp,
                        "new_temp": new_temp,
                        "dt_seconds": dt_seconds,
                    })

                # Don't overwrite manually expired deals
                if existing["manually_expired"]:
                    conn.execute(
                        """UPDATE deals SET temperature=%s, is_hot=%s,
                           temp_updated_at=%s, temp_rate=%s WHERE thread_id=%s""",
                        (d["temperature"], d["is_hot"], now, rate, d["thread_id"]),
                    )
                else:
                    conn.execute(
                        """UPDATE deals SET temperature=%s, is_expired=%s, is_hot=%s,
                           temp_updated_at=%s, temp_rate=%s WHERE thread_id=%s""",
                        (d["temperature"], d["is_expired"], d["is_hot"], now, rate, d["thread_id"]),
                    )

    spike_deals = _detect_spikes(candidates)
    return new_ids, spike_deals


def mark_expired(thread_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET is_expired=1, manually_expired=1 WHERE thread_id=%s", (thread_id,)
        )


def get_unnotified_deals():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM deals WHERE notified=0 ORDER BY published_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_notified(thread_ids):
    if not thread_ids:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET notified=1 WHERE thread_id = ANY(%s)",
            (list(thread_ids),),
        )


def get_all_deals(include_new=False):
    with get_conn() as conn:
        if include_new:
            cutoff = int(time.time()) - 2400
            rows = conn.execute(
                """SELECT * FROM deals
                   WHERE source = 'preisfehler'
                      OR (source = 'new' AND published_at >= %s)
                   ORDER BY published_at DESC""",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deals WHERE source = 'preisfehler' ORDER BY published_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def cleanup_new_deals():
    """Remove non-preisfehler deals posted more than 1 hour ago."""
    cutoff = int(time.time()) - 3600
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM deals WHERE source = 'new' AND published_at < %s", (cutoff,)
        )


def set_sync_status(is_running, deals_found=0, deals_new=0, message=""):
    with get_conn() as conn:
        conn.execute(
            """UPDATE sync_status SET
               is_running=%s, last_sync=%s, deals_found=%s, deals_new=%s, message=%s
               WHERE id=1""",
            (int(is_running), datetime.utcnow().isoformat(), deals_found, deals_new, message),
        )


def get_sync_status():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sync_status WHERE id=1").fetchone()
    return dict(row) if row else {}


def increment_empty_sync():
    with get_conn() as conn:
        conn.execute("UPDATE sync_status SET consecutive_empty = consecutive_empty + 1 WHERE id=1")
        row = conn.execute("SELECT consecutive_empty FROM sync_status WHERE id=1").fetchone()
    return row["consecutive_empty"] if row else 0


def reset_empty_sync():
    with get_conn() as conn:
        conn.execute("UPDATE sync_status SET consecutive_empty = 0 WHERE id=1")


def count_visible_new_this_sync(sync_seq):
    """Count source='new' deals that are new this sync AND within the 40-min display window."""
    cutoff = int(time.time()) - 2400
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM deals WHERE source='new' AND sync_seq=%s AND published_at>=%s",
            (sync_seq, cutoff),
        ).fetchone()
    return row["cnt"] if row else 0


def add_sync_log(preisfehler_found, preisfehler_new, new_deals_found, new_deals_new, message=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sync_log
               (synced_at, preisfehler_found, preisfehler_new, new_deals_found, new_deals_new, message)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (datetime.utcnow().isoformat(), preisfehler_found, preisfehler_new,
             new_deals_found, new_deals_new, message),
        )
        conn.execute(
            "DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 100)"
        )


def get_sync_log():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

def get_keywords():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM keywords ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def add_keyword(keyword):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO keywords (keyword, active, created_at) VALUES (%s, 1, %s)",
            (keyword.strip(), datetime.utcnow().isoformat()),
        )


def delete_keyword(keyword_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM keywords WHERE id=%s", (keyword_id,))


def toggle_keyword(keyword_id):
    with get_conn() as conn:
        conn.execute("UPDATE keywords SET active = 1 - active WHERE id=%s", (keyword_id,))


def get_unnotified_keyword_deals(keywords):
    """Return deals not yet keyword-notified that match any active keyword."""
    active = [k["keyword"].lower() for k in keywords if k["active"]]
    if not active:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM deals WHERE keyword_notified=0 AND is_expired=0"
        ).fetchall()
    matches = []
    for r in rows:
        d = dict(r)
        title_lower = (d.get("title") or "").lower()
        for kw in active:
            if kw in title_lower:
                d["matched_keyword"] = kw
                matches.append(d)
                break
    return matches


def mark_keyword_notified(thread_ids):
    if not thread_ids:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET keyword_notified=1 WHERE thread_id = ANY(%s)",
            (list(thread_ids),),
        )


def mark_spike_notified(thread_ids):
    if not thread_ids:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET temp_spike_notified=1 WHERE thread_id = ANY(%s)",
            (list(thread_ids),),
        )
