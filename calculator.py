# ── calculator.py ─────────────────────────────────────────────────
# Calcule si une liquidation est rentable après gas
# Règle d'or : profit net > 2× coût du gas → sinon on skip
# ─────────────────────────────────────────────────────────────────

import os, logging

log = logging.getLogger("calculator")

# Paramètres (modifiables sans toucher à la logique)
MIN_PROFIT_EUR    = float(os.getenv("MIN_PROFIT_EUR", "0.50"))
GAS_SAFETY_FACTOR = 2.0   # profit doit couvrir 2× le gas estimé
ETH_EUR_FALLBACK  = 2400.0 # utilisé si l'API price échoue
GAS_LIMIT_LIQ     = 400_000 # gas units estimées pour liquidationCall()


def get_eth_price_eur() -> float:
    """Prix ETH/EUR via CoinGecko (gratuit, sans clé API)."""
    import requests
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "eur"},
            timeout=5
        )
        price = r.json()["ethereum"]["eur"]
        log.debug(f"ETH/EUR = {price}")
        return float(price)
    except Exception as e:
        log.warning(f"CoinGecko inaccessible ({e}) — fallback {ETH_EUR_FALLBACK}€")
        return ETH_EUR_FALLBACK


def estimate_gas_cost_eur(w3) -> float:
    """
    Estime le coût du gas en EUR pour une liquidationCall().
    Utilise le gas price actuel du réseau Arbitrum.
    """
    try:
        # Gas price actuel en wei
        gas_price_wei = w3.eth.gas_price

        # Coût en ETH = gas_limit × gas_price
        gas_cost_eth = (GAS_LIMIT_LIQ * gas_price_wei) / 1e18

        # Conversion en EUR
        eth_eur      = get_eth_price_eur()
        gas_cost_eur = gas_cost_eth * eth_eur

        log.debug(
            f"Gas: {gas_price_wei/1e9:.4f} gwei × {GAS_LIMIT_LIQ:,} "
            f"= {gas_cost_eth:.6f} ETH = {gas_cost_eur:.4f}€"
        )
        return gas_cost_eur

    except Exception as e:
        log.warning(f"Erreur estimation gas ({e}) — fallback 0.20€")
        return 0.20  # valeur conservative si erreur


def is_profitable(debt_usd: float, bonus_pct: float, w3) -> tuple[bool, float]:
    """
    Décide si une liquidation vaut la peine d'être exécutée.

    Args:
        debt_usd  : montant de la dette en USD
        bonus_pct : bonus de liquidation Aave (5% ETH, 10% WBTC…)
        w3        : instance Web3 connectée

    Returns:
        (profitable: bool, net_profit_eur: float)
    """
    # Aave permet de liquider max 50% de la dette en une tx
    liquidatable_usd = debt_usd * 0.5

    # Profit brut en USD → converti en EUR (approximation USD≈EUR ok ici)
    gross_profit_eur = liquidatable_usd * (bonus_pct / 100)

    # Coût réel du gas au moment de l'appel
    gas_cost_eur = estimate_gas_cost_eur(w3)

    # Profit net
    net_profit_eur = gross_profit_eur - gas_cost_eur

    # Critères de rentabilité (les deux doivent être vrais)
    covers_gas     = gross_profit_eur >= (gas_cost_eur * GAS_SAFETY_FACTOR)
    above_min      = net_profit_eur >= MIN_PROFIT_EUR

    profitable = covers_gas and above_min

    log.info(
        f"💰 Calcul | dette={debt_usd:.2f}$ | "
        f"brut={gross_profit_eur:.3f}€ | "
        f"gas={gas_cost_eur:.4f}€ | "
        f"net={net_profit_eur:.3f}€ | "
        f"{'✅ GO' if profitable else '⏭️  SKIP'}"
    )

    return profitable, net_profit_eur