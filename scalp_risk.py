"""
Scalping risk layer — the final gate before execution.

The entry rules in scalp_signal.py are the *smaller* half of a scalping system.
What actually determines whether a scalper survives is here: fee-aware minimum
edge, volatility-scaled stops, and hard limits on overtrading and revenge
trading. A mediocre entry with this risk envelope beats a great entry without it.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger("scalp_risk")

# Bybit spot BTCUSDT minimum order value.
MIN_NOTIONAL_USDT = 5.0


# ── Brackets ──────────────────────────────────────────────────────────────────

def compute_brackets(entry: float, atr: float, target: Optional[float] = None) -> dict:
    """
    Volatility-scaled stop-loss and take-profit.

    A fixed 2%/4% bracket (the swing bot's default) is wrong for scalping in
    both directions: too wide to scalp, and unrelated to what the market is
    actually doing right now. ATR ties the bracket to live volatility.

    The take-profit is then floored so that it clears round-trip fees by
    MIN_EDGE_FEE_MULT. This is the check that makes the difference between a
    scalping strategy and an elaborate way to pay Bybit.
    """
    # ATR can collapse to ~0 in dead markets; fall back to the percentage floor.
    sl_dist = atr * config.SL_ATR_MULT
    sl_pct = (sl_dist / entry * 100) if entry else config.MIN_SL_PCT
    sl_pct = max(config.MIN_SL_PCT, min(sl_pct, config.MAX_SL_PCT))

    tp_dist = atr * config.TP_ATR_MULT
    tp_pct = (tp_dist / entry * 100) if entry else 0.0

    # --- The fee floor -------------------------------------------------------
    min_tp_pct = config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT
    fee_floored = tp_pct < min_tp_pct
    tp_pct = max(tp_pct, min_tp_pct)

    # If the setup named its own objective, respect it — but never below the
    # fee floor, and never beyond what ATR says is reachable.
    if target and target > entry:
        target_pct = (target - entry) / entry * 100
        tp_pct = max(min_tp_pct, min(tp_pct, target_pct))

    return {
        "sl_price": round(entry * (1 - sl_pct / 100), 2),
        "tp_price": round(entry * (1 + tp_pct / 100), 2),
        "sl_pct": round(sl_pct, 4),
        "tp_pct": round(tp_pct, 4),
        "rr": round(tp_pct / sl_pct, 2) if sl_pct else 0.0,
        "fee_floored": fee_floored,
    }


def net_expectancy_pct(tp_pct: float, sl_pct: float, win_rate: float) -> float:
    """
    Expected value per trade, net of fees, in percent of position size.
    Used by validate() to refuse structurally losing trades and by backtest.py
    to report whether an edge survived costs.
    """
    fee = config.ROUND_TRIP_FEE_PCT
    return win_rate * (tp_pct - fee) - (1 - win_rate) * (sl_pct + fee)


# ── Position sizing ───────────────────────────────────────────────────────────

def position_size(balance_usdt: float, entry: float, sl_price: float) -> float:
    """
    Size off the stop distance so every trade risks the same fraction of the
    pot, regardless of how wide the current volatility makes the stop.

    The swing bot's RISK_PER_TRADE_PCT=100 puts the whole pot on every signal,
    which under scalping frequency is a guaranteed path to ruin.
    """
    risk_usdt = config.MAX_INVESTMENT_USDT * config.SCALP_RISK_PCT / 100
    stop_dist = entry - sl_price
    if stop_dist <= 0:
        return 0.0

    qty = risk_usdt / stop_dist
    notional = qty * entry

    # Cap by both the configured pot and what's actually in the account.
    notional = min(notional, config.MAX_INVESTMENT_USDT, balance_usdt)
    return round(notional, 2)


# ── Trade gating ──────────────────────────────────────────────────────────────

def validate(signal, state: dict, balance: dict, ind: dict) -> Tuple[bool, str]:
    """Returns (allowed, reason). Every rule here can only ever block a trade."""

    if signal.action == "HOLD":
        return True, "No action."

    if signal.confidence < config.MIN_AI_CONFIDENCE:
        return False, (f"Confidence {signal.confidence} below minimum "
                       f"{config.MIN_AI_CONFIDENCE}.")

    # --- Daily loss limit — the circuit breaker --------------------------------
    daily_pnl = float(state.get("daily_pnl_usdt", 0))
    daily_limit = config.MAX_INVESTMENT_USDT * config.MAX_DAILY_LOSS_PCT / 100
    if daily_pnl < -daily_limit:
        return False, (f"Daily loss limit hit (${daily_pnl:.2f}, "
                       f"limit -${daily_limit:.2f}). Stopped until UTC midnight.")

    if signal.action == "SELL":
        if not state.get("in_position"):
            return False, "No open position to sell."
        if float(state.get("qty", 0)) <= 0:
            return False, "Position qty is 0."
        return True, "Exit allowed."

    # --- BUY-side gates -------------------------------------------------------
    if state.get("in_position"):
        return False, "Already in a position — one scalp at a time."

    # Overtrading guard. Scalping's frequency makes this bite far sooner than
    # it would on a swing bot, which is exactly the point.
    if int(state.get("trade_count_today", 0)) >= config.MAX_TRADES_PER_DAY:
        return False, (f"Daily trade cap reached "
                       f"({config.MAX_TRADES_PER_DAY}). Overtrading guard.")

    # Consecutive-loss guard — if the market has stopped matching the model,
    # stop feeding it. This catches regime changes the indicators missed.
    losses = int(state.get("consecutive_losses", 0))
    if losses >= config.MAX_CONSECUTIVE_LOSSES:
        return False, (f"{losses} consecutive losses — halted. "
                       f"Conditions have changed; reset manually after review.")

    # Cooldown after a loss — the anti-revenge-trade rule.
    last_loss = state.get("last_loss_time")
    if last_loss:
        try:
            last_dt = datetime.fromisoformat(last_loss)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            if elapsed < config.COOLDOWN_AFTER_LOSS_MIN:
                return False, (f"Cooldown: {config.COOLDOWN_AFTER_LOSS_MIN - elapsed:.1f} "
                               f"min left after last loss.")
        except (ValueError, TypeError):
            pass

    # --- Fee viability --------------------------------------------------------
    # The setup's own objective must clear fees. This matters most for
    # MEAN_REVERSION, which exits at the mean via _exit_signal() rather than at
    # the TP bracket — so without this check the bot would happily fade a mean
    # 0.1% away and book a guaranteed net loss after paying 0.2% to get in and
    # out. The bracket's fee floor alone does not catch that path.
    min_edge_pct = config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT
    if signal.target:
        target_pct = (signal.target - ind["price"]) / ind["price"] * 100
        if target_pct < min_edge_pct:
            return False, (f"{signal.setup} target is only {target_pct:.3f}% away; "
                           f"needs {min_edge_pct:.3f}% to clear "
                           f"{config.ROUND_TRIP_FEE_PCT:.2f}% round-trip fees.")

    brackets = compute_brackets(ind["price"], ind["atr"], signal.target)
    if brackets["rr"] < 1.0 and signal.setup != "MEAN_REVERSION":
        # Mean-reversion legitimately runs R:R below 1 on a high hit rate;
        # breakouts and pullbacks do not and should be refused.
        return False, (f"Risk/reward {brackets['rr']} below 1.0 for "
                       f"{signal.setup} after the fee floor.")

    # Is the *reachable* move even big enough to pay for itself? If ATR says
    # the market isn't moving, no entry rule can manufacture an edge.
    if ind["atr_pct"] < config.ROUND_TRIP_FEE_PCT:
        return False, (f"ATR {ind['atr_pct']:.3f}% is below round-trip fees "
                       f"{config.ROUND_TRIP_FEE_PCT:.2f}% — market too quiet to scalp.")

    # --- Capital --------------------------------------------------------------
    usdt = float(balance.get("usdt", 0))
    notional = position_size(usdt, ind["price"], brackets["sl_price"])
    if notional < MIN_NOTIONAL_USDT:
        return False, (f"Position ${notional:.2f} below Bybit's "
                       f"${MIN_NOTIONAL_USDT} minimum order value.")
    if usdt < notional * 0.99:
        return False, f"Insufficient USDT: ${usdt:.2f} available, ${notional:.2f} needed."

    return True, "All risk rules passed."


# ── In-trade management ───────────────────────────────────────────────────────

def update_trailing_stop(state: dict, price: float, atr: float) -> dict:
    """
    Ratchet the stop upward once the trade has run in our favour.

    Scalping win rates are only decent because winners are capped early; the
    trailing stop is what stops a winner from round-tripping back to the entry,
    which is the most common way a profitable-looking scalp system bleeds out.
    """
    if not config.TRAIL_ENABLED or not state.get("in_position"):
        return state

    entry = float(state.get("entry_price", 0))
    if entry <= 0 or atr <= 0:
        return state

    if price - entry < atr * config.TRAIL_TRIGGER_ATR:
        return state  # not far enough in profit yet

    new_sl = round(price - atr * config.TRAIL_DISTANCE_ATR, 2)
    current_sl = float(state.get("sl_price", 0))

    # Only ever move a stop up, never down.
    if new_sl > current_sl:
        state["sl_price"] = new_sl
        state["trailing_active"] = True
        log.info(f"[trail] stop raised to ${new_sl:,.2f} (price ${price:,.2f})")

    return state


def check_exit_triggers(state: dict, price: float) -> Optional[str]:
    """Returns 'SL', 'TP', 'TIME', or None."""
    if not state.get("in_position"):
        return None

    sl = float(state.get("sl_price", 0) or 0)
    tp = float(state.get("tp_price", 0) or 0)

    if sl > 0 and price <= sl:
        return "TRAIL" if state.get("trailing_active") else "SL"
    if tp > 0 and price >= tp:
        return "TP"

    # Time stop — a scalp that hasn't worked within MAX_HOLD_MINUTES has
    # stopped being a scalp, and the capital is better used elsewhere.
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
