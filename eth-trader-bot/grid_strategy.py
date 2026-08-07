"""
Trend-following hedged grid for BTC perpetual.

Shape of the strategy
---------------------
An EMA cross on GRID_INTERVAL sets a directional bias. Around the current
price the bot maintains a grid of at most GRID_LEVELS_BELOW buy levels and
GRID_LEVELS_ABOVE sell levels, spaced GRID_ATR_MULT x ATR apart.

The bias decides what those levels mean:

  bias LONG   below levels  -> BUY, opening/adding to the LONG (positionIdx 1)
              above levels  -> SELL, reduce-only against the LONG (take profit);
                               once the long is fully spoken for, any remaining
                               above level opens a hedge SHORT (positionIdx 2)

  bias SHORT  mirrored.

So the grid always accumulates in the direction of the trend on pullbacks,
scalps back into strength, and carries an opposite-side hedge when price runs
past the grid instead of reverting. Both sides are held simultaneously, which
is what hedge mode is for.

Reconciliation, not event tracking
----------------------------------
Every cycle recomputes the full desired order set from scratch and diffs it
against the open orders the bot owns. Missing orders are placed, stale ones
cancelled. There is no fill-event bookkeeping to drift out of sync, and a
restart or a missed websocket message costs nothing — the next cycle repairs
the book. This is slower than event-driven but it cannot silently desync,
which matters more when the failure mode is unintended leverage.
"""
import json
import time
from datetime import datetime, timezone

import pandas as pd

import grid_client as gcl
import grid_config as gc
import indicators as ind_calc
from logger import get_logger

log = get_logger("grid.strategy")

# A desired and an existing order are the same order if their prices agree to
# within this many ticks. Avoids cancel/replace churn from float noise.
_PRICE_TOLERANCE_TICKS = 1


# ── State ─────────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "bias":            "neutral",
        "centre":          0.0,
        "step":            0.0,
        "levels_above":    [],
        "levels_below":    [],
        "grid_built_at":   None,
        "halted":          False,
        "halt_reason":     "",
        "day":             _utc_day(),
        "day_start_ms":    _utc_day_start_ms(),
        "realised_today":  0.0,
        "cycles":          0,
        "last_error":      "",
        "updated_at":      None,
    }


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_day_start_ms() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def load_state() -> dict:
    try:
        if gc.GRID_STATE_FILE.exists():
            state = json.loads(gc.GRID_STATE_FILE.read_text())
            # Fill in keys added after the file was written.
            base = _empty_state()
            base.update(state)
            return base
    except Exception as e:
        log.error(f"Could not read grid state, starting fresh: {e}")
    return _empty_state()


def save_state(state: dict) -> None:
    try:
        gc.GRID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        gc.GRID_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error(f"Could not save grid state: {e}")


def _append_history(entry: dict) -> None:
    try:
        gc.GRID_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        if gc.GRID_HISTORY_FILE.exists():
            rows = json.loads(gc.GRID_HISTORY_FILE.read_text())
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        rows.append(entry)
        rows = rows[-gc.GRID_MAX_HISTORY:]
        gc.GRID_HISTORY_FILE.write_text(json.dumps(rows, indent=2))
    except Exception as e:
        log.error(f"Could not append grid history: {e}")


# ── Signal ────────────────────────────────────────────────────────────────────

