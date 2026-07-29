"""
4H Bollinger Band Short Strategy — BTC/USDT Linear Perpetual, 25× leverage.

Rules (SHORT ONLY — never long):
  Entry (all four must be true on the last two closed 4H candles):
    1. Price is below MA200 (downtrend confirmation).
    2. The signal candle's high touches or crosses the upper Bollinger Band (20, 2σ).
    3. The very next candle (confirm candle) closes RED (close < open).
    4. The confirm candle's close is above MA28 (room to fall to the target).
  Enter at the close of the confirm candle (market order).

  Exit — whichever comes first:
    Take-profit: current MA28 (recalculated every cycle — moving target).
    Stop-loss  : high of the signal candle, capped at entry + 1.5 × ATR(14).

State machine:
  in_position=False → watching for BB short setup
  in_position=True  → managing open SHORT (SL fixed, TP moving with MA28)
"""
import json
from datetime import datetime, timezone
from typing import Optional

import bybit_client
import indicators as ind_calc
import risk
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("strategy")


# ── Default state ─────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    # Position tracking
    "in_position":       False,
    "side":              None,        # always 'SHORT' in this strategy
    "entry_price":       0.0,
    "entry_time":        None,
    "qty":               0.0,
    "sl_price":          0.0,
    "tp_price":          0.0,
    # Dedup: ts of the last signal candle we acted on (prevent re-entry same candle)
    "signal_candle_ts":  None,
    # Kept for dashboard / Android app compatibility (always None in this strategy)
    "pending_signal":    None,
    # P&L tracking
    "daily_pnl_usdt":    0.0,
    "daily_reset_date":  "",
    "trade_count_today": 0,
    "last_action":       "NONE",
    "last_reason":       "",
    "total_pnl_usdt":    0.0,
    "total_trades":      0,
    "_last_price":       0.0,
}


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if config.STATE_FILE.exists():
            saved = json.loads(config.STATE_FILE.read_text())
            return {**_DEFAULTS, **saved}
    except Exception as e:
        log.error(f"State load error: {e}")
    return _DEFAULTS.copy()


def save_state(state: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"State save error: {e}")


def reset_daily_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pnl_usdt"]    = 0.0
        state["trade_count_today"] = 0
        state["daily_reset_date"]  = today
        log.info("Daily P&L counters reset")
    return state


# ── Trade history persistence ─────────────────────────────────────────────────

