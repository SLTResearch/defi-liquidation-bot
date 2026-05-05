# ── logger.py v3 ──────────────────────────────────────────────────
# Fix : headers construits dynamiquement à chaque appel API
#       timeout 30s, clé JSONBin toujours fraîche depuis os.getenv
# ─────────────────────────────────────────────────────────────────

import os, requests, logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("logger")

# ── Constantes ────────────────────────────────────────────────────

def _url() -> str:
    bid = os.getenv("JSONBIN_BIN_ID", "")
    return f"https://api.jsonbin.io/v3/b/{bid}"

def _headers() -> dict:
    """Construit les headers à chaque appel — clé toujours fraîche."""
    return {
        "Content-Type":     "application/json",
        "X-Master-Key":    os.getenv("JSONBIN_API_KEY", ""),
    }

DEFAULT = {
    "watchlist": [],
    "events":    [],
    "stats": {
        "total_profit":          0,
        "total_liquidations":    0,
        "total_gas":             0,
        "positions_watched":     0,
        "last_block":            0,
        "last_scan":             "",
        "last_subgraph_refresh": ""
    }
}

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Lecture / Écriture ────────────────────────────────────────────

def read_bin() -> dict:
    """Lit le bin JSONBin. Retourne DEFAULT si erreur."""
    try:
        r = requests.get(
            _url(),
            headers=_headers(),
            timeout=30
        )
        if r.status_code != 200:
            log.warning(f"JSONBin read HTTP {r.status_code}: {r.text[:80]}")
            return DEFAULT.copy()

        data = r.json().get("record", {})
        # Garantit que toutes les clés existent
        for k, v in DEFAULT.items():
            if k not in data:
                data[k] = v
        return data

    except Exception as e:
        log.warning(f"JSONBin read error: {e}")
        return DEFAULT.copy()


def write_bin(data: dict) -> bool:
    """Écrit dans le bin JSONBin. Retourne True si succès."""
    try:
        r = requests.put(
            _url(),
            json=data,
            headers=_headers(),
            timeout=30
        )
        if r.status_code != 200:
            log.warning(f"JSONBin write HTTP {r.status_code}: {r.text[:80]}")
            return False
        return True

    except Exception as e:
        log.warning(f"JSONBin write error: {e}")
        return False


# ── Watchlist ─────────────────────────────────────────────────────

def load_watchlist() -> list:
    """Charge la watchlist depuis JSONBin au démarrage."""
    data = read_bin()
    wl = data.get("watchlist", [])
    log.info(f"📋 Watchlist chargée: {len(wl)} adresses")
    return wl


def save_watchlist(watchlist: list):
    """Merge et sauvegarde la watchlist dans JSONBin."""
    data = read_bin()
    existing  = set(data.get("watchlist", []))
    merged    = list(existing | set(watchlist))
    data["watchlist"] = merged[:500]
    data["stats"]["positions_watched"]     = len(merged)
    data["stats"]["last_subgraph_refresh"]  = now()
    write_bin(data)
    log.info(f"💾 Watchlist sauvegardée: {len(merged)} adresses")


def remove_from_watchlist(address: str):
    """Retire une adresse saine (HF > 1.3) de la watchlist."""
    data   = read_bin()
    before = len(data.get("watchlist", []))
    data["watchlist"] = [
        a for a in data.get("watchlist", [])
        if a.lower() != address.lower()
    ]
    after = len(data["watchlist"])
    if before != after:
        write_bin(data)
        log.info(f"🗑️  Retiré watchlist: {address[:10]}…")


# ── Events ────────────────────────────────────────────────────────

def log_opportunity(address: str, hf: float, debt_usd: float,
                     net_profit: float, dry_run: bool):
    """Log une opportunité détectée (simulation ou live)."""
    data = read_bin()
    data["events"].insert(0, {
        "type":           "opportunity",
        "timestamp":      now(),
        "address":        address[:10] + "…",
        "health_factor":  round(hf, 4),
        "debt_usd":       round(debt_usd, 2),
        "net_profit_eur": round(net_profit, 3),
        "dry_run":        dry_run,
        "executed":       False
    })
    data["events"] = data["events"][:100]
    data["stats"]["last_scan"] = now()
    write_bin(data)
    log.info(f"📝 Opportunité loggée — net={net_profit:.2f}€")


def log_liquidation(address: str, debt_usd: float,
                     gross: float, gas: float,
                     net: float, tx_hash: str = ""):
    """Log une liquidation exécutée (phase live)."""
    data = read_bin()
    data["events"].insert(0, {
        "type":      "liquidation",
        "timestamp": now(),
        "address":   address[:10] + "…",
        "debt_usd":  round(debt_usd, 2),
        "gross_eur": round(gross, 3),
        "gas_eur":   round(gas, 4),
        "net_eur":   round(net, 3),
        "tx_hash":   tx_hash,
        "executed":  True
    })
    data["events"] = data["events"][:100]
    s = data["stats"]
    s["total_profit"]       = round(s.get("total_profit", 0) + net, 3)
    s["total_liquidations"] = s.get("total_liquidations", 0) + 1
    s["total_gas"]          = round(s.get("total_gas", 0) + gas, 4)
    write_bin(data)
    log.info(f"📝 Liquidation loggée — net={net:.2f}€ | tx={tx_hash[:10]}")


def log_scan(block: int, positions_watched: int):
    """Mise à jour légère du statut — appelé toutes les 10 itérations."""
    data = read_bin()
    data["stats"]["last_block"]        = block
    data["stats"]["last_scan"]         = now()
    data["stats"]["positions_watched"]  = positions_watched
    write_bin(data)
