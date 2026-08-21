# ── monitor.py v5 ─────────────────────────────────────────────────
# Architecture simplifiée :
#
#   Démarrage → Dune charge les emprunteurs actifs (1000 adresses)
#   Boucle    → scan RPC direct toutes les 12s sur la watchlist
#   Refresh   → Dune recharge toutes les 5 min (nouvelles positions)
#   JSONBin   → uniquement pour les events et stats du dashboard
#
# Zéro persistence watchlist = zéro complexité = zéro bug
# ─────────────────────────────────────────────────────────────────

import os, time, json, logging, requests
from web3 import Web3
from dotenv import load_dotenv
from calculator import is_profitable
from notifier import send_alert
from logger import log_opportunity, log_scan

load_dotenv()

# ── Config ────────────────────────────────────────────────────────

RPC_URL         = os.getenv("ALCHEMY_RPC_URL")
DRY_RUN         = os.getenv("DRY_RUN", "True") == "True"
DUNE_API_KEY    = os.getenv("DUNE_API_KEY")
DUNE_QUERY_ID   = "7431829"
POLL_SEC        = 12     # 1 bloc Arbitrum
DUNE_REFRESH    = 300    # refresh Dune toutes les 5 min
HF_DANGER       = 1.05   # log orange
HF_WATCH        = 1.1    # log jaune
HF_REMOVE       = 1.5    # retirer de la watchlist
MAX_DEBT        = 5000   # notre niche — petites positions
MIN_DEBT        = 100    # pas de micro-positions

AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

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

log.info(f"✅ Connecté à Arbitrum — bloc #{w3.eth.block_number}")


# ── Dune : charge les emprunteurs actifs ──────────────────────────

def load_from_dune() -> list:
    """
    Execute la query Dune puis récupère les résultats.
    Zéro dépendance sur le cache — toujours des données fraîches.
    """
    try:
        headers = {"X-Dune-API-Key": DUNE_API_KEY}

        # 1. Lance l'exécution
        exec_r = requests.post(
            f"https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/execute",
            headers=headers,
            timeout=30
        )
        execution_id = exec_r.json().get("execution_id")
        if not execution_id:
            log.warning(f"Dune execute failed: {exec_r.text[:100]}")
            return []

        log.info(f"⏳ Dune query en cours ({execution_id[:8]}…)")

        # 2. Poll jusqu'à completion (max 60s)
        for attempt in range(12):
            time.sleep(5)
            status_r = requests.get(
                f"https://api.dune.com/api/v1/execution/{execution_id}/status",
                headers=headers,
                timeout=15
            )
            state = status_r.json().get("state", "")
            log.info(f"⏳ Dune status: {state} ({attempt+1}/12)")

            if state == "QUERY_STATE_COMPLETED":
                break
            if state in ["QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"]:
                log.warning(f"Dune query {state}")
                return []

        # 3. Récupère les résultats
        results_r = requests.get(
            f"https://api.dune.com/api/v1/execution/{execution_id}/results",
            headers=headers,
            params={"limit": 1000},
            timeout=30
        )
        rows = results_r.json().get("result", {}).get("rows", [])
        addrs = [row["user_address"] for row in rows if row.get("user_address")]
        log.info(f"📡 Dune: {len(addrs)} emprunteurs chargés")
        return addrs

    except Exception as e:
        log.warning(f"❌ Dune error: {e}")
        return []


# ── RPC : lecture directe du contrat ─────────────────────────────

def get_account_data(address: str) -> dict | None:
    """getUserAccountData() directement via Alchemy RPC."""
    try:
        d = pool.functions.getUserAccountData(
            Web3.to_checksum_address(address)
        ).call()
        hf   = d[5] / 1e18
        debt = d[1] / 1e8
        return {
            "address":        address,
            "health_factor":  hf,
            "debt_usd":       debt,
            "collateral_usd": d[0] / 1e8,
            "liquidatable":   hf < 1.0 and MIN_DEBT < debt < MAX_DEBT
        }
    except:
        return None


# ── Scan de la watchlist ──────────────────────────────────────────

