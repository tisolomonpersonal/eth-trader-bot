"""
BTC scalping strategy — orchestration and state.

Same shape as strategy.py (the original AI-led swing path, kept intact and
still reachable with SCALP_MODE=false), but the signal comes from the
deterministic rule engine and exits are managed by ATR brackets, a trailing
stop and a time stop rather than flat percentage targets.
"""
import json
from datetime import datetime, timezone
from typing import Optional

import bybit_client
import indicators as ind_calc
import scalp_signal
import scalp_risk
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("scalp")


_DEFAULTS: dict = {
    "in_position":        False,
    "entry_price":        0.0,
    "entry_time":         None,
    "qty":                0.0,
    "entry_usdt":         0.0,
    "sl_price":           0.0,
    "tp_price":           0.0,
    "setup":              "",
    "trailing_active":    False,
    "daily_pnl_usdt":     0.0,
    "daily_reset_date":   "",
    "trade_count_today":  0,
    "consecutive_losses": 0,
    "last_loss_time":     None,
    "last_action":        "NONE",
    "last_reason":        "",
    "last_confidence":    0,
    "total_pnl_usdt":     0.0,
    "total_trades":       0,
    "wins":               0,
    "losses":             0,
}


# ── Persistence ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if config.SCALP_STATE_FILE.exists():
            saved = json.loads(config.SCALP_STATE_FILE.read_text())
            return {**_DEFAULTS, **saved}
    except Exception as e:
        log.error(f"State load error: {e}")
    return _DEFAULTS.copy()


def save_state(state: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.SCALP_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"State save error: {e}")


def reset_daily_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pnl_usdt"] = 0.0
        state["trade_count_today"] = 0
        state["daily_reset_date"] = today
        # A new session gets a clean slate on the loss streak — the streak
        # guard is there to catch an intraday regime change, not to punish
        # the bot forever.
        state["consecutive_losses"] = 0
        log.info("Daily counters reset")
    return state


