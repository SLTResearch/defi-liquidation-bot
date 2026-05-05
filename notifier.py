# ── notifier.py ───────────────────────────────────────────────────
# Envoie les alertes Telegram au bot
# ─────────────────────────────────────────────────────────────────

import os, logging, requests
from dotenv import load_dotenv

load_dotenv()  # ← ajouter ici aussi

log = logging.getLogger("notifier")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_alert(message: str) -> bool:
    """Envoie un message Telegram. Retourne True si succès."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré — variables manquantes")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)

        if r.status_code == 200:
            log.debug("Telegram ✅")
            return True
        else:
            log.warning(f"Telegram erreur {r.status_code}: {r.text}")
            return False

    except Exception as e:
        log.warning(f"Telegram inaccessible: {e}")
        return False


def send_daily_report(stats: dict):
    """Envoie le rapport quotidien à 8h."""
    msg = (
        f"📊 <b>RAPPORT 24H</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Liquidations  : {stats.get('count', 0)} exécutées\n"
        f"Profit brut   : +€{stats.get('gross', 0):.2f}\n"
        f"Gas dépensé   : -€{stats.get('gas', 0):.2f}\n"
        f"Profit net    : <b>+€{stats.get('net', 0):.2f}</b>\n"
        f"Wallet gas    : €{stats.get('wallet_eur', 0):.2f} restants"
    )
    return send_alert(msg)