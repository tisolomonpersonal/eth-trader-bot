"""
Core trading strategy.
Combines: market data → indicators → AI signal → risk check → execution.
Also owns state and trade history persistence.
"""
import json
from datetime import datetime, timezone
from typing import Optional

import bybit_client
import indicators as ind_calc
import ai
import risk
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("strategy")


# ── State persistence ─────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "in_position":       False,
    "entry_price":       0.0,
    "entry_time":        None,
    "qty":               0.0,
    "entry_usdt":        0.0,
    "sl_price":          0.0,
    "tp_price":          0.0,
    "daily_pnl_usdt":    0.0,
    "daily_reset_date":  "",
    "trade_count_today": 0,
    "last_action":       "NONE",
    "last_reason":       "",
    "last_confidence":   0,
    "total_pnl_usdt":    0.0,
    "total_trades":      0,
}


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
        # Keep only the last N trades
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
    qty   = float(state["qty"])
    entry = float(state["entry_price"])

    try:
        filled = bybit_client.place_market_sell(qty, price)
    except Exception as e:
        log.error(f"Exit sell failed: {e}")
        tg.alert_api_error("place_market_sell", str(e))
        return state

    pnl = (filled - entry) * qty

    _append_history({
        "time":    datetime.now(timezone.utc).isoformat(),
        "side":    trigger,
        "price":   filled,
        "qty":     qty,
        "pnl":     round(pnl, 4),
        "reason":  reason,
        "trigger": trigger,
    })

    state.update({
        "in_position":    False,
        "qty":            0.0,
        "entry_price":    0.0,
        "entry_time":     None,
        "sl_price":       0.0,
        "tp_price":       0.0,
        "daily_pnl_usdt": state["daily_pnl_usdt"] + pnl,
        "total_pnl_usdt": state["total_pnl_usdt"] + pnl,
        "last_action":    trigger,
        "last_reason":    reason,
    })

    if trigger == "SL":
        tg.alert_stop_loss(filled, qty, pnl, entry)
    elif trigger == "TP":
        tg.alert_take_profit(filled, qty, pnl, entry)
    else:
        tg.alert_sell(filled, qty, qty * filled, pnl, reason, trigger)

    log.info(f"[{trigger}] {qty:.4f} BNB @ ${filled:,.4f} | P&L ${pnl:+.4f}")
    return state


# ── Main cycle ─────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """Run one 1-minute trading cycle. Returns updated state."""

    # 1. Fetch market data
    df  = bybit_client.get_klines()
    ind = ind_calc.calculate(df)
    price = ind["price"]

    # 2. Fetch account balance
    balance = bybit_client.get_balance()

    # 3. SL / TP check — highest priority, runs before AI
    trigger = risk.check_sl_tp(state, price)
    if trigger:
        entry  = state["entry_price"]
        reason = (
            f"{'Stop loss' if trigger=='SL' else 'Take profit'} triggered. "
            f"Entry ${entry:,.4f} → Exit ${price:,.4f}."
        )
        return _execute_exit(state, price, trigger, reason)

    # 4. AI signal
    sig = ai.get_signal(ind, balance, state)
    log.info(f"[cycle] ${price} trend={ind['trend']} RSI={ind['rsi']} "
             f"AI({sig.provider})={sig.action} conf={sig.confidence}")

    # 5. Risk validation
    allowed, block_reason = risk.validate_action(sig.action, sig.confidence, state, balance)
    if not allowed:
        log.info(f"[risk] {sig.action} blocked: {block_reason}")
        state["last_action"] = "HOLD"
        state["last_reason"] = f"Signal was {sig.action} ({sig.confidence}/100) but blocked: {block_reason}"
        return state

    # 6. Execute
    if sig.action == "BUY":
        usdt_amount = risk.calculate_position_size(balance["usdt"], price)
        try:
            qty, filled = bybit_client.place_market_buy(usdt_amount, price)
        except Exception as e:
            log.error(f"BUY failed: {e}")
            tg.alert_api_error("place_market_buy", str(e))
            return state

        sl = round(filled * (1 - config.STOP_LOSS_PCT    / 100), 4)
        tp = round(filled * (1 + config.TAKE_PROFIT_PCT  / 100), 4)

        state.update({
            "in_position":       True,
            "entry_price":       filled,
            "entry_time":        datetime.now(timezone.utc).isoformat(),
            "qty":               qty,
            "entry_usdt":        usdt_amount,
            "sl_price":          sl,
            "tp_price":          tp,
            "last_action":       "BUY",
            "last_reason":       sig.reason,
            "last_confidence":   sig.confidence,
            "trade_count_today": state["trade_count_today"] + 1,
            "total_trades":      state["total_trades"] + 1,
        })

        _append_history({
            "time":       datetime.now(timezone.utc).isoformat(),
            "side":       "BUY",
            "price":      filled,
            "qty":        qty,
            "usdt":       usdt_amount,
            "sl":         sl,
            "tp":         tp,
            "confidence": sig.confidence,
            "reason":     sig.reason,
            "provider":   sig.provider,
        })

        tg.alert_buy(filled, qty, usdt_amount, sig.confidence, sig.reason)
        log.info(f"[BUY] {qty:.4f} BNB @ ${filled:,.4f} SL=${sl} TP=${tp}")

    elif sig.action == "SELL":
        reason = sig.reason
        return _execute_exit(state, price, "AI-SELL", reason)

    else:  # HOLD
        state["last_action"]    = "HOLD"
        state["last_reason"]    = sig.reason
        state["last_confidence"]= sig.confidence

    return state
