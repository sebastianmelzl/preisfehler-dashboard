import logging
import os
import statistics
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import database as db
import notifier
import scheduler as sched
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_deploy_time = datetime.utcnow()
_sync_lock = threading.Lock()


@app.before_request
def require_auth():
    """Gate every route but /health behind HTTP Basic Auth.

    Only enforced when DASHBOARD_PASSWORD is set, so local dev without
    the env var keeps working unauthenticated.
    """
    if request.path == "/health":
        return
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        return
    auth = request.authorization
    if not auth or auth.username != os.environ.get("DASHBOARD_USER", "admin") or auth.password != password:
        return Response(
            "Zugriff verweigert", 401, {"WWW-Authenticate": 'Basic realm="Preisfehler Dashboard"'}
        )


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
        "published_at": t.get("publishedAt") or int(time.time()),
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
        current_seq = db.next_sync_seq()

        # Preisfehler — fetch page 1 + 2 (sorted by new) to keep recently added
        # deals updated even after they fall off page 1
        deals_raw, total = scraper.fetch_deals(limit=50, page=1)
        try:
            page2_raw, _ = scraper.fetch_deals(limit=50, page=2)
            seen = {t["threadId"] for t in deals_raw}
            deals_raw += [t for t in page2_raw if t["threadId"] not in seen]
        except Exception as e:
            logger.warning("Page 2 fetch failed: %s", e)
        normalised = [_normalise(t) for t in deals_raw]
        new_ids, _ = db.upsert_deals(normalised, source="preisfehler", sync_seq=current_seq)

        if normalised:
            db.reset_empty_sync()
        else:
            consecutive = db.increment_empty_sync()
            if consecutive >= 3:
                notifier.notify_scraper_warning(consecutive)

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

        # Keyword notifications (all sources)
        keywords = db.get_keywords()
        if keywords:
            kw_matches = db.get_unnotified_keyword_deals(keywords)
            if kw_matches:
                notifier.notify_keyword_matches(kw_matches)
                db.mark_keyword_notified([d["thread_id"] for d in kw_matches])

        # General new deals
        visible_new_count = 0
        try:
            new_raw, _ = scraper.fetch_new_deals(limit=20)
            new_normalised = [_normalise(t) for t in new_raw]
            _, spike_deals = db.upsert_deals(new_normalised, source="new", sync_seq=current_seq)
            if spike_deals:
                notifier.notify_temperature_spike(spike_deals)
                db.mark_spike_notified([d["thread_id"] for d in spike_deals])
            db.cleanup_new_deals()
            visible_new_count = db.count_visible_new_this_sync(current_seq)
        except Exception as e:
            logger.warning("New deals fetch failed: %s", e)

        # Availability check: re-verify a small batch of active preisfehler
        # deals' shop links haven't gone dead since the last check
        try:
            due_checks = db.get_deals_needing_availability_check(max_age_seconds=900, limit=10)
            for dd in due_checks:
                status = scraper.check_availability(dd["shop_url"])
                db.update_availability(dd["thread_id"], status)
        except Exception as e:
            logger.warning("Availability check failed: %s", e)

        db.set_sync_status(
            False,
            deals_found=len(normalised),
            deals_new=len(new_ids),
            message=f"OK – {len(new_ids)} neu",
        )
        db.add_sync_log(
            preisfehler_found=len(normalised),
            preisfehler_new=len(new_ids),
            new_deals_found=0,
            new_deals_new=visible_new_count,
        )
        logger.info("Sync done: %d deals, %d new", len(normalised), len(new_ids))
    except Exception as e:
        db.set_sync_status(False, message=f"Fehler: {e}")
        db.add_sync_log(0, 0, 0, 0, message=str(e))
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

    current_seq = db.get_sync_status().get("sync_seq", 0)
    keywords = db.get_keywords()
    active_kws = [k["keyword"].lower() for k in keywords if k["active"]]
    max_temp = max((d["temperature"] or 0) for d in raw) or 1

    # Spike detection: combine per-sync rate with cumulative rate since discovery.
    # A deal is a spike candidate if EITHER its recent velocity OR its overall
    # trajectory since first seen is disproportionately high relative to peers.
    _MIN_RATE = 20.0
    _SPIKE_FACTOR = 4.0

    def _cumulative_rate(d):
        """°/min since the deal was first discovered."""
        disc = d.get("discovered_at")
        if not disc or d.get("source") != "new":
            return 0.0
        try:
            disc_dt = datetime.fromisoformat(disc)
            minutes = max((datetime.utcnow() - disc_dt).total_seconds() / 60.0, 1.0)
            return ((d.get("temperature") or 0) - (d.get("initial_temp") or 0)) / minutes
        except Exception:
            return 0.0

    new_deals = [d for d in raw if d.get("source") == "new" and (d.get("temperature") or 0) >= 30]

    # Collect both rate types for each eligible new deal
    rate_pairs = [
        (d["thread_id"], max(d.get("temp_rate") or 0, _cumulative_rate(d)))
        for d in new_deals
    ]
    eligible_rates = [r for _, r in rate_pairs if r >= _MIN_RATE]

    if len(eligible_rates) >= 3:
        p75 = statistics.quantiles(eligible_rates, n=4)[2]
        spike_threshold = max(_MIN_RATE, _SPIKE_FACTOR * p75)
    elif eligible_rates:
        spike_threshold = _MIN_RATE * 3
    else:
        spike_threshold = None

    spiking_ids = set()
    if spike_threshold is not None:
        spiking_ids = {tid for tid, r in rate_pairs if r >= spike_threshold}

    result = []
    for d in raw:
        temp = d["temperature"] or 0
        is_spiking = d["thread_id"] in spiking_ids
        hot_bar_width = scraper.hot_bar_width(temp, max_temp)
        keyword_match = next((kw for kw in active_kws if kw in (d.get("title") or "").lower()), None)

        # Priority score (0-100): how much this deal deserves attention right
        # now, combining community validation (discount, temperature, the
        # preisfehler tag itself), momentum (spiking), and personal relevance
        # (keyword match) into one sortable number instead of ad-hoc tiers.
        score = 0.0
        score += min(d.get("discount_pct") or 0, 100) * 0.35
        score += hot_bar_width * 0.25
        score += 20 if d.get("source") == "preisfehler" else 0
        score += 15 if is_spiking else 0
        score += 10 if keyword_match else 0

        result.append({
            **d,
            "hot_bar_width": hot_bar_width,
            "price_fmt": scraper.fmt_price(d["price"]),
            "next_best_fmt": scraper.fmt_price(d["next_best"]) if d["next_best"] else None,
            "published_fmt": (
                datetime.utcfromtimestamp(d["published_at"]).strftime("%d.%m.%Y")
                if d["published_at"] else "?"
            ),
            "is_new_this_sync": int(d.get("sync_seq") or 0) == current_seq and current_seq > 0,
            "keyword_match": keyword_match,
            "is_spiking": is_spiking,
            "score": min(round(score), 100),
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
    status["deployed_at"] = _deploy_time.isoformat()
    status["deploy_commit"] = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
    return jsonify(status)




@app.route("/api/keywords", methods=["GET"])
def api_keywords_get():
    return jsonify(db.get_keywords())


@app.route("/api/keywords", methods=["POST"])
def api_keywords_add():
    keyword = (request.json or {}).get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "empty"}), 400
    db.add_keyword(keyword)
    return jsonify({"ok": True})


