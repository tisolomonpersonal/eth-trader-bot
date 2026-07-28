"""
BTC perpetual scalping strategy — orchestration and state.

Differs from the spot swing path in three ways that matter:

  1. Positions, with a side. P&L on a short is (entry - exit), so direction is
     threaded through every calculation rather than assumed long.
  2. The EXCHANGE is the source of truth for whether a position is open.
     Local JSON drifts from reality after a crash, a manual trade, or a
     liquidation — and on a leveraged account that drift is how you end up
     trading against a position you don't know you have.
  3. Stops live on Bybit, attached to the entry order, so a dead bot still has
     a protected position.
"""
import json
from datetime import datetime, timezone
from typing import Optional

import perp_client
import indicators as ind_calc
import scalp_signal
import scalp_risk
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("scalp")


_DEFAULTS: dict = {
    "in_position":        False,
    "side":               None,      # "Buy" (long) | "Sell" (short)
    "entry_price":        0.0,
    "entry_time":         None,
    "qty":                0.0,
    "notional":           0.0,
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
    "longs":              0,
    "shorts":             0,
}


# ── Persistence ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if config.SCALP_STATE_FILE.exists():
            return {**_DEFAULTS, **json.loads(config.SCALP_STATE_FILE.read_text())}
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
        # The streak guard catches an intraday regime change; it should not
        # punish the bot forever.
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
        config.SCALP_HISTORY_FILE.write_text(
            json.dumps(history[-config.MAX_HISTORY:], indent=2, default=str))
    except Exception as e:
        log.error(f"History write error: {e}")


def get_history() -> list:
    try:
        if config.SCALP_HISTORY_FILE.exists():
            return json.loads(config.SCALP_HISTORY_FILE.read_text())
    except Exception as e:
        log.error(f"History read error: {e}")
    return []


# ── Reconciliation ────────────────────────────────────────────────────────────

def reconcile(state: dict) -> dict:
    """
    Make local state agree with the exchange before acting on it.

    Two cases, both real:

      Exchange flat, we think we're in  -> the exchange-held SL or TP fired
         while we weren't looking (or the position was liquidated). Book it and
         move on, rather than managing a position that no longer exists.

      Exchange open, we think we're flat -> a manual trade, or our state file
         was lost. Refuse to trade rather than opening a second position on top
         of one we can't account for.
    """
    if config.PAPER_MODE:
        return state

    try:
        pos = perp_client.get_position()
    except Exception as e:
        log.error(f"Could not read position for reconciliation: {e}")
        return state

    exchange_open = pos["side"] is not None
    local_open = bool(state.get("in_position"))

    if local_open and not exchange_open:
        entry = float(state.get("entry_price", 0))
        qty = float(state.get("qty", 0))
        log.warning("Exchange shows flat but local state says in-position — "
                    "the attached SL/TP almost certainly fired. Booking it.")
        # We don't know the exact fill, so record it at the bracket that was
        # most likely hit and flag the estimate in history.
        _append_history({
            "time": datetime.now(timezone.utc).isoformat(),
            "side": "CLOSE",
            "position_side": state.get("side"),
            "entry": entry,
            "qty": qty,
            "setup": state.get("setup", ""),
            "trigger": "EXCHANGE_BRACKET",
            "pnl": None,
            "reason": "Closed by exchange-held SL/TP while the bot was not "
                      "watching. Exact fill unknown — reconcile against Bybit.",
            "estimated": True,
        })
        tg.alert_critical(
            f"Position closed by the exchange bracket while the bot was not "
            f"watching ({state.get('setup')} {state.get('side')} from "
            f"${entry:,.2f}). Local P&L for this trade is unknown — check Bybit."
        )
        state.update({"in_position": False, "side": None, "qty": 0.0,
                      "entry_price": 0.0, "entry_time": None, "sl_price": 0.0,
                      "tp_price": 0.0, "setup": "", "trailing_active": False})

    elif exchange_open and not local_open:
        log.critical(f"Exchange shows an open {pos['side']} position of "
                     f"{pos['size']} @ ${pos['entry']:,.2f} that this bot has no "
                     f"record of. Adopting it as read-only — no new entries "
                     f"until it is closed.")
        tg.alert_critical(
            f"UNTRACKED POSITION on Bybit: {pos['side']} {pos['size']} "
            f"{config.BASE_COIN} @ ${pos['entry']:,.2f}"
            + (f", liquidation ${pos['liq_price']:,.2f}" if pos['liq_price'] else "")
            + ".\n\nThe bot did not open this and will not trade until it is "
              "closed. Close it on Bybit, or clear scalp_state.json to adopt it."
        )
        state["untracked_position"] = True

    else:
        state.pop("untracked_position", None)

    return state


