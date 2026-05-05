# ── monitor.py v3 ─────────────────────────────────────────────────
# Logique de surveillance :
#
# Subgraph (toutes les 5 min)
#   → enrichit la watchlist persistée dans JSONBin
#   → survit aux restarts GitHub Actions
#
# Watchlist RPC (toutes les 12s — chaque bloc)
#   → surveille directement chaque adresse connue
#   → délai < 12 secondes, pas de dépendance subgraph
#   → retire les adresses saines (HF > 1.3)
# ─────────────────────────────────────────────────────────────────

import os, time, json, logging, requests
from web3 import Web3
from dotenv import load_dotenv
from calculator import is_profitable
from notifier import send_alert
from logger import (
    load_watchlist, save_watchlist, remove_from_watchlist,
    log_opportunity, log_liquidation, log_scan
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────

RPC_URL          = os.getenv("ALCHEMY_RPC_URL")
DRY_RUN          = os.getenv("DRY_RUN", "True") == "True"
POLL_SEC         = 12    # 1 bloc Arbitrum
SUBGRAPH_REFRESH = 300   # refresh subgraph toutes les 5 minutes
HF_WATCH_MAX     = 1.1   # ajouter à watchlist si HF < 1.1
HF_REMOVE        = 1.3   # retirer de watchlist si HF > 1.3 (position saine)

AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

SUBGRAPH_URLS = [
    "https://gateway.thegraph.com/api/subgraphs/id/GQFbb95cE6d8mV989mL5figjaGaKCQB3xqYrr1bRyXqF",
    "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum",
]

POOL_ABI = json.loads('''[{
  "name": "getUserAccountData",
  "type": "function",
  "stateMutability": "view",
  "inputs":  [{"name": "user", "type": "address"}],
  "outputs": [
    {"name": "totalCollateralBase",         "type": "uint256"},
    {"name": "totalDebtBase",               "type": "uint256"},
    {"name": "availableBorrowsBase",        "type": "uint256"},
    {"name": "currentLiquidationThreshold", "type": "uint256"},
    {"name": "ltv",                         "type": "uint256"},
    {"name": "healthFactor",                "type": "uint256"}
  ]
}]''')

# ── Setup ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/monitor.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("monitor")

w3   = Web3(Web3.HTTPProvider(RPC_URL))
pool = w3.eth.contract(
    address=Web3.to_checksum_address(AAVE_POOL),
    abi=POOL_ABI
)

if not w3.is_connected():
    log.error("❌ Connexion RPC échouée")
    raise SystemExit(1)

log.info(f"✅ Connecté — bloc #{w3.eth.block_number}")

# ── Subgraph ──────────────────────────────────────────────────────

SUBGRAPH_QUERY = """
{
  users(
    first: 200
    where: { healthFactor_lt: "1100000000000000000" }
    orderBy: healthFactor
    orderDirection: asc
  ) { id }
}
"""

def refresh_from_subgraph(watchlist: list) -> list:
    """
    Interroge le subgraph et enrichit la watchlist.
    Appelé toutes les 5 minutes — pas à chaque bloc.
    Retourne la watchlist mise à jour.
    """
    for url in SUBGRAPH_URLS:
        try:
            r = requests.post(
                url, json={"query": SUBGRAPH_QUERY}, timeout=15
            )
            users = r.json().get("data", {}).get("users", [])
            if users:
                new_addrs = [u["id"] for u in users]
                # Merge sans doublons
                existing = set(watchlist)
                added = [a for a in new_addrs if a not in existing]
                watchlist = list(existing | set(new_addrs))
                log.info(
                    f"📡 Subgraph refresh: {len(new_addrs)} trouvées, "
                    f"+{len(added)} nouvelles → watchlist={len(watchlist)}"
                )
                save_watchlist(watchlist)
                return watchlist
        except Exception as e:
            log.warning(f"Subgraph failed ({url[:35]}…): {e}")

    log.warning("⚠️  Subgraph indisponible — watchlist inchangée")
    return watchlist


# ── Lecture RPC directe ───────────────────────────────────────────

def get_account_data(address: str) -> dict | None:
    try:
        d = pool.functions.getUserAccountData(
            Web3.to_checksum_address(address)
        ).call()
        return {
            "address":        address,
            "health_factor":  d[5] / 1e18,
            "debt_usd":       d[1] / 1e8,
            "collateral_usd": d[0] / 1e8,
            "liquidatable":   (d[5] / 1e18) < 1.0
        }
    except Exception as e:
        log.warning(f"RPC error ({address[:8]}…): {e}")
        return None


def scan_watchlist(watchlist: list) -> list:
    """
    Scanne toutes les adresses de la watchlist via RPC direct.
    - Retire les positions saines (HF > 1.3)
    - Traite les positions liquidables
    Retourne la watchlist nettoyée.
    """
    to_remove = []

    for address in watchlist:
        data = get_account_data(address)
        if not data:
            continue

        hf   = data["health_factor"]
        debt = data["debt_usd"]

        # Position redevenue saine → retirer de la watchlist
        if hf > HF_REMOVE:
            to_remove.append(address)
            continue

        # Log si proche du seuil
        if hf < 1.05:
            log.warning(f"🔴 HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…")
        elif hf < HF_WATCH_MAX:
            log.info(f"🟡 HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…")

        # Position liquidable
        if data["liquidatable"]:
            process_liquidatable(address, hf, debt)

        time.sleep(0.1)  # rate limit Alchemy

    # Nettoyage des positions saines
    for addr in to_remove:
        watchlist.remove(addr)
        remove_from_watchlist(addr)
        log.info(f"✅ Retiré watchlist (HF sain): {addr[:10]}…")

    return watchlist


def process_liquidatable(address: str, hf: float, debt: float):
    """Calcule et déclenche si rentable."""
    log.warning(f"🎯 LIQUIDABLE | HF={hf:.4f} | dette={debt:.2f}$")

    profitable, net_profit = is_profitable(
        debt_usd=debt, bonus_pct=5.0, w3=w3
    )

    log_opportunity(
        address=address, hf=hf, debt_usd=debt,
        net_profit=net_profit, dry_run=DRY_RUN
    )

    if profitable:
        send_alert(
            f"⚡ LIQUIDATION DÉTECTÉE\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Health    : {hf:.4f}\n"
            f"Dette     : {debt:.2f} USDC\n"
            f"Profit net: +€{net_profit:.2f}\n"
            f"Mode      : {'🔵 SIMULATION' if DRY_RUN else '🟢 LIVE'}"
        )
        if not DRY_RUN:
            from executor import execute_liquidation
            execute_liquidation(address, debt)
    else:
        log.info(f"⏭️  Skip — net={net_profit:.3f}€ insuffisant")


# ── Boucle principale ─────────────────────────────────────────────

def main():
    log.info(f"🤖 LiqBot v3 — DRY_RUN={DRY_RUN}")

    # Charge la watchlist persistée depuis JSONBin au démarrage
    watchlist = load_watchlist()

    send_alert(
        f"🤖 LiqBot v3 démarré\n"
        f"Mode     : {'SIMULATION' if DRY_RUN else 'LIVE'}\n"
        f"Watchlist: {len(watchlist)} adresses chargées\n"
        f"Réseau   : Arbitrum One"
    )

    last_subgraph_refresh = 0
    consecutive_errors    = 0
    scan_count            = 0

    while True:
        try:
            now_ts = time.time()
            block  = w3.eth.block_number

            # Refresh subgraph toutes les 5 minutes
            if now_ts - last_subgraph_refresh > SUBGRAPH_REFRESH:
                log.info("🔄 Refresh subgraph…")
                watchlist = refresh_from_subgraph(watchlist)
                last_subgraph_refresh = now_ts

            # Scan RPC direct sur toute la watchlist
            log.info(
                f"🔍 Bloc #{block} | watchlist={len(watchlist)} adresses"
            )
            watchlist = scan_watchlist(watchlist)

            # Update stats JSONBin toutes les 10 itérations
            scan_count += 1
            if scan_count % 10 == 0:
                log_scan(block, len(watchlist))

            consecutive_errors = 0
            time.sleep(POLL_SEC)

        except KeyboardInterrupt:
            log.info("⏹️  Arrêt")
            break

        except Exception as e:
            consecutive_errors += 1
            log.error(f"❌ Erreur #{consecutive_errors}: {e}")
            if consecutive_errors >= 3:
                send_alert(f"🚨 3 erreurs consécutives\n{e}\nPause 1h")
                time.sleep(3600)
                consecutive_errors = 0
            else:
                time.sleep(30)


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    main()
