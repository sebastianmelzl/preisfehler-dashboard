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
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
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


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def _refresh_xsrf():
    sess = _get_session()
    sess.get(
        "https://www.mydealz.de/gruppe/preisfehler",
        headers={"Accept": "text/html"},
        timeout=15,
    )
    raw = sess.cookies.get("xsrf_t", "")
    return requests.utils.unquote(raw).strip('"')


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
            xsrf = _refresh_xsrf()
            sess = _get_session()
            resp = sess.post(
                GRAPHQL_URL,
                json={"query": GRAPHQL_QUERY, "variables": variables},
                headers={
                    "X-XSRF-TOKEN": xsrf,
                    "Origin": "https://www.mydealz.de",
                    "Referer": referer,
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
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


def fetch_deals(limit=10, retries=2):
    return _fetch(
        variables={"input": {"q": "Preisfehler", "sortBy": "new", "type": "Deal", "page": 1}},
        referer="https://www.mydealz.de/search?q=Preisfehler&sortby=new",
        limit=limit,
        retries=retries,
    )


def fetch_new_deals(limit=20, retries=2):
    sess = _get_session()
    for attempt in range(retries + 1):
        try:
            resp = sess.get(
                "https://www.mydealz.de/new",
                headers={"Accept": "text/html"},
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
