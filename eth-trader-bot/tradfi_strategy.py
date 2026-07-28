"""
TradFi trading strategy — mirrors strategy.py's shape (data → indicators →
AI signal → risk check → execution) but runs against Bybit TradFi
(category=linear CFD-style perpetuals) via tradfi_client.py.

Kept fully independent from the BNB spot bot: separate state file, separate
trade history, separate risk envelope (tradfi_risk.py / config.TRADFI_*).
Nothing here can affect the crypto bot's state or vice versa.
"""
import json
from datetime import datetime, timezone
from typing import Optional

import tradfi_client
import indicators as ind_calc
import ai
import tradfi_risk as risk
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("tradfi_strategy")


# ── State persistence ─────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "symbol":             config.TRADFI_SYMBOL,
    "in_position":        False,
    "side":               "",       # "Buy" or "Sell"
    "entry_price":        0.0,
    "entry_time":         None,
    "qty":                0.0,
    "entry_usdt":         0.0,
    "sl_price":           0.0,
    "tp_price":           0.0,
    "daily_pnl_usdt":     0.0,
    "daily_reset_date":   "",
    "trade_count_today":  0,
    "last_action":        "NONE",
    "last_reason":        "",
    "last_confidence":    0,
    "total_pnl_usdt":     0.0,
    "total_trades":       0,
}


def load_state() -> dict:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if config.TRADFI_STATE_FILE.exists():
            saved = json.loads(config.TRADFI_STATE_FILE.read_text())
            return {**_DEFAULTS, **saved}
    except Exception as e:
        log.error(f"TradFi state load error: {e}")
    return _DEFAULTS.copy()


def save_state(state: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.TRADFI_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"TradFi state save error: {e}")


def reset_daily_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pnl_usdt"]    = 0.0
        state["trade_count_today"] = 0
        state["daily_reset_date"]  = today
        log.info("TradFi daily P&L counters reset")
    return state


# ── Trade history persistence ─────────────────────────────────────────────────

def _append_history(record: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        history = []
        if config.TRADFI_HISTORY_FILE.exists():
            history = json.loads(config.TRADFI_HISTORY_FILE.read_text())
        history.append(record)
        history = history[-config.MAX_HISTORY:]
        config.TRADFI_HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        log.error(f"TradFi history write error: {e}")


def get_history() -> list:
    try:
        if config.TRADFI_HISTORY_FILE.exists():
            return json.loads(config.TRADFI_HISTORY_FILE.read_text())
    except Exception as e:
        log.error(f"TradFi history read error: {e}")
    return []


# ── Exit helpers ──────────────────────────────────────────────────────────────

def _execute_exit(state: dict, price: float, trigger: str, reason: str) -> dict:
    qty   = float(state["qty"])
    entry = float(state["entry_price"])
    side  = state.get("side", "Buy")

    try:
        result = tradfi_client.close_position(state["symbol"])
        # close_position already flattens; use current price as fill estimate
        # (paper mode returns a dict with 'paper': True and no live fill price)
        filled = price
    except Exception as e:
        log.error(f"TradFi exit failed: {e}")
        tg.alert_api_error("tradfi_close_position", str(e))
        return state

    pnl = (filled - entry) * qty if side == "Buy" else (entry - filled) * qty

    _append_history({
        "time":    datetime.now(timezone.utc).isoformat(),
        "symbol":  state["symbol"],
        "side":    trigger,
        "price":   filled,
        "qty":     qty,
        "pnl":     round(pnl, 4),
        "reason":  reason,
        "trigger": trigger,
    })

    state.update({
        "in_position":    False,
        "side":           "",
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

    tg.alert_tradfi_exit(state["symbol"], trigger, filled, qty, pnl, reason)
    log.info(f"[TradFi {trigger}] {qty} {state['symbol']} @ {filled:,.4f} | P&L ${pnl:+.4f}")
    return state


# ── Main cycle ─────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """Run one TradFi trading cycle. Returns updated state."""
    symbol = state.get("symbol") or config.TRADFI_SYMBOL

    market_open = tradfi_client.is_market_open(symbol)
    if not market_open:
        reason = tradfi_client.market_status_reason(symbol)
        state["last_action"] = "HOLD"
        state["last_reason"] = reason
        log.info(f"[tradfi cycle] {symbol} not tradable — {reason}")
        return state

    # 1. Fetch market data
    df  = tradfi_client.get_klines(symbol)
    if df.empty:
        state["last_action"] = "HOLD"
        state["last_reason"] = f"No candle data returned for {symbol}."
        return state
    ind   = ind_calc.calculate(df)
    price = ind["price"]

    # 2. Fetch account balance
    balance = tradfi_client.get_balance()

    # 3. SL / TP check — highest priority, runs before AI
    trigger = risk.check_sl_tp(state, price)
    if trigger:
        entry  = state["entry_price"]
        reason = (
            f"{'Stop loss' if trigger=='SL' else 'Take profit'} triggered. "
            f"Entry {entry:,.4f} → Exit {price:,.4f}."
        )
        return _execute_exit(state, price, trigger, reason)

    # 4. AI signal
    sig = ai.get_tradfi_signal(ind, symbol, balance, state)
    log.info(f"[tradfi cycle] {symbol} {price} trend={ind['trend']} RSI={ind['rsi']} "
             f"AI({sig.provider})={sig.action} conf={sig.confidence}")

    # 5. Risk validation
    allowed, block_reason = risk.validate_action(sig.action, sig.confidence, state, balance, market_open)
    if not allowed:
        log.info(f"[tradfi risk] {sig.action} blocked: {block_reason}")
        state["last_action"] = "HOLD"
        state["last_reason"] = f"Signal was {sig.action} ({sig.confidence}/100) but blocked: {block_reason}"
        return state

    # 6. Execute
    if sig.action == "BUY":
        instrument = tradfi_client.get_instrument_info(symbol)
        usdt_amount = config.TRADFI_MAX_INVESTMENT_USDT * config.TRADFI_RISK_PER_TRADE_PCT / 100
        qty = risk.calculate_position_qty(balance["usdt"], price, instrument)
        if qty <= 0:
            state["last_action"] = "HOLD"
            state["last_reason"] = "Calculated qty too small for available balance / lot size."
            return state

        try:
            tradfi_client.place_market_buy(qty, symbol)
        except Exception as e:
            log.error(f"TradFi BUY failed: {e}")
            tg.alert_api_error("tradfi_place_market_buy", str(e))
            return state

        filled = price  # market order fill estimate
        sl = round(filled * (1 - config.TRADFI_STOP_LOSS_PCT   / 100), 4)
        tp = round(filled * (1 + config.TRADFI_TAKE_PROFIT_PCT / 100), 4)

        state.update({
            "symbol":            symbol,
            "in_position":       True,
            "side":              "Buy",
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
            "symbol":     symbol,
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

        tg.alert_tradfi_entry(symbol, "BUY", filled, qty, usdt_amount, sig.confidence, sig.reason)
        log.info(f"[TradFi BUY] {qty} {symbol} @ {filled:,.4f} SL={sl} TP={tp}")

    elif sig.action == "SELL":
        return _execute_exit(state, price, "AI-SELL", sig.reason)

    else:  # HOLD
        state["last_action"]     = "HOLD"
        state["last_reason"]     = sig.reason
        state["last_confidence"] = sig.confidence

    return state
