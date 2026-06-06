"""
ETH Master Pattern Trading Bot
================================
Strategy: 3-phase master pattern (Contraction → Expansion → Trend)
- 4H timeframe: directional bias (where smart money has settled)
- 1H timeframe: entry signal (counter-trend expansion = entry zone)
- No hard SL: exit only when 4H bias reverses (trend change)
- TP: dynamic, placed at 1H average price (contraction midpoint)
- Single position at a time
- 10x leverage, 0.04 ETH per trade
"""

import requests, json, os, time, hmac, hashlib, traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except Exception:
    anthropic = None

# ── CONFIG ────────────────────────────────────────────────────────────────────
BYBIT_API_KEY      = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL             = "ETHUSDT"

LEVERAGE           = int(os.environ.get("LEVERAGE", "10"))
QTY                = float(os.environ.get("QTY", "0.04"))           # ETH per trade
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "300"))   # seconds between cycles

# Master pattern timeframes
BIAS_TF            = os.environ.get("BIAS_TF", "240")               # 4H — directional bias
ENTRY_TF           = os.environ.get("ENTRY_TF", "60")               # 1H — entry timing
BOX_LOOKBACK       = int(os.environ.get("BOX_LOOKBACK", "40"))      # candles to search for contraction
BOX_WINDOW         = int(os.environ.get("BOX_WINDOW", "5"))         # candles in contraction box
SETTLE_CANDLES     = int(os.environ.get("SETTLE_CANDLES", "2"))     # 4H candles needed to confirm bias
MIN_TP_PROFIT      = float(os.environ.get("MIN_TP_PROFIT", "0.30")) # skip entry if TP < this USD

# Risk
PORTFOLIO_STOPLOSS = float(os.environ.get("PORTFOLIO_STOPLOSS", "-4.0"))  # emergency close all

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")

# ── STOP FLAG ─────────────────────────────────────────────────────────────────
_stop_flag = False
def request_stop():  global _stop_flag; _stop_flag = True
def clear_stop():    global _stop_flag; _stop_flag = False
def is_stop_requested(): return _stop_flag


# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {
        "in_trade":         False,
        "trade_side":       None,       # "long" or "short"
        "entry_price":      None,
        "tp_price":         None,
        "tp_order_id":      None,
        "bias_4h":          None,       # "long", "short", "neutral"
        "avg_price_4h":     None,
        "avg_price_1h":     None,
        "total_profit":     0.0,
        "lifetime_pnl":     0.0,
        "daily_pnl":        0.0,
        "daily_pnl_date":   "",
        "total_fills":      0,
        "trade_history":    [],
        "live_pnl":         None,
        "position_side":    None,
        "mark_price":       None,
        "equity":           None,
        "last_fill_check":  0,
        "trading_enabled":  True,
        "paused_until":     0,
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
    hist  = state.get("trade_history") or []
    pnls  = [float(t.get("pnl") or 0) for t in hist if t.get("pnl") is not None]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p < 0]
    total = len(pnls)
    return {
        "trades":   total,
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  round(len(wins)/total*100, 2) if total else 0,
        "avg_win":  round(sum(wins)/len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses)/len(losses), 4) if losses else 0,
    }


# ── BYBIT AUTH ────────────────────────────────────────────────────────────────

def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1_000_000))

def sign_get(query):
    ts  = get_server_time()
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   (ts + BYBIT_API_KEY + "5000" + query).encode(),
                   hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000"}

def sign_post(params):
    ts   = get_server_time()
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    sig  = hmac.new(BYBIT_API_SECRET.encode(),
                    (ts + BYBIT_API_KEY + "5000" + body).encode(),
                    hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000",
               "Content-Type": "application/json"}
    return headers, body


# ── BYBIT API ─────────────────────────────────────────────────────────────────

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except:
        pass

