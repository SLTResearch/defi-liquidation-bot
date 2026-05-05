# ── monitor.py v4 ─────────────────────────────────────────────────
# Architecture : zéro dépendance externe
#
# Sources de données :
#   1. Events Borrow on-chain (Alchemy RPC) → alimente la watchlist
#   2. Watchlist persistée JSONBin → survit aux restarts
#   3. Scan RPC direct toutes les 12s → détection < 12s
#
# Zéro subgraph, zéro API tierce pour les données de marché
# ─────────────────────────────────────────────────────────────────

import os, time, json, logging
from web3 import Web3
from dotenv import load_dotenv
from calculator import is_profitable
from notifier import send_alert
from logger import (
    load_watchlist, save_watchlist, remove_from_watchlist,
    log_opportunity, log_scan
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────

RPC_URL          = os.getenv("ALCHEMY_RPC_URL")
DRY_RUN          = os.getenv("DRY_RUN", "True") == "True"
POLL_SEC         = 12      # 1 bloc Arbitrum
ONCHAIN_REFRESH  = 300     # refresh events on-chain toutes les 5 min
BLOCKS_LOOKBACK  = 50_000  # ~7 jours sur Arbitrum
HF_WATCH_MAX     = 1.1    # ajouter à watchlist si HF < 1.1
HF_REMOVE        = 1.3    # retirer de watchlist si HF > 1.3

# Contrat Aave v3 Arbitrum One
AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

# Topic keccak256 de l'event Borrow — identifie les emprunteurs actifs
# Borrow(address asset, address user, address onBehalfOf, uint256 amount,
#        uint8 interestRateMode, uint256 borrowRate, uint16 referralCode)
BORROW_TOPIC = "0xb3d084820fb1a9decffb176436bd02b9d7285aea2f6e9fbef932c07c70af5805"

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
    log.error("❌ Connexion RPC échouée — vérifie ALCHEMY_RPC_URL")
    raise SystemExit(1)

log.info(f"✅ Connecté à Arbitrum — bloc #{w3.eth.block_number}")


# ── Source : Events on-chain ──────────────────────────────────────

def fetch_borrowers_onchain(watchlist: list) -> list:
    """
    Récupère les emprunteurs actifs via Dune Analytics.
    Query 7431829 — Aave v3 Arbitrum borrowers 7 derniers jours.
    """
    import requests

    try:
        r = requests.get(
            "https://api.dune.com/api/v1/query/7431829/results",
            headers={"X-Dune-API-Key": os.getenv("DUNE_API_KEY")},
            params={"limit": 500},
            timeout=30
        )

        if r.status_code != 200:
            log.warning(f"Dune API error {r.status_code}: {r.text[:100]}")
            return watchlist

        rows = r.json().get("result", {}).get("rows", [])
        borrowers = [row["user_address"] for row in rows if row.get("user_address")]

        existing  = set(watchlist)
        new_addrs = [b for b in borrowers if b not in existing]
        merged    = list(existing | set(borrowers))

        log.info(
            f"⛓️  Dune: {len(borrowers)} emprunteurs | "
            f"+{len(new_addrs)} nouveaux | "
            f"watchlist={len(merged)}"
        )

        save_watchlist(merged)
        return merged

    except Exception as e:
        log.warning(f"❌ Dune API error: {e}")
        return watchlist


# ── Scan RPC direct ───────────────────────────────────────────────

def get_account_data(address: str) -> dict | None:
    """Interroge getUserAccountData() directement via RPC."""
    try:
        d = pool.functions.getUserAccountData(
            Web3.to_checksum_address(address)
        ).call()
        hf = d[5] / 1e18
        return {
            "address":        address,
            "health_factor":  hf,
            "debt_usd":       d[1] / 1e8,
            "collateral_usd": d[0] / 1e8,
            "liquidatable":   hf < 1.0
        }
    except:
        return None


def scan_watchlist(watchlist: list) -> list:
    """
    Scanne chaque adresse de la watchlist via RPC direct.
    - Retire les positions saines (HF > 1.3)
    - Traite les positions liquidables immédiatement
    """
    to_remove = []

    for address in watchlist:
        data = get_account_data(address)
        if not data:
            continue

        hf   = data["health_factor"]
        debt = data["debt_usd"]

        # Position redevenue saine → retirer
        if hf > HF_REMOVE or debt < 1.0:
            to_remove.append(address)
            continue

        # Log selon le niveau de danger
        if hf < 1.0:
            log.warning(f"🔴 LIQUIDABLE | HF={hf:.4f} | dette={debt:.2f}$")
            process_liquidatable(address, hf, debt)
        elif hf < 1.05:
            log.warning(f"🟠 DANGER     | HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…")
        elif hf < HF_WATCH_MAX:
            log.info(f"🟡 VIGILANCE  | HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…")

        time.sleep(0.05)  # évite le rate limit Alchemy

    # Nettoyage watchlist
    for addr in to_remove:
        watchlist.remove(addr)
        remove_from_watchlist(addr)

    if to_remove:
        log.info(f"🗑️  {len(to_remove)} positions saines retirées")

    return watchlist


# ── Traitement liquidation ────────────────────────────────────────

def process_liquidatable(address: str, hf: float, debt: float):
    """Calcule la rentabilité et exécute si profitable."""
    profitable, net_profit = is_profitable(
        debt_usd=debt,
        bonus_pct=5.0,
        w3=w3
    )

    log_opportunity(
        address=address,
        hf=hf,
        debt_usd=debt,
        net_profit=net_profit,
        dry_run=DRY_RUN
    )

    if profitable:
        send_alert(
            f"⚡ <b>LIQUIDATION DÉTECTÉE</b>\n"
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
    log.info(f"🤖 LiqBot v4 — DRY_RUN={DRY_RUN}")

    # Charge la watchlist persistée depuis JSONBin
    watchlist = load_watchlist()

    send_alert(
        f"🤖 <b>LiqBot v4 démarré</b>\n"
        f"Mode     : {'SIMULATION' if DRY_RUN else 'LIVE'}\n"
        f"Watchlist: {len(watchlist)} adresses\n"
        f"Source   : 100% on-chain Alchemy"
    )

    last_onchain_refresh = 0
    consecutive_errors   = 0
    scan_count           = 0

    while True:
        try:
            now_ts = time.time()
            block  = w3.eth.block_number

            # Refresh on-chain toutes les 5 minutes
            if now_ts - last_onchain_refresh > ONCHAIN_REFRESH:
                watchlist = fetch_borrowers_onchain(watchlist)
                last_onchain_refresh = now_ts

            # Scan RPC direct sur toute la watchlist
            log.info(
                f"🔍 Bloc #{block} | watchlist={len(watchlist)} adresses"
            )
            watchlist = scan_watchlist(watchlist)

            # Update JSONBin toutes les 10 itérations
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
