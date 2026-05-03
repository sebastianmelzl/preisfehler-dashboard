import os
import sqlite3
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "deals.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deals (
                thread_id    TEXT PRIMARY KEY,
                title        TEXT,
                title_slug   TEXT,
                price        REAL,
                next_best    REAL,
                discount_pct INTEGER,
                merchant     TEXT,
                category     TEXT,
                temperature  REAL,
                is_expired   INTEGER DEFAULT 0,
                is_hot       INTEGER DEFAULT 0,
                published_at INTEGER,
                discovered_at TEXT,
                notified     INTEGER DEFAULT 0,
                url          TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_status (
                id                   INTEGER PRIMARY KEY DEFAULT 1,
                is_running           INTEGER DEFAULT 0,
                last_sync            TEXT,
                deals_found          INTEGER DEFAULT 0,
                deals_new            INTEGER DEFAULT 0,
                message              TEXT DEFAULT '',
                sync_interval_minutes INTEGER DEFAULT NULL
            );

            INSERT OR IGNORE INTO sync_status (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS keywords (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword    TEXT NOT NULL,
                active     INTEGER DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at        TEXT,
                preisfehler_found INTEGER DEFAULT 0,
                preisfehler_new  INTEGER DEFAULT 0,
                new_deals_found  INTEGER DEFAULT 0,
                new_deals_new    INTEGER DEFAULT 0,
                message          TEXT DEFAULT ''
            );
        """)
        try:
            conn.execute("ALTER TABLE sync_status ADD COLUMN sync_interval_minutes INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE sync_status ADD COLUMN sync_interval_max INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN source TEXT DEFAULT 'preisfehler'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN manually_expired INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN link_host TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN shop_url TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN keyword_notified INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE sync_status ADD COLUMN consecutive_empty INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE sync_status ADD COLUMN sync_seq INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN sync_seq INTEGER DEFAULT 0")
        except Exception:
            pass
    logger.info("Database initialised")


def next_sync_seq():
    """Increment and return the new sync sequence number."""
    with get_conn() as conn:
        conn.execute("UPDATE sync_status SET sync_seq = sync_seq + 1 WHERE id=1")
        row = conn.execute("SELECT sync_seq FROM sync_status WHERE id=1").fetchone()
    return row["sync_seq"] if row else 1


def upsert_deals(deals_data, source="preisfehler", sync_seq=0):
    """Insert new deals, update existing ones. Returns list of new thread_ids."""
    new_ids = []
    with get_conn() as conn:
        for d in deals_data:
            existing = conn.execute(
                "SELECT thread_id, source, manually_expired FROM deals WHERE thread_id = ?", (d["thread_id"],)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO deals
                       (thread_id, title, title_slug, price, next_best, discount_pct,
                        merchant, category, temperature, is_expired, is_hot,
                        published_at, discovered_at, notified, url, source, link_host, shop_url, sync_seq)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                    (
                        d["thread_id"], d["title"], d["title_slug"],
                        d["price"], d["next_best"], d["discount_pct"],
                        d["merchant"], d["category"], d["temperature"],
                        d["is_expired"], d["is_hot"], d["published_at"],
                        datetime.utcnow().isoformat(), d["url"], source,
                        d.get("link_host", ""), d.get("shop_url", ""), sync_seq,
                    ),
                )
                new_ids.append(d["thread_id"])
            else:
                # Never downgrade a preisfehler deal to a normal deal
                if existing["source"] == "preisfehler" and source == "new":
                    continue
                # Don't overwrite manually expired deals
                if existing["manually_expired"]:
                    conn.execute(
                        "UPDATE deals SET temperature=?, is_hot=? WHERE thread_id=?",
                        (d["temperature"], d["is_hot"], d["thread_id"]),
                    )
                else:
                    conn.execute(
                        """UPDATE deals SET temperature=?, is_expired=?, is_hot=?
                           WHERE thread_id=?""",
                        (d["temperature"], d["is_expired"], d["is_hot"], d["thread_id"]),
                    )
    return new_ids



def mark_expired(thread_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET is_expired=1, manually_expired=1 WHERE thread_id=?", (thread_id,)
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
    placeholders = ",".join("?" * len(thread_ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE deals SET notified=1 WHERE thread_id IN ({placeholders})",
            thread_ids,
        )


def get_all_deals(include_new=False):
    with get_conn() as conn:
        if include_new:
            cutoff = int(time.time()) - 600
            rows = conn.execute(
                """SELECT * FROM deals
                   WHERE source = 'preisfehler'
                      OR (source = 'new' AND published_at >= ?)
                   ORDER BY published_at DESC""",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deals WHERE source = 'preisfehler' ORDER BY published_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def cleanup_new_deals():
    """Remove non-preisfehler deals posted more than 1 hour ago.
    Display still filters to 10-min window via published_at in get_all_deals,
    but keeping records for 1h prevents re-insertion within the same lifecycle."""
    cutoff = int(time.time()) - 3600
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM deals WHERE source = 'new' AND published_at < ?", (cutoff,)
        )


def set_sync_status(is_running, deals_found=0, deals_new=0, message=""):
    with get_conn() as conn:
        conn.execute(
            """UPDATE sync_status SET
               is_running=?, last_sync=?, deals_found=?, deals_new=?, message=?
               WHERE id=1""",
            (
                is_running,
                datetime.utcnow().isoformat(),
                deals_found,
                deals_new,
                message,
            ),
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
    """Count source='new' deals that are new this sync AND within the 10-min display window."""
    cutoff = int(time.time()) - 600
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM deals WHERE source='new' AND sync_seq=? AND published_at>=?",
            (sync_seq, cutoff),
        ).fetchone()
    return row["cnt"] if row else 0


def add_sync_log(preisfehler_found, preisfehler_new, new_deals_found, new_deals_new, message=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sync_log
               (synced_at, preisfehler_found, preisfehler_new, new_deals_found, new_deals_new, message)
               VALUES (?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), preisfehler_found, preisfehler_new,
             new_deals_found, new_deals_new, message),
        )
        conn.execute("DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 100)")


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
            "INSERT INTO keywords (keyword, active, created_at) VALUES (?, 1, ?)",
            (keyword.strip(), datetime.utcnow().isoformat()),
        )


def delete_keyword(keyword_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM keywords WHERE id=?", (keyword_id,))


def toggle_keyword(keyword_id):
    with get_conn() as conn:
        conn.execute("UPDATE keywords SET active = 1 - active WHERE id=?", (keyword_id,))


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
    placeholders = ",".join("?" * len(thread_ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE deals SET keyword_notified=1 WHERE thread_id IN ({placeholders})",
            thread_ids,
        )