# ── Exit ──────────────────────────────────────────────────────────────────────

def _execute_exit(state: dict, price: float, trigger: str, reason: str) -> dict:
    qty = float(state["qty"])
    entry = float(state["entry_price"])
    side = state.get("side", "Buy")

    try:
        filled = perp_client.close_position(side, qty, price)
    except Exception as e:
        log.error(f"Close failed: {e}")
        tg.alert_api_error("close_position", str(e))
        return state

    # Direction matters: a short profits when the exit is BELOW the entry.
    gross = (filled - entry) * qty if side == "Buy" else (entry - filled) * qty
    fees = (entry * qty + filled * qty) * (config.TAKER_FEE_PCT / 100)
    pnl = gross - fees

    pnl_pct = ((filled - entry) / entry * 100) if side == "Buy" \
        else ((entry - filled) / entry * 100)

    _append_history({
        "time":     datetime.now(timezone.utc).isoformat(),
        "side":     "CLOSE",
        "position_side": side,
        "price":    filled,
        "qty":      qty,
        "entry":    entry,
        "gross":    round(gross, 4),
        "fees":     round(fees, 4),
        "pnl":      round(pnl, 4),
        "pnl_pct":  round(pnl_pct, 4),
        "setup":    state.get("setup", ""),
        "trigger":  trigger,
        "reason":   reason,
    })

    won = pnl > 0
    state.update({
        "in_position":     False,
        "side":            None,
        "qty":             0.0,
        "entry_price":     0.0,
        "entry_time":      None,
        "notional":        0.0,
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

    log.info(f"[{trigger}] closed {side} {qty} {config.BASE_COIN} @ ${filled:,.2f} | "
             f"net ${pnl:+.4f} ({pnl_pct:+.3f}%, fees ${fees:.4f})")
    return state


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """One scalping cycle. Runs every config.SCALP_CYCLE_SECONDS."""

    state = reconcile(state)

    df = perp_client.get_klines(config.INTERVAL)
    trend_df = perp_client.get_klines(config.TREND_INTERVAL, limit=100)
    ind = ind_calc.calculate_scalp(df, trend_df)
    price = ind["price"]

    # A position the bot didn't open is a position it can't reason about.
    if state.get("untracked_position"):
        state["last_action"] = "HOLD"
        state["last_reason"] = "Untracked position on the exchange — trading paused."
        return state

    # 1. In-trade management runs first and unconditionally. Protecting an open
    #    position always outranks looking for a new one.
    if state.get("in_position"):
        state, moved = scalp_risk.update_trailing_stop(state, price, ind["atr"])
        if moved:
            # Push the new stop to the exchange so it survives this process.
            perp_client.update_stop(state["sl_price"])

        trigger = scalp_risk.check_exit_triggers(state, price)
        if trigger:
            entry = state["entry_price"]
            reasons = {
                "SL":    f"Stop loss hit. ${entry:,.2f} → ${price:,.2f}.",
                "TP":    f"Take profit hit. ${entry:,.2f} → ${price:,.2f}.",
                "TRAIL": f"Trailing stop hit, gain locked. ${entry:,.2f} → ${price:,.2f}.",
                "TIME":  (f"Time stop — {config.MAX_HOLD_MINUTES}min without "
                          f"resolution. ${entry:,.2f} → ${price:,.2f}."),
            }
            return _execute_exit(state, price, trigger, reasons[trigger])

    # 2. Signal.
    sig = scalp_signal.get_signal(ind, state)

    # 3. Optional AI veto — can only block a rule entry, never create one.
    if config.AI_VETO_ENABLED and sig.is_entry:
        try:
            import ai
            ai_sig = ai.get_signal(ind, perp_client.get_balance(), state)
            opposes = ((sig.action == "LONG" and ai_sig.action == "SELL")
                       or (sig.action == "SHORT" and ai_sig.action == "BUY"))
            if opposes:
                log.info(f"[ai-veto] blocked {sig.setup} {sig.action}: {ai_sig.reason}")
                state["last_action"] = "HOLD"
                state["last_reason"] = f"{sig.setup} vetoed by AI: {ai_sig.reason}"
                return state
        except Exception as e:
            # A veto layer must never be able to stop the bot trading.
            log.warning(f"AI veto unavailable, proceeding on rules: {e}")

    balance = perp_client.get_balance()

    log.info(f"[cycle] ${price:,.2f} regime={ind['regime']} ADX={ind['adx']} "
             f"RSI={ind['rsi']} %B={ind['bb_pct_b']:.2f} squeeze={ind['squeeze']} "
             f"bias={ind['htf_bias']} → {sig.action} ({sig.setup or 'none'})")

    # 4. Risk gate.
    allowed, block_reason = scalp_risk.validate(sig, state, balance, ind)
    if not allowed:
        if sig.action != "HOLD":
            log.info(f"[risk] {sig.action}/{sig.setup} blocked: {block_reason}")
            state["last_reason"] = f"{sig.setup} blocked: {block_reason}"
        state["last_action"] = "HOLD"
        return state

    # 5. Execute.
    if sig.is_entry:
        side = sig.side
        brackets = scalp_risk.compute_brackets(price, ind["atr"], side, sig.target)
        qty = perp_client.round_qty(
            scalp_risk.position_qty(balance["usdt"], price, brackets["sl_price"]))

        try:
            qty, filled = perp_client.open_position(
                side, qty, brackets["sl_price"], brackets["tp_price"], price)
        except Exception as e:
            log.error(f"{sig.action} failed: {e}")
            tg.alert_api_error(f"open_{side}", str(e))
            return state

        # Recompute brackets off the actual fill rather than the signal price.
        brackets = scalp_risk.compute_brackets(filled, ind["atr"], side, sig.target)
        notional = qty * filled

        state.update({
            "in_position":       True,
            "side":              side,
            "entry_price":       filled,
            "entry_time":        datetime.now(timezone.utc).isoformat(),
            "qty":               qty,
            "notional":          round(notional, 2),
            "sl_price":          brackets["sl_price"],
            "tp_price":          brackets["tp_price"],
            "setup":             sig.setup,
            "trailing_active":   False,
            "last_action":       sig.action,
            "last_reason":       sig.reason,
            "last_confidence":   sig.confidence,
            "trade_count_today": state["trade_count_today"] + 1,
            "total_trades":      state["total_trades"] + 1,
            "longs":             state.get("longs", 0) + (1 if side == "Buy" else 0),
            "shorts":            state.get("shorts", 0) + (1 if side == "Sell" else 0),
        })

        _append_history({
            "time":       datetime.now(timezone.utc).isoformat(),
            "side":       sig.action,
            "price":      filled,
            "qty":        qty,
            "notional":   round(notional, 2),
            "margin":     round(scalp_risk.margin_required(qty, filled), 2),
            "leverage":   config.LEVERAGE,
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

        tg.alert_buy(filled, qty, notional, sig.confidence,
                     f"[{sig.action}] {sig.reason}")
        log.info(f"[{sig.action}/{sig.setup}] {qty} {config.BASE_COIN} @ ${filled:,.2f} "
                 f"(${notional:,.2f} notional, {config.LEVERAGE:g}x) "
                 f"SL=${brackets['sl_price']:,.2f} TP=${brackets['tp_price']:,.2f} "
                 f"R:R={brackets['rr']}")

    elif sig.action == "CLOSE":
        return _execute_exit(state, price, "SIGNAL", sig.reason)

    else:
        state["last_action"] = "HOLD"
        state["last_reason"] = sig.reason
        state["last_confidence"] = sig.confidence

    return state
