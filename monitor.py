# ── monitor.py ────────────────────────────────────────────────────
# Surveille les health factors Aave v3 sur Arbitrum en temps réel
# DRY_RUN=True → aucune transaction soumise (phase 1)
# ─────────────────────────────────────────────────────────────────

import os, time, json, logging
from web3 import Web3
from dotenv import load_dotenv
from calculator import is_profitable
from notifier import send_alert

load_dotenv()
# ── Config ────────────────────────────────────────────────────────

RPC_URL  = os.getenv("ALCHEMY_RPC_URL")
DRY_RUN  = os.getenv("DRY_RUN", "True") == "True"
POLL_SEC = 12   # ~1 bloc Arbitrum

# Contrats Aave v3 Arbitrum One (adresses officielles)
AAVE_POOL     = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
AAVE_PROVIDER = "0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654"

# ABI minimal — uniquement les fonctions dont on a besoin
POOL_ABI = json.loads('''[
  {
    "name": "getUserAccountData",
    "type": "function",
    "stateMutability": "view",
    "inputs":  [{"name": "user", "type": "address"}],
    "outputs": [
      {"name": "totalCollateralBase",      "type": "uint256"},
      {"name": "totalDebtBase",            "type": "uint256"},
      {"name": "availableBorrowsBase",     "type": "uint256"},
      {"name": "currentLiquidationThreshold", "type": "uint256"},
      {"name": "ltv",                      "type": "uint256"},
      {"name": "healthFactor",             "type": "uint256"}
    ]
  }
]''')

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

w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    log.error("❌ Connexion RPC échouée — vérifie ALCHEMY_RPC_URL")
    raise SystemExit(1)

log.info(f"✅ Connecté à Arbitrum — bloc #{w3.eth.block_number}")

pool = w3.eth.contract(
    address=Web3.to_checksum_address(AAVE_POOL),
    abi=POOL_ABI
)

# ── Fonctions ─────────────────────────────────────────────────────

def get_health_factor(address: str) -> dict:
    """Récupère les données de compte Aave pour une adresse."""
    try:
        data = pool.functions.getUserAccountData(
            Web3.to_checksum_address(address)
        ).call()

        # healthFactor est en 1e18 — on le ramène à un float lisible
        hf = data[5] / 1e18

        # totalDebtBase est en USD avec 8 décimales (format Aave)
        debt_usd = data[1] / 1e8

        return {
            "address":    address,
            "health_factor": hf,
            "debt_usd":   debt_usd,
            "collateral_usd": data[0] / 1e8,
            "liquidatable": hf < 1.0
        }
    except Exception as e:
        log.warning(f"Erreur getUserAccountData({address}): {e}")
        return None


def fetch_at_risk_positions() -> list:
    """
    Récupère les positions à risque via le subgraph Aave.
    Retourne les adresses avec HF entre 0.9 et 1.1 (zone de danger).
    """
    import requests

    query = """
    {
      users(
        first: 100
        where: { healthFactor_lt: "1100000000000000000" }
        orderBy: healthFactor
        orderDirection: asc
      ) {
        id
        healthFactor
        totalDebtETH
      }
    }
    """

    url = "https://gateway.thegraph.com/api/subgraphs/id/GQFbb95cE6d8mV989mL5figjaGaKCQB3xqYrr1bRyXqF"
    try:
        r = requests.post(url, json={"query": query}, timeout=10)
        users = r.json().get("data", {}).get("users", [])
        log.info(f"📡 Subgraph: {len(users)} positions à risque trouvées")
        return [u["id"] for u in users]
    except Exception as e:
        log.warning(f"Subgraph inaccessible: {e} — fallback liste vide")
        return []


def process_position(address: str):
    """Analyse une position et décide si on liquide."""
    data = get_health_factor(address)
    if not data:
        return

    hf   = data["health_factor"]
    debt = data["debt_usd"]

    # Log toutes les positions sous 1.1 (zone de vigilance)
    if hf < 1.1:
        log.info(
            f"⚠️  HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…"
        )

    # Position liquidable
    if data["liquidatable"]:
        log.warning(
            f"🎯 LIQUIDABLE | HF={hf:.4f} | dette={debt:.2f}$ | {address[:10]}…"
        )

        profitable, net_profit = is_profitable(
            debt_usd=debt,
            bonus_pct=5.0,   # bonus Aave v3 ETH = 5%
            w3=w3
        )

        if profitable:
            msg = (
                f"⚡ LIQUIDATION DÉTECTÉE\n"
                f"Protocole : Aave v3 Arbitrum\n"
                f"Health    : {hf:.4f}\n"
                f"Dette     : {debt:.2f} USDC\n"
                f"Profit net: +€{net_profit:.2f}\n"
                f"Mode      : {'🔵 SIMULATION' if DRY_RUN else '🟢 LIVE'}"
            )
            send_alert(msg)
            log.info(f"✅ Profitable — net={net_profit:.2f}€")

            if not DRY_RUN:
                from executor import execute_liquidation
                execute_liquidation(address, debt)
        else:
            log.info(f"⏭️  Skip — pas rentable après gas")


# ── Boucle principale ─────────────────────────────────────────────

def main():
    log.info(f"🤖 LiqBot démarré — DRY_RUN={DRY_RUN}")
    send_alert(f"🤖 LiqBot démarré\nMode: {'SIMULATION' if DRY_RUN else 'LIVE'}\nRéseau: Arbitrum One")

    consecutive_errors = 0

    while True:
        try:
            block = w3.eth.block_number
            log.info(f"🔍 Bloc #{block} — scan en cours…")

            positions = fetch_at_risk_positions()

            for address in positions:
                process_position(address)
                time.sleep(0.2)  # éviter le rate limit Alchemy

            consecutive_errors = 0
            time.sleep(POLL_SEC)

        except KeyboardInterrupt:
            log.info("⏹️  Arrêt manuel")
            break

        except Exception as e:
            consecutive_errors += 1
            log.error(f"❌ Erreur #{consecutive_errors}: {e}")

            if consecutive_errors >= 3:
                send_alert(f"🚨 LiqBot — 3 erreurs consécutives\n{e}\nPause 1h")
                time.sleep(3600)
                consecutive_errors = 0
            else:
                time.sleep(30)


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    main()