def scan_watchlist(watchlist: list) -> list:
    """
    Scanne toutes les adresses via RPC direct.
    Retire les positions saines ou hors niche.
    Traite immédiatement les positions liquidables.
    """
    to_remove  = []
    vigilance  = 0
    danger     = 0
    liquidable = 0

    for address in watchlist:
        data = get_account_data(address)
        if not data:
            continue

        hf   = data["health_factor"]
        debt = data["debt_usd"]

        # Hors niche ou position saine → retirer
        if hf > HF_REMOVE or debt < MIN_DEBT or debt > MAX_DEBT:
            to_remove.append(address)
            continue

        # Classement par niveau de danger
        if data["liquidatable"]:
            liquidable += 1
            process_liquidatable(address, hf, debt)
        elif hf < HF_DANGER:
            danger += 1
            log.warning(
                f"🟠 DANGER    | HF={hf:.4f} | "
                f"dette={debt:.0f}$ | {address[:10]}…"
            )
        elif hf < HF_WATCH:
            vigilance += 1
            log.info(
                f"🟡 VIGILANCE | HF={hf:.4f} | "
                f"dette={debt:.0f}$ | {address[:10]}…"
            )

        time.sleep(0.05)

    # Nettoyage
    for addr in to_remove:
        watchlist.remove(addr)

    if vigilance or danger or liquidable:
        log.info(
            f"📊 Scan | 🔴 {liquidable} liquidable(s) | "
            f"🟠 {danger} danger | 🟡 {vigilance} vigilance | "
            f"🗑️  {len(to_remove)} retirés"
        )

    return watchlist


# ── Traitement liquidation ────────────────────────────────────────

def process_liquidatable(address: str, hf: float, debt: float):
    """Calcule la rentabilité et alerte si profitable."""
    log.warning(f"🔴 LIQUIDABLE | HF={hf:.4f} | dette={debt:.0f}$")

    profitable, net_profit = is_profitable(
        debt_usd=debt, bonus_pct=5.0, w3=w3
    )

    log_opportunity(
        address=address, hf=hf, debt_usd=debt,
        net_profit=net_profit, dry_run=DRY_RUN
    )

    if profitable:
        send_alert(
            f"⚡ <b>LIQUIDATION DÉTECTÉE</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Health    : {hf:.4f}\n"
            f"Dette     : {debt:.0f} USDC\n"
            f"Profit net: +€{net_profit:.2f}\n"
            f"Mode      : {'🔵 SIMULATION' if DRY_RUN else '🟢 LIVE'}"
        )
        if not DRY_RUN:
            from executor import execute_liquidation
            execute_liquidation(address, debt)
    else:
        log.info(f"⏭️  Skip — net={net_profit:.3f}€")


# ── Boucle principale ─────────────────────────────────────────────

def main():
    log.info(f"🤖 LiqBot v5 — DRY_RUN={DRY_RUN}")

    # Charge immédiatement depuis Dune
    watchlist = load_from_dune()

    send_alert(
        f"🤖 <b>LiqBot v5</b>\n"
        f"Mode     : {'SIMULATION' if DRY_RUN else 'LIVE'}\n"
        f"Watchlist: {len(watchlist)} adresses\n"
        f"Niche    : dette 100–5000$"
    )

    last_dune_refresh = time.time()
    consecutive_errors = 0
    scan_count = 0

    while True:
        try:
            now_ts = time.time()
            block  = w3.eth.block_number

            # Refresh Dune toutes les 5 minutes
            if now_ts - last_dune_refresh > DUNE_REFRESH:
                new_addrs  = load_from_dune()
                # Merge : garde les adresses déjà en watchlist + nouvelles
                existing   = set(watchlist)
                watchlist  = list(existing | set(new_addrs))
                last_dune_refresh = now_ts
                log.info(f"🔄 Watchlist refreshée: {len(watchlist)} adresses")

            log.info(f"🔍 Bloc #{block} | {len(watchlist)} adresses")
            watchlist = scan_watchlist(watchlist)

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
                send_alert(f"🚨 3 erreurs\n{e}\nPause 1h")
                time.sleep(3600)
                consecutive_errors = 0
            else:
                time.sleep(30)


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    main()
