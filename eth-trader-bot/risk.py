"""
Hard risk rules — final gate before any trade executes.
4H Bollinger Band Short Strategy, BTC/USDT linear perpetual, config.LEVERAGE (default 28×).
"""
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("risk")


def check_sl_tp(state: dict, price: float) -> Optional[str]:
    """
    Check if stop-loss or take-profit has been triggered.
    This strategy is SHORT only.
    Returns 'SL', 'TP', or None.

    TP (MA28) is a moving target — the caller updates state['tp_price'] to
    the current MA28 before calling this function each cycle.
    """
    if not state.get("in_position"):
        return None

    side = state.get("side", "SHORT")
    sl   = float(state.get("sl_price", 0) or 0)
    tp   = float(state.get("tp_price", 0) or 0)

    if side == "SHORT":
        if sl > 0 and price >= sl:
            return "SL"
        if tp > 0 and price <= tp:
            return "TP"
    else:  # LONG (kept for robustness / reconciled positions)
        if sl > 0 and price <= sl:
            return "SL"
        if tp > 0 and price >= tp:
            return "TP"

    return None


def validate_action(
    action: str,
    state: dict,
    balance: dict,
) -> Tuple[bool, str]:
    """
    Validate a proposed SHORT entry against all hard risk rules.
    Returns (allowed: bool, reason: str).
    """
    if action == "HOLD":
        return True, "HOLD always allowed."

    # Daily loss limit
    daily_pnl = float(state.get("daily_pnl_usdt", 0))
    if daily_pnl < -config.MAX_DAILY_LOSS_USDT:
        return False, (
            f"Daily loss limit reached "
            f"(${daily_pnl:.2f} today, limit ${-config.MAX_DAILY_LOSS_USDT:.2f})."
        )

    # Daily trade count
    if action == "SHORT":
        if state.get("trade_count_today", 0) >= config.MAX_TRADES_PER_DAY:
            return False, f"Max daily trades reached ({config.MAX_TRADES_PER_DAY})."

    # No double entries
    if action == "SHORT" and state.get("in_position"):
        return False, "Already in a position — only one open trade at a time."

    # Nothing to close
    if action == "CLOSE" and not state.get("in_position"):
        return False, "No open position to close."

    return True, "All risk rules passed."


def calculate_sl(bb_touch_high: float, entry_price: float, atr_value: float) -> float:
    """
    Stop-loss for a SHORT position.

    Candidate 1: high of the BB-touch candle + small buffer (exact candle tip).
    Candidate 2: entry price + 1.5 × ATR (the hard cap).

    Final SL = whichever is LOWER (closer to entry), so a single huge candle
    can never hand us an oversized stop.
    """
    buf = config.SL_BUFFER_PCT / 100
    candidate_candle = round(bb_touch_high * (1 + buf), 2)
    candidate_atr    = round(entry_price + config.ATR_CAP_MULT * atr_value, 2)
    sl = min(candidate_candle, candidate_atr)
    log.debug(
        f"[SL calc] bb_high={bb_touch_high:.2f} candle_sl={candidate_candle:.2f} "
        f"atr_sl={candidate_atr:.2f} (ATR={atr_value:.2f}) → final={sl:.2f}"
    )
    return sl


def estimate_pnl(entry: float, exit_price: float, qty: float, side: str) -> float:
    """Compute realised P&L for a futures position (leverage already included in qty notional)."""
    if side == "LONG":
        return round((exit_price - entry) * qty, 4)
    else:  # SHORT
        return round((entry - exit_price) * qty, 4)
