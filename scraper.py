import json
import math
import re
import time
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}

GRAPHQL_URL = "https://www.mydealz.de/graphql"
GRAPHQL_QUERY = """
query searchThreads($input: ThreadSearchFilter!) {
  searchThreads(input: $input) {
    listHtml
    pagination { count }
  }
}
"""

_session = None
_xsrf_token = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def _reset_session():
    """Drop the session (and its cookies + cached XSRF) so the next call
    starts clean — used after a 403 that looks like the session got blocked."""
    global _session, _xsrf_token
    _session = None
    _xsrf_token = None


def _refresh_xsrf():
    sess = _get_session()
    sess.get(
        "https://www.mydealz.de/gruppe/preisfehler",
        headers={"Accept": "text/html"},
        timeout=15,
    )
    raw = sess.cookies.get("xsrf_t", "")
    return requests.utils.unquote(raw).strip('"')


def _get_xsrf(force_refresh=False):
    global _xsrf_token
    if _xsrf_token is None or force_refresh:
        _xsrf_token = _refresh_xsrf()
    return _xsrf_token


def _parse_deals(html_src):
    marker = '"name":"ThreadMainListItemNormalizer","props":{"thread":'
    deals = []
    pos = 0
    while True:
        idx = html_src.find(marker, pos)
        if idx == -1:
            break
        start = idx + len(marker)
        depth = in_str = esc = 0
        end = start
        for i, c in enumerate(html_src[start:], start):
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        try:
            t = json.loads(html_src[start:end])
            if t:
                deals.append(t)
        except Exception:
            pass
        pos = idx + 1
    return deals


def _fetch(variables, referer, limit, retries=2):
    for attempt in range(retries + 1):
        try:
            xsrf = _get_xsrf(force_refresh=attempt > 0)
            sess = _get_session()
            resp = sess.post(
                GRAPHQL_URL,
                json={"query": GRAPHQL_QUERY, "variables": variables},
                headers={
                    "X-XSRF-TOKEN": xsrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": "https://www.mydealz.de",
                    "Referer": referer,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                },
                timeout=20,
            )
            if resp.status_code == 403 and attempt < retries:
                # Likely a stale/blocked session — start a fresh one and slow down.
                _reset_session()
                time.sleep(5 + attempt * 5)
            resp.raise_for_status()
            data = resp.json()
            html_src = data["data"]["searchThreads"]["listHtml"]
            total = data["data"]["searchThreads"]["pagination"]["count"]
            deals = _parse_deals(html_src)[:limit]
            logger.info("Fetched %d deals (total: %d)", len(deals), total)
            return deals, total
        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt + 1, e)
            if attempt < retries:
                time.sleep(3)
            else:
                raise


def fetch_deals(limit=50, page=1, retries=2):
    return _fetch(
        variables={"input": {"q": "Preisfehler", "sortBy": "new", "type": "Deal", "page": page}},
        referer="https://www.mydealz.de/search?q=Preisfehler&sortby=new",
        limit=limit,
        retries=retries,
    )


def fetch_new_deals(limit=50, retries=2):
    sess = _get_session()
    for attempt in range(retries + 1):
        try:
            resp = sess.get(
                f"https://www.mydealz.de/deals-new?_={int(time.time())}",
                headers={
                    "Accept": "text/html",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                timeout=15,
            )
            resp.raise_for_status()
            deals = _parse_deals(resp.text)[:limit]
            logger.info("Fetched %d new deals from /new", len(deals))
            return deals, len(deals)
        except Exception as e:
            logger.warning("fetch_new_deals attempt %d failed: %s", attempt + 1, e)
            if attempt < retries:
                time.sleep(3)
            else:
                raise


def deal_url(t):
    return f"https://www.mydealz.de/deals/{t['titleSlug']}-{t['threadId']}"


def has_thread_update(url, timeout=10):
    """Checks the thread detail page for mydealz's own "Aktualisiert vor …"
    label. That label comes from a non-empty threadUpdates array embedded in
    the page — the list/search API always returns threadUpdates as [], so
    this can only be answered by fetching the thread page itself."""
    try:
        sess = _get_session()
        resp = sess.get(url, headers={"Accept": "text/html"}, timeout=timeout)
        html_src = resp.text
        marker = '"threadUpdates":['
        idx = html_src.find(marker)
        if idx == -1:
            return False
        start = idx + len(marker) - 1  # include the opening '['
        depth = in_str = esc = 0
        end = start
        for i, c in enumerate(html_src[start:], start):
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
            if not in_str:
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        updates = json.loads(html_src[start:end])
        return any(not u.get("isDeleted") for u in updates)
    except Exception:
        logger.warning("has_thread_update failed for %s", url, exc_info=True)
        return False


def check_deal_status(url, timeout=12):
    """Ask the thread page directly whether a deal is still live.

    mydealz removes a deal that moderation deactivates entirely — the thread
    URL then answers 404/410 ("gone"). A deal that merely expired the normal
    way keeps a reachable 200 page with "isExpired":true embedded in it.
    Deals that fall off the search API are otherwise indistinguishable from
    live ones, so this is the only way to tell a moderated/expired deal from
    one that's just old.

    Returns 'gone', 'expired', 'active', or 'unknown' (network error).
    """
    try:
        sess = _get_session()
        resp = sess.get(
            url,
            headers={"Accept": "text/html"},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code in (404, 410):
            return "gone"
        if resp.status_code >= 400:
            return "unknown"
        # Look for the deal's own isExpired flag. The thread object is the
        # first "isExpired" occurrence in the page; "similar deals" blocks
        # come later, so checking only the first keeps it specific.
        idx = resp.text.find('"isExpired"')
        if idx != -1:
            snippet = resp.text[idx:idx + 40]
            if "true" in snippet:
                return "expired"
        return "active"
    except Exception as e:
        logger.warning("check_deal_status failed for %s: %s", url, e)
        return "unknown"


def discount_pct(price, next_best):
    if next_best and next_best > 0 and price is not None and price < next_best:
        return round((next_best - price) / next_best * 100)
    return None


def fmt_price(price):
    if price is None:
        return "?"
    if price == 0:
        return "~0 €"
    return f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def hot_bar_width(temp, max_temp=1):
    if not temp or temp <= 0:
        return 0
    return min(100, round(math.log10(temp + 1) / math.log10(max(max_temp, temp) + 1) * 100))


_AVAILABILITY_KEYWORDS = [
    "ausverkauft", "nicht verfügbar", "nicht auf lager", "leider ausverkauft",
    "derzeit nicht lieferbar", "nicht mehr verfügbar", "produkt nicht gefunden",
    "out of stock", "sold out", "currently unavailable", "no longer available",
]


def check_availability(shop_url, timeout=10):
    """Best-effort heuristic for whether a shop link still looks orderable.

    Not a reliable stock check — shops render availability wildly
    differently and this only looks at status code + a keyword scan, so
    treat the result as a hint, not a fact. Returns 'available',
    'unavailable', or 'unknown' (network error, timeout, no clear signal).
    """
    if not shop_url:
        return "unknown"
    try:
        resp = requests.get(shop_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code >= 400:
            return "unavailable"
        html_lower = resp.text.lower()
        if any(kw in html_lower for kw in _AVAILABILITY_KEYWORDS):
            return "unavailable"
        return "available"
    except Exception as e:
        logger.warning("Availability check failed for %s: %s", shop_url, e)
        return "unknown"
