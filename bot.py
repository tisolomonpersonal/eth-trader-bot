"""
EURUSD Master Pattern Trading Bot — Bybit V5 API Edition
=========================================================
Bybit TradFi (Forex/Metals) uses category="linear" on the standard V5 REST API.
No MT5, no Wine, no Docker bridge needed.

Strategy: 3-phase master pattern (Contraction -> Expansion -> Trend)
- 4H timeframe: directional bias
- 1H timeframe: entry signal (pullback to counter-trend side of 1H avg)
- TP set directly on the pending limit order
- Single position at a time
- Dynamic lot sizing: risks RISK_PCT of equity per trade
- Skips cycles when forex market is closed (weekends)
"""

import hashlib, hmac, json, os, time, traceback, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import anthropic as _anthropic_lib
except Exception:
    _anthropic_lib = None

# == CONFIG ====================================================================
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://api.bybit.com"
CATEGORY = "linear"   # Bybit TradFi lives under the linear category
SYMBOL   = os.environ.get("SYMBOL", "EURUSD")

# Forex precision
PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", "5"))
PIP_SIZE       = float(os.environ.get("PIP_SIZE", "0.0001"))

# Position sizing
RISK_PCT         = float(os.environ.get("RISK_PCT", "0.01"))        # 1%
MAX_ADVERSE_PIPS = int(os.environ.get("MAX_ADVERSE_PIPS", "100"))
MIN_LOT          = float(os.environ.get("MIN_LOT", "0.01"))
MAX_LOT          = float(os.environ.get("MAX_LOT", "10.0"))
CONTRACT_SIZE    = int(os.environ.get("CONTRACT_SIZE", "100000"))   # units/lot

# Entry offsets
ENTRY_OFFSET_PCT = float(os.environ.get("ENTRY_OFFSET_PCT", "0.001"))
MIN_TP_PIPS      = int(os.environ.get("MIN_TP_PIPS", "10"))

# Master pattern timeframes (Bybit kline intervals: "1","5","15","30","60","240","D")
BIAS_TF        = os.environ.get("BIAS_TF", "240")
ENTRY_TF       = os.environ.get("ENTRY_TF", "60")
BOX_LOOKBACK   = int(os.environ.get("BOX_LOOKBACK", "40"))
BOX_WINDOW     = int(os.environ.get("BOX_WINDOW", "5"))
SETTLE_CANDLES = int(os.environ.get("SETTLE_CANDLES", "2"))

# Timing
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "60"))
CLAUDE_INTERVAL    = int(os.environ.get("CLAUDE_INTERVAL", "1800"))
PORTFOLIO_STOPLOSS = float(os.environ.get("PORTFOLIO_STOPLOSS", "-4.0"))

STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")


# == BYBIT REST API ============================================================

def _sign(method, params):
    ts  = str(int(time.time() * 1000))
    recv_window = "5000"
    if method == "GET":
        payload = urllib.parse.urlencode(params)
    else:
        payload = json.dumps(params)
    sign_str = ts + BYBIT_API_KEY + recv_window + payload
    sig = hmac.new(BYBIT_API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type":       "application/json",
    }
    return headers, payload


