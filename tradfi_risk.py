"""
Hard risk rules for TradFi trading — final gate before any TradFi trade executes.
Mirrors risk.py's structure but reads the independent TRADFI_* risk envelope
so a bad day on TradFi can never eat into (or be masked by) the crypto bot's
own daily loss counters, and vice versa.
"""
import math
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("tradfi_risk")


def check_sl_tp(state: dict, price: float) -> Optional[str]:
    """Check if stop-loss or take-profit has been triggered. Returns 'SL', 'TP', or None."""
    if not state.get("in_position"):
        return None
    sl = float(state.get("sl_price", 0) or 0)
    tp = float(state.get("tp_price", 0) or 0)
    side = state.get("side", "Buy")

    if side == "Buy":
        if sl > 0 and price <= sl:
            return "SL"
        if tp > 0 and price >= tp:
            return "TP"
    else:  # short
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
    market_open: bool,
) -> Tuple[bool, str]:
    """Validate a proposed TradFi action against all hard risk rules."""
    if not market_open and action != "HOLD":
        return False, "Market appears closed for this instrument — holding only."

    if confidence < config.TRADFI_MIN_AI_CONFIDENCE and action != "HOLD":
        return False, (f"AI confidence {confidence} below minimum "
                       f"{config.TRADFI_MIN_AI_CONFIDENCE}. Waiting for higher conviction signal.")

    daily_pnl   = float(state.get("daily_pnl_usdt", 0))
    daily_limit = config.TRADFI_MAX_INVESTMENT_USDT * config.TRADFI_MAX_DAILY_LOSS_PCT / 100
    if daily_pnl < -daily_limit:
        return False, f"TradFi daily loss limit reached (${daily_pnl:.2f} today, limit ${-daily_limit:.2f})."

    if action == "BUY":
        if state.get("in_position"):
            if config.TRADFI_ALLOW_AVERAGING_DOWN:
                return True, "Averaging down enabled — allowing additional entry."
            return False, "Already holding a TradFi position. Only 1 open position allowed."

        usdt_avail = float(balance.get("usdt", 0))
        needed     = config.TRADFI_MAX_INVESTMENT_USDT * config.TRADFI_RISK_PER_TRADE_PCT / 100
        if usdt_avail < needed * 0.99:
            return False, f"Insufficient USDT: ${usdt_avail:.2f} available, ${needed:.2f} needed."

    if action == "SELL":
        if not state.get("in_position"):
            return False, "No open TradFi position to close."
        qty = float(state.get("qty", 0))
        if qty <= 0:
            return False, "Position qty is 0 — nothing to close."

    return True, "All TradFi risk rules passed."


def calculate_position_qty(balance_usdt: float, price: float, instrument_info: dict) -> float:
    """
    Convert the USDT amount to deploy into a valid order qty (lots/contracts),
    respecting the instrument's qtyStep and minOrderQty from Bybit's
    instruments-info response.
    """
    max_trade = config.TRADFI_MAX_INVESTMENT_USDT * config.TRADFI_RISK_PER_TRADE_PCT / 100
    usdt_to_use = min(balance_usdt, max_trade)

    lot_filter = instrument_info.get("lotSizeFilter", {})
    qty_step   = float(lot_filter.get("qtyStep", 0.01) or 0.01)
    min_qty    = float(lot_filter.get("minOrderQty", qty_step) or qty_step)

    raw_qty = usdt_to_use / price if price > 0 else 0
    steps = math.floor(raw_qty / qty_step)
    qty   = round(steps * qty_step, 8)

    if qty < min_qty:
        log.warning(f"Calculated qty {qty} below minOrderQty {min_qty} — "
                    f"insufficient balance for a valid TradFi order.")
        return 0.0

    return qty
