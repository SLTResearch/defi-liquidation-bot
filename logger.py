# ── logger.py v2 ──────────────────────────────────────────────────
# - Watchlist persistée dans JSONBin (survit aux restarts)
# - Stats globales mises à jour en temps réel
# - Historique des 100 derniers événements
# ─────────────────────────────────────────────────────────────────

import os, requests, logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("logger")

JSONBIN_BIN_ID  = os.getenv("JSONBIN_BIN_ID")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY")
JSONBIN_URL     = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
HEADERS = {
    "Content-Type":    "application/json",
    "X-Master-Key":   JSONBIN_API_KEY,
    "X-Bin-Versioning": "false"
}

# Structure par défaut du bin
DEFAULT = {
    "watchlist": [],      # adresses surveillées en permanence
    "events":    [],      # historique des 100 derniers événements
    "stats": {
        "total_profit":        0,
        "total_liquidations":  0,
        "total_gas":           0,
        "positions_watched":   0,
        "last_block":          0,
        "last_scan":           "",
        "last_subgraph_refresh": ""
    }
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_bin() -> dict:
    try:
        r = requests.get(JSONBIN_URL, headers=HEADERS, timeout=10)
        data = r.json().get("record", {})
        # Merge avec DEFAULT pour garantir toutes les clés
        for k, v in DEFAULT.items():
            if k not in data:
                data[k] = v
        return data
    except Exception as e:
        log.warning(f"JSONBin read error: {e}")
        return DEFAULT.copy()


def write_bin(data: dict) -> bool:
    try:
        r = requests.put(JSONBIN_URL, json=data, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"JSONBin write error: {e}")
        return False


# ── Watchlist ─────────────────────────────────────────────────────

def load_watchlist() -> list:
    """Charge la watchlist depuis JSONBin au démarrage du bot."""
    data = read_bin()
    wl = data.get("watchlist", [])
    log.info(f"📋 Watchlist chargée: {len(wl)} adresses")
    return wl


def save_watchlist(watchlist: list):
    """
    Sauvegarde la watchlist dans JSONBin.
    Appelé après chaque refresh subgraph.
    """
    data = read_bin()
    # Merge : garde les anciennes adresses + ajoute les nouvelles
    existing = set(data.get("watchlist", []))
    new_addrs = set(watchlist)
    merged = list(existing | new_addrs)

    data["watchlist"] = merged[:500]  # max 500 adresses
    data["stats"]["positions_watched"]    = len(merged)
    data["stats"]["last_subgraph_refresh"] = now()

    write_bin(data)
    log.info(f"💾 Watchlist sauvegardée: {len(merged)} adresses totales")


def remove_from_watchlist(address: str):
    """
    Retire une adresse quand son HF repasse > 1.3
    (position saine, plus besoin de la surveiller).
    """
    data = read_bin()
    before = len(data.get("watchlist", []))
    data["watchlist"] = [
        a for a in data.get("watchlist", [])
        if a.lower() != address.lower()
    ]
    after = len(data["watchlist"])
    if before != after:
        write_bin(data)
        log.info(f"🗑️  Retiré de watchlist: {address[:10]}…")


# ── Events ────────────────────────────────────────────────────────

def log_opportunity(address: str, hf: float, debt_usd: float,
                     net_profit: float, dry_run: bool):
    """Log une opportunité détectée."""
    data = read_bin()
    data["events"].insert(0, {
        "type":            "opportunity",
        "timestamp":       now(),
        "address":         address[:10] + "…",
        "health_factor":   round(hf, 4),
        "debt_usd":        round(debt_usd, 2),
        "net_profit_eur":  round(net_profit, 3),
        "dry_run":         dry_run,
        "executed":        False
    })
    data["events"] = data["events"][:100]
    data["stats"]["last_scan"] = now()
    write_bin(data)


def log_liquidation(address: str, debt_usd: float,
                     gross: float, gas: float, net: float, tx_hash: str = ""):
    """Log une liquidation exécutée."""
    data = read_bin()
    data["events"].insert(0, {
        "type":       "liquidation",
        "timestamp":  now(),
        "address":    address[:10] + "…",
        "debt_usd":   round(debt_usd, 2),
        "gross_eur":  round(gross, 3),
        "gas_eur":    round(gas, 4),
        "net_eur":    round(net, 3),
        "tx_hash":    tx_hash,
        "executed":   True
    })
    data["events"] = data["events"][:100]
    s = data["stats"]
    s["total_profit"]       = round(s.get("total_profit", 0) + net, 3)
    s["total_liquidations"] = s.get("total_liquidations", 0) + 1
    s["total_gas"]          = round(s.get("total_gas", 0) + gas, 4)
    write_bin(data)


def log_scan(block: int, positions_watched: int):
    """Mise à jour légère du statut — appelé à chaque bloc."""
    data = read_bin()
    data["stats"]["last_block"]       = block
    data["stats"]["last_scan"]        = now()
    data["stats"]["positions_watched"] = positions_watched
    write_bin(data)
