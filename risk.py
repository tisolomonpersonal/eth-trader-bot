"""
Hard risk rules — final gate before any trade executes.
The AI recommends; risk.py decides whether execution is allowed.
"""
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("risk")


def check_sl_tp(state: dict, price: float) -> Optional[str]:
    """
    Check if stop-loss or take-profit has been triggered.
    Returns 'SL', 'TP', or None.
    """
    if not state.get("in_position"):
        return None
    sl = float(state.get("sl_price", 0) or 0)
    tp = float(state.get("tp_price", 0) or 0)
    if sl > 0 and price <= sl:
        return "SL"
    if tp > 0 and price >= tp:
        return "TP"
    return None


def validate_action(
    action: str,
    confidence: int,
    state: dict,
    balance: dict,
) -> Tuple[bool, str]:
    """
    Validate a proposed action against all hard risk rules.
    Returns (allowed: bool, reason: str).
    """
    # Minimum AI confidence threshold
    if confidence < config.MIN_AI_CONFIDENCE and action != "HOLD":
        return False, (f"AI confidence {confidence} below minimum {config.MIN_AI_CONFIDENCE}. "
                       "Waiting for higher conviction signal.")

    # Daily loss limit
    daily_pnl  = float(state.get("daily_pnl_usdt", 0))
    daily_limit = config.MAX_INVESTMENT_USDT * config.MAX_DAILY_LOSS_PCT / 100
    if daily_pnl < -daily_limit:
        return False, f"Daily loss limit reached (${daily_pnl:.2f} today, limit ${-daily_limit:.2f})."

    if action == "BUY":
        if state.get("in_position"):
            if config.ALLOW_AVERAGING_DOWN:
                return True, "Averaging down enabled — allowing additional buy."
            return False, "Already holding BNB. Only 1 open position allowed."

        usdt_avail = float(balance.get("usdt", 0))
        needed     = config.MAX_INVESTMENT_USDT * config.RISK_PER_TRADE_PCT / 100
        if usdt_avail < needed * 0.99:  # 1% tolerance for fees
            return False, f"Insufficient USDT: ${usdt_avail:.2f} available, ${needed:.2f} needed."

    if action == "SELL":
        if not state.get("in_position"):
            return False, "No open position to sell."
        qty = float(state.get("qty", 0))
        if qty <= 0:
            return False, "Position qty is 0 — nothing to sell."

    return True, "All risk rules passed."


def calculate_position_size(balance_usdt: float, price: float) -> float:
    """
    Return the USDT amount to deploy in the next trade.
    Respects MAX_INVESTMENT_USDT and RISK_PER_TRADE_PCT.
    """
    max_trade = config.MAX_INVESTMENT_USDT * config.RISK_PER_TRADE_PCT / 100
    available = min(balance_usdt, max_trade)
    return round(available, 2)