def _get(endpoint, params=None):
    params = params or {}
    headers, _ = _sign("GET", params)
    r = requests.get(BASE_URL + endpoint, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(endpoint, params=None):
    params = params or {}
    headers, body = _sign("POST", params)
    r = requests.post(BASE_URL + endpoint, data=body, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


# == MARKET HOURS ==============================================================

def is_market_open():
    """Forex (EURUSD) is closed Saturdays and Sunday before 22:00 UTC."""
    now = datetime.now(timezone.utc)
    wd  = now.weekday()   # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
    h   = now.hour
    if wd == 5:             return False   # Saturday — always closed
    if wd == 6 and h < 22:  return False   # Sunday before 22:00 UTC
    if wd == 4 and h >= 22: return False   # Friday after 22:00 UTC
    return True


# == STOP FLAG =================================================================

_stop_flag = False
def request_stop():      global _stop_flag; _stop_flag = True
def clear_stop():        global _stop_flag; _stop_flag = False
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
        "in_trade":             False,
        "trade_side":           None,
        "entry_price":          None,
        "tp_price":             None,
        "order_id":             None,
        "lot_size":             None,
        "bias_4h":              None,
        "avg_price_4h":         None,
        "avg_price_1h":         None,
        "total_profit":         0.0,
        "lifetime_pnl":         0.0,
        "daily_pnl":            0.0,
        "daily_pnl_date":       "",
        "total_fills":          0,
        "trade_history":        [],
        "live_pnl":             None,
        "position_side":        None,
        "mark_price":           None,
        "equity":               None,
        "last_fill_check_ts":   0,
        "trading_enabled":      True,
        "paused_until":         0,
        "last_claude_analysis": 0,
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
        "winrate":  round(len(wins) / total * 100, 2) if total else 0,
        "avg_win":  round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
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


# == MARKET DATA ===============================================================

def get_price():
    r = _get("/v5/market/tickers", {"category": CATEGORY, "symbol": SYMBOL})
    if r.get("retCode") != 0:
        raise Exception(f"Tickers error: {r.get('retMsg')}")
    lst = r["result"]["list"]
    if not lst:
        raise Exception(f"No ticker for {SYMBOL}")
    t   = lst[0]
    ask = float(t.get("ask1Price") or t.get("lastPrice") or 0)
    bid = float(t.get("bid1Price") or t.get("lastPrice") or 0)
    if ask == 0 and bid == 0:
        raise Exception(f"Zero prices for {SYMBOL} — market may be closed")
    return (ask + bid) / 2.0


def get_candles(interval, limit=100):
    try:
        r = _get("/v5/market/kline", {
            "category": CATEGORY,
            "symbol":   SYMBOL,
            "interval": str(interval),
            "limit":    limit,
        })
        if r.get("retCode") != 0:
            return []
        # rows: [timestamp, open, high, low, close, volume, turnover] — newest first
        return [
            {"open": float(row[1]), "high": float(row[2]),
             "low":  float(row[3]), "close": float(row[4])}
            for row in reversed(r["result"]["list"])
        ]
    except Exception as e:
        print(f"[get_candles] {e}")
        return []


def get_wallet_equity_usdt():
    try:
        r = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if r.get("retCode") != 0:
            return None
        for acct in r["result"]["list"]:
            eq = acct.get("totalEquity")
            if eq is not None:
                return float(eq)
        return None
    except:
        return None


def get_position():
    try:
        r = _get("/v5/position/list", {"category": CATEGORY, "symbol": SYMBOL})
        if r.get("retCode") != 0:
            return None
        for pos in r["result"]["list"]:
            size = float(pos.get("size", 0))
            if size > 0:
                return {
                    "side":        pos["side"],
                    "size":        size,
                    "entry_price": float(pos["avgPrice"]),
                    "mark_price":  float(pos.get("markPrice") or pos["avgPrice"]),
                    "live_pnl":    float(pos.get("unrealisedPnl", 0)),
                }
        return None
    except Exception as e:
        print(f"[get_position] {e}")
        return None


def get_open_orders():
    try:
        r = _get("/v5/order/realtime", {"category": CATEGORY, "symbol": SYMBOL})
        if r.get("retCode") != 0:
            return []
        return r["result"]["list"]
    except:
        return []


# == ORDER EXECUTION ===========================================================

def calculate_lot_size(equity):
    if not equity or equity <= 0:
        return MIN_LOT
    risk_amount   = equity * RISK_PCT
    pip_value_lot = PIP_SIZE * CONTRACT_SIZE   # $ per pip per 1 lot
    lot = risk_amount / (MAX_ADVERSE_PIPS * pip_value_lot)
    return max(MIN_LOT, min(round(lot, 2), MAX_LOT))


def place_pending_order(order_type_str, entry_price, tp_price, lot_size):
    """
    Place a Buy Limit or Sell Limit with TP baked in.
    Returns: (success, order_id, message)
    """
    try:
        side = "Buy" if order_type_str == "buy_limit" else "Sell"
        params = {
            "category":    CATEGORY,
            "symbol":      SYMBOL,
            "side":        side,
            "orderType":   "Limit",
            "qty":         str(round(lot_size, 2)),
            "price":       str(round(entry_price, PRICE_DECIMALS)),
            "takeProfit":  str(round(tp_price, PRICE_DECIMALS)),
            "tpTriggerBy": "LastPrice",
            "timeInForce": "GTC",
            "positionIdx": 0,
        }
        r = _post("/v5/order/create", params)
        if r.get("retCode") != 0:
            return False, None, r.get("retMsg", "Unknown error")
        return True, r["result"]["orderId"], "ok"
    except Exception as e:
        return False, None, str(e)


def cancel_all_bot_orders():
    try:
        _post("/v5/order/cancel-all", {"category": CATEGORY, "symbol": SYMBOL})
    except Exception as e:
        print(f"[cancel_all] {e}")


def close_all_positions():
    try:
        pos = get_position()
        if not pos:
            return
        close_side = "Sell" if pos["side"] == "Buy" else "Buy"
        _post("/v5/order/create", {
            "category":    CATEGORY,
            "symbol":      SYMBOL,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(pos["size"]),
            "timeInForce": "IOC",
            "positionIdx": 0,
            "reduceOnly":  True,
        })
    except Exception as e:
        print(f"[close_all] {e}")


def get_closed_pnl(since_ts=None):
    try:
        r = _get("/v5/position/closed-pnl", {"category": CATEGORY, "symbol": SYMBOL, "limit": "50"})
        if r.get("retCode") != 0:
            return []
        items = r["result"]["list"]
        if since_ts:
            items = [x for x in items if int(x.get("updatedTime", 0)) / 1000 > since_ts]
        return items
    except Exception as e:
        print(f"[closed_pnl] {e}")
        return []


# == MASTER PATTERN ANALYSIS ===================================================

def find_contraction_box(candles, lookback=40, window=5):
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
    equity   = get_wallet_equity_usdt() or 10.0
    lot_size = calculate_lot_size(equity)
    pip_dist = abs(price - tp_price) / PIP_SIZE
    pip_val  = PIP_SIZE * CONTRACT_SIZE * lot_size
    profit_est = pip_dist * pip_val

    if bias == "short":
        entry_price = round(price * (1 + ENTRY_OFFSET_PCT), PRICE_DECIMALS)
        order_type  = "sell_limit"
    else:
        entry_price = round(price * (1 - ENTRY_OFFSET_PCT), PRICE_DECIMALS)
        order_type  = "buy_limit"

    ok, order_id, msg = place_pending_order(order_type, entry_price, tp_price, lot_size)
    if not ok:
        send_telegram(f"Entry failed: {msg}")
        append_log("ENTRY_FAIL", {"bias": bias, "price": entry_price, "msg": msg})
        return state

    state["in_trade"]    = True
    state["trade_side"]  = bias
    state["entry_price"] = entry_price
    state["tp_price"]    = tp_price
    state["order_id"]    = order_id
    state["lot_size"]    = lot_size

    send_telegram(
        f"{'📉' if bias == 'short' else '📈'} <b>ORDER PLACED [{bias.upper()}]</b>\n"
        f"Entry: {entry_price:.{PRICE_DECIMALS}f} | TP: {tp_price:.{PRICE_DECIMALS}f}\n"
        f"Distance: {pip_dist:.1f} pips | Est. profit: ~${profit_est:.2f}\n"
        f"Lots: {lot_size} | Equity: ${equity:.2f} | Risk: {RISK_PCT*100:.0f}%"
    )
    append_log("ENTRY", {
        "bias": bias, "entry": entry_price, "tp": tp_price,
        "pips": round(pip_dist, 1), "profit_est": round(profit_est, 4),
        "lots": lot_size, "equity": round(equity, 2), "order_id": order_id,
    })
    return state


def exit_trade(state, reason, price):
    cancel_all_bot_orders()
    time.sleep(0.5)
    close_all_positions()

    state["in_trade"]    = False
    state["trade_side"]  = None
    state["entry_price"] = None
    state["tp_price"]    = None
    state["order_id"]    = None
    state["lot_size"]    = None

    send_telegram(
        f"🚪 <b>TRADE CLOSED</b>\nReason: {reason}\nPrice: {price:.{PRICE_DECIMALS}f}"
    )
    append_log("EXIT", {"reason": reason, "price": price})
    return state


# == LIVE TRACKING =============================================================

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
        orders = get_open_orders()
        if state.get("in_trade") and not orders:
            state["in_trade"]    = False
            state["trade_side"]  = None
            state["order_id"]    = None
    return state


def update_fills_and_pnl(state):
    now_ts        = time.time()
    last_check_ts = float(state.get("last_fill_check_ts") or 0)
    if last_check_ts == 0:
        last_check_ts = now_ts - 86400

    items = get_closed_pnl(since_ts=last_check_ts)
    state["last_fill_check_ts"] = now_ts
    if not items:
        return state

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_pnl_date") != today_str:
        state["daily_pnl"]      = 0.0
        state["daily_pnl_date"] = today_str

    new_fills = 0
    for item in items:
        try:
            closed_pnl = float(item.get("closedPnl", 0))
            state["total_profit"] = round(state.get("total_profit", 0) + closed_pnl, 6)
            state["lifetime_pnl"] = round(state.get("lifetime_pnl", 0) + closed_pnl, 6)
            state["daily_pnl"]    = round(state.get("daily_pnl", 0) + closed_pnl, 6)
            state["total_fills"]  = state.get("total_fills", 0) + 1
            ts_str = datetime.fromtimestamp(
                int(item.get("updatedTime", 0)) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            record = {
                "ts":    ts_str,
                "side":  item.get("side"),
                "qty":   float(item.get("qty", 0)),
                "pnl":   round(closed_pnl, 6),
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
    pos    = get_position()
    orders = get_open_orders()

    if not pos and not orders:
        send_telegram("Startup: clean — no open position or pending orders.")
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
        state["in_trade"]    = True
        state["order_id"]    = o.get("orderId")
        state["entry_price"] = float(o.get("price", 0))
        state["tp_price"]    = float(o.get("takeProfit", 0)) or None
        state["trade_side"]  = "short" if o.get("side") == "Sell" else "long"
        send_telegram(f"Recovery: pending order found (id {o.get('orderId')})")

    save_state(state)
    return state


# == CLAUDE AI ANALYSIS ========================================================

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
            f"- 4H Contraction avg: {avg_4h}\n"
            f"- 1H Average: {avg_1h}\n"
            f"- In trade: {state.get('in_trade')} ({state.get('trade_side')})\n"
            f"- Live PnL: ${state.get('live_pnl', 0):.2f}\n"
            f"- Daily PnL: ${state.get('daily_pnl', 0):.2f}\n"
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
    if not is_market_open():
        print("[cycle] Market closed — skipping.")
        return

    price = get_price()
    state = load_state()

    state["equity"] = get_wallet_equity_usdt()
    state["price"]  = price
    state = update_live_position(state)
    state = update_fills_and_pnl(state)
    save_state(state)

    if not state.get("trading_enabled", True):
        return

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
        append_log("EMERGENCY_STOP", {"live_pnl": live_pnl})
        return

    bias_4h, avg_4h, box_4h = get_directional_bias(price)
    state["bias_4h"]      = bias_4h
    state["avg_price_4h"] = avg_4h

    now_ts = time.time()
    if now_ts - state.get("last_claude_analysis", 0) >= CLAUDE_INTERVAL:
        claude_market_analysis(price, bias_4h, avg_4h, state.get("avg_price_1h"), state)
        state["last_claude_analysis"] = now_ts

    if state.get("in_trade"):
        trade_side = state.get("trade_side")
        pos        = get_position()
        orders     = get_open_orders()

        if not pos and not orders:
            state["in_trade"]   = False
            state["trade_side"] = None
            save_state(state)
            send_telegram(
                f"✅ <b>Trade closed</b> (TP hit)\n"
                f"Price: {price:.{PRICE_DECIMALS}f} | Daily: ${state.get('daily_pnl', 0):.4f}"
            )
            return

        bias_reversed = (
            (trade_side == "short" and bias_4h == "long") or
            (trade_side == "long"  and bias_4h == "short")
        )
        if bias_reversed:
            state = exit_trade(state, f"4H bias reversed to {bias_4h.upper()}", price)
            save_state(state)
            return

        pips_from_entry = abs(price - (state.get("entry_price") or price)) / PIP_SIZE
        send_telegram(
            f"{'📉' if trade_side == 'short' else '📈'} <b>HOLDING [{trade_side.upper()}]</b>\n"
            f"Entry: {state.get('entry_price', '?')} | TP: {state.get('tp_price', '?')}\n"
            f"Current: {price:.{PRICE_DECIMALS}f} | PnL: ${live_pnl:.4f}\n"
            f"Pips from entry: {pips_from_entry:.1f} | 4H bias: {bias_4h.upper()}"
        )
        save_state(state)
        return

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
            f"Price: {price:.{PRICE_DECIMALS}f} | 1H avg: {avg_str}\n"
            f"Need price {direction} 1H avg"
        )

    save_state(state)


# == RUN LOOP ==================================================================

def run_loop():
    clear_stop()
    send_telegram(
        f"<b>EURUSD Master Pattern Bot — Bybit V5</b>\n"
        f"Symbol: {SYMBOL} | Category: {CATEGORY}\n"
        f"Risk: {RISK_PCT*100:.0f}% equity/trade | Starting..."
    )

    # Quick connectivity check
    try:
        price = get_price()
        send_telegram(f"Connected. {SYMBOL} mid: {price:.{PRICE_DECIMALS}f}")
    except Exception as e:
        send_telegram(f"⚠️ Price check failed: {e}\nBot will retry each cycle.")

    state = load_state()
    try:
        state = recover_on_startup(state)
    except Exception as e:
        send_telegram(f"Recovery error: {e}")

    equity = get_wallet_equity_usdt() or 0
    lots   = calculate_lot_size(equity)
    send_telegram(
        f"<b>Bot Ready</b>\n"
        f"Equity: ${equity:.2f} | Lots: {lots}\n"
        f"Emergency stop: ${PORTFOLIO_STOPLOSS}"
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
