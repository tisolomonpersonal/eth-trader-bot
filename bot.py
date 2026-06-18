"""
EURUSD Master Pattern Trading Bot — MT5 Edition
================================================
Strategy: 3-phase master pattern (Contraction -> Expansion -> Trend)
- 4H timeframe: directional bias (where smart money has settled)
- 1H timeframe: entry signal (counter-trend expansion = entry zone)
- No hard SL: exit only when 4H bias reverses
- TP: set directly on the pending order at 1H average price
- Single position at a time
- Dynamic lot sizing: risks RISK_PCT of equity per trade
- Connects to MT5 terminal via mt5linux RPC bridge (Zeabur internal network)
"""

import json, os, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    from mt5linux import MetaTrader5
except ImportError:
    MetaTrader5 = None

try:
    import anthropic as _anthropic_lib
except Exception:
    _anthropic_lib = None

# == CONFIG ====================================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

# MT5 connection (set these as Zeabur environment variables)
MT5_HOST   = os.environ.get("MT5_HOST", "mt5.zeabur.internal")  # your MT5 service name
MT5_PORT   = int(os.environ.get("MT5_PORT", "8001"))

# Symbol — use what appears in your Bybit MT5 terminal (check the Market Watch)
SYMBOL     = os.environ.get("SYMBOL", "EURUSD.s")
MAGIC      = 234567   # bot identifier on all orders

# Forex precision
PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", "5"))   # EURUSD: 5 dp
PIP_SIZE       = float(os.environ.get("PIP_SIZE", "0.0001"))  # 1 pip EURUSD

# Position sizing: risk RISK_PCT of equity, assume MAX_ADVERSE_PIPS worst case
RISK_PCT         = float(os.environ.get("RISK_PCT", "0.01"))        # 1%
MAX_ADVERSE_PIPS = int(os.environ.get("MAX_ADVERSE_PIPS", "100"))   # 100 pip buffer
MIN_LOT          = float(os.environ.get("MIN_LOT", "0.01"))
MAX_LOT          = float(os.environ.get("MAX_LOT", "10.0"))

# Entry
ENTRY_OFFSET_PCT = float(os.environ.get("ENTRY_OFFSET_PCT", "0.001"))  # 0.1% offset
MIN_TP_PIPS      = int(os.environ.get("MIN_TP_PIPS", "10"))            # skip if TP < 10 pips

# Master pattern timeframes (in minutes as strings, mapped to MT5 constants)
BIAS_TF        = os.environ.get("BIAS_TF", "240")   # 4H
ENTRY_TF       = os.environ.get("ENTRY_TF", "60")   # 1H
BOX_LOOKBACK   = int(os.environ.get("BOX_LOOKBACK", "40"))
BOX_WINDOW     = int(os.environ.get("BOX_WINDOW", "5"))
SETTLE_CANDLES = int(os.environ.get("SETTLE_CANDLES", "2"))

# Risk / timing
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "60"))      # 1 min cycles
CLAUDE_INTERVAL  = int(os.environ.get("CLAUDE_INTERVAL", "1800"))   # 30 min AI read
PORTFOLIO_STOPLOSS = float(os.environ.get("PORTFOLIO_STOPLOSS", "-4.0"))

STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")

# MT5 timeframe map
TF_MAP = {
    "1": 1, "5": 5, "15": 15, "30": 30,
    "60": 16385,    # TIMEFRAME_H1
    "240": 16388,   # TIMEFRAME_H4
    "1440": 16390,  # TIMEFRAME_D1
}

# == MT5 CONNECTION ============================================================

_mt5 = None

def get_mt5():
    """Return MT5 instance, (re)connecting if needed."""
    global _mt5
    if MetaTrader5 is None:
        raise Exception("mt5linux not installed. Add it to requirements.txt.")
    if _mt5 is None:
        _mt5 = MetaTrader5(host=MT5_HOST, port=MT5_PORT)
    if not _mt5.initialize():
        err = _mt5.last_error()
        _mt5 = None
        raise Exception(f"MT5 connection failed ({MT5_HOST}:{MT5_PORT}): {err}")
    return _mt5