def _append_history(record: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        history = []
        if config.HISTORY_FILE.exists():
            history = json.loads(config.HISTORY_FILE.read_text())
        history.append(record)
        history = history[-config.MAX_HISTORY:]
        config.HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        log.error(f"History write error: {e}")


def get_history() -> list:
    try:
        if config.HISTORY_FILE.exists():
            return json.loads(config.HISTORY_FILE.read_text())
    except Exception as e:
        log.error(f"History read error: {e}")
    return []


# ── Startup reconciliation ────────────────────────────────────────────────────

def reconcile_position_on_startup(state: dict) -> dict:
    """
    Sync internal state against the live Bybit position on startup.

    If the bot restarts mid-trade and the saved state shows no position
    but Bybit has one open, this populates state so SL/TP monitoring
    resumes immediately.
    """
    if state.get("in_position"):
        log.info("[reconcile] State already shows open position — skipping")
        return state

    try:
        pos = bybit_client.get_position()
    except Exception as e:
        log.warning(f"[reconcile] Could not fetch position: {e}")
        return state

    if pos is None:
        log.info("[reconcile] No open position on Bybit — state is consistent")
        return state

    raw_side = pos.get("side", "")
    side = "LONG" if raw_side == "Buy" else "SHORT" if raw_side == "Sell" else None
    if side is None:
        log.warning(f"[reconcile] Unrecognised side '{raw_side}' — skipping")
        return state

    entry_price = float(pos.get("avgPrice", 0) or 0)
    qty         = float(pos.get("size", 0) or 0)
    sl_price    = float(pos.get("stopLoss", 0) or 0)
    tp_price    = float(pos.get("takeProfit", 0) or 0)
    entry_time  = pos.get("createdTime", datetime.now(timezone.utc).isoformat())

    log.warning(
        f"[reconcile] Live {side} found on Bybit that state missed. "
        f"Entry={entry_price:.2f} qty={qty} SL={sl_price:.2f} TP={tp_price:.2f}. "
        f"Resuming SL/TP management."
    )

    state.update({
        "in_position":    True,
        "side":           side,
        "entry_price":    entry_price,
        "entry_time":     entry_time,
        "qty":            qty,
        "sl_price":       sl_price,
        "tp_price":       tp_price,
        "pending_signal": None,
        "last_action":    side,
        "last_reason":    "Position synced from Bybit on bot startup/restart.",
    })

    return state


# ── Exit helper ───────────────────────────────────────────────────────────────

def _execute_exit(state: dict, price: float, trigger: str, reason: str) -> dict:
    side = state.get("side", "SHORT")
    qty  = float(state["qty"])

    try:
        if side == "LONG":
            filled = bybit_client.close_long(qty, price)
        else:
            filled = bybit_client.close_short(qty, price)
    except Exception as e:
        log.error(f"Exit {side} failed: {e}")
        tg.alert_api_error(f"close_{side.lower()}", str(e))
        return state

    pnl = risk.estimate_pnl(float(state["entry_price"]), filled, qty, side)

    _append_history({
        "time":    datetime.now(timezone.utc).isoformat(),
        "side":    side,
        "trigger": trigger,
        "entry":   state["entry_price"],
        "exit":    filled,
        "qty":     qty,
        "pnl":     pnl,
        "reason":  reason,
        "leverage": config.LEVERAGE,
    })

    state.update({
        "in_position":    False,
        "side":           None,
        "qty":            0.0,
        "entry_price":    0.0,
        "entry_time":     None,
        "sl_price":       0.0,
        "tp_price":       0.0,
        "daily_pnl_usdt": round(state["daily_pnl_usdt"] + pnl, 4),
        "total_pnl_usdt": round(state["total_pnl_usdt"] + pnl, 4),
        "last_action":    trigger,
        "last_reason":    reason,
    })

    if trigger == "SL":
        tg.alert_stop_loss(filled, qty, pnl, float(state.get("entry_price", 0)))
    elif trigger == "TP":
        tg.alert_take_profit(filled, qty, pnl, float(state.get("entry_price", 0)))
    else:
        tg.alert_sell(filled, qty, qty * filled, pnl, reason, trigger)

    log.info(
        f"[{trigger}] {side} {qty} BTC | entry={state.get('entry_price')} "
        f"exit={filled:.2f} | P&L ${pnl:+.4f}"
    )
    return state


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """
    Run one 4H trading cycle. Returns updated state.

    Phases:
      A) SL/TP check — runs every cycle when in a position.
         TP is updated to the current MA28 before checking (moving target).
      B) BB short setup detection — runs when flat (no position).
         If setup is new (dedup by signal_candle_ts), validate and enter SHORT.
    """

    # ── Fetch market data ────────────────────────────────────────────────────
    df = bybit_client.get_klines_h4()
    # Use the last row's close as the current price (last forming candle — live price)
    price = round(float(df["close"].iloc[-1]), 2)
    state["_last_price"] = price

    log.info(
        f"[cycle] BTC=${price:,.2f} | in_pos={state['in_position']} "
        f"side={state.get('side')} | "
        f"sl={state.get('sl_price', 0):.2f} tp={state.get('tp_price', 0):.2f}"
    )

    # ── Phase A: SL / TP management ──────────────────────────────────────────
    if state["in_position"]:
        # Update the take-profit to the CURRENT MA28 (moving target)
        ma28 = ind_calc.get_ma28_current(df)
        if ma28 > 0:
            old_tp = state.get("tp_price", 0)
            state["tp_price"] = ma28
            if abs(ma28 - old_tp) > 0.01:
                log.info(f"[TP update] MA28 moved {old_tp:.2f} → {ma28:.2f}")

        trigger = risk.check_sl_tp(state, price)
        if trigger:
            side  = state.get("side", "SHORT")
            entry = float(state["entry_price"])
            tp    = float(state.get("tp_price", 0))
            sl    = float(state.get("sl_price", 0))
            reason = (
                f"{'Stop loss' if trigger == 'SL' else 'Take profit (MA28)'} triggered. "
                f"{side} | Entry ${entry:,.2f} → Exit ${price:,.2f} | "
                f"SL={sl:.2f} TP={tp:.2f}"
            )
            return _execute_exit(state, price, trigger, reason)

        # Still in position — update status
        state["last_action"] = "HOLD"
        state["last_reason"] = (
            f"Managing {state['side']} position. "
            f"Entry=${state['entry_price']:.2f} SL={state['sl_price']:.2f} "
            f"TP(MA28)={state['tp_price']:.2f} price={price:.2f}"
        )
        return state

    # ── Phase B: BB short setup detection ────────────────────────────────────
    setup = ind_calc.detect_bb_short_setup(df)

    if setup is None:
        state["last_action"] = "HOLD"
        state["last_reason"] = (
            f"Watching 4H chart. No BB short setup. "
            f"price=${price:,.2f}"
        )
        return state

    # Dedup: same signal candle means we already acted (or skipped) it
    if setup["signal_candle_ts"] == state.get("signal_candle_ts"):
        log.debug(f"[Dedup] Signal candle {setup['signal_candle_ts']} already processed")
        state["last_action"] = "HOLD"
        state["last_reason"] = (
            f"Waiting for next 4H candle. Last setup already processed "
            f"({setup['signal_candle_ts']})."
        )
        return state

    # Mark this signal candle as seen regardless of what happens next
    state["signal_candle_ts"] = setup["signal_candle_ts"]

    # Risk validation (daily limits, no double entry)
    allowed, block_reason = risk.validate_action("SHORT", state, bybit_client.get_balance())
    if not allowed:
        log.info(f"[risk] SHORT blocked: {block_reason}")
        state["last_action"] = "HOLD"
        state["last_reason"] = f"BB setup found but blocked by risk rules: {block_reason}"
        return state

    # Calculate SL: candle high capped at 1.5 ATR
    atr_val = ind_calc.atr(df, config.ATR_PERIOD)
    sl      = risk.calculate_sl(setup["bb_touch_high"], price, atr_val)

    # TP: current MA28 (starting value — will move each cycle)
    ma28 = ind_calc.get_ma28_current(df)

    # Execute SHORT entry
    qty = config.BTC_QTY
    try:
        qty, filled = bybit_client.open_short(qty, price)
    except Exception as e:
        log.error(f"SHORT entry failed: {e}")
        tg.alert_api_error("open_short", str(e))
        return state

    entry_time = datetime.now(timezone.utc).isoformat()

    state.update({
        "in_position":       True,
        "side":              "SHORT",
        "entry_price":       filled,
        "entry_time":        entry_time,
        "qty":               qty,
        "sl_price":          sl,
        "tp_price":          ma28,
        "pending_signal":    None,
        "last_action":       "SHORT",
        "last_reason":       (
            f"BB short setup: upper band touch + red confirm candle. "
            f"MA200={setup['ma200']:.2f} MA28={ma28:.2f} "
            f"SL={sl:.2f} ATR={atr_val:.2f}"
        ),
        "trade_count_today": state.get("trade_count_today", 0) + 1,
        "total_trades":      state.get("total_trades", 0) + 1,
    })

    _append_history({
        "time":             entry_time,
        "side":             "SHORT",
        "entry":            filled,
        "qty":              qty,
        "sl":               sl,
        "tp":               ma28,
        "leverage":         config.LEVERAGE,
        "bb_touch_high":    setup["bb_touch_high"],
        "signal_candle_ts": setup["signal_candle_ts"],
        "atr":              atr_val,
        "ma28_at_entry":    ma28,
        "ma200_at_entry":   setup["ma200"],
    })

    rr = round(abs(filled - ma28) / abs(sl - filled), 2) if sl != filled else 0
    tg.alert_buy(
        filled, qty, qty * filled, 0,
        f"SHORT | SL={sl:.2f} TP(MA28)={ma28:.2f} RR≈{rr}:1 | "
        f"ATR={atr_val:.2f} leverage={config.LEVERAGE}×"
    )

    log.info(
        f"[ENTRY] SHORT {qty} BTC @ ${filled:,.2f} | "
        f"SL={sl:.2f} TP(MA28)={ma28:.2f} RR≈{rr}:1 | {config.LEVERAGE}×"
    )
    return state