def get_price():
    r = requests.get(
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}",
        timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles(interval, limit=100):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category=linear"
            f"&symbol={SYMBOL}&interval={interval}&limit={limit}", timeout=10)
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        return [
            {"open": float(c[1]), "high": float(c[2]),
             "low":  float(c[3]), "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(data["result"]["list"])
        ]
    except:
        return []

def get_wallet_equity_usdt():
    for acct in [BYBIT_ACCOUNT_TYPE, "UNIFIED", "CONTRACT"]:
        try:
            query = f"accountType={acct}&coin=USDT"
            r = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}",
                             headers=sign_get(query), timeout=10)
            data = r.json()
            if data.get("retCode") != 0:
                continue
            items = data.get("result", {}).get("list", [])
            if not items:
                continue
            for k in ("totalEquity", "totalWalletBalance"):
                v = items[0].get(k)
                if v not in (None, ""):
                    try:
                        eq = float(v)
                        if eq >= 0:
                            return eq
                    except:
                        pass
        except:
            pass
    return None

def get_position():
    try:
        query = f"category=linear&symbol={SYMBOL}"
        r = requests.get(f"https://api.bybit.com/v5/position/list?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return None
        for pos in data.get("result", {}).get("list", []):
            size = float(pos.get("size") or 0)
            if size > 0:
                return {
                    "side":        pos.get("side"),
                    "size":        size,
                    "entry_price": float(pos.get("avgPrice") or 0),
                    "mark_price":  float(pos.get("markPrice") or 0),
                    "live_pnl":    float(pos.get("unrealisedPnl") or 0),
                    "pos_idx":     int(pos.get("positionIdx", 0)),
                }
        return None
    except Exception as e:
        print(f"[position] {e}")
        return None

def get_open_orders():
    query = f"category=linear&symbol={SYMBOL}&limit=50"
    try:
        r = requests.get(f"https://api.bybit.com/v5/order/realtime?{query}",
                         headers=sign_get(query), timeout=10)
        return r.json().get("result", {}).get("list", [])
    except:
        return []

def cancel_all_orders():
    params = {"category": "linear", "symbol": SYMBOL}
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/cancel-all",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except:
        return {}

def cancel_order(order_id):
    params = {"category": "linear", "symbol": SYMBOL, "orderId": order_id}
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/cancel",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except:
        return {}

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL,
              "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    headers, body = sign_post(params)
    try:
        requests.post("https://api.bybit.com/v5/position/set-leverage",
                      data=body, headers=headers, timeout=10)
    except:
        pass

def place_limit_order(side, price, qty, pos_idx=None, reduce_only=False):
    if pos_idx is None:
        pos_idx = 1 if side == "Buy" else 2
    params = {
        "category":    "linear",
        "symbol":      SYMBOL,
        "side":        side,
        "orderType":   "Limit",
        "qty":         str(round(qty, 3)),
        "price":       str(round(price, 2)),
        "positionIdx": pos_idx,
        "timeInForce": "GTC",
    }
    if reduce_only:
        params["reduceOnly"] = True
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/create",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

def close_position_market(pos_side, qty, pos_idx):
    close_side = "Buy" if pos_side == "Sell" else "Sell"
    params = {
        "category":    "linear",
        "symbol":      SYMBOL,
        "side":        close_side,
        "orderType":   "Market",
        "qty":         str(round(qty, 3)),
        "positionIdx": pos_idx,
        "reduceOnly":  True,
    }
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/create",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

def close_all_positions():
    try:
        query = f"category=linear&symbol={SYMBOL}"
        r = requests.get(f"https://api.bybit.com/v5/position/list?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return
        for pos in data.get("result", {}).get("list", []):
            size = float(pos.get("size") or 0)
            if size <= 0:
                continue
            res = close_position_market(pos["side"], size, int(pos.get("positionIdx", 0)))
            print(f"[close_all] {pos['side']} {size} → {res.get('retCode')} {res.get('retMsg')}")
            time.sleep(0.2)
    except Exception as e:
        print(f"[close_all] {e}")

def get_closed_pnl(start_time_ms=None):
    try:
        query = f"category=linear&symbol={SYMBOL}&limit=50"
        if start_time_ms:
            query += f"&startTime={int(start_time_ms)}"
        r = requests.get(f"https://api.bybit.com/v5/position/closed-pnl?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        return data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []
    except:
        return []


# ── MASTER PATTERN ANALYSIS ───────────────────────────────────────────────────

def find_contraction_box(candles, lookback=40, window=5):
    """
    Find the tightest price range (contraction box) in recent candles.
    The contraction is Phase 1 of the master pattern — the setup.
    Returns: {high, low, avg, range_pct} or None
    """
    if len(candles) < window + 2:
        return None
    recent = candles[-min(lookback, len(candles)):]
    best_range = float('inf')
    best_high = best_low = 0

    for i in range(len(recent) - window + 1):
        subset = recent[i:i + window]
        hi  = max(c['high'] for c in subset)
        lo  = min(c['low']  for c in subset)
        rng = hi - lo
        if rng < best_range:
            best_range = rng
            best_high  = hi
            best_low   = lo

    if best_high == 0:
        return None

    avg       = (best_high + best_low) / 2
    range_pct = (best_range / avg) * 100 if avg > 0 else 0

    return {
        "high":      round(best_high, 2),
        "low":       round(best_low, 2),
        "avg":       round(avg, 2),
        "range":     round(best_range, 2),
        "range_pct": round(range_pct, 3),
    }


def get_directional_bias(price):
    """
    4H directional bias using master pattern average price.
    Phase 3 (trend) confirmed when price settles above/below avg price.
    Returns: (bias, avg_price_4h, box_4h)
      bias: "long", "short", or "neutral"
    """
    candles = get_candles(BIAS_TF, 60)
    if len(candles) < 15:
        return "neutral", None, None

    box = find_contraction_box(candles, lookback=BOX_LOOKBACK, window=BOX_WINDOW)
    if not box:
        return "neutral", None, None

    avg = box["avg"]

    # Check the last SETTLE_CANDLES to see where price has settled
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
    1H entry signal using master pattern.
    Entry zone = when price is on the COUNTER-TREND side of 1H average price.
    This is the expansion phase on the smaller timeframe — smart money's entry.

    Returns: (signal, tp_price, avg_1h, box_1h)
      signal: True if entry conditions met
      tp_price: target price (1H average — where price should return)
    """
    candles = get_candles(ENTRY_TF, 60)
    if len(candles) < 10:
        return False, None, None, None

    box = find_contraction_box(candles, lookback=20, window=3)
    if not box:
        return False, None, None, None

    avg = box["avg"]

    if bias == "short":
        # Price expanded UP above average → counter-trend expansion = entry zone
        # TP = return to average (below current price)
        if price > avg:
            tp = avg
            profit_est = (price - tp) * QTY
            if profit_est >= MIN_TP_PROFIT:
                return True, round(tp, 2), avg, box

    elif bias == "long":
        # Price expanded DOWN below average → counter-trend expansion = entry zone
        # TP = return to average (above current price)
        if price < avg:
            tp = avg
            profit_est = (tp - price) * QTY
            if profit_est >= MIN_TP_PROFIT:
                return True, round(tp, 2), avg, box

    return False, None, avg, box


# ── TRADE EXECUTION ───────────────────────────────────────────────────────────

def enter_trade(bias, price, tp_price, state):
    """
    Enter a single position in the direction of the 4H bias.
    Entry: limit order slightly against price for better fill.
    TP: limit order at 1H average price.
    No SL — exit only on bias reversal.
    """
    set_leverage()

    if bias == "short":
        # Sell limit slightly above current price (catch the expansion bounce)
        entry_price = round(price * 1.001, 2)
        pos_idx     = 2
        tp_side     = "Buy"
    else:  # long
        # Buy limit slightly below current price (catch the expansion dip)
        entry_price = round(price * 0.999, 2)
        pos_idx     = 1
        tp_side     = "Sell"

    # Place entry order
    res = place_limit_order(
        "Sell" if bias == "short" else "Buy",
        entry_price, QTY, pos_idx=pos_idx
    )
    if res.get("retCode") != 0:
        send_telegram(f"❌ Entry failed: {res.get('retMsg')}")
        append_log("ENTRY_FAIL", {"bias": bias, "price": entry_price, "msg": res.get("retMsg")})
        return state

    entry_oid = res["result"]["orderId"]

    # Place TP order (reduce-only, closes position when price returns to avg)
    tp_res = place_limit_order(tp_side, tp_price, QTY, pos_idx=pos_idx, reduce_only=True)
    tp_oid = tp_res["result"]["orderId"] if tp_res.get("retCode") == 0 else None

    profit_est = abs(entry_price - tp_price) * QTY

    state["in_trade"]    = True
    state["trade_side"]  = bias
    state["entry_price"] = entry_price
    state["tp_price"]    = tp_price
    state["tp_order_id"] = tp_oid

    send_telegram(
        f"{'📉' if bias == 'short' else '📈'} <b>TRADE ENTERED [{bias.upper()}]</b>\n"
        f"Entry: ${entry_price:.2f} | TP: ${tp_price:.2f}\n"
        f"Est. profit: ~${profit_est:.2f} | Qty: {QTY} ETH\n"
        f"Exit: TP fill OR 4H bias reversal"
    )
    append_log("ENTRY", {
        "bias": bias, "entry": entry_price,
        "tp": tp_price, "profit_est": round(profit_est, 4)
    })
    return state


def exit_trade(state, reason, price):
    """Close open position with a market order and cancel any open orders."""
    cancel_all_orders()
    time.sleep(0.5)
    close_all_positions()

    state["in_trade"]    = False
    state["trade_side"]  = None
    state["entry_price"] = None
    state["tp_price"]    = None
    state["tp_order_id"] = None

    send_telegram(
        f"🚪 <b>TRADE CLOSED</b>\n"
        f"Reason: {reason}\n"
        f"Price: ${price:.2f}"
    )
    append_log("EXIT", {"reason": reason, "price": price})
    return state


# ── PnL & POSITION TRACKING ───────────────────────────────────────────────────

def update_live_position(state):
    pos = get_position()
    if pos:
        state["live_pnl"]      = round(pos["live_pnl"], 4)
        state["position_side"] = pos["side"]
        state["entry_price"]   = round(pos["entry_price"], 2)
        state["mark_price"]    = round(pos["mark_price"], 2)
    else:
        state["live_pnl"]      = 0.0
        state["position_side"] = None
        state["mark_price"]    = None
        # If we thought we were in a trade but no position exists → TP was hit
        if state.get("in_trade"):
            state["in_trade"]    = False
            state["trade_side"]  = None
            state["tp_order_id"] = None
    return state

def update_fills_and_pnl(state):
    now_ms        = int(time.time() * 1000)
    last_check_ms = int(state.get("last_fill_check") or 0)
    if last_check_ms == 0:
        last_check_ms = now_ms - 24 * 60 * 60 * 1000

    fills = get_closed_pnl(start_time_ms=last_check_ms)
    if not fills:
        state["last_fill_check"] = now_ms
        return state

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_pnl_date") != today_str:
        state["daily_pnl"]      = 0.0
        state["daily_pnl_date"] = today_str

    new_fills = 0
    for fill in fills:
        try:
            fill_ms    = int(fill.get("updatedTime") or fill.get("createdTime") or 0)
            if fill_ms <= last_check_ms:
                continue
            closed_pnl = float(fill.get("closedPnl") or 0)
            state["total_profit"]  = round(state.get("total_profit", 0) + closed_pnl, 6)
            state["lifetime_pnl"]  = round(state.get("lifetime_pnl", 0) + closed_pnl, 6)
            state["daily_pnl"]     = round(state.get("daily_pnl", 0) + closed_pnl, 6)
            state["total_fills"]   = state.get("total_fills", 0) + 1
            trade_record = {
                "ts":      datetime.fromtimestamp(fill_ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "side":    fill.get("side"),
                "qty":     float(fill.get("qty") or 0),
                "pnl":     round(closed_pnl, 6),
                "exit_px": float(fill.get("avgExitPrice") or 0),
            }
            hist = state.get("trade_history") or []
            hist.append(trade_record)
            state["trade_history"] = hist[-200:]
            new_fills += 1
            append_log("FILL", trade_record)
        except Exception as e:
            print(f"[fill] {e}")

    state["last_fill_check"] = now_ms
    if new_fills:
        send_telegram(
            f"📊 <b>{new_fills} fill(s)</b>\n"
            f"Session: ${state['total_profit']:.4f} | Daily: ${state['daily_pnl']:.4f}"
        )
    return state


# ── STARTUP RECOVERY ──────────────────────────────────────────────────────────

def recover_on_startup(state):
    """
    On restart: check for open positions and orders.
    Restore trade state and place missing TP if needed.
    """
    open_orders = get_open_orders()
    pos         = get_position()

    if not pos and not open_orders:
        send_telegram("🔄 Startup: clean slate — no open positions or orders.")
        append_log("RECOVERY", {"result": "clean"})
        return state

    if pos:
        state["in_trade"]      = True
        state["position_side"] = pos["side"]
        state["entry_price"]   = round(pos["entry_price"], 2)
        state["trade_side"]    = "short" if pos["side"] == "Sell" else "long"
        state["live_pnl"]      = round(pos["live_pnl"], 4)

        # Check if TP order exists
        tp_exists = any(
            o.get("reduceOnly") or o.get("side") != pos["side"]
            for o in open_orders
        )

        if not tp_exists:
            # Recalculate TP using 1H average price
            candles_1h = get_candles(ENTRY_TF, 60)
            box_1h     = find_contraction_box(candles_1h, lookback=20, window=3) if candles_1h else None
            if box_1h:
                tp_price = box_1h["avg"]
                tp_side  = "Buy" if pos["side"] == "Sell" else "Sell"
                pos_idx  = 2 if pos["side"] == "Sell" else 1
                res      = place_limit_order(tp_side, tp_price, pos["size"],
                                             pos_idx=pos_idx, reduce_only=True)
                if res.get("retCode") == 0:
                    state["tp_order_id"] = res["result"]["orderId"]
                    state["tp_price"]    = tp_price
                    send_telegram(
                        f"🔧 <b>Recovery: TP placed</b>\n"
                        f"Open {pos['side']} @ ${pos['entry_price']:.2f}\n"
                        f"TP: ${tp_price:.2f} (1H avg price)"
                    )
                    append_log("RECOVERY_TP", {"entry": pos["entry_price"], "tp": tp_price})
        else:
            # Find existing TP order
            for o in open_orders:
                if o.get("side") != pos["side"]:
                    state["tp_order_id"] = o.get("orderId")
                    state["tp_price"]    = float(o.get("price") or 0)
                    break
            send_telegram(
                f"🔄 <b>Recovery: trade restored</b>\n"
                f"Open {pos['side']} @ ${pos['entry_price']:.2f}\n"
                f"TP order found @ ${state.get('tp_price', '?')}"
            )

    save_state(state)
    return state


# ── MAIN CYCLE ────────────────────────────────────────────────────────────────

def run_cycle():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price = get_price()
    state = load_state()

    # Refresh live data
    state["equity"] = get_wallet_equity_usdt()
    state["price"]  = price
    state = update_live_position(state)
    state = update_fills_and_pnl(state)
    save_state(state)

    # Dashboard kill-switch
    if not state.get("trading_enabled", True):
        send_telegram(f"⏹ Trading disabled. Price: ${price:.2f}")
        return

    # ── Emergency portfolio stop ───────────────────────────────────────────
    live_pnl = state.get("live_pnl") or 0.0
    if live_pnl <= PORTFOLIO_STOPLOSS:
        cancel_all_orders()
        close_all_positions()
        state["in_trade"]        = False
        state["trading_enabled"] = False
        save_state(state)
        send_telegram(
            f"🛑 <b>EMERGENCY STOP</b>\n"
            f"Live PnL ${live_pnl:.2f} ≤ ${PORTFOLIO_STOPLOSS:.2f}\n"
            f"All closed. Re-enable from dashboard."
        )
        append_log("EMERGENCY_STOP", {"live_pnl": live_pnl, "price": price})
        return

    # ── Get 4H directional bias (master pattern) ───────────────────────────
    bias_4h, avg_4h, box_4h = get_directional_bias(price)
    state["bias_4h"]      = bias_4h
    state["avg_price_4h"] = avg_4h

    # ── If in a trade: check exit conditions ───────────────────────────────
    if state.get("in_trade"):
        trade_side = state.get("trade_side")
        pos        = get_position()

        if not pos:
            # Position closed — TP was hit or SL triggered
            state["in_trade"]   = False
            state["trade_side"] = None
            save_state(state)
            send_telegram(
                f"✅ <b>Position closed</b> (TP hit or filled)\n"
                f"Price: ${price:.2f} | Daily PnL: ${state.get('daily_pnl', 0):.4f}"
            )
            return

        # Check if 4H bias has reversed against our position
        bias_reversed = (
            (trade_side == "short" and bias_4h == "long") or
            (trade_side == "long"  and bias_4h == "short")
        )

        if bias_reversed:
            state = exit_trade(state, f"4H bias reversed to {bias_4h.upper()}", price)
            save_state(state)
            return

        # Hold — report status
        send_telegram(
            f"{'📉' if trade_side == 'short' else '📈'} <b>HOLDING [{trade_side.upper()}]</b>\n"
            f"Entry: ${state.get('entry_price', '?')} | TP: ${state.get('tp_price', '?')}\n"
            f"Current: ${price:.2f} | PnL: ${live_pnl:.4f}\n"
            f"4H bias: {bias_4h.upper()} | Avg: ${avg_4h:.2f}" if avg_4h else
            f"Current: ${price:.2f} | PnL: ${live_pnl:.4f}\n4H bias: {bias_4h.upper()}"
        )
        return

    # ── Not in trade: look for entry ───────────────────────────────────────
    if bias_4h == "neutral":
        send_telegram(
            f"⏳ <b>WAITING</b> — 4H neutral, no bias yet\n"
            f"Price: ${price:.2f} | 4H avg: ${avg_4h:.2f}" if avg_4h else
            f"Price: ${price:.2f} | Watching for contraction to resolve..."
        )
        return

    # Check 1H for entry signal
    signal, tp_price, avg_1h, box_1h = get_entry_signal(bias_4h, price)
    state["avg_price_1h"] = avg_1h

    if signal and tp_price:
        state = enter_trade(bias_4h, price, tp_price, state)
    else:
        direction = "below" if bias_4h == "short" else "above"
        avg_str   = f"${avg_1h:.2f}" if avg_1h else "N/A"
        send_telegram(
            f"👀 <b>WATCHING [{bias_4h.upper()}]</b>\n"
            f"4H bias confirmed | Waiting for 1H expansion\n"
            f"Price: ${price:.2f} | 1H avg: {avg_str}\n"
            f"Need price {direction} 1H avg to enter"
        )

    save_state(state)


# ── RUN LOOP ──────────────────────────────────────────────────────────────────

def run_loop():
    clear_stop()
    send_telegram(
        f"🤖 <b>ETH Master Pattern Bot</b>\n"
        f"Checking existing positions..."
    )

    state = load_state()
    try:
        state = recover_on_startup(state)
    except Exception as e:
        send_telegram(f"⚠️ Recovery error: {e}")
        append_log("RECOVERY_ERROR", {"error": str(e)})

    send_telegram(
        f"🤖 <b>Master Pattern Bot Ready</b>\n"
        f"{QTY} ETH | {LEVERAGE}x leverage | {CHECK_INTERVAL//60}min checks\n"
        f"4H bias TF: {BIAS_TF} | 1H entry TF: {ENTRY_TF}\n"
        f"Emergency stop: ${PORTFOLIO_STOPLOSS} | No hard SL (exits on bias reversal)"
    )
    append_log("START", {
        "qty": QTY, "leverage": LEVERAGE, "bias_tf": BIAS_TF, "entry_tf": ENTRY_TF
    })

    while not is_stop_requested():
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {e}"
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