# == STOP FLAG =================================================================
_stop_flag = False
def request_stop():  global _stop_flag; _stop_flag = True
def clear_stop():    global _stop_flag; _stop_flag = False
def is_stop_requested(): return _stop_flag


# == STATE =====================================================================

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {
        "in_trade":              False,
        "trade_side":            None,
        "entry_price":           None,
        "tp_price":              None,
        "entry_ticket":          None,   # MT5 pending order ticket
        "lot_size":              None,
        "bias_4h":               None,
        "avg_price_4h":          None,
        "avg_price_1h":          None,
        "total_profit":          0.0,
        "lifetime_pnl":          0.0,
        "daily_pnl":             0.0,
        "daily_pnl_date":        "",
        "total_fills":           0,
        "trade_history":         [],
        "live_pnl":              None,
        "position_side":         None,
        "mark_price":            None,
        "equity":                None,
        "last_fill_check_ts":    0,
        "trading_enabled":       True,
        "paused_until":          0,
        "last_claude_analysis":  0,
    }

def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def append_log(event, payload=None):
    try:
        record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "event": event}
        if isinstance(payload, dict):
            record.update(payload)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except:
        pass

def performance_summary(state):
    hist   = state.get("trade_history") or []
    pnls   = [float(t.get("pnl") or 0) for t in hist if t.get("pnl") is not None]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = len(pnls)
    return {
        "trades":   total,
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  round(len(wins)/total*100, 2) if total else 0,
        "avg_win":  round(sum(wins)/len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses)/len(losses), 4) if losses else 0,
    }


# == TELEGRAM ==================================================================

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except:
        pass


# == MT5 MARKET DATA ===========================================================

def get_price():
    mt5  = get_mt5()
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        raise Exception(f"No tick data for {SYMBOL}. Check symbol name in MT5 Market Watch.")
    return (tick.ask + tick.bid) / 2.0   # mid price


def get_candles(interval, limit=100):
    try:
        mt5 = get_mt5()
        tf  = TF_MAP.get(str(interval), 16385)   # default H1
        rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, limit)
        if rates is None or len(rates) == 0:
            return []
        return [
            {"open":  float(r["open"]),  "high": float(r["high"]),
             "low":   float(r["low"]),   "close": float(r["close"])}
            for r in rates
        ]
    except Exception as e:
        print(f"[get_candles] {e}")
        return []


def get_wallet_equity_usdt():
    try:
        mt5  = get_mt5()
        info = mt5.account_info()
        if info is None:
            return None
        return float(info.equity)
    except:
        return None


def get_position():
    """Return first open position for SYMBOL, or None."""
    try:
        mt5       = get_mt5()
        positions = mt5.positions_get(symbol=SYMBOL)
        if not positions:
            return None
        pos = positions[0]
        return {
            "side":        "Buy"  if pos.type == 0 else "Sell",
            "size":        float(pos.volume),
            "entry_price": float(pos.price_open),
            "mark_price":  float(pos.price_current),
            "live_pnl":    float(pos.profit),
            "ticket":      int(pos.ticket),
        }
    except Exception as e:
        print(f"[get_position] {e}")
        return None


def get_open_orders():
    """Return all pending orders for SYMBOL placed by this bot."""
    try:
        mt5    = get_mt5()
        orders = mt5.orders_get(symbol=SYMBOL)
        if not orders:
            return []
        return [o for o in orders if o.magic == MAGIC]
    except:
        return []


# == MT5 ORDER EXECUTION =======================================================

def calculate_lot_size(equity):
    """
    Risk RISK_PCT of equity; size so MAX_ADVERSE_PIPS adverse move = risk_amount.
    $10  -> 0.01 lots (min) | $100 -> 0.10 lots | $1000 -> 1.00 lot
    """
    if not equity or equity <= 0:
        return MIN_LOT
    risk_amount       = equity * RISK_PCT
    pip_value_std_lot = PIP_SIZE * 100_000   # $10 per pip for EURUSD 1 std lot
    lot               = risk_amount / (MAX_ADVERSE_PIPS * pip_value_std_lot)
    return max(MIN_LOT, min(round(lot, 2), MAX_LOT))


