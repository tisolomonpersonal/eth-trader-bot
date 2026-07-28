"""
Directional Candle Strategy — BTC/USDT Linear Perpetual, 25× leverage.

Execution loop:
  1. Detect H1 directional candle (sweep + close beyond prior range).
  2. Contextual filter: abort if signal candle extreme is at a 50-bar HTF level.
  3. Arm a pending signal with the 61.8%–70.5% Fibonacci entry zone.
  4. On M5 cycle: if price retraces into the fib zone (optional FVG confluence),
     enter with fixed 0.004 BTC qty. SL = beyond H1 extreme. TP = next M5 swing.
  5. SL/TP are checked every cycle until the position is closed.

State machine:
  in_position=False, pending_signal=None → watching for H1 signal
  in_position=False, pending_signal=dict → waiting for M5 retracement entry
  in_position=True                        → managing open position
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
    "side":              None,        # 'LONG' | 'SHORT'
    "entry_price":       0.0,
    "entry_time":        None,
    "qty":               0.0,
    "sl_price":          0.0,
    "tp_price":          0.0,
    # Pending H1 signal (pre-entry)
    "pending_signal":    None,        # dict or None
    # P&L tracking
    "daily_pnl_usdt":    0.0,
    "daily_reset_date":  "",
    "trade_count_today": 0,
    "last_action":       "NONE",
    "last_reason":       "",
    "last_confidence":   0,
    "total_pnl_usdt":    0.0,
    "total_trades":      0,
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


# ── Exit helpers ──────────────────────────────────────────────────────────────

def _execute_exit(state: dict, price: float, trigger: str, reason: str) -> dict:
    side = state.get("side", "LONG")
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

    label = "Stop loss" if trigger == "SL" else "Take profit" if trigger == "TP" else trigger
    if trigger == "SL":
        tg.alert_stop_loss(filled, qty, pnl, float(state.get("entry_price", 0)))
    elif trigger == "TP":
        tg.alert_take_profit(filled, qty, pnl, float(state.get("entry_price", 0)))
    else:
        tg.alert_sell(filled, qty, qty * filled, pnl, reason, trigger)

    log.info(f"[{trigger}] {side} {qty} BTC | entry={state.get('entry_price')} "
             f"exit={filled:.2f} | P&L ${pnl:+.4f}")
    return state


# ── Main cycle ─────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """
    Run one M5 trading cycle. Returns updated state.

    Phases:
      A) SL/TP check (highest priority — always runs when in a position)
      B) H1 signal detection (only when flat and no pending signal)
      C) M5 entry trigger (when a pending signal is armed)
    """

    # ── Fetch market data ──────────────────────────────────────────────────────
    df_h1 = bybit_client.get_klines_h1()
    df_m5 = bybit_client.get_klines_m5()
    price = round(float(df_m5["close"].iloc[-1]), 2)

    log.info(f"[cycle] BTC price=${price:,.2f} | in_pos={state['in_position']} "
             f"side={state.get('side')} | pending={'yes' if state.get('pending_signal') else 'no'}")

    # ── Phase A: SL / TP check ─────────────────────────────────────────────────
    if state["in_position"]:
        trigger = risk.check_sl_tp(state, price)
        if trigger:
            side  = state.get("side", "LONG")
            entry = float(state["entry_price"])
            reason = (
                f"{'Stop loss' if trigger == 'SL' else 'Take profit'} triggered. "
                f"{side} | Entry ${entry:,.2f} → Exit ${price:,.2f}."
            )
            return _execute_exit(state, price, trigger, reason)

        # Still in position — update last action and return
        state["last_action"] = "HOLD"
        state["last_reason"] = (
            f"Managing {state['side']} position. Entry=${state['entry_price']:.2f} "
            f"SL={state['sl_price']:.2f} TP={state['tp_price']:.2f} price={price:.2f}"
        )
        return state

    # ── Phase B: H1 signal detection ──────────────────────────────────────────
    if not state.get("pending_signal"):
        signal = ind_calc.detect_directional_candle(df_h1)

        if signal:
            blocked = ind_calc.check_structural_block(
                df_h1,
                signal["h1_high"],
                signal["h1_low"],
                signal["direction"],
            )

            if blocked:
                state["last_action"] = "HOLD"
                state["last_reason"] = (
                    f"H1 {signal['direction']} signal rejected — "
                    f"candle extreme at major HTF structural level."
                )
                return state

            fib_low, fib_high = ind_calc.fib_entry_zone(
                signal["h1_high"], signal["h1_low"], signal["direction"]
            )

            from datetime import timedelta
            expiry = (
                datetime.now(timezone.utc) +
                timedelta(hours=config.SIGNAL_EXPIRY_HOURS)
            ).isoformat()

            state["pending_signal"] = {
                "direction":  signal["direction"],
                "h1_high":    signal["h1_high"],
                "h1_low":     signal["h1_low"],
                "fib_low":    fib_low,
                "fib_high":   fib_high,
                "candle_ts":  signal["candle_ts"],
                "expires_at": expiry,
            }

            log.info(
                f"[Signal Armed] {signal['direction']} | "
                f"H1 candle {signal['h1_low']:.2f}–{signal['h1_high']:.2f} | "
                f"Fib entry zone {fib_low:.2f}–{fib_high:.2f} | "
                f"expires {expiry}"
            )
            state["last_action"] = "SIGNAL"
            state["last_reason"] = (
                f"H1 {signal['direction']} directional candle detected. "
                f"Waiting for M5 retracement into fib zone {fib_low:.2f}–{fib_high:.2f}."
            )

        return state

    # ── Phase C: M5 entry trigger ──────────────────────────────────────────────
    ps = state["pending_signal"]

    # Check expiry
    expires_at = datetime.fromisoformat(ps["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        log.info(f"[Signal Expired] {ps['direction']} signal from {ps['candle_ts']} expired.")
        state["pending_signal"] = None
        state["last_action"]    = "HOLD"
        state["last_reason"]    = f"H1 {ps['direction']} signal expired — no M5 entry triggered."
        return state

    direction = ps["direction"]
    fib_low   = ps["fib_low"]
    fib_high  = ps["fib_high"]

    # Check if price has retraced into the fib entry zone
    in_fib_zone = fib_low <= price <= fib_high
    if not in_fib_zone:
        log.info(
            f"[Waiting] {direction} | price={price:.2f} not yet in "
            f"fib zone [{fib_low:.2f}–{fib_high:.2f}]"
        )
        state["last_action"] = "HOLD"
        state["last_reason"] = (
            f"Waiting for {direction} retracement into fib zone "
            f"{fib_low:.2f}–{fib_high:.2f}. Current price: {price:.2f}."
        )
        return state

    # Optional FVG confluence — log but don't block
    has_fvg = ind_calc.detect_fvg(df_m5, direction)
    if has_fvg:
        log.info(f"[FVG Confluence] {direction} FVG confirmed in M5.")
    else:
        log.info(f"[FVG Confluence] No {direction} FVG found — entering on fib zone alone.")

    # Risk validation (daily limits, no double entry)
    allowed, block_reason = risk.validate_action(direction, 70, state, bybit_client.get_balance())
    if not allowed:
        log.info(f"[risk] {direction} blocked: {block_reason}")
        state["pending_signal"] = None
        state["last_action"]    = "HOLD"
        state["last_reason"]    = f"Signal blocked by risk rules: {block_reason}"
        return state

    # Calculate SL and TP
    sl = risk.calculate_sl(ps["h1_high"], ps["h1_low"], direction)
    tp = ind_calc.find_swing_tp(df_m5, direction, price, sl)

    # Execute entry
    qty = config.BTC_QTY
    try:
        if direction == "LONG":
            qty, filled = bybit_client.open_long(qty, price)
        else:
            qty, filled = bybit_client.open_short(qty, price)
    except Exception as e:
        log.error(f"{direction} entry failed: {e}")
        tg.alert_api_error(f"open_{direction.lower()}", str(e))
        return state

    entry_time = datetime.now(timezone.utc).isoformat()

    state.update({
        "in_position":       True,
        "side":              direction,
        "entry_price":       filled,
        "entry_time":        entry_time,
        "qty":               qty,
        "sl_price":          sl,
        "tp_price":          tp,
        "pending_signal":    None,
        "last_action":       direction,
        "last_reason":       (
            f"H1 directional candle → M5 fib retracement entry. "
            f"FVG confluence: {'yes' if has_fvg else 'no'}."
        ),
        "last_confidence":   70,
        "trade_count_today": state.get("trade_count_today", 0) + 1,
        "total_trades":      state.get("total_trades", 0) + 1,
    })

    _append_history({
        "time":      entry_time,
        "side":      direction,
        "entry":     filled,
        "qty":       qty,
        "sl":        sl,
        "tp":        tp,
        "leverage":  config.LEVERAGE,
        "fib_zone":  [fib_low, fib_high],
        "fvg":       has_fvg,
        "h1_candle": {"high": ps["h1_high"], "low": ps["h1_low"]},
    })

    rr = round(abs(tp - filled) / abs(filled - sl), 2) if sl != filled else 0
    tg.alert_buy(filled, qty, qty * filled, 70,
                 f"{direction} | SL={sl:.2f} TP={tp:.2f} RR={rr}:1 | "
                 f"Leverage={config.LEVERAGE}× FVG={'✓' if has_fvg else '✗'}")

    log.info(
        f"[ENTRY] {direction} {qty} BTC @ ${filled:,.2f} | "
        f"SL={sl:.2f} TP={tp:.2f} RR={rr}:1 | {config.LEVERAGE}×"
    )
    return state
