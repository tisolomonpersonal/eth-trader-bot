"""
bot.py — ETH Grid Trading Bot (MT5 via mt5linux bridge)
Connects to mt5-server running on Zeabur at MT5_HOST:8001
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── mt5linux client (talks to the Wine/MT5 RPC bridge) ───────────────────────
from mt5linux import MetaTrader5 as MT5Client

# ── Config from environment ───────────────────────────────────────────────────
MT5_HOST         = os.environ.get("MT5_HOST", "localhost")
MT5_PORT         = int(os.environ.get("MT5_PORT", "8001"))
MT5_LOGIN        = int(os.environ.get("MT5_LOGIN", "0"))
MT5_PASSWORD     = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER       = os.environ.get("MT5_SERVER", "")   # e.g. "ICMarkets-Demo"

SYMBOL           = os.environ.get("TRADE_SYMBOL", "ETHUSD")
TRADE_LOT        = float(os.environ.get("TRADE_LOT", "0.01"))
GRID_STEP_USD    = float(os.environ.get("GRID_STEP_USD", "10.0"))   # grid spacing in price
TAKE_PROFIT_USD  = float(os.environ.get("TAKE_PROFIT_USD", "15.0")) # TP per trade in price
STOP_LOSS_USD    = float(os.environ.get("STOP_LOSS_USD", "30.0"))   # SL per trade in price
MAX_OPEN_ORDERS  = int(os.environ.get("MAX_OPEN_ORDERS", "5"))
MAX_DAILY_LOSS   = float(os.environ.get("MAX_DAILY_LOSS", "50.0"))  # USD
LOOP_INTERVAL    = int(os.environ.get("LOOP_INTERVAL", "30"))       # seconds between cycles

STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE   = Path(__file__).parent / "log.txt"

# ── Shared stop flag ──────────────────────────────────────────────────────────
_stop_event = threading.Event()

# ── MT5 connection (module-level, reconnected on error) ───────────────────────
_mt5 = None
_mt5_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# State helpers
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log(event: str, **kwargs):
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    line = json.dumps(entry)
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Zeabur lifecycle hooks (called by app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def clear_stop():
    """Reset stop flag so the bot loop can (re)start."""
    _stop_event.clear()
    log("lifecycle", reason="clear_stop called")


def request_stop():
    """Signal the bot loop to exit gracefully."""
    _stop_event.set()
    log("lifecycle", reason="request_stop called")


# ═══════════════════════════════════════════════════════════════════════════════
# MT5 connection
# ═══════════════════════════════════════════════════════════════════════════════

def _connect_mt5() -> bool:
    global _mt5
    try:
        mt5 = MT5Client(host=MT5_HOST, port=MT5_PORT)
        # initialize() returns True on success
        ok = mt5.initialize(
            login=MT5_LOGIN,
            password=MT5_PASSWORD,
            server=MT5_SERVER,
        )
        if not ok:
            err = mt5.last_error()
            log("mt5_connect_failed", error=str(err))
            return False
        info = mt5.account_info()
        if info is None:
            log("mt5_account_info_failed")
            return False
        _mt5 = mt5
        log("mt5_connected",
            login=info.login,
            broker=info.company,
            balance=info.balance,
            equity=info.equity)
        return True
    except Exception as e:
        log("mt5_connect_exception", error=str(e))
        return False


def _ensure_mt5() -> bool:
    """Return True if MT5 is connected (reconnects if needed)."""
    global _mt5
    with _mt5_lock:
        if _mt5 is not None:
            try:
                # Quick liveness check
                if _mt5.terminal_info() is not None:
                    return True
            except Exception:
                pass
            _mt5 = None
        return _connect_mt5()


# ═══════════════════════════════════════════════════════════════════════════════
# Market data
# ═══════════════════════════════════════════════════════════════════════════════

def get_price() -> float | None:
    if not _ensure_mt5():
        return None
    tick = _mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    return (tick.bid + tick.ask) / 2


def get_wallet_equity_usdt() -> float | None:
    if not _ensure_mt5():
        return None
    info = _mt5.account_info()
    if info is None:
        return None
    return float(info.equity)


def get_balance() -> float | None:
    if not _ensure_mt5():
        return None
    info = _mt5.account_info()
    return float(info.balance) if info else None


# ═══════════════════════════════════════════════════════════════════════════════
# Order management
# ═══════════════════════════════════════════════════════════════════════════════

def _get_open_positions() -> list:
    """Return list of open positions for SYMBOL."""
    if not _ensure_mt5():
        return []
    positions = _mt5.positions_get(symbol=SYMBOL)
    return list(positions) if positions else []


def _place_order(order_type: int, price: float, sl: float, tp: float, comment: str = "") -> dict | None:
    """
    order_type: _mt5.ORDER_TYPE_BUY or _mt5.ORDER_TYPE_SELL
    Returns result dict or None on failure.
    """
    if not _ensure_mt5():
        return None

    symbol_info = _mt5.symbol_info(SYMBOL)
    if symbol_info is None:
        log("symbol_info_failed", symbol=SYMBOL)
        return None

    # Normalize price, sl, tp to symbol's digit precision
    digits = symbol_info.digits
    price = round(price, digits)
    sl    = round(sl, digits)
    tp    = round(tp, digits)

    request = {
        "action":    _mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    TRADE_LOT,
        "type":      order_type,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "deviation": 20,
        "magic":     20240001,
        "comment":   comment or "eth-grid-bot",
        "type_time": _mt5.ORDER_TIME_GTC,
        "type_filling": _mt5.ORDER_FILLING_IOC,
    }

    result = _mt5.order_send(request)
    if result is None or result.retcode != _mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else "None"
        retmsg  = result.comment if result else "no result"
        log("order_failed", retcode=retcode, retmsg=retmsg, type=order_type, price=price)
        return None

    log("order_placed", ticket=result.order, type=order_type, price=price, sl=sl, tp=tp, lot=TRADE_LOT)
    return {"ticket": result.order, "price": price, "type": order_type}


def close_position(ticket: int) -> bool:
    """Close a specific position by ticket."""
    if not _ensure_mt5():
        return False
    positions = _mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos = positions[0]
    close_type = _mt5.ORDER_TYPE_SELL if pos.type == 0 else _mt5.ORDER_TYPE_BUY
    tick = _mt5.symbol_info_tick(SYMBOL)
    price = tick.bid if close_type == _mt5.ORDER_TYPE_SELL else tick.ask

    request = {
        "action":    _mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    pos.volume,
        "type":      close_type,
        "position":  ticket,
        "price":     price,
        "deviation": 20,
        "magic":     20240001,
        "comment":   "grid-close",
        "type_time": _mt5.ORDER_TIME_GTC,
        "type_filling": _mt5.ORDER_FILLING_IOC,
    }
    result = _mt5.order_send(request)
    if result and result.retcode == _mt5.TRADE_RETCODE_DONE:
        log("position_closed", ticket=ticket, profit=pos.profit)
        return True
    log("close_failed", ticket=ticket, retcode=result.retcode if result else "None")
    return False


def close_all_positions():
    for pos in _get_open_positions():
        close_position(pos.ticket)


# ═══════════════════════════════════════════════════════════════════════════════
# Grid logic
# ═══════════════════════════════════════════════════════════════════════════════

def _get_grid_levels(mid_price: float) -> list[float]:
    """Return grid price levels centered around current price."""
    levels = []
    for i in range(-(MAX_OPEN_ORDERS // 2), MAX_OPEN_ORDERS // 2 + 1):
        levels.append(round(mid_price + i * GRID_STEP_USD, 2))
    return levels


def _existing_order_prices() -> set[float]:
    positions = _get_open_positions()
    return {round(p.price_open, 0) for p in positions}


def _run_grid_cycle(state: dict) -> dict:
    """One grid cycle: check positions, place missing grid orders, update state."""
    price = get_price()
    if price is None:
        log("price_fetch_failed")
        return state

    equity = get_wallet_equity_usdt()
    balance = get_balance()
    positions = _get_open_positions()

    # Calculate live PnL across all open positions
    live_pnl = sum(p.profit for p in positions)
    daily_pnl = float(state.get("daily_pnl") or 0) + 0  # accumulated via closed trades

    state["mark_price"]   = price
    state["equity"]       = equity
    state["live_pnl"]     = live_pnl
    state["daily_pnl"]    = daily_pnl
    state["in_trade"]     = len(positions) > 0
    state["position_side"] = (
        "BUY" if positions and positions[0].type == 0 else
        "SELL" if positions else "NONE"
    )
    if positions:
        state["entry_price"] = positions[0].price_open

    # ── Safety checks ──────────────────────────────────────────────────────
    trading_enabled = state.get("trading_enabled", True)
    is_paused = bool(state.get("paused")) or time.time() < int(state.get("paused_until") or 0)

    if not trading_enabled or is_paused:
        log("cycle_skipped", reason="paused or disabled", paused=is_paused, enabled=trading_enabled)
        return state

    # Daily loss guard
    if daily_pnl <= -abs(MAX_DAILY_LOSS):
        log("daily_loss_limit_hit", daily_pnl=daily_pnl, limit=MAX_DAILY_LOSS)
        state["paused"] = True
        state["paused_until"] = int(time.time() + 60 * 60 * 4)  # pause 4 hours
        return state

    # ── Place grid orders ──────────────────────────────────────────────────
    if len(positions) >= MAX_OPEN_ORDERS:
        log("grid_full", open=len(positions), max=MAX_OPEN_ORDERS)
        return state

    existing_prices = _existing_order_prices()
    grid_levels = _get_grid_levels(price)

    # Determine market bias from 4h candles if available (simple: last close vs open)
    bias = state.get("bias_4h", "neutral")
    order_type = _mt5.ORDER_TYPE_BUY if bias != "downtrend" else _mt5.ORDER_TYPE_SELL

    placed = 0
    for level in grid_levels:
        if len(positions) + placed >= MAX_OPEN_ORDERS:
            break
        # Skip levels already occupied (within GRID_STEP_USD/2 tolerance)
        if any(abs(level - ep) < GRID_STEP_USD / 2 for ep in existing_prices):
            continue

        tick = _mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            break

        if order_type == _mt5.ORDER_TYPE_BUY:
            entry = tick.ask
            sl    = entry - STOP_LOSS_USD
            tp    = entry + TAKE_PROFIT_USD
        else:
            entry = tick.bid
            sl    = entry + STOP_LOSS_USD
            tp    = entry - TAKE_PROFIT_USD

        result = _place_order(order_type, entry, sl, tp, comment=f"grid@{level:.0f}")
        if result:
            placed += 1
            existing_prices.add(round(entry, 0))

    state["grid_mode"]   = bias
    state["grid_active"] = len(_get_open_positions()) > 0
    return state


def _update_closed_trade_stats(state: dict) -> dict:
    """Pull recently closed deals and update win/loss stats + daily PnL."""
    if not _ensure_mt5():
        return state

    from_ts = int(state.get("_last_deal_check", time.time() - 3600))
    to_ts   = int(time.time())

    deals = _mt5.history_deals_get(
        datetime.fromtimestamp(from_ts, tz=timezone.utc),
        datetime.fromtimestamp(to_ts, tz=timezone.utc),
    )
    state["_last_deal_check"] = to_ts

    if not deals:
        return state

    wins   = int(state.get("_wins", 0))
    losses = int(state.get("_losses", 0))
    total_win_profit  = float(state.get("_total_win_profit", 0))
    total_loss_profit = float(state.get("_total_loss_profit", 0))
    lifetime_pnl = float(state.get("lifetime_pnl") or 0)
    daily_pnl    = float(state.get("daily_pnl") or 0)
    fills        = int(state.get("total_fills", 0))

    for deal in deals:
        if deal.magic != 20240001:
            continue
        if deal.entry != 1:  # 1 = DEAL_ENTRY_OUT (closing deal)
            continue
        profit = float(deal.profit)
        lifetime_pnl += profit
        daily_pnl    += profit
        fills        += 1
        if profit >= 0:
            wins += 1
            total_win_profit += profit
        else:
            losses += 1
            total_loss_profit += abs(profit)

    state["_wins"]              = wins
    state["_losses"]            = losses
    state["_total_win_profit"]  = total_win_profit
    state["_total_loss_profit"] = total_loss_profit
    state["lifetime_pnl"]       = lifetime_pnl
    state["daily_pnl"]          = daily_pnl
    state["total_fills"]        = fills
    return state


def _update_bias(state: dict) -> dict:
    """Simple 4H bias: if last 4H candle closed above open → uptrend, else downtrend."""
    if not _ensure_mt5():
        return state
    rates = _mt5.copy_rates_from_pos(SYMBOL, _mt5.TIMEFRAME_H4, 0, 3)
    if rates is None or len(rates) < 2:
        return state
    last = rates[-2]  # last closed candle
    if last["close"] > last["open"]:
        state["bias_4h"] = "uptrend"
    elif last["close"] < last["open"]:
        state["bias_4h"] = "downtrend"
    else:
        state["bias_4h"] = "neutral"
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# Performance summary (called by app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def performance_summary(state: dict | None = None) -> dict:
    if state is None:
        state = load_state()
    wins   = int(state.get("_wins", 0))
    losses = int(state.get("_losses", 0))
    total_win  = float(state.get("_total_win_profit", 0))
    total_loss = float(state.get("_total_loss_profit", 0))
    return {
        "wins":    wins,
        "losses":  losses,
        "avg_win":  round(total_win  / wins,   4) if wins   else 0,
        "avg_loss": round(total_loss / losses, 4) if losses else 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop (runs in background thread started by app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def run_loop():
    log("bot_started", symbol=SYMBOL, lot=TRADE_LOT,
        grid_step=GRID_STEP_USD, tp=TAKE_PROFIT_USD, sl=STOP_LOSS_USD)

    # Initial MT5 connection — retry until connected or stopped
    while not _stop_event.is_set():
        if _ensure_mt5():
            break
        log("waiting_for_mt5", host=MT5_HOST, port=MT5_PORT)
        time.sleep(15)

    _bias_tick = 0

    while not _stop_event.is_set():
        try:
            state = load_state()

            # Update 4H bias every 10 cycles (~5 min at 30s interval)
            _bias_tick += 1
            if _bias_tick >= 10:
                state = _update_bias(state)
                _bias_tick = 0

            # Update closed trade stats
            state = _update_closed_trade_stats(state)

            # Run one grid cycle
            state = _run_grid_cycle(state)

            save_state(state)

        except Exception:
            log("loop_error", traceback=traceback.format_exc())

        _stop_event.wait(LOOP_INTERVAL)

    log("bot_stopped")
    close_all_positions()


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    clear_stop()
    run_loop()