def place_pending_order(order_type_str, entry_price, tp_price, lot_size):
    """
    Place a Buy Limit or Sell Limit with TP set directly on the order.
    order_type_str: "buy_limit" or "sell_limit"
    Returns: (success: bool, ticket: int or None, msg: str)
    """
    try:
        mt5 = get_mt5()
        if order_type_str == "sell_limit":
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        else:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT

        request = {
            "action":      mt5.TRADE_ACTION_PENDING,
            "symbol":      SYMBOL,
            "volume":      round(lot_size, 2),
            "type":        order_type,
            "price":       round(entry_price, PRICE_DECIMALS),
            "tp":          round(tp_price,    PRICE_DECIMALS),
            "deviation":   10,
            "magic":       MAGIC,
            "comment":     "master_pattern",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            msg = result.comment if result else "No response"
            return False, None, msg
        return True, int(result.order), "ok"
    except Exception as e:
        return False, None, str(e)


def cancel_order_by_ticket(ticket):
    try:
        mt5 = get_mt5()
        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}
        mt5.order_send(request)
    except Exception as e:
        print(f"[cancel_order] {e}")


def cancel_all_bot_orders():
    """Cancel all pending orders placed by this bot (matching MAGIC number)."""
    try:
        mt5    = get_mt5()
        orders = mt5.orders_get(symbol=SYMBOL)
        if not orders:
            return
        for o in orders:
            if o.magic == MAGIC:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                time.sleep(0.1)
    except Exception as e:
        print(f"[cancel_all] {e}")


