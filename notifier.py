import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def _send_telegram(text):
    token, chat_id = _credentials()
    if not token or not chat_id:
        logger.warning("Telegram not configured – skipping notification")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram notification sent")
        return True
    except Exception as e:
        logger.error("Telegram error: %s", e)
        return False


def _send_ntfy(text, url=None):
    """Send to ntfy.sh (or a self-hosted server) as a backup channel.

    Only ASCII goes in headers — ntfy metadata headers go through
    requests/urllib3's latin-1 header encoding, and message text here can
    contain arbitrary unicode (umlauts, emoji), so title stays static and
    the actual content goes in the UTF-8 body instead.
    """
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        headers = {"Title": "Preisfehler Dashboard"}
        if url:
            headers["Click"] = url
        resp = requests.post(
            f"{server}/{topic}",
            data=_HTML_TAG_RE.sub("", text).encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("ntfy notification sent")
        return True
    except Exception as e:
        logger.error("ntfy error: %s", e)
        return False


def _send(text, url=None):
    telegram_ok = _send_telegram(text)
    ntfy_ok = _send_ntfy(text, url=url)
    return telegram_ok or ntfy_ok


def notify_new_deals(deals):
    """Send one Telegram message per new deal."""
    for d in deals:
        pct = d.get("discount_pct")
        price = d.get("price")
        next_best = d.get("next_best")
        merchant = d.get("merchant", "?")
        title = d.get("title", "")
        url = d.get("url", "")
        temp = d.get("temperature", 0)
        expired = d.get("is_expired", False)

        price_str = f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if price else "?"
        orig_str = (
            f" <s>{next_best:,.2f} €</s>".replace(",", "X").replace(".", ",").replace("X", ".")
            if next_best and next_best > 0 else ""
        )
        pct_str = f" → <b>-{pct}%</b>" if pct else ""
        status = "⚠️ Bereits abgelaufen" if expired else "✅ Noch aktiv"

        text = (
            f"🚨 <b>Neuer Preisfehler!</b>\n\n"
            f"📦 {title}\n\n"
            f"💰 <b>{price_str}</b>{orig_str}{pct_str}\n"
            f"🏪 {merchant}\n"
            f"🌡 {temp:.0f}°\n"
            f"{status}\n\n"
            f"🔗 <a href='{url}'>Deal ansehen</a>"
        )
        _send(text, url=url)


def notify_high_discount_deals(deals):
    """Send one Telegram message per new deal with ≥80% discount."""
    for d in deals:
        pct = d.get("discount_pct")
        price = d.get("price")
        next_best = d.get("next_best")
        merchant = d.get("merchant", "?")
        title = d.get("title", "")
        url = d.get("url", "")
        shop_url = d.get("shop_url", url)
        temp = d.get("temperature", 0)

        price_str = f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if price else "?"
        orig_str = (
            f" <s>{next_best:,.2f} €</s>".replace(",", "X").replace(".", ",").replace("X", ".")
            if next_best and next_best > 0 else ""
        )

        text = (
            f"💥 <b>-{pct}% Rabatt!</b>\n\n"
            f"📦 {title}\n\n"
            f"💰 <b>{price_str}</b>{orig_str} → <b>-{pct}%</b>\n"
            f"🏪 {merchant}\n"
            f"🌡 {temp:.0f}°\n\n"
            f"🔗 <a href='{shop_url}'>Zum Shop</a>  |  <a href='{url}'>mydealz</a>"
        )
        _send(text, url=url)


def notify_keyword_matches(deals):
    """Send Telegram for deals matching a user-configured keyword."""
    for d in deals:
        kw = d.get("matched_keyword", "?")
        price = d.get("price")
        next_best = d.get("next_best")
        pct = d.get("discount_pct")
        title = d.get("title", "")
        merchant = d.get("merchant", "?")
        url = d.get("url", "")
        shop_url = d.get("shop_url") or url

        price_str = f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if price else "?"
        orig_str = (
            f" <s>{next_best:,.2f} €</s>".replace(",", "X").replace(".", ",").replace("X", ".")
            if next_best and next_best > 0 else ""
        )
        pct_str = f" → <b>-{pct}%</b>" if pct else ""

        text = (
            f"🔍 <b>Keyword-Treffer: \"{kw}\"</b>\n\n"
            f"📦 {title}\n\n"
            f"💰 <b>{price_str}</b>{orig_str}{pct_str}\n"
            f"🏪 {merchant}\n\n"
            f"🔗 <a href='{shop_url}'>Zum Shop</a>  |  <a href='{url}'>mydealz</a>"
        )
        _send(text, url=url)


def notify_temperature_spike(deals):
    """Send Telegram for deals whose temperature rose disproportionately fast."""
    for d in deals:
        title = d.get("title", "")
        price = d.get("price")
        old_temp = d.get("old_temp", 0)
        new_temp = d.get("new_temp") or d.get("temperature", 0) or 0
        rate = d.get("rate", 0)
        merchant = d.get("merchant", "?")
        url = d.get("url", "")
        shop_url = d.get("shop_url") or url
        delta = new_temp - old_temp

        price_str = f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if price else "?"
        rate_str = f"{rate:.1f}°/min" if rate else ""

        text = (
            f"🔥 <b>Deal heizt ungewöhnlich schnell auf!</b>\n\n"
            f"📦 {title}\n\n"
            f"🌡 <b>{new_temp:.0f}°</b>  (+{delta:.0f}°{f'  ·  {rate_str}' if rate_str else ''})\n"
            f"💰 {price_str}\n"
            f"🏪 {merchant}\n\n"
            f"🔗 <a href='{shop_url}'>Zum Shop</a>  |  <a href='{url}'>mydealz</a>"
        )
        _send(text, url=url)


def notify_scraper_warning(consecutive):
    text = (
        f"⚠️ <b>Scraper-Warnung</b>\n\n"
        f"Die letzten <b>{consecutive} Syncs</b> haben 0 Preisfehler-Deals zurückgegeben.\n"
        f"Möglicherweise hat mydealz die Seitenstruktur geändert und der Parser funktioniert nicht mehr."
    )
    _send(text)


def send_test(token, chat_id):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "✅ <b>Preisfehler Dashboard</b> – Verbindung erfolgreich!",
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)


def send_test_ntfy(topic, server):
    if not topic:
        return False, "Kein NTFY_TOPIC konfiguriert"
    try:
        resp = requests.post(
            f"{server.rstrip('/')}/{topic}",
            data="Preisfehler Dashboard - Verbindung erfolgreich!".encode("utf-8"),
            headers={"Title": "Preisfehler Dashboard"},
            timeout=10,
        )
        resp.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)
