"""
Scalping risk layer for BTC perpetuals — the final gate before execution.

The entry rules in scalp_signal.py are the *smaller* half of a scalping system.
What determines whether a scalper survives is here: fee-aware minimum edge,
volatility-scaled stops, and hard limits on overtrading and revenge trading.
A mediocre entry inside this envelope beats a great entry without it.

Everything is direction-aware. On a perp a short's stop sits ABOVE entry and
its target BELOW, and getting that backwards produces a stop that can never
trigger — an unprotected leveraged position. Every function here takes an
explicit side.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("scalp_risk")


def _is_long(side: str) -> bool:
    return side == "Buy"


# ── Brackets ──────────────────────────────────────────────────────────────────

def compute_brackets(entry: float, atr: float, side: str,
                     target: Optional[float] = None) -> dict:
    """
    Volatility-scaled stop-loss and take-profit for either direction.

    A fixed 2%/4% bracket is wrong for scalping in both directions: too wide to
    scalp, and unrelated to what the market is doing right now. ATR ties the
    bracket to live volatility.

    The take-profit is then floored so it clears round-trip fees by
    MIN_EDGE_FEE_MULT — the check that separates a scalping strategy from an
    elaborate way to pay Bybit.
    """
    long = _is_long(side)

    # ATR can collapse toward zero in dead markets; the percentage floor catches it.
    sl_pct = (atr * config.SL_ATR_MULT / entry * 100) if entry else config.MIN_SL_PCT
    sl_pct = max(config.MIN_SL_PCT, min(sl_pct, config.MAX_SL_PCT))

    tp_pct = (atr * config.TP_ATR_MULT / entry * 100) if entry else 0.0

    min_tp_pct = config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT
    fee_floored = tp_pct < min_tp_pct
    tp_pct = max(tp_pct, min_tp_pct)

    # Respect the setup's own objective, but never below the fee floor and
    # never beyond what ATR says is reachable.
    if target:
        target_pct = ((target - entry) / entry * 100) if long else ((entry - target) / entry * 100)
        if target_pct > 0:
            tp_pct = max(min_tp_pct, min(tp_pct, target_pct))

    if long:
        sl_price = entry * (1 - sl_pct / 100)
        tp_price = entry * (1 + tp_pct / 100)
    else:
        # Mirrored: a short is stopped out ABOVE entry and profits BELOW it.
        sl_price = entry * (1 + sl_pct / 100)
        tp_price = entry * (1 - tp_pct / 100)

    return {
        "sl_price": round(sl_price, 2),
        "tp_price": round(tp_price, 2),
        "sl_pct": round(sl_pct, 4),
        "tp_pct": round(tp_pct, 4),
        "rr": round(tp_pct / sl_pct, 2) if sl_pct else 0.0,
        "fee_floored": fee_floored,
    }


def net_expectancy_pct(tp_pct: float, sl_pct: float, win_rate: float) -> float:
    """Expected value per trade, net of fees, as a percent of position notional."""
    fee = config.ROUND_TRIP_FEE_PCT
    return win_rate * (tp_pct - fee) - (1 - win_rate) * (sl_pct + fee)


# ── Position sizing ───────────────────────────────────────────────────────────

def position_qty(balance_usdt: float, entry: float, sl_price: float) -> float:
    """
    Contracts to trade, sized so the distance to the stop equals a fixed
    fraction of the pot. Every trade then risks the same amount regardless of
    how wide current volatility makes the stop.

    Note this deliberately sizes off RISK, not off available margin. With
    leverage it is trivially easy to open a position far larger than you can
    afford to be wrong about — margin tells you what you *can* open, which is
    the wrong question.
    """
    risk_usdt = config.MAX_INVESTMENT_USDT * config.SCALP_RISK_PCT / 100
    stop_dist = abs(entry - sl_price)
    if stop_dist <= 0 or entry <= 0:
        return 0.0

    qty = risk_usdt / stop_dist

    # Never let notional exceed the configured pot times leverage, nor the
    # margin actually available.
    max_notional = min(config.MAX_INVESTMENT_USDT * config.LEVERAGE,
                       balance_usdt * config.LEVERAGE)
    if qty * entry > max_notional:
        qty = max_notional / entry

    return qty


def margin_required(qty: float, entry: float) -> float:
    return qty * entry / max(config.LEVERAGE, 1)


# ── Trade gating ──────────────────────────────────────────────────────────────

def validate(signal, state: dict, balance: dict, ind: dict) -> Tuple[bool, str]:
    """Returns (allowed, reason). Every rule here can only ever block a trade."""

    if signal.action == "HOLD":
        return True, "No action."

    if signal.action == "CLOSE":
        if not state.get("in_position"):
            return False, "No open position to close."
        return True, "Exit allowed."

    if signal.confidence < config.MIN_AI_CONFIDENCE:
        return False, (f"Confidence {signal.confidence} below minimum "
                       f"{config.MIN_AI_CONFIDENCE}.")

    # --- Daily loss circuit breaker -------------------------------------------
    daily_pnl = float(state.get("daily_pnl_usdt", 0))
    daily_limit = config.MAX_INVESTMENT_USDT * config.MAX_DAILY_LOSS_PCT / 100
    if daily_pnl < -daily_limit:
        return False, (f"Daily loss limit hit (${daily_pnl:.2f}, "
                       f"limit -${daily_limit:.2f}). Stopped until UTC midnight.")

    # --- Entry gates ----------------------------------------------------------
    if state.get("in_position"):
        return False, "Already in a position — one scalp at a time."

    if int(state.get("trade_count_today", 0)) >= config.MAX_TRADES_PER_DAY:
        return False, (f"Daily trade cap reached ({config.MAX_TRADES_PER_DAY}). "
                       f"Overtrading guard.")

    # If the market has stopped matching the model, stop feeding it. This
    # catches regime changes the indicators missed.
    losses = int(state.get("consecutive_losses", 0))
    if losses >= config.MAX_CONSECUTIVE_LOSSES:
        return False, (f"{losses} consecutive losses — halted. Conditions have "
                       f"changed; review before resetting.")

    # Anti-revenge-trade cooldown.
    last_loss = state.get("last_loss_time")
    if last_loss:
        try:
            elapsed = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last_loss)).total_seconds() / 60
            if elapsed < config.COOLDOWN_AFTER_LOSS_MIN:
                return False, (f"Cooldown: {config.COOLDOWN_AFTER_LOSS_MIN - elapsed:.1f} "
                               f"min left after last loss.")
        except (ValueError, TypeError):
            pass

    # --- Fee viability --------------------------------------------------------
    # The setup's own objective must clear fees. This matters most for
    # MEAN_REVERSION, which exits at the mean via the signal rather than at the
    # TP bracket — so without this check the bot would fade a mean that is
    # nearer than the round-trip cost and book a guaranteed net loss.
    price = ind["price"]
    min_edge_pct = config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT
    if signal.target:
        if signal.action == "LONG":
            target_pct = (signal.target - price) / price * 100
        else:
            target_pct = (price - signal.target) / price * 100
        if target_pct < min_edge_pct:
            return False, (f"{signal.setup} target is only {target_pct:.3f}% away; "
                           f"needs {min_edge_pct:.3f}% to clear "
                           f"{config.ROUND_TRIP_FEE_PCT:.3f}% round-trip fees.")

    side = signal.side
    brackets = compute_brackets(price, ind["atr"], side, signal.target)

    if brackets["rr"] < 1.0 and signal.setup != "MEAN_REVERSION":
        # Mean reversion legitimately runs R:R below 1 on a high hit rate;
        # breakouts and pullbacks do not and should be refused.
        return False, (f"Risk/reward {brackets['rr']} below 1.0 for "
                       f"{signal.setup} after the fee floor.")

    if ind["atr_pct"] < config.ROUND_TRIP_FEE_PCT:
        return False, (f"ATR {ind['atr_pct']:.3f}% is below round-trip fees "
                       f"{config.ROUND_TRIP_FEE_PCT:.3f}% — too quiet to scalp.")

    # --- Capital and instrument minimums --------------------------------------
    usdt = float(balance.get("usdt", 0))
    qty = position_qty(usdt, price, brackets["sl_price"])

    try:
        import perp_client
        min_q = perp_client.min_qty()
        qty_rounded = perp_client.round_qty(qty)
    except Exception:
        min_q, qty_rounded = 0.001, qty

    if qty_rounded < min_q:
        needed_margin = min_q * price / max(config.LEVERAGE, 1)
        return False, (
            f"Risk-based size {qty:.6f} rounds below the {min_q} "
            f"{config.BASE_COIN} minimum. The smallest allowed position is "
            f"${min_q * price:,.2f} notional / ${needed_margin:,.2f} margin at "
            f"{config.LEVERAGE:g}x — larger than {config.SCALP_RISK_PCT}% of a "
            f"${config.MAX_INVESTMENT_USDT:,.2f} pot allows. Increase the pot, "
            f"or accept a larger risk per trade."
        )

    margin = margin_required(qty_rounded, price)
    if usdt < margin * 1.01:   # 1% headroom for fees
        return False, (f"Insufficient margin: ${usdt:.2f} available, "
                       f"${margin:.2f} needed at {config.LEVERAGE:g}x.")

    return True, "All risk rules passed."


# ── In-trade management ───────────────────────────────────────────────────────

def update_trailing_stop(state: dict, price: float, atr: float) -> Tuple[dict, bool]:
    """
    Ratchet the stop toward price once the trade has run in our favour.
    Returns (state, changed) — `changed` tells the caller whether the
    exchange-held stop needs moving.

    Scalping win rates are only decent because winners are capped early; the
    trailing stop is what stops a winner round-tripping back to entry, which is
    the most common way a profitable-looking scalp system bleeds out.
    """
    if not config.TRAIL_ENABLED or not state.get("in_position"):
        return state, False

    entry = float(state.get("entry_price", 0))
    side = state.get("side", "Buy")
    if entry <= 0 or atr <= 0:
        return state, False

    long = _is_long(side)
    move = (price - entry) if long else (entry - price)
    if move < atr * config.TRAIL_TRIGGER_ATR:
        return state, False       # not far enough in profit yet

    current_sl = float(state.get("sl_price", 0))
    if long:
        new_sl = round(price - atr * config.TRAIL_DISTANCE_ATR, 2)
        improved = new_sl > current_sl      # stops only ever move up on a long
    else:
        new_sl = round(price + atr * config.TRAIL_DISTANCE_ATR, 2)
        improved = new_sl < current_sl      # and only down on a short

    if improved:
        state["sl_price"] = new_sl
        state["trailing_active"] = True
        log.info(f"[trail] {side} stop moved to ${new_sl:,.2f} (price ${price:,.2f})")
        return state, True

    return state, False


def check_exit_triggers(state: dict, price: float) -> Optional[str]:
    """
    Returns 'SL', 'TRAIL', 'TP', 'TIME', or None.

    Note the brackets are ALSO held on the exchange, so this is a secondary
    check — the exchange normally fires first and more reliably. This exists to
    keep local state in step and to enforce the time stop, which Bybit has no
    concept of.
    """
    if not state.get("in_position"):
        return None

    long = _is_long(state.get("side", "Buy"))
    sl = float(state.get("sl_price", 0) or 0)
    tp = float(state.get("tp_price", 0) or 0)

    if long:
        if sl > 0 and price <= sl:
            return "TRAIL" if state.get("trailing_active") else "SL"
        if tp > 0 and price >= tp:
            return "TP"
    else:
        if sl > 0 and price >= sl:
            return "TRAIL" if state.get("trailing_active") else "SL"
        if tp > 0 and price <= tp:
            return "TP"

    # A scalp that hasn't worked within MAX_HOLD_MINUTES has stopped being a
    # scalp, and the margin is better used elsewhere.
    entry_time = state.get("entry_time")
    if entry_time and config.MAX_HOLD_MINUTES > 0:
        try:
            entered = datetime.fromisoformat(entry_time)
            if datetime.now(timezone.utc) - entered > timedelta(minutes=config.MAX_HOLD_MINUTES):
                return "TIME"
        except (ValueError, TypeError):
            pass

    return None


def record_trade_result(state: dict, pnl: float) -> dict:
    """Update the loss-streak and cooldown counters that gate the next entry."""
    if pnl < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        state["last_loss_time"] = datetime.now(timezone.utc).isoformat()
    else:
        state["consecutive_losses"] = 0
    return state
