import logging
import os

import requests

logger = logging.getLogger(__name__)


def _credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def _send(text):
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
        _send(text)


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
        _send(text)


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
        _send(text)


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