@app.route("/api/keywords/<int:keyword_id>", methods=["DELETE"])
def api_keywords_delete(keyword_id):
    db.delete_keyword(keyword_id)
    return jsonify({"ok": True})


@app.route("/api/keywords/<int:keyword_id>/toggle", methods=["POST"])
def api_keywords_toggle(keyword_id):
    db.toggle_keyword(keyword_id)
    return jsonify({"ok": True})


@app.route("/api/fast-poll")
def api_fast_poll_get():
    return jsonify({"enabled": db.get_fast_poll_enabled()})


@app.route("/api/fast-poll/toggle", methods=["POST"])
def api_fast_poll_toggle():
    enabled = not db.get_fast_poll_enabled()
    db.set_fast_poll_enabled(enabled)
    return jsonify({"enabled": enabled})


@app.route("/api/sync/log")
def api_sync_log():
    return jsonify(db.get_sync_log())




@app.route("/api/deals/<thread_id>/expire", methods=["POST"])
def api_expire_deal(thread_id):
    db.mark_expired(thread_id)
    return jsonify({"ok": True})


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    tg_ok, tg_msg = notifier.send_test(token, chat_id)

    ntfy_topic = os.environ.get("NTFY_TOPIC", "")
    ntfy_server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    ntfy_ok, ntfy_msg = notifier.send_test_ntfy(ntfy_topic, ntfy_server)

    return jsonify({
        "ok": tg_ok or ntfy_ok,
        "telegram": {"ok": tg_ok, "message": tg_msg},
        "ntfy": {"ok": ntfy_ok, "message": ntfy_msg},
    })


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
    status = db.get_sync_status()
    consecutive_empty = status.get("consecutive_empty") or 0
    return jsonify({
        "status": "ok" if consecutive_empty < 3 else "degraded",
        "consecutive_empty_syncs": consecutive_empty,
        "last_sync": status.get("last_sync"),
    })


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