def _append_history(record: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        history = []
        if config.SCALP_HISTORY_FILE.exists():
            history = json.loads(config.SCALP_HISTORY_FILE.read_text())
        history.append(record)
        history = history[-config.MAX_HISTORY:]
        config.SCALP_HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        log.error(f"History write error: {e}")


def get_history() -> list:
    try:
        if config.SCALP_HISTORY_FILE.exists():
            return json.loads(config.SCALP_HISTORY_FILE.read_text())
    except Exception as e:
        log.error(f"History read error: {e}")
    return []


# ── Exit ──────────────────────────────────────────────────────────────────────

def _execute_exit(state: dict, price: float, trigger: str, reason: str) -> dict:
    qty = float(state["qty"])
    entry = float(state["entry_price"])

    try:
        filled = bybit_client.place_market_sell(qty, price)
    except Exception as e:
        log.error(f"Exit sell failed: {e}")
        tg.alert_api_error("place_market_sell", str(e))
        return state

    gross = (filled - entry) * qty
    # Charge fees explicitly so reported P&L is what actually lands in the
    # account. Reporting gross P&L on a scalping bot is self-deception — fees
    # are a third of the outcome at this trade size.
    fees = (entry * qty + filled * qty) * (config.TAKER_FEE_PCT / 100)
    pnl = gross - fees

    _append_history({
        "time":     datetime.now(timezone.utc).isoformat(),
        "side":     "SELL",
        "price":    filled,
        "qty":      qty,
        "entry":    entry,
        "gross":    round(gross, 4),
        "fees":     round(fees, 4),
        "pnl":      round(pnl, 4),
        "pnl_pct":  round((filled - entry) / entry * 100, 4) if entry else 0.0,
        "setup":    state.get("setup", ""),
        "trigger":  trigger,
        "reason":   reason,
    })

    won = pnl > 0
    state.update({
        "in_position":     False,
        "qty":             0.0,
        "entry_price":     0.0,
        "entry_time":      None,
        "sl_price":        0.0,
        "tp_price":        0.0,
        "setup":           "",
        "trailing_active": False,
        "daily_pnl_usdt":  state["daily_pnl_usdt"] + pnl,
        "total_pnl_usdt":  state["total_pnl_usdt"] + pnl,
        "wins":            state.get("wins", 0) + (1 if won else 0),
        "losses":          state.get("losses", 0) + (0 if won else 1),
        "last_action":     trigger,
        "last_reason":     reason,
    })
    state = scalp_risk.record_trade_result(state, pnl)

    if trigger == "SL":
        tg.alert_stop_loss(filled, qty, pnl, entry)
    elif trigger == "TP":
        tg.alert_take_profit(filled, qty, pnl, entry)
    else:
        tg.alert_sell(filled, qty, qty * filled, pnl, reason, trigger)

    log.info(f"[{trigger}] {qty:.6f} {config.BASE_COIN} @ ${filled:,.2f} | "
             f"net ${pnl:+.4f} (fees ${fees:.4f})")
    return state


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """One scalping cycle. Runs every config.SCALP_CYCLE_SECONDS."""

    # 1. Market data — 1m for signals, 5m for the higher-timeframe bias.
    df = bybit_client.get_klines(config.INTERVAL)
    trend_df = bybit_client.get_klines(config.TREND_INTERVAL, limit=100)
    ind = ind_calc.calculate_scalp(df, trend_df)
    price = ind["price"]

    # 2. In-trade management runs FIRST and unconditionally. Protecting an open
    #    position always outranks looking for a new one.
    if state.get("in_position"):
        state = scalp_risk.update_trailing_stop(state, price, ind["atr"])

        trigger = scalp_risk.check_exit_triggers(state, price)
        if trigger:
            entry = state["entry_price"]
            reasons = {
                "SL":    f"Stop loss hit. ${entry:,.2f} → ${price:,.2f}.",
                "TP":    f"Take profit hit. ${entry:,.2f} → ${price:,.2f}.",
                "TRAIL": f"Trailing stop hit, gain locked. ${entry:,.2f} → ${price:,.2f}.",
                "TIME":  (f"Time stop — {config.MAX_HOLD_MINUTES}min elapsed without "
                          f"resolution. ${entry:,.2f} → ${price:,.2f}."),
            }
            return _execute_exit(state, price, trigger, reasons[trigger])

    # 3. Signal.
    sig = scalp_signal.get_signal(ind, state)

    # 4. Optional AI veto — can only block a rule entry, never create one.
    if config.AI_VETO_ENABLED and sig.action == "BUY":
        try:
            import ai
            balance = bybit_client.get_balance()
            ai_sig = ai.get_signal(ind, balance, state)
            if ai_sig.action == "SELL":
                log.info(f"[ai-veto] blocked {sig.setup}: {ai_sig.reason}")
                state["last_action"] = "HOLD"
                state["last_reason"] = f"{sig.setup} vetoed by AI: {ai_sig.reason}"
                return state
        except Exception as e:
            # A veto layer must never be able to stop the bot trading.
            log.warning(f"AI veto unavailable, proceeding on rules: {e}")

    balance = bybit_client.get_balance()

    log.info(f"[cycle] ${price:,.2f} regime={ind['regime']} ADX={ind['adx']} "
             f"RSI={ind['rsi']} %B={ind['bb_pct_b']:.2f} squeeze={ind['squeeze']} "
             f"bias={ind['htf_bias']} → {sig.action} ({sig.setup or 'none'})")

    # 5. Risk gate.
    allowed, block_reason = scalp_risk.validate(sig, state, balance, ind)
    if not allowed:
        if sig.action != "HOLD":
            log.info(f"[risk] {sig.action}/{sig.setup} blocked: {block_reason}")
            state["last_reason"] = f"{sig.setup} blocked: {block_reason}"
        state["last_action"] = "HOLD"
        return state

    # 6. Execute.
    if sig.action == "BUY":
        brackets = scalp_risk.compute_brackets(price, ind["atr"], sig.target)
        usdt_amount = scalp_risk.position_size(balance["usdt"], price, brackets["sl_price"])

        try:
            qty, filled = bybit_client.place_market_buy(usdt_amount, price)
        except Exception as e:
            log.error(f"BUY failed: {e}")
            tg.alert_api_error("place_market_buy", str(e))
            return state

        # Recompute brackets off the actual fill, not the signal price.
        brackets = scalp_risk.compute_brackets(filled, ind["atr"], sig.target)

        state.update({
            "in_position":       True,
            "entry_price":       filled,
            "entry_time":        datetime.now(timezone.utc).isoformat(),
            "qty":               qty,
            "entry_usdt":        usdt_amount,
            "sl_price":          brackets["sl_price"],
            "tp_price":          brackets["tp_price"],
            "setup":             sig.setup,
            "trailing_active":   False,
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
            "sl":         brackets["sl_price"],
            "tp":         brackets["tp_price"],
            "sl_pct":     brackets["sl_pct"],
            "tp_pct":     brackets["tp_pct"],
            "rr":         brackets["rr"],
            "setup":      sig.setup,
            "confidence": sig.confidence,
            "regime":     ind["regime"],
            "reason":     sig.reason,
        })

        tg.alert_buy(filled, qty, usdt_amount, sig.confidence, sig.reason)
        log.info(f"[BUY/{sig.setup}] {qty:.6f} {config.BASE_COIN} @ ${filled:,.2f} "
                 f"SL=${brackets['sl_price']:,.2f} (-{brackets['sl_pct']:.2f}%) "
                 f"TP=${brackets['tp_price']:,.2f} (+{brackets['tp_pct']:.2f}%) "
                 f"R:R={brackets['rr']}")

    elif sig.action == "SELL":
        return _execute_exit(state, price, "SIGNAL", sig.reason)

    else:
        state["last_action"] = "HOLD"
        state["last_reason"] = sig.reason
        state["last_confidence"] = sig.confidence

    return state
