"""
Hard risk rules — final gate before any trade executes.
Updated for BTC linear perpetuals with 25× leverage.
"""
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("risk")


def check_sl_tp(state: dict, price: float) -> Optional[str]:
    """
    Check if stop-loss or take-profit has been triggered.
    Works for both LONG and SHORT positions.
    Returns 'SL', 'TP', or None.
    """
    if not state.get("in_position"):
        return None

    side = state.get("side", "LONG")
    sl   = float(state.get("sl_price", 0) or 0)
    tp   = float(state.get("tp_price", 0) or 0)

    if side == "LONG":
        if sl > 0 and price <= sl:
            return "SL"
        if tp > 0 and price >= tp:
            return "TP"
    else:  # SHORT
        if sl > 0 and price >= sl:
            return "SL"
        if tp > 0 and price <= tp:
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
    action: 'LONG' | 'SHORT' | 'CLOSE' | 'HOLD'
    """
    if action == "HOLD":
        return True, "HOLD always allowed."

    # Minimum AI confidence
    if action in ("LONG", "SHORT") and confidence < config.MIN_AI_CONFIDENCE:
        return False, (
            f"AI confidence {confidence} below minimum {config.MIN_AI_CONFIDENCE}. "
            "Waiting for higher conviction signal."
        )

    # Daily loss limit
    daily_pnl  = float(state.get("daily_pnl_usdt", 0))
    if daily_pnl < -config.MAX_DAILY_LOSS_USDT:
        return False, (
            f"Daily loss limit reached "
            f"(${daily_pnl:.2f} today, limit ${-config.MAX_DAILY_LOSS_USDT:.2f})."
        )

    # Daily trade count
    if action in ("LONG", "SHORT"):
        if state.get("trade_count_today", 0) >= config.MAX_TRADES_PER_DAY:
            return False, (
                f"Max daily trades reached ({config.MAX_TRADES_PER_DAY})."
            )

    # No double entries
    if action in ("LONG", "SHORT") and state.get("in_position"):
        return False, "Already in a position — only one open trade at a time."

    # Nothing to close
    if action == "CLOSE" and not state.get("in_position"):
        return False, "No open position to close."

    return True, "All risk rules passed."


def calculate_sl(h1_high: float, h1_low: float, direction: str) -> float:
    """
    Place SL strictly beyond the H1 candle extreme + SL_BUFFER_PCT.
    LONG  → SL below h1_low
    SHORT → SL above h1_high
    """
    buf = config.SL_BUFFER_PCT / 100

    if direction == "LONG":
        sl = round(h1_low  * (1 - buf), 2)
    else:
        sl = round(h1_high * (1 + buf), 2)

    return sl


def estimate_pnl(entry: float, exit_price: float, qty: float, side: str) -> float:
    """Compute realised P&L for a futures position (leverage already included in qty notional)."""
    if side == "LONG":
        return round((exit_price - entry) * qty, 4)
    else:  # SHORT
        return round((entry - exit_price) * qty, 4)