def compute_bias(df: pd.DataFrame) -> tuple:
    """
    EMA-cross trend bias.

    Returns (bias, ema_fast, ema_slow). Bias is "long", "short", or "neutral"
    when the EMAs are closer together than GRID_EMA_MIN_SEP_PCT of price — the
    band exists so a grid is not torn down and rebuilt on every wobble of two
    EMAs sitting on top of each other.
    """
    closes = df["close"]
    ema_fast = closes.ewm(span=gc.GRID_EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow = closes.ewm(span=gc.GRID_EMA_SLOW, adjust=False).mean().iloc[-1]

    price = float(closes.iloc[-1])
    sep_pct = abs(ema_fast - ema_slow) / price * 100 if price else 0.0

    if sep_pct < gc.GRID_EMA_MIN_SEP_PCT:
        bias = "neutral"
    elif ema_fast > ema_slow:
        bias = "long"
    else:
        bias = "short"

    return bias, float(ema_fast), float(ema_slow)


def compute_step(df: pd.DataFrame) -> float:
    """Grid level spacing in USD = GRID_ATR_MULT x ATR."""
    atr = ind_calc.atr(df, gc.GRID_ATR_PERIOD)
    return float(atr) * gc.GRID_ATR_MULT


# ── Grid geometry ─────────────────────────────────────────────────────────────

def build_levels(centre: float, step: float) -> tuple:
    """Level prices below and above the centre, nearest first."""
    below = [gcl.round_price(centre - step * i) for i in range(1, gc.GRID_LEVELS_BELOW + 1)]
    above = [gcl.round_price(centre + step * i) for i in range(1, gc.GRID_LEVELS_ABOVE + 1)]
    return below, above


def needs_rebuild(state: dict, price: float, bias: str, step: float) -> str:
    """Reason the grid should be recentred, or "" if it is still valid."""
    if not state.get("grid_built_at") or not state.get("centre"):
        return "no grid"
    if bias != state.get("bias"):
        return f"bias {state.get('bias')} -> {bias}"
    if step <= 0:
        return ""
    # Drift is measured in ATRs, so recover ATR from the stored step.
    atr = state.get("step", step) / max(gc.GRID_ATR_MULT, 1e-9)
    drift = abs(price - state["centre"])
    if drift > gc.GRID_RECENTER_ATR * atr:
        return f"price drifted ${drift:,.0f} from centre"
    # Volatility regime shift — spacing no longer reflects the market.
    old_step = state.get("step", 0)
    if old_step > 0 and (step / old_step > 1.75 or step / old_step < 0.57):
        return f"ATR step {old_step:,.0f} -> {step:,.0f}"
    return ""


# ── Desired order set ─────────────────────────────────────────────────────────

def desired_orders(bias: str, below: list, above: list, positions: dict) -> list:
    """
    The full set of orders that should be resting, given the bias, the grid
    levels and the current two-sided position.

    Each spec: {side, price, qty, position_idx, reduce_only, tag}
    """
    if bias == "neutral":
        return []

    long_size = gcl.position_size(positions, "long")
    short_size = gcl.position_size(positions, "short")
    qty = gc.GRID_QTY
    cap = gc.GRID_MAX_POSITION_BTC

    orders = []

    if bias == "long":
        entry_levels, exit_levels = below, above
        entry_side, exit_side = "Buy", "Sell"
        trend_idx, hedge_idx = 1, 2
        trend_size, hedge_size = long_size, short_size
    else:
        entry_levels, exit_levels = above, below
        entry_side, exit_side = "Sell", "Buy"
        trend_idx, hedge_idx = 2, 1
        trend_size, hedge_size = short_size, long_size

    # 1. Accumulate on pullbacks, up to the per-side cap.
    room = max(0.0, cap - trend_size)
    for i, price in enumerate(entry_levels):
        if room < gc.GRID_QTY:
            break
        orders.append({
            "side": entry_side, "price": price, "qty": qty,
            "position_idx": trend_idx, "reduce_only": False,
            "tag": f"entry{i + 1}",
        })
        room -= qty

    # 2. Scalp back into strength: reduce-only take-profits covering the
    #    position we actually hold, nearest level first. Anything the position
    #    cannot cover becomes a hedge entry on the opposite side instead —
    #    that is the leg that makes this a hedged grid rather than a plain one.
    unallocated = trend_size
    hedge_room = max(0.0, cap - hedge_size)

    for i, price in enumerate(exit_levels):
        if unallocated >= gc.GRID_QTY:
            take = min(qty, unallocated)
            orders.append({
                "side": exit_side, "price": price, "qty": take,
                "position_idx": trend_idx, "reduce_only": True,
                "tag": f"tp{i + 1}",
            })
            unallocated -= take
        elif hedge_room >= gc.GRID_QTY:
            orders.append({
                "side": exit_side, "price": price, "qty": qty,
                "position_idx": hedge_idx, "reduce_only": False,
                "tag": f"hedge{i + 1}",
            })
            hedge_room -= qty

    # 3. Give the hedge somewhere to go: a reduce-only target one step back
    #    toward the trend, so it closes for a profit if price reverts rather
    #    than sitting as a permanent drag.
    if hedge_size >= gc.GRID_QTY and entry_levels:
        hedge_exit_side = entry_side          # opposite of how the hedge opened
        orders.append({
            "side": hedge_exit_side, "price": entry_levels[0], "qty": hedge_size,
            "position_idx": hedge_idx, "reduce_only": True,
            "tag": "hedge_tp",
        })

    return orders


# ── Reconciliation ────────────────────────────────────────────────────────────

def _same_order(desired: dict, existing: dict) -> bool:
    if desired["side"] != existing["side"]:
        return False
    if desired["position_idx"] != existing["position_idx"]:
        return False
    if bool(desired["reduce_only"]) != bool(existing["reduce_only"]):
        return False
    tol = gcl.filters()["tick_size"] * _PRICE_TOLERANCE_TICKS
    if abs(desired["price"] - existing["price"]) > tol:
        return False
    # Quantity must match too, or a partially-covered TP never gets resized.
    return abs(gcl.round_qty(desired["qty"]) - existing["qty"]) < 1e-9


def reconcile(desired: list, existing: list) -> dict:
    """Cancel orders no longer wanted, place the ones that are missing."""
    matched_existing = set()
    to_place = []

    for d in desired:
        hit = None
        for i, e in enumerate(existing):
            if i in matched_existing:
                continue
            if _same_order(d, e):
                hit = i
                break
        if hit is None:
            to_place.append(d)
        else:
            matched_existing.add(hit)

    to_cancel = [e for i, e in enumerate(existing) if i not in matched_existing]

    cancelled = 0
    for e in to_cancel:
        try:
            if gcl.cancel_order(e["order_id"]):
                cancelled += 1
        except Exception as ex:
            log.error(f"cancel {e['order_id'][:8]} failed: {ex}")

    placed = 0
    for d in to_place:
        try:
            if gcl.place_limit(
                side=d["side"], qty=d["qty"], price=d["price"],
                position_idx=d["position_idx"], reduce_only=d["reduce_only"],
                tag=d["tag"],
            ):
                placed += 1
        except Exception as ex:
            log.error(f"place {d['tag']} @ {d['price']} failed: {ex}")

    return {"placed": placed, "cancelled": cancelled, "kept": len(matched_existing)}


# ── Risk ──────────────────────────────────────────────────────────────────────

def reset_daily_if_needed(state: dict) -> dict:
    today = _utc_day()
    if state.get("day") != today:
        log.info(f"UTC day rollover {state.get('day')} -> {today}; clearing halt")
        state["day"] = today
        state["day_start_ms"] = _utc_day_start_ms()
        state["realised_today"] = 0.0
        state["halted"] = False
        state["halt_reason"] = ""
    return state


def flatten_everything(state: dict, price: float, reason: str) -> dict:
    """Cancel all grid orders and market-close both sides."""
    log.warning(f"FLATTEN: {reason}")
    gcl.cancel_all_ours()
    positions = gcl.get_positions()
    for side in ("long", "short"):
        size = gcl.position_size(positions, side)
        if size > 0:
            try:
                gcl.close_position(side, size, price)
            except Exception as e:
                log.error(f"close {side} failed: {e}")
    _append_history({"event": "flatten", "reason": reason, "price": price})
    return state


def check_kill_switch(state: dict, price: float) -> dict:
    """Halt the grid if realised losses breach the daily limit."""
    realised = gcl.get_closed_pnl(state["day_start_ms"])
    state["realised_today"] = realised

    if realised <= -abs(gc.GRID_MAX_DAILY_LOSS_USDT):
        reason = (
            f"daily realised PnL ${realised:,.2f} breached limit "
            f"-${gc.GRID_MAX_DAILY_LOSS_USDT:,.2f}"
        )
        state["halted"] = True
        state["halt_reason"] = reason
        flatten_everything(state, price, reason)
    return state


def check_net_stop(positions: dict, price: float, step: float) -> bool:
    """
    Backstop stop on the trend-side position: if it is underwater by more than
    GRID_STOP_ATR_MULT ATRs from its average entry, close it. The grid alone
    has no exit for a sustained one-way move against it.
    """
    if gc.GRID_STOP_ATR_MULT <= 0 or step <= 0:
        return False

    atr = step / max(gc.GRID_ATR_MULT, 1e-9)
    limit = atr * gc.GRID_STOP_ATR_MULT

    for side in ("long", "short"):
        p = positions.get(side)
        if not p or p["size"] <= 0:
            continue
        entry = p["avg_price"]
        adverse = (entry - price) if side == "long" else (price - entry)
        if adverse > limit:
            reason = (
                f"{side} {p['size']} BTC is ${adverse:,.0f} against entry "
                f"${entry:,.0f} (> {gc.GRID_STOP_ATR_MULT}x ATR = ${limit:,.0f})"
            )
            log.warning(f"NET STOP: {reason}")
            gcl.cancel_all_ours()
            gcl.close_position(side, p["size"], price)
            _append_history({"event": "net_stop", "side": side, "reason": reason,
                             "price": price, "entry": entry})
            return True
    return False


# ── Cycle ─────────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    df = gcl.get_klines()
    if df.empty or len(df) < gc.GRID_EMA_SLOW:
        log.warning(f"Only {len(df)} candles, need {gc.GRID_EMA_SLOW}; skipping cycle")
        return state

    price = float(df["close"].iloc[-1])
    bias, ema_fast, ema_slow = compute_bias(df)
    step = compute_step(df)

    state["cycles"] = state.get("cycles", 0) + 1

    # Kill switch first — nothing else should run if the day is blown.
    state = check_kill_switch(state, price)
    if state.get("halted"):
        log.warning(f"Grid halted: {state['halt_reason']}")
        return state

    positions = gcl.get_positions()

    # Backstop stop before placing anything new.
    if check_net_stop(positions, price, step):
        positions = gcl.get_positions()

    # Bias flip: optionally close what is now counter-trend, then rebuild.
    prev_bias = state.get("bias", "neutral")
    if bias != prev_bias and prev_bias in ("long", "short") and bias in ("long", "short"):
        log.info(f"Bias flip {prev_bias} -> {bias}")
        if gc.GRID_CLOSE_COUNTER_ON_FLIP:
            stale_side = prev_bias        # the old trend side is now counter-trend
            size = gcl.position_size(positions, stale_side)
            if size > 0:
                gcl.cancel_all_ours()
                gcl.close_position(stale_side, size, price)
                _append_history({"event": "flip_close", "side": stale_side,
                                 "size": size, "price": price,
                                 "from": prev_bias, "to": bias})
                positions = gcl.get_positions()

    if bias == "neutral":
        # No conviction: stop adding risk, pull the resting orders, but leave
        # existing positions alone — their TPs are re-placed once bias returns.
        if state.get("grid_built_at"):
            log.info("Bias neutral — clearing resting grid orders")
            gcl.cancel_all_ours()
        state.update({"bias": bias, "grid_built_at": None,
                      "levels_above": [], "levels_below": []})
        return state

    # Recentre when needed.
    reason = needs_rebuild(state, price, bias, step)
    if reason:
        below, above = build_levels(price, step)
        state.update({
            "bias": bias,
            "centre": gcl.round_price(price),
            "step": round(step, 2),
            "levels_below": below,
            "levels_above": above,
            "grid_built_at": datetime.now(timezone.utc).isoformat(),
        })
        log.info(
            f"Grid rebuilt ({reason}) | bias={bias} centre=${price:,.1f} "
            f"step=${step:,.1f} below={below} above={above}"
        )
        _append_history({"event": "rebuild", "reason": reason, "bias": bias,
                         "centre": price, "step": step,
                         "below": below, "above": above})
    else:
        below = state["levels_below"]
        above = state["levels_above"]

    desired = desired_orders(bias, below, above, positions)
    existing = gcl.get_open_orders()
    result = reconcile(desired, existing)

    log.info(
        f"cycle #{state['cycles']} | ${price:,.1f} bias={bias} "
        f"(ema{gc.GRID_EMA_FAST}=${ema_fast:,.0f} ema{gc.GRID_EMA_SLOW}=${ema_slow:,.0f}) "
        f"step=${step:,.0f} | long={gcl.position_size(positions, 'long')} "
        f"short={gcl.position_size(positions, 'short')} | "
        f"orders +{result['placed']} -{result['cancelled']} ={result['kept']} | "
        f"pnl_today=${state['realised_today']:,.2f}"
    )

    state["last_error"] = ""
    return state


def startup(state: dict) -> dict:
    """Prepare the account and clear any stale orders from a previous run."""
    log.info(
        f"Grid bot starting | {gc.GRID_SYMBOL} {gc.GRID_INTERVAL}m | "
        f"qty={gc.GRID_QTY} BTC x{gc.GRID_LEVERAGE} | "
        f"{gc.GRID_LEVELS_BELOW} below / {gc.GRID_LEVELS_ABOVE} above @ "
        f"{gc.GRID_ATR_MULT}xATR({gc.GRID_ATR_PERIOD}) | "
        f"EMA{gc.GRID_EMA_FAST}/{gc.GRID_EMA_SLOW} | "
        f"cap={gc.GRID_MAX_POSITION_BTC} BTC/side | "
        f"paper={gc.GRID_PAPER_MODE} dry_run={gc.GRID_DRY_RUN} "
        f"testnet={gc.GRID_TESTNET}"
    )

    if not gcl.ensure_hedge_mode():
        raise RuntimeError(
            "Could not put the symbol into hedge mode. If another strategy holds "
            "a one-way position on this symbol, Bybit refuses the switch — close "
            "it, or run the grid on a separate sub-account."
        )

    # Orders from a previous process are not tracked in state; drop them so the
    # first cycle starts from a known-empty book.
    gcl.cancel_all_ours()

    state = reset_daily_if_needed(state)
    state["grid_built_at"] = None
    return state
