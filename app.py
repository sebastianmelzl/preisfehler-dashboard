import logging
import os
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import database as db
import notifier
import scheduler as sched
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_sync_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def _normalise(t):
    """Map raw API thread dict to our DB schema."""
    price = t.get("price")
    next_best = t.get("nextBestPrice") or 0
    return {
        "thread_id": t["threadId"],
        "title": t.get("title", ""),
        "title_slug": t.get("titleSlug", ""),
        "price": price,
        "next_best": next_best if next_best > 0 else None,
        "discount_pct": scraper.discount_pct(price, next_best),
        "merchant": (t.get("merchant") or {}).get("merchantName", "?"),
        "category": (t.get("mainGroup") or {}).get("threadGroupName", ""),
        "temperature": t.get("temperature") or 0,
        "is_expired": int(bool(t.get("isExpired"))),
        "is_hot": int(bool(t.get("isHot"))),
        "published_at": t.get("publishedAt") or 0,
        "url": scraper.deal_url(t),
        "shop_url": f"https://www.mydealz.de/visit/threadmain/{t['threadId']}",
        "link_host": (t.get("linkHost") or "").lower(),
    }


def run_sync():
    if not _sync_lock.acquire(blocking=False):
        logger.info("Sync already running – skipping")
        return
    try:
        db.set_sync_status(True, message="Lädt Deals…")

        # Preisfehler
        deals_raw, total = scraper.fetch_deals(limit=50)
        normalised = [_normalise(t) for t in deals_raw]
        new_ids = db.upsert_deals(normalised, source="preisfehler")

        unnotified = db.get_unnotified_deals()
        if unnotified:
            active_pf = [d for d in unnotified if not d["is_expired"] and d.get("source") == "preisfehler"]
            if active_pf:
                notifier.notify_new_deals(active_pf)

            high_disc = [
                d for d in unnotified
                if not d["is_expired"]
                and d.get("source") == "new"
                and (d.get("discount_pct") or 0) >= 80
            ]
            if high_disc:
                notifier.notify_high_discount_deals(high_disc)

            db.mark_notified([d["thread_id"] for d in unnotified])

        # General new deals (no Telegram notifications)
        try:
            new_raw, _ = scraper.fetch_new_deals(limit=20)
            new_normalised = [_normalise(t) for t in new_raw]
            db.upsert_deals(new_normalised, source="new")
            db.cleanup_new_deals()
        except Exception as e:
            logger.warning("New deals fetch failed: %s", e)

        db.set_sync_status(
            False,
            deals_found=len(normalised),
            deals_new=len(new_ids),
            message=f"OK – {len(new_ids)} neu",
        )
        logger.info("Sync done: %d deals, %d new", len(normalised), len(new_ids))
    except Exception as e:
        db.set_sync_status(False, message=f"Fehler: {e}")
        logger.error("Sync failed: %s", e)
    finally:
        _sync_lock.release()


def run_sync_async():
    threading.Thread(target=run_sync, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/deals")
def api_deals():
    include_new = request.args.get("include_new") == "1"
    raw = db.get_all_deals(include_new=include_new)
    if not raw:
        return jsonify([])

    max_temp = max((d["temperature"] or 0) for d in raw) or 1
    result = []
    for d in raw:
        temp = d["temperature"] or 0
        result.append({
            **d,
            "hot_bar_width": scraper.hot_bar_width(temp, max_temp),
            "price_fmt": scraper.fmt_price(d["price"]),
            "next_best_fmt": scraper.fmt_price(d["next_best"]) if d["next_best"] else None,
            "published_fmt": (
                datetime.utcfromtimestamp(d["published_at"]).strftime("%d.%m.%Y")
                if d["published_at"] else "?"
            ),
        })
    return jsonify(result)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    run_sync_async()
    return jsonify({"status": "started"})


@app.route("/api/sync/status")
def api_sync_status():
    status = db.get_sync_status()
    status["next_sync"] = sched.next_run()
    return jsonify(status)




@app.route("/api/deals/<thread_id>/expire", methods=["POST"])
def api_expire_deal(thread_id):
    db.mark_expired(thread_id)
    return jsonify({"ok": True})


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    ok, msg = notifier.send_test(token, chat_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/debug/new-deals")
def api_debug_new_deals():
    try:
        deals, _ = scraper.fetch_new_deals(limit=50)
        return jsonify([{"id": d.get("threadId"), "title": d.get("title"), "published": d.get("publishedAt")} for d in deals])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/components")
def api_debug_components():
    try:
        import re
        sess = scraper._get_session()
        import time as _time
        resp = sess.get(f"https://www.mydealz.de/deals-new?_={int(_time.time())}", headers={"Accept": "text/html", "Cache-Control": "no-cache"}, timeout=15)
        names = re.findall(r'"name":"([^"]*Normalizer[^"]*)"', resp.text)
        counts = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        return jsonify({"normalizers": counts, "total_chars": len(resp.text)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

db.init_db()

# Run initial sync in background at startup
run_sync_async()

# Start scheduler (always in production, only in main process locally)
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("START_SCHEDULER"):
    sched.start(run_sync)

if __name__ == "__main__":
    import scheduler as sched_local
    sched_local.start(run_sync)
    app.run(debug=False, port=5003)