def close_all_positions():
    """Market-close all open positions for SYMBOL."""
    try:
        mt5       = get_mt5()
        positions = mt5.positions_get(symbol=SYMBOL)
        if not positions:
            return
        for pos in positions:
            tick = mt5.symbol_info_tick(SYMBOL)
            if pos.type == 0:   # BUY position -> close with SELL
                close_type  = mt5.ORDER_TYPE_SELL
                close_price = tick.bid
            else:               # SELL position -> close with BUY
                close_type  = mt5.ORDER_TYPE_BUY
                close_price = tick.ask

            request = {
                "action":      mt5.TRADE_ACTION_DEAL,
                "symbol":      SYMBOL,
                "volume":      float(pos.volume),
                "type":        close_type,
                "position":    pos.ticket,
                "price":       close_price,
                "deviation":   20,
                "magic":       MAGIC,
                "comment":     "emergency_close",
                "type_time":   mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            mt5.order_send(request)
            time.sleep(0.2)
    except Exception as e:
        print(f"[close_all] {e}")


def get_closed_deals(since_ts=None):
    """Return closed deals since since_ts (unix timestamp)."""
    try:
        mt5       = get_mt5()
        from_dt   = datetime.fromtimestamp(since_ts or (time.time() - 86400), tz=timezone.utc)
        to_dt     = datetime.now(timezone.utc)
        deals     = mt5.history_deals_get(from_dt, to_dt)
        if deals is None:
            return []
        # DEAL_ENTRY_OUT = 1 means closing deal
        return [d for d in deals if d.symbol == SYMBOL and d.entry == 1 and d.magic == MAGIC]
    except Exception as e:
        print(f"[closed_deals] {e}")
        return []


# == MASTER PATTERN ANALYSIS ===================================================

def find_contraction_box(candles, lookback=40, window=5):
    """
    Find the tightest price range (contraction box) in recent candles.
    Phase 1 of the master pattern — where smart money accumulates.
    Returns: {high, low, avg, range, range_pct} or None
    """
    if len(candles) < window + 2:
        return None
    recent     = candles[-min(lookback, len(candles)):]
    best_range = float("inf")
    best_high  = best_low = 0.0

    for i in range(len(recent) - window + 1):
        subset = recent[i:i + window]
        hi  = max(c["high"] for c in subset)
        lo  = min(c["low"]  for c in subset)
        rng = hi - lo
        if rng < best_range:
            best_range = rng
            best_high  = hi
            best_low   = lo

    if best_high == 0:
        return None

    avg       = (best_high + best_low) / 2.0
    range_pct = (best_range / avg) * 100.0 if avg > 0 else 0

    return {
        "high":      round(best_high, PRICE_DECIMALS),
        "low":       round(best_low,  PRICE_DECIMALS),
        "avg":       round(avg,       PRICE_DECIMALS),
        "range":     round(best_range, PRICE_DECIMALS),
        "range_pct": round(range_pct, 3),
    }


def get_directional_bias(price):
    """
    4H directional bias via master pattern average price.
    Confirmed when SETTLE_CANDLES consecutive 4H closes settle above/below avg.
    Returns: (bias, avg_4h, box_4h)   bias = "long" | "short" | "neutral"
    """
    candles = get_candles(BIAS_TF, 60)
    if len(candles) < 15:
        return "neutral", None, None

    box = find_contraction_box(candles, lookback=BOX_LOOKBACK, window=BOX_WINDOW)
    if not box:
        return "neutral", None, None

    avg    = box["avg"]
    recent = candles[-SETTLE_CANDLES:]
    closes = [c["close"] for c in recent]
    above  = sum(1 for c in closes if c > avg)
    below  = sum(1 for c in closes if c < avg)

    if above >= SETTLE_CANDLES and price > avg:
        return "long", avg, box
    elif below >= SETTLE_CANDLES and price < avg:
        return "short", avg, box
    return "neutral", avg, box


def get_entry_signal(bias, price):
    """
    1H entry signal.
    Entry zone = price on COUNTER-TREND side of 1H average (pullback).
    TP = 1H average (where price should return to).
    Returns: (signal, tp_price, avg_1h, box_1h)
    """
    candles = get_candles(ENTRY_TF, 60)
    if len(candles) < 10:
        return False, None, None, None

    box = find_contraction_box(candles, lookback=20, window=3)
    if not box:
        return False, None, None, None

    avg = box["avg"]

    if bias == "short" and price > avg:
        pip_dist = (price - avg) / PIP_SIZE
        if pip_dist >= MIN_TP_PIPS:
            return True, round(avg, PRICE_DECIMALS), avg, box

    elif bias == "long" and price < avg:
        pip_dist = (avg - price) / PIP_SIZE
        if pip_dist >= MIN_TP_PIPS:
            return True, round(avg, PRICE_DECIMALS), avg, box

    return False, None, avg, box


# == TRADE EXECUTION ===========================================================

def enter_trade(bias, price, tp_price, state):
    """
    Place a pending limit order with TP baked in.
    Lot size is calculated dynamically from current equity.
    """
    equity   = get_wallet_equity_usdt() or 10.0
    lot_size = calculate_lot_size(equity)
    pip_dist = abs(price - tp_price) / PIP_SIZE
    pip_val  = PIP_SIZE * 100_000 * lot_size   # USD per pip for this lot size
    profit_est = pip_dist * pip_val

    if bias == "short":
        entry_price  = round(price * (1 + ENTRY_OFFSET_PCT), PRICE_DECIMALS)
        order_type   = "sell_limit"
    else:
        entry_price  = round(price * (1 - ENTRY_OFFSET_PCT), PRICE_DECIMALS)
        order_type   = "buy_limit"

    ok, ticket, msg = place_pending_order(order_type, entry_price, tp_price, lot_size)
    if not ok:
        send_telegram(f"Entry failed: {msg}")
        append_log("ENTRY_FAIL", {"bias": bias, "price": entry_price, "msg": msg})
        return state

    state["in_trade"]      = True
    state["trade_side"]    = bias
    state["entry_price"]   = entry_price
    state["tp_price"]      = tp_price
    state["entry_ticket"]  = ticket
    state["lot_size"]      = lot_size

    send_telegram(
        f"{'📉' if bias == 'short' else '📈'} <b>ORDER PLACED [{bias.upper()}]</b>\n"
        f"Entry: {entry_price:.{PRICE_DECIMALS}f} | TP: {tp_price:.{PRICE_DECIMALS}f}\n"
        f"Distance: {pip_dist:.1f} pips | Est. profit: ~${profit_est:.2f}\n"
        f"Lots: {lot_size} | Equity: ${equity:.2f} | Risk: {RISK_PCT*100:.0f}%\n"
        f"Exit: TP fill OR 4H bias reversal"
    )
    append_log("ENTRY", {
        "bias": bias, "entry": entry_price, "tp": tp_price,
        "pips": round(pip_dist, 1), "profit_est": round(profit_est, 4),
        "lots": lot_size, "equity": round(equity, 2), "ticket": ticket,
    })
    return state


def exit_trade(state, reason, price):
    """Cancel pending orders and close any open position."""
    cancel_all_bot_orders()
    time.sleep(0.5)
    close_all_positions()

    state["in_trade"]     = False
    state["trade_side"]   = None
    state["entry_price"]  = None
    state["tp_price"]     = None
    state["entry_ticket"] = None
    state["lot_size"]     = None

    send_telegram(
        f"🚪 <b>TRADE CLOSED</b>\n"
        f"Reason: {reason}\n"
        f"Price: {price:.{PRICE_DECIMALS}f}"
    )
    append_log("EXIT", {"reason": reason, "price": price})
    return state


# == LIVE POSITION & PnL TRACKING ==============================================

def update_live_position(state):
    pos = get_position()
    if pos:
        state["live_pnl"]      = round(pos["live_pnl"], 4)
        state["position_side"] = pos["side"]
        state["entry_price"]   = round(pos["entry_price"], PRICE_DECIMALS)
        state["mark_price"]    = round(pos["mark_price"], PRICE_DECIMALS)
    else:
        state["live_pnl"]      = 0.0
        state["position_side"] = None
        state["mark_price"]    = None
        # No position AND no pending order = TP was hit
        orders = get_open_orders()
        if state.get("in_trade") and not orders:
            state["in_trade"]     = False
            state["trade_side"]   = None
            state["entry_ticket"] = None
    return state


def update_fills_and_pnl(state):
    now_ts        = time.time()
    last_check_ts = float(state.get("last_fill_check_ts") or 0)
    if last_check_ts == 0:
        last_check_ts = now_ts - 86400

    deals = get_closed_deals(since_ts=last_check_ts)
    state["last_fill_check_ts"] = now_ts
    if not deals:
        return state

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_pnl_date") != today_str:
        state["daily_pnl"]      = 0.0
        state["daily_pnl_date"] = today_str

    new_fills = 0
    for deal in deals:
        try:
            closed_pnl = float(deal.profit)
            state["total_profit"] = round(state.get("total_profit", 0) + closed_pnl, 6)
            state["lifetime_pnl"] = round(state.get("lifetime_pnl", 0) + closed_pnl, 6)
            state["daily_pnl"]    = round(state.get("daily_pnl", 0) + closed_pnl, 6)
            state["total_fills"]  = state.get("total_fills", 0) + 1
            ts_str = datetime.fromtimestamp(deal.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            record = {
                "ts":      ts_str,
                "side":    "Buy" if deal.type == 1 else "Sell",
                "qty":     float(deal.volume),
                "pnl":     round(closed_pnl, 6),
                "exit_px": float(deal.price),
            }
            hist = state.get("trade_history") or []
            hist.append(record)
            state["trade_history"] = hist[-200:]
            new_fills += 1
            append_log("FILL", record)
        except Exception as e:
            print(f"[fill] {e}")

    if new_fills:
        send_telegram(
            f"<b>{new_fills} fill(s)</b>\n"
            f"Session: ${state['total_profit']:.4f} | Daily: ${state['daily_pnl']:.4f}"
        )
    return state


# == STARTUP RECOVERY ==========================================================

def recover_on_startup(state):
    """On restart, check for open position/orders and restore state."""
    pos    = get_position()
    orders = get_open_orders()

    if not pos and not orders:
        send_telegram("Startup: clean slate — no open position or pending orders.")
        append_log("RECOVERY", {"result": "clean"})
        return state

    if pos:
        state["in_trade"]      = True
        state["position_side"] = pos["side"]
        state["entry_price"]   = round(pos["entry_price"], PRICE_DECIMALS)
        state["trade_side"]    = "short" if pos["side"] == "Sell" else "long"
        state["live_pnl"]      = round(pos["live_pnl"], 4)
        send_telegram(
            f"Recovery: open {pos['side']} position found\n"
            f"Entry: {pos['entry_price']:.{PRICE_DECIMALS}f} | PnL: ${pos['live_pnl']:.2f}"
        )
    elif orders:
        o = orders[0]
        state["in_trade"]     = True
        state["entry_ticket"] = o.ticket
        state["entry_price"]  = round(o.price_open, PRICE_DECIMALS)
        state["tp_price"]     = round(o.tp, PRICE_DECIMALS) if o.tp else None
        state["trade_side"]   = "short" if o.type in (2, 4) else "long"
        send_telegram(
            f"Recovery: pending order found (ticket {o.ticket})\n"
            f"Price: {o.price_open:.{PRICE_DECIMALS}f} | TP: {o.tp:.{PRICE_DECIMALS}f}"
        )

    save_state(state)
    return state


# == CLAUDE AI MARKET ANALYSIS =================================================

def claude_market_analysis(price, bias_4h, avg_4h, avg_1h, state):
    if not _anthropic_lib or not ANTHROPIC_API_KEY:
        return
    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        perf   = performance_summary(state)
        prompt = (
            f"You are a concise forex analyst reviewing a Master Pattern bot on {SYMBOL}.\n\n"
            f"Snapshot:\n"
            f"- Price: {price:.{PRICE_DECIMALS}f}\n"
            f"- 4H Bias: {bias_4h}\n"
            f"- 4H Contraction avg (key level): {avg_4h}\n"
            f"- 1H Average: {avg_1h}\n"
            f"- In trade: {state.get('in_trade')} ({state.get('trade_side')})\n"
            f"- Live PnL: ${state.get('live_pnl', 0):.2f}\n"
            f"- Daily PnL: ${state.get('daily_pnl', 0):.2f}\n"
            f"- Equity: ${state.get('equity', 0):.2f}\n"
            f"- Win rate: {perf['winrate']}% ({perf['wins']}W/{perf['losses']}L)\n\n"
            f"Give exactly 3 lines:\n"
            f"1. Setup quality right now (valid / weak / wait)\n"
            f"2. Key level to watch\n"
            f"3. One risk to be aware of\n"
            f"Be direct. No fluff."
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = response.content[0].text.strip()
        send_telegram(f"<b>AI (30min read)</b>\n{analysis}")
        append_log("CLAUDE_ANALYSIS", {"price": price, "bias": bias_4h, "text": analysis[:300]})
    except Exception as e:
        print(f"[claude_analysis] {e}")


# == MAIN CYCLE ================================================================

def run_cycle():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price = get_price()
    state = load_state()

    state["equity"] = get_wallet_equity_usdt()
    state["price"]  = price
    state = update_live_position(state)
    state = update_fills_and_pnl(state)
    save_state(state)

    if not state.get("trading_enabled", True):
        return

    # == Emergency portfolio stop ==============================================
    live_pnl = state.get("live_pnl") or 0.0
    if live_pnl <= PORTFOLIO_STOPLOSS:
        cancel_all_bot_orders()
        close_all_positions()
        state["in_trade"]        = False
        state["trading_enabled"] = False
        save_state(state)
        send_telegram(
            f"🛑 <b>EMERGENCY STOP</b>\n"
            f"Live PnL ${live_pnl:.2f} <= ${PORTFOLIO_STOPLOSS:.2f}\n"
            f"All closed. Re-enable from dashboard."
        )
        append_log("EMERGENCY_STOP", {"live_pnl": live_pnl, "price": price})
        return

    # == 4H directional bias ==================================================
    bias_4h, avg_4h, box_4h = get_directional_bias(price)
    state["bias_4h"]      = bias_4h
    state["avg_price_4h"] = avg_4h

    # == Claude AI analysis every 30 min ======================================
    now_ts = time.time()
    if now_ts - state.get("last_claude_analysis", 0) >= CLAUDE_INTERVAL:
        claude_market_analysis(price, bias_4h, avg_4h, state.get("avg_price_1h"), state)
        state["last_claude_analysis"] = now_ts

    # == In a trade: check exit conditions =====================================
    if state.get("in_trade"):
        trade_side = state.get("trade_side")
        pos        = get_position()
        orders     = get_open_orders()

        # TP hit: position gone and no more pending orders
        if not pos and not orders:
            state["in_trade"]   = False
            state["trade_side"] = None
            save_state(state)
            send_telegram(
                f"✅ <b>Trade closed</b> (TP hit)\n"
                f"Price: {price:.{PRICE_DECIMALS}f} | Daily: ${state.get('daily_pnl', 0):.4f}"
            )
            return

        # Order filled (position open) — just holding
        if pos and not orders:
            state["entry_ticket"] = None   # no more pending order

        # 4H bias reversed against our position
        bias_reversed = (
            (trade_side == "short" and bias_4h == "long") or
            (trade_side == "long"  and bias_4h == "short")
        )
        if bias_reversed:
            state = exit_trade(state, f"4H bias reversed to {bias_4h.upper()}", price)
            save_state(state)
            return

        pips_from_entry = (
            abs(price - (state.get("entry_price") or price)) / PIP_SIZE
        )
        send_telegram(
            f"{'📉' if trade_side == 'short' else '📈'} <b>HOLDING [{trade_side.upper()}]</b>\n"
            f"Entry: {state.get('entry_price', '?')} | TP: {state.get('tp_price', '?')}\n"
            f"Current: {price:.{PRICE_DECIMALS}f} | PnL: ${live_pnl:.4f}\n"
            f"Pips from entry: {pips_from_entry:.1f} | Lots: {state.get('lot_size', '?')}\n"
            f"4H bias: {bias_4h.upper()}"
        )
        save_state(state)
        return

    # == Not in trade: look for entry =========================================
    if bias_4h == "neutral":
        send_telegram(
            f"⏳ WAITING — 4H neutral\n"
            f"Price: {price:.{PRICE_DECIMALS}f}"
            + (f" | 4H avg: {avg_4h:.{PRICE_DECIMALS}f}" if avg_4h else "")
        )
        save_state(state)
        return

    signal, tp_price, avg_1h, box_1h = get_entry_signal(bias_4h, price)
    state["avg_price_1h"] = avg_1h

    if signal and tp_price:
        state = enter_trade(bias_4h, price, tp_price, state)
    else:
        direction = "below" if bias_4h == "short" else "above"
        avg_str   = f"{avg_1h:.{PRICE_DECIMALS}f}" if avg_1h else "N/A"
        send_telegram(
            f"👀 WATCHING [{bias_4h.upper()}]\n"
            f"4H bias confirmed | Waiting for 1H pullback\n"
            f"Price: {price:.{PRICE_DECIMALS}f} | 1H avg: {avg_str}\n"
            f"Need price {direction} 1H avg"
        )

    save_state(state)


# == RUN LOOP ==================================================================

def run_loop():
    clear_stop()
    send_telegram(
        f"<b>MT5 Master Pattern Bot</b>\n"
        f"Symbol: {SYMBOL} | MT5: {MT5_HOST}:{MT5_PORT}\n"
        f"Risk: {RISK_PCT*100:.0f}% equity/trade | Connecting..."
    )

    # Wait for MT5 to be ready (give it up to 60s on cold start)
    for attempt in range(12):
        try:
            get_mt5()
            break
        except Exception as e:
            print(f"[startup] MT5 not ready yet ({attempt+1}/12): {e}")
            time.sleep(5)
    else:
        send_telegram("MT5 connection failed after 60s. Check MT5 service and VNC login.")
        return

    state = load_state()
    try:
        state = recover_on_startup(state)
    except Exception as e:
        send_telegram(f"Recovery error: {e}")
        append_log("RECOVERY_ERROR", {"error": str(e)})

    equity = get_wallet_equity_usdt() or 0
    lots   = calculate_lot_size(equity)
    send_telegram(
        f"<b>Bot Ready</b>\n"
        f"Equity: ${equity:.2f} | Starting lots: {lots}\n"
        f"Emergency stop: ${PORTFOLIO_STOPLOSS} | Exit: TP or 4H bias reversal"
    )
    append_log("START", {"symbol": SYMBOL, "equity": equity, "risk_pct": RISK_PCT})

    while not is_stop_requested():
        try:
            run_cycle()
        except Exception as e:
            err = f"Error: {type(e).__name__}: {e}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
            append_log("ERROR", {"error": err})
        for _ in range(CHECK_INTERVAL):
            if is_stop_requested():
                break
            time.sleep(1)
    append_log("STOP", {"reason": "stop_flag"})


def main():
    run_loop()


if __name__ == "__main__":
    main()
