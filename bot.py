import requests, json, os, time, hmac, hashlib
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except Exception:
    anthropic = None

# --- CONFIG ---
BYBIT_API_KEY     = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET  = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL            = "ETHUSDT"
LEVERAGE          = int(os.environ.get("LEVERAGE", "30"))
CHECK_INTERVAL    = int(os.environ.get("CHECK_INTERVAL", "300"))   # 5 min

# Grid settings
GRID_LEVELS       = int(os.environ.get("GRID_LEVELS", "2"))
GRID_SPACING_PCT  = float(os.environ.get("GRID_SPACING_PCT", "0.004"))   # 0.4% per level
QTY_PER_LEVEL     = float(os.environ.get("QTY_PER_LEVEL", "0.04"))       # ETH per order

# Regime thresholds
ADX_PERIOD        = int(os.environ.get("ADX_PERIOD", "14"))
ADX_SIDEWAYS_MAX  = float(os.environ.get("ADX_SIDEWAYS_MAX", "25"))      # below = sideways
ADX_TREND_MIN     = float(os.environ.get("ADX_TREND_MIN", "25"))         # above = trend active
ADX_EXTREME_EXIT  = float(os.environ.get("ADX_EXTREME_EXIT", "80"))      # Layer 3: violent move exit
BB_WIDTH_MIN      = float(os.environ.get("BB_WIDTH_MIN", "0.005"))
BB_WIDTH_MAX      = float(os.environ.get("BB_WIDTH_MAX", "0.025"))

# Risk management
LIVE_PNL_STOPLOSS   = float(os.environ.get("LIVE_PNL_STOPLOSS", "-4.0"))    # Layer 2: close all if PnL < this
MAX_POSITION_ETH    = float(os.environ.get("MAX_POSITION_ETH", "0.12"))      # stop grid if position >= this
TARGET_PROFIT_USD   = float(os.environ.get("TARGET_PROFIT_USD", "1.0"))      # fixed TP target per trade in USD
BREAKEVEN_THRESHOLD = float(os.environ.get("BREAKEVEN_THRESHOLD", "0.50"))   # move SL to entry once profit >= $X

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")

# ── thread-stop flag ─────────────────────────────────────────────────────────
_stop_flag = False

def request_stop():
    global _stop_flag
    _stop_flag = True

def clear_stop():
    global _stop_flag
    _stop_flag = False

def is_stop_requested():
    return _stop_flag


# ── state helpers ────────────────────────────────────────────────────────────

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {
        "grid_active":          False,
        "grid_mode":            "neutral",   # "neutral" | "uptrend" | "downtrend"
        "center_price":         None,
        "grid_upper":           None,
        "grid_lower":           None,
        "tracked_orders":       {},          # {order_id: {side, price, qty, pos_idx, center}}
        "counter_placed":       {},          # {filled_order_id: True}  — prevent double counter
        "total_profit":         0.0,
        "lifetime_pnl":         0.0,
        "total_fills":          0,
        "trade_history":        [],
        "last_placed":          None,
        "trading_enabled":      True,
        "paused_until":         0,
        "pause_reason":         "",
        "equity":               None,
        "daily_pnl":            0.0,
        "daily_pnl_date":       "",
        "consecutive_loss":     0.0,
        "live_pnl":             None,
        "position_side":        None,
        "entry_price":          None,
        "mark_price":           None,
        "last_fill_check_time": 0,
        "waiting_for_trend_change": False,
        "stopped_mode":         None,   # mode that was active when loss hit
    }

def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def append_log(event, payload):
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
    pnls  = []
    for t in hist:
        try:
            pnls.append(float(t.get("pnl") or 0.0))
        except:
            pass
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = len(pnls)
    return {
        "trades":   total,
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  round((len(wins) / total) * 100, 2) if total else 0.0,
        "avg_win":  round(sum(wins) / len(wins), 4)    if wins   else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "last_pnl": round(pnls[-1], 4) if pnls else 0.0,
    }


# ── Bybit helpers ────────────────────────────────────────────────────────────

def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1000000))

def sign_get(query):
    ts  = get_server_time()
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   (ts + BYBIT_API_KEY + "5000" + query).encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000"}

def sign_post(params):
    ts   = get_server_time()
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    sig  = hmac.new(BYBIT_API_SECRET.encode(),
                    (ts + BYBIT_API_KEY + "5000" + body).encode(), hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000",
               "Content-Type": "application/json"}
    return headers, body

def get_wallet_equity_usdt():
    for acct in [BYBIT_ACCOUNT_TYPE, "CONTRACT", "UNIFIED", "SPOT"]:
        try:
            query   = f"accountType={acct}&coin=USDT"
            headers = sign_get(query)
            r    = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}",
                                headers=headers, timeout=10)
            data = r.json()
            if data.get("retCode") != 0:
                continue
            items = data.get("result", {}).get("list", [])
            if not items:
                continue
            item = items[0]
            for k in ("totalEquity", "totalWalletBalance", "totalMarginBalance"):
                v = item.get(k)
                if v not in (None, ""):
                    try:
                        eq = float(v)
                        if eq >= 0:
                            return eq
                    except:
                        pass
            for c in (item.get("coin") or []):
                if (c.get("coin") or "").upper() == "USDT":
                    for k in ("equity", "walletBalance", "availableToWithdraw", "availableBalance"):
                        v = c.get(k)
                        if v not in (None, ""):
                            try:
                                eq = float(v)
                                if eq >= 0:
                                    return eq
                            except:
                                pass
        except Exception as e:
            print(f"[wallet] {acct} error: {e}")
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
                }
        return None
    except Exception as e:
        print(f"[position] error: {e}")
        return None

def get_closed_pnl(start_time_ms=None):
    try:
        query = f"category=linear&symbol={SYMBOL}&limit=50"
        if start_time_ms:
            query += f"&startTime={int(start_time_ms)}"
        r = requests.get(f"https://api.bybit.com/v5/position/closed-pnl?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return []
        return data.get("result", {}).get("list", [])
    except Exception as e:
        print(f"[closed_pnl] error: {e}")
        return []

def get_filled_orders(start_time_ms=None):
    """Fetch recently filled orders from order history."""
    try:
        query = f"category=linear&symbol={SYMBOL}&orderStatus=Filled&limit=50"
        if start_time_ms:
            query += f"&startTime={int(start_time_ms)}"
        r = requests.get(f"https://api.bybit.com/v5/order/history?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return []
        return data.get("result", {}).get("list", [])
    except Exception as e:
        print(f"[filled_orders] error: {e}")
        return []

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
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles(interval="60", limit=100):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}"
            f"&interval={interval}&limit={limit}", timeout=10)
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        return [
            {"open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]),  "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(data["result"]["list"])
        ]
    except:
        return []

def get_open_orders():
    query = f"category=linear&symbol={SYMBOL}&limit=50"
    r = requests.get(f"https://api.bybit.com/v5/order/realtime?{query}",
                     headers=sign_get(query), timeout=10)
    try:
        return r.json().get("result", {}).get("list", [])
    except:
        return []

def cancel_all_orders():
    params = {"category": "linear", "symbol": SYMBOL}
    headers, body = sign_post(params)
    r = requests.post("https://api.bybit.com/v5/order/cancel-all",
                      data=body, headers=headers, timeout=10)
    return r.json()

def cancel_order(order_id):
    """Cancel a single order by ID."""
    params = {"category": "linear", "symbol": SYMBOL, "orderId": order_id}
    headers, body = sign_post(params)
    r = requests.post("https://api.bybit.com/v5/order/cancel",
                      data=body, headers=headers, timeout=10)
    return r.json()

def cancel_grid_orders_only(state):
    """
    Cancel only the grid (entry) orders — preserve TP/counter orders so open
    positions keep their take-profit protection.
    Falls back to cancel_all if tracked_orders is empty.
    """
    tracked = state.get("tracked_orders", {})
    if not tracked:
        return cancel_all_orders()

    tp_ids      = {oid for oid, info in tracked.items() if info.get("is_counter")}
    grid_ids    = {oid for oid in tracked if oid not in tp_ids}

    # Also cancel any open orders we don't have in tracked (safety net)
    open_orders = get_open_orders()
    open_ids    = {o["orderId"] for o in open_orders}
    unknown_ids = open_ids - set(tracked.keys())
    to_cancel   = (grid_ids | unknown_ids) & open_ids

    cancelled = 0
    for oid in to_cancel:
        res = cancel_order(oid)
        if res.get("retCode") == 0:
            cancelled += 1
        time.sleep(0.1)

    # Remove cancelled grid orders from tracked; keep TPs
    for oid in grid_ids:
        tracked.pop(oid, None)
    state["tracked_orders"] = tracked

    print(f"[cancel_grid] cancelled {cancelled} grid orders, preserved {len(tp_ids)} TP orders")
    return {"cancelled": cancelled, "tp_preserved": len(tp_ids)}

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL,
              "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    headers, body = sign_post(params)
    requests.post("https://api.bybit.com/v5/position/set-leverage",
                  data=body, headers=headers, timeout=10)

def place_limit_order(side, price_level, qty, pos_idx=None):
    """
    pos_idx: 0=one-way, 1=long hedge, 2=short hedge.
    For uptrend grid: buys and their counter-sells use pos_idx=1 (long side).
    For downtrend grid: sells and their counter-buys use pos_idx=2 (short side).
    For neutral grid: buys=1, sells=2.
    """
    if pos_idx is None:
        pos_idx = 1 if side == "Buy" else 2
    params = {
        "category":    "linear",
        "symbol":      SYMBOL,
        "side":        side,
        "orderType":   "Limit",
        "qty":         str(round(qty, 3)),
        "price":       str(round(price_level, 2)),
        "positionIdx": pos_idx,
        "timeInForce": "GTC",
    }
    headers, body = sign_post(params)
    r = requests.post("https://api.bybit.com/v5/order/create",
                      data=body, headers=headers, timeout=10)
    return r.json()


# ── risk management helpers ───────────────────────────────────────────────────

def set_position_stop_loss(pos_idx, stop_price):
    """Set a hard stop loss on an open position."""
    try:
        params = {
            "category":    "linear",
            "symbol":      SYMBOL,
            "stopLoss":    str(round(stop_price, 2)),
            "slTriggerBy": "LastPrice",
            "positionIdx": pos_idx,
        }
        headers, body = sign_post(params)
        r = requests.post("https://api.bybit.com/v5/position/trading-stop",
                          data=body, headers=headers, timeout=10)
        res = r.json()
        if res.get("retCode") != 0:
            print(f"[sl] set SL failed: {res.get('retMsg')}")
        return res
    except Exception as e:
        print(f"[sl] error: {e}")
        return {}

def set_position_trailing_stop(pos_idx, trail_distance, active_price):
    """
    Set a Bybit trailing stop on an open position.
    trail_distance: price distance the market must reverse before exit (e.g. $7.52)
    active_price:   price at which trailing activates (must be in-profit direction)
    Bybit then follows price, exiting only when it reverses by trail_distance.
    """
    try:
        params = {
            "category":      "linear",
            "symbol":        SYMBOL,
            "trailingStop":  str(round(trail_distance, 2)),
            "activePrice":   str(round(active_price, 2)),
            "positionIdx":   pos_idx,
        }
        headers, body = sign_post(params)
        r = requests.post("https://api.bybit.com/v5/position/trading-stop",
                          data=body, headers=headers, timeout=10)
        res = r.json()
        if res.get("retCode") != 0:
            print(f"[trail] set trailing stop failed: {res.get('retMsg')}")
        return res
    except Exception as e:
        print(f"[trail] error: {e}")
        return {}

def close_position_market(pos_side, qty, pos_idx):
    """Close an open position immediately with a market order."""
    try:
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
        r = requests.post("https://api.bybit.com/v5/order/create",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[close_pos] error: {e}")
        return {}

def close_all_positions():
    """Market-close every open position for ETHUSDT."""
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
            pos_side = pos.get("side")
            pos_idx  = int(pos.get("positionIdx", 0))
            res = close_position_market(pos_side, size, pos_idx)
            print(f"[close_all] {pos_side} {size} ETH → {res.get('retCode')} {res.get('retMsg')}")
            time.sleep(0.2)
    except Exception as e:
        print(f"[close_all] error: {e}")

def get_total_position_size():
    """Return total open ETH position size across all sides."""
    try:
        query = f"category=linear&symbol={SYMBOL}"
        r = requests.get(f"https://api.bybit.com/v5/position/list?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return 0.0
        total = sum(
            float(p.get("size") or 0)
            for p in data.get("result", {}).get("list", [])
        )
        return round(total, 4)
    except Exception as e:
        print(f"[pos_size] error: {e}")
        return 0.0

# ── indicators ───────────────────────────────────────────────────────────────

def _wilder(vals, period):
    s = sum(vals[:period])
    out = [s]
    for v in vals[period:]:
        s = s - s / period + v
        out.append(s)
    return out

def _dm_tr(candles):
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(candles)):
        hd = candles[i]["high"] - candles[i-1]["high"]
        ld = candles[i-1]["low"] - candles[i]["low"]
        plus_dm.append(hd if hd > ld and hd > 0 else 0)
        minus_dm.append(ld if ld > hd and ld > 0 else 0)
        tr.append(max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"]),
        ))
    return plus_dm, minus_dm, tr

def calculate_adx(candles, period=14):
    if len(candles) < period * 2:
        return 50
    plus_dm, minus_dm, tr = _dm_tr(candles)
    tr_s    = _wilder(tr,       period)
    plus_s  = _wilder(plus_dm,  period)
    minus_s = _wilder(minus_dm, period)
    dx_list = []
    for i in range(len(tr_s)):
        if tr_s[i] == 0:
            continue
        plus_di  = 100 * plus_s[i]  / tr_s[i]
        minus_di = 100 * minus_s[i] / tr_s[i]
        di_sum   = plus_di + minus_di
        dx_list.append(100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0)
    if len(dx_list) < period:
        return 50
    return round(sum(dx_list[-period:]) / period, 2)

def calculate_di(candles, period=14):
    """Return (plus_di, minus_di) for the most recent bar."""
    if len(candles) < period * 2:
        return 0.0, 0.0
    plus_dm, minus_dm, tr = _dm_tr(candles)
    tr_s    = _wilder(tr,       period)
    plus_s  = _wilder(plus_dm,  period)
    minus_s = _wilder(minus_dm, period)
    if not tr_s or tr_s[-1] == 0:
        return 0.0, 0.0
    plus_di  = round(100 * plus_s[-1]  / tr_s[-1], 2)
    minus_di = round(100 * minus_s[-1] / tr_s[-1], 2)
    return plus_di, minus_di

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    recent = closes[-period:]
    sma    = sum(recent) / period
    std    = (sum((p - sma) ** 2 for p in recent) / period) ** 0.5
    return round(sma, 2), round(sma + 2*std, 2), round(sma - 2*std, 2)

def detect_di_crossover(candles, period=14):
    """
    Detect if DI lines just crossed in the last 2 bars — signals a FRESH trend,
    not a mature one. Returns ('up', True/False) or ('down', True/False).
    fresh=True means the cross happened in the last 2 candles.
    """
    if len(candles) < period * 2 + 2:
        return None, False

    def di_pair(subset):
        plus_dm, minus_dm, tr = _dm_tr(subset)
        tr_s    = _wilder(tr,       period)
        plus_s  = _wilder(plus_dm,  period)
        minus_s = _wilder(minus_dm, period)
        if not tr_s or tr_s[-1] == 0:
            return 0.0, 0.0
        return (100 * plus_s[-1] / tr_s[-1],
                100 * minus_s[-1] / tr_s[-1])

    plus_now,  minus_now  = di_pair(candles)
    plus_prev, minus_prev = di_pair(candles[:-1])

    if plus_now > minus_now and plus_prev <= minus_prev:
        return "up",   True   # just crossed bullish
    if minus_now > plus_now and minus_prev <= plus_prev:
        return "down", True   # just crossed bearish
    if plus_now > minus_now:
        return "up",   False  # ongoing uptrend, not fresh
    if minus_now > plus_now:
        return "down", False  # ongoing downtrend, not fresh
    return None, False

def detect_regime(candles, price):
    """
    Returns (mode, reason, indicators) where mode is:
      'neutral'   – sideways, use symmetric grid
      'uptrend'   – trending up, use directional buy-only grid
      'downtrend' – trending down, use directional sell-only grid
    """
    closes  = [c["close"] for c in candles]
    adx     = calculate_adx(candles, ADX_PERIOD)
    plus_di, minus_di = calculate_di(candles, ADX_PERIOD)
    mid, upper, lower = calculate_bollinger(closes)
    bb_width = (upper - lower) / mid if mid > 0 else 0

    di_direction, di_fresh = detect_di_crossover(candles)
    indicators = {
        "adx":         adx,
        "plus_di":     plus_di,
        "minus_di":    minus_di,
        "di_fresh":    di_fresh,       # True = DI just crossed (fresh signal)
        "di_direction": di_direction,
        "bb_width_pct": round(bb_width * 100, 3),
        "bb_upper":    upper,
        "bb_lower":    lower,
        "bb_mid":      mid,
    }

    # Sideways: low ADX AND BB width in range
    if adx < ADX_SIDEWAYS_MAX and BB_WIDTH_MIN <= bb_width <= BB_WIDTH_MAX:
        return "neutral", f"ADX {adx} + BB {bb_width*100:.2f}% — sideways", indicators

    # Trending: ADX above threshold
    if adx >= ADX_TREND_MIN:
        if plus_di > minus_di:
            return "uptrend", f"ADX {adx} DI+ {plus_di} > DI- {minus_di} — uptrend", indicators
        else:
            return "downtrend", f"ADX {adx} DI- {minus_di} > DI+ {plus_di} — downtrend", indicators

    # Edge: ADX ambiguous — treat as sideways but skip if BB out of range
    if bb_width < BB_WIDTH_MIN:
        return "neutral", f"ADX {adx} — BB too tight ({bb_width*100:.2f}%), skipping", indicators
    if bb_width > BB_WIDTH_MAX:
        return "neutral", f"ADX {adx} — BB too wide ({bb_width*100:.2f}%), skipping", indicators

    return "neutral", f"ADX {adx} — default neutral", indicators


# ── fill & PnL tracking ───────────────────────────────────────────────────────

def update_fills_and_pnl(state):
    now_ms         = int(time.time() * 1000)
    last_check_ms  = int(state.get("last_fill_check_time") or 0)
    if last_check_ms == 0:
        last_check_ms = now_ms - 24 * 60 * 60 * 1000

    fills = get_closed_pnl(start_time_ms=last_check_ms)
    if not fills:
        state["last_fill_check_time"] = now_ms
        return state

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_pnl_date") != today_str:
        state["daily_pnl"] = 0.0
        state["daily_pnl_date"] = today_str

    new_fills   = 0
    consecutive = float(state.get("consecutive_loss") or 0.0)

    for fill in fills:
        try:
            fill_time_ms = int(fill.get("updatedTime") or fill.get("createdTime") or 0)
            if fill_time_ms <= last_check_ms:
                continue
            closed_pnl = float(fill.get("closedPnl") or 0.0)
            qty        = float(fill.get("qty") or 0.0)
            side       = fill.get("side", "")
            avg_exit   = float(fill.get("avgExitPrice") or 0.0)

            state["total_profit"]  = round(state.get("total_profit", 0.0) + closed_pnl, 6)
            state["lifetime_pnl"]  = round(state.get("lifetime_pnl", 0.0) + closed_pnl, 6)
            state["daily_pnl"]     = round(state.get("daily_pnl", 0.0) + closed_pnl, 6)
            state["total_fills"]   = state.get("total_fills", 0) + 1
            consecutive = 0.0 if closed_pnl >= 0 else round(consecutive + abs(closed_pnl), 6)

            trade_record = {
                "ts":      datetime.fromtimestamp(fill_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "side":    side,
                "qty":     qty,
                "pnl":     round(closed_pnl, 6),
                "exit_px": avg_exit,
            }
            hist = state.get("trade_history") or []
            hist.append(trade_record)
            state["trade_history"] = hist[-200:]
            new_fills += 1
            append_log("FILL", trade_record)
        except Exception as e:
            print(f"[fill] parse error: {e}")

    state["consecutive_loss"]     = consecutive
    state["last_fill_check_time"] = now_ms

    if new_fills:
        send_telegram(
            f"📊 <b>{new_fills} fill(s) detected</b>\n"
            f"Session profit: ${state['total_profit']:.4f} | Daily: ${state['daily_pnl']:.4f}\n"
            f"Consecutive loss: ${consecutive:.4f}"
        )
    return state

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
        state["entry_price"]   = None
        state["mark_price"]    = None
    return state


# ── counter-order placement (directional grids) ───────────────────────────────

def check_and_place_counter_orders(state, price):
    """
    Places a TP counter-order whenever a grid order fills:
    - Any mode: buy fill  → sell TP at fill_price + spacing  (close long for profit)
    - Any mode: sell fill → buy  TP at fill_price - spacing  (close short for profit)
    Detects fills by comparing tracked order IDs against currently open orders.
    """
    grid_mode = state.get("grid_mode", "neutral")

    tracked   = state.get("tracked_orders", {})
    countered = state.get("counter_placed", {})

    if not tracked:
        return state

    # Get currently open order IDs
    open_orders  = get_open_orders()
    open_ids     = {o["orderId"] for o in open_orders}

    # Also check Bybit filled order history to confirm fill (not cancel)
    filled_history = get_filled_orders(
        start_time_ms=int(state.get("last_fill_check_time") or 0) - 60_000
    )
    filled_ids = {o["orderId"] for o in filled_history}

    center = state.get("center_price") or price
    spacing_abs = center * GRID_SPACING_PCT

    new_counter_count = 0

    for order_id, info in list(tracked.items()):
        if order_id in countered:
            continue                       # counter already placed
        if order_id in open_ids:
            continue                       # still open
        if order_id not in filled_ids:
            continue                       # likely cancelled, not filled — skip

        fill_price = float(info.get("price", price))
        qty        = float(info.get("qty", QTY_PER_LEVEL))
        pos_idx    = int(info.get("pos_idx", 1))

        side = info.get("side")

        # TP distance: target $1 profit, minimum 1× spacing
        # $1 / qty = price move needed. Use whichever is larger.
        tp_distance = max(TARGET_PROFIT_USD / qty, spacing_abs)
        sl_distance = tp_distance   # 1:1 risk/reward

        # Buy filled → fixed TP sell above + SL below
        if side == "Buy" and grid_mode in ("uptrend", "neutral"):
            tp_price = round(fill_price + tp_distance, 2)
            sl_price = round(fill_price - sl_distance, 2)
            res = place_limit_order("Sell", tp_price, qty, pos_idx=pos_idx)
            if res.get("retCode") == 0:
                counter_id = res["result"]["orderId"]
                tracked[counter_id] = {"side": "Sell", "price": tp_price, "qty": qty,
                                       "pos_idx": pos_idx, "center": center, "is_counter": True}
                countered[order_id] = True
                new_counter_count += 1
                set_position_stop_loss(pos_idx, sl_price)
                append_log("TP_SELL", {"mode": grid_mode, "fill": fill_price,
                                       "tp": tp_price, "sl": sl_price, "target_usd": TARGET_PROFIT_USD})
                send_telegram(
                    f"🎯 <b>TP SELL placed</b> [{grid_mode.upper()}]\n"
                    f"Buy filled @ ${fill_price:.2f}\n"
                    f"TP: ${tp_price:.2f} (+${tp_distance:.2f}) | SL: ${sl_price:.2f}\n"
                    f"Target profit: ~${round(tp_distance * qty, 2)}"
                )
            else:
                print(f"[tp] sell failed: {res.get('retMsg')}")

        # Sell filled → fixed TP buy below + SL above
        elif side == "Sell" and grid_mode in ("downtrend", "neutral"):
            tp_price = round(fill_price - tp_distance, 2)
            sl_price = round(fill_price + sl_distance, 2)
            res = place_limit_order("Buy", tp_price, qty, pos_idx=pos_idx)
            if res.get("retCode") == 0:
                counter_id = res["result"]["orderId"]
                tracked[counter_id] = {"side": "Buy", "price": tp_price, "qty": qty,
                                       "pos_idx": pos_idx, "center": center, "is_counter": True}
                countered[order_id] = True
                new_counter_count += 1
                set_position_stop_loss(pos_idx, sl_price)
                append_log("TP_BUY", {"mode": grid_mode, "fill": fill_price,
                                      "tp": tp_price, "sl": sl_price, "target_usd": TARGET_PROFIT_USD})
                send_telegram(
                    f"🎯 <b>TP BUY placed</b> [{grid_mode.upper()}]\n"
                    f"Sell filled @ ${fill_price:.2f}\n"
                    f"TP: ${tp_price:.2f} (-${tp_distance:.2f}) | SL: ${sl_price:.2f}\n"
                    f"Target profit: ~${round(tp_distance * qty, 2)}"
                )
            else:
                print(f"[tp] buy failed: {res.get('retMsg')}")

    state["tracked_orders"] = tracked
    state["counter_placed"] = countered
    return state


# ── margin helpers ────────────────────────────────────────────────────────────

def get_available_margin():
    """Return free USDT margin available for new orders."""
    try:
        query   = "accountType=UNIFIED&coin=USDT"
        headers = sign_get(query)
        r = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}",
                         headers=headers, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return None
        items = data.get("result", {}).get("list", [])
        if not items:
            return None
        for c in (items[0].get("coin") or []):
            if (c.get("coin") or "").upper() == "USDT":
                for k in ("availableToWithdraw", "availableBalance"):
                    v = c.get(k)
                    if v not in (None, ""):
                        try:
                            return float(v)
                        except:
                            pass
        # fallback to total equity
        v = items[0].get("totalEquity")
        return float(v) if v not in (None, "") else None
    except Exception as e:
        print(f"[margin] error: {e}")
        return None

def margin_required(price, qty):
    """Estimated margin for one order at given price/qty with leverage."""
    return (price * qty) / LEVERAGE

# ── grid placement functions ──────────────────────────────────────────────────

def place_neutral_grid(center_price, state):
    """Symmetric grid: buys below + sells above. TP placed on each fill."""
    tracked = {}
    placed_buys, placed_sells = [], []
    available = get_available_margin()
    skipped = 0

    for i in range(1, GRID_LEVELS + 1):
        buy_price  = center_price * (1 - i * GRID_SPACING_PCT)
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)

        needed = margin_required(buy_price, QTY_PER_LEVEL)
        if available is not None and available < needed * 2 + 0.5:  # buffer for both sides
            skipped += 1
            append_log("MARGIN_SKIP", {"level": i, "available": available, "needed": needed * 2})
            continue

        res = place_limit_order("Buy", buy_price, QTY_PER_LEVEL, pos_idx=1)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_buys.append(round(buy_price, 2))
            tracked[oid] = {"side": "Buy", "price": round(buy_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 1, "center": center_price}
        else:
            append_log("ORDER_FAIL", {"side": "Buy", "price": buy_price, "msg": res.get("retMsg")})

        res = place_limit_order("Sell", sell_price, QTY_PER_LEVEL, pos_idx=2)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_sells.append(round(sell_price, 2))
            tracked[oid] = {"side": "Sell", "price": round(sell_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 2, "center": center_price}
        else:
            append_log("ORDER_FAIL", {"side": "Sell", "price": sell_price, "msg": res.get("retMsg")})

        time.sleep(0.15)

    if skipped:
        send_telegram(f"⚠️ Neutral grid: skipped {skipped} level(s) — insufficient margin (${available:.2f} free)")

    state["tracked_orders"] = tracked
    state["counter_placed"] = {}
    return placed_buys, placed_sells, state


def place_uptrend_grid(center_price, state):
    """Directional buy-only grid. TP sell placed on each fill."""
    tracked = {}
    placed_buys = []
    available = get_available_margin()
    skipped = 0

    for i in range(1, GRID_LEVELS + 1):
        buy_price = center_price * (1 - i * GRID_SPACING_PCT)
        needed = margin_required(buy_price, QTY_PER_LEVEL)
        if available is not None and available < needed + 0.5:
            skipped += 1
            append_log("MARGIN_SKIP", {"level": i, "available": available, "needed": needed})
            continue

        res = place_limit_order("Buy", buy_price, QTY_PER_LEVEL, pos_idx=1)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_buys.append(round(buy_price, 2))
            tracked[oid] = {"side": "Buy", "price": round(buy_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 1, "center": center_price}
        else:
            append_log("ORDER_FAIL", {"side": "Buy", "price": buy_price, "msg": res.get("retMsg")})
        time.sleep(0.15)

    if skipped:
        send_telegram(f"⚠️ Uptrend grid: skipped {skipped} level(s) — insufficient margin (${available:.2f} free)")

    state["tracked_orders"] = tracked
    state["counter_placed"] = {}
    return placed_buys, state


def place_downtrend_grid(center_price, state):
    """Directional sell-only grid. TP buy placed on each fill."""
    tracked = {}
    placed_sells = []
    available = get_available_margin()
    skipped = 0

    for i in range(1, GRID_LEVELS + 1):
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)
        needed = margin_required(sell_price, QTY_PER_LEVEL)
        if available is not None and available < needed + 0.5:
            skipped += 1
            append_log("MARGIN_SKIP", {"level": i, "available": available, "needed": needed})
            continue

        res = place_limit_order("Sell", sell_price, QTY_PER_LEVEL, pos_idx=2)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_sells.append(round(sell_price, 2))
            tracked[oid] = {"side": "Sell", "price": round(sell_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 2, "center": center_price}
        else:
            append_log("ORDER_FAIL", {"side": "Sell", "price": sell_price, "msg": res.get("retMsg")})
        time.sleep(0.15)

    if skipped:
        send_telegram(f"⚠️ Downtrend grid: skipped {skipped} level(s) — insufficient margin (${available:.2f} free)")

    state["tracked_orders"] = tracked
    state["counter_placed"] = {}
    return placed_sells, state


# ── trailing grid logic ───────────────────────────────────────────────────────

def should_trail(state, price, mode):
    """
    Returns True if the grid should be re-centered (trailing).
    - Uptrend: re-center when price breaks above upper bound.
    - Downtrend: re-center when price breaks below lower bound.
    - Neutral: re-center when price leaves either bound.
    """
    if not state.get("grid_active"):
        return False, "not active"
    upper = state.get("grid_upper")
    lower = state.get("grid_lower")
    if upper is None or lower is None:
        return False, "no bounds"

    if mode == "uptrend" and price > upper:
        return True, f"Trailing up: price ${price:.2f} broke upper ${upper:.2f}"
    if mode == "downtrend" and price < lower:
        return True, f"Trailing down: price ${price:.2f} broke lower ${lower:.2f}"
    if mode == "neutral" and (price > upper or price < lower):
        return True, f"Price ${price:.2f} left range ${lower:.2f}–${upper:.2f}"

    return False, "within range"


# ── Claude decision ───────────────────────────────────────────────────────────

def ask_claude_grid(price, indicators, open_count, state, mode):
    if not ANTHROPIC_API_KEY or anthropic is None:
        if not state.get("grid_active") or open_count == 0:
            return "PLACE", "No Claude key — auto placing grid.", price
        return "SKIP", "No Claude key — grid already active.", price

    mode_desc = {
        "neutral":   "Symmetric grid (buys below + sells above). Market is sideways.",
        "uptrend":   "Directional BUY-ONLY grid below price. Counter sells placed on fill. Riding uptrend.",
        "downtrend": "Directional SELL-ONLY grid above price. Counter buys placed on fill. Riding downtrend.",
    }.get(mode, mode)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are managing a grid trading bot for ETH/USDT perpetual on Bybit.

Current price: ${price:.2f}
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
Current mode: {mode.upper()} — {mode_desc}

Market indicators:
- ADX: {indicators['adx']} | DI+: {indicators.get('plus_di')} | DI-: {indicators.get('minus_di')}
- DI Cross: {'🆕 FRESH cross just happened' if indicators.get('di_fresh') else '⏳ Mature trend — cross was earlier'}
- BB Upper: ${indicators['bb_upper']} | Mid: ${indicators['bb_mid']} | Lower: ${indicators['bb_lower']}
- BB Width: {indicators['bb_width_pct']}%

Grid config: {GRID_LEVELS} levels | {GRID_SPACING_PCT*100:.2f}% spacing | {QTY_PER_LEVEL} ETH/order | {LEVERAGE}x leverage
Open orders: {open_count}
Session profit: ${state.get('total_profit', 0):.4f} | Fills: {state.get('total_fills', 0)}
Daily PnL: ${state.get('daily_pnl', 0):.4f}

Decide:
- PLACE: place/rebuild the grid now
- SKIP: grid is working, leave it
- CANCEL: cancel grid (conditions changed)

Respond ONLY in this format:
DECISION: PLACE or SKIP or CANCEL
REASON: (1-2 sentences)
CENTER_PRICE: $X.XX"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text     = msg.content[0].text
        decision = "SKIP"
        reason   = ""
        center   = price
        for line in text.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("DECISION:"):
                decision = line.split(":", 1)[1].strip().upper()
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.upper().startswith("CENTER_PRICE:"):
                try:
                    center = float(line.split(":", 1)[1].replace("$", "").strip())
                except:
                    center = price
        return decision, reason, center
    except Exception as e:
        return "SKIP", f"Claude error: {e}", price


# ── startup recovery ─────────────────────────────────────────────────────────

def recover_on_startup():
    """
    Called once at bot start.
    1. Fetches open orders and live position from Bybit.
    2. If open orders exist, reconstructs grid state (center, bounds, tracked IDs, mode).
    3. If an open position exists with no matching TP order, places the TP immediately.
    4. Sends a Telegram summary of what was found and what action was taken.
    """
    state = load_state()

    open_orders = get_open_orders()
    position    = get_position()

    has_orders   = len(open_orders) > 0
    has_position = position is not None

    if not has_orders and not has_position:
        send_telegram("🔄 <b>Startup recovery:</b> No open orders or positions found. Starting fresh.")
        append_log("RECOVERY", {"result": "clean_start"})
        return

    # ── Reconstruct grid state from open orders ───────────────────────────────
    if has_orders:
        prices    = [float(o["price"]) for o in open_orders if o.get("price")]
        sides     = [o.get("side") for o in open_orders]
        buy_only  = all(s == "Buy"  for s in sides)
        sell_only = all(s == "Sell" for s in sides)
        mixed     = not buy_only and not sell_only

        center_price = round(sum(prices) / len(prices), 2)
        grid_upper   = round(max(prices) * (1 + GRID_SPACING_PCT), 2)
        grid_lower   = round(min(prices) * (1 - GRID_SPACING_PCT), 2)

        # Infer mode from order composition
        if buy_only:
            mode = "uptrend"
        elif sell_only:
            mode = "downtrend"
        else:
            mode = "neutral"

        # Rebuild tracked_orders from open order list
        tracked = {}
        for o in open_orders:
            oid = o.get("orderId")
            if not oid:
                continue
            tracked[oid] = {
                "side":    o.get("side"),
                "price":   float(o.get("price") or 0),
                "qty":     float(o.get("qty") or QTY_PER_LEVEL),
                "pos_idx": 1 if o.get("side") == "Buy" else 2,
                "center":  center_price,
            }

        state["grid_active"]    = True
        state["grid_mode"]      = mode
        state["center_price"]   = center_price
        state["grid_upper"]     = grid_upper
        state["grid_lower"]     = grid_lower
        state["tracked_orders"] = tracked
        state["counter_placed"] = state.get("counter_placed", {})

        send_telegram(
            f"🔄 <b>Startup recovery: Grid restored</b>\n"
            f"Mode: {mode.upper()} | {len(open_orders)} open orders\n"
            f"Center: ${center_price:.2f} | Range: ${grid_lower}–${grid_upper}\n"
            f"Prices: {sorted(prices)}"
        )
        append_log("RECOVERY", {
            "result": "grid_restored", "mode": mode,
            "orders": len(open_orders), "center": center_price,
        })

    # ── Handle open position with no TP order ────────────────────────────────
    if has_position:
        pos_side    = position["side"]           # "Buy" or "Sell"
        entry_price = position["entry_price"]
        qty         = position["size"]
        spacing_abs = entry_price * GRID_SPACING_PCT

        # Check if there's already a TP order on the opposite side
        tp_exists = any(
            o.get("side") != pos_side
            for o in open_orders
        )

        if not tp_exists:
            # Place missing TP
            if pos_side == "Buy":
                tp_price = round(entry_price + spacing_abs, 2)
                res = place_limit_order("Sell", tp_price, qty, pos_idx=1)
                tp_side_label = "SELL"
            else:
                tp_price = round(entry_price - spacing_abs, 2)
                res = place_limit_order("Buy", tp_price, qty, pos_idx=2)
                tp_side_label = "BUY"

            if res.get("retCode") == 0:
                new_oid = res["result"]["orderId"]
                tracked = state.get("tracked_orders", {})
                tracked[new_oid] = {
                    "side":       "Sell" if pos_side == "Buy" else "Buy",
                    "price":      tp_price,
                    "qty":        qty,
                    "pos_idx":    1 if pos_side == "Buy" else 2,
                    "center":     entry_price,
                    "is_counter": True,
                }
                state["tracked_orders"] = tracked
                send_telegram(
                    f"🎯 <b>Recovery TP placed</b>\n"
                    f"Open {pos_side} position @ ${entry_price:.2f} ({qty} ETH)\n"
                    f"→ TP {tp_side_label} placed @ ${tp_price:.2f}"
                )
                append_log("RECOVERY_TP", {
                    "pos_side": pos_side, "entry": entry_price,
                    "tp_price": tp_price, "qty": qty,
                })
            else:
                send_telegram(
                    f"⚠️ <b>Recovery TP FAILED</b>\n"
                    f"Open {pos_side} @ ${entry_price:.2f} — could not place TP: {res.get('retMsg')}"
                )
        else:
            send_telegram(
                f"✅ <b>Recovery:</b> Open {pos_side} position @ ${entry_price:.2f} "
                f"already has a TP order — no action needed."
            )

    # Update live position fields in state
    if has_position:
        state["live_pnl"]      = round(position["live_pnl"], 4)
        state["position_side"] = position["side"]
        state["entry_price"]   = round(position["entry_price"], 2)
        state["mark_price"]    = round(position["mark_price"], 2)

    save_state(state)


# ── main cycle ────────────────────────────────────────────────────────────────

def run_cycle():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price = get_price()
    state = load_state()

    # Refresh equity + live position
    state["equity"] = get_wallet_equity_usdt()
    state["price"]  = price
    state = update_live_position(state)
    state = update_fills_and_pnl(state)
    save_state(state)

    # ── Trend-change watcher (runs while paused after a loss) ────────────────
    if state.get("waiting_for_trend_change"):
        candles = get_candles("60", 100)
        if len(candles) >= 30:
            current_mode, regime_reason, indicators = detect_regime(candles, price)
            stopped_mode = state.get("stopped_mode", "neutral")

            # Trend has changed if mode is different from what caused the loss
            trend_changed = current_mode != stopped_mode

            if trend_changed:
                state["waiting_for_trend_change"] = False
                state["trading_enabled"]          = True
                state["stopped_mode"]             = None
                save_state(state)
                send_telegram(
                    f"✅ <b>TREND CHANGED — Resuming</b>\n"
                    f"Was: {stopped_mode.upper()} → Now: {current_mode.upper()}\n"
                    f"ADX: {indicators['adx']} | DI+: {indicators.get('plus_di')} | DI-: {indicators.get('minus_di')}\n"
                    f"Price: ${price:.2f} | {regime_reason}"
                )
                append_log("TREND_CHANGE_RESUME", {
                    "from": stopped_mode, "to": current_mode,
                    "adx": indicators["adx"], "price": price,
                })
                # Fall through to normal cycle with new mode
            else:
                send_telegram(
                    f"👀 <b>Watching for trend change</b>\n"
                    f"Still {current_mode.upper()} (same as stopped mode)\n"
                    f"ADX: {indicators['adx']} | DI+: {indicators.get('plus_di')} | DI-: {indicators.get('minus_di')}\n"
                    f"Price: ${price:.2f} — not re-entering yet."
                )
                return
        else:
            return  # not enough candles, wait

    if not state.get("trading_enabled", True):
        send_telegram(f"⏹ Trading disabled by dashboard. Price: ${price:.2f}")
        return

    # ── Layer 2: Live PnL hard stop ──────────────────────────────────────────
    live_pnl = state.get("live_pnl") or 0.0
    if live_pnl <= LIVE_PNL_STOPLOSS and not state.get("waiting_for_trend_change"):
        cancel_all_orders()
        close_all_positions()
        current_mode = state.get("grid_mode", "neutral")
        state["grid_active"]              = False
        state["trading_enabled"]          = False
        state["tracked_orders"]           = {}
        state["counter_placed"]           = {}
        state["waiting_for_trend_change"] = True
        state["stopped_mode"]             = current_mode
        save_state(state)
        send_telegram(
            f"🛑 <b>HARD STOP TRIGGERED</b>\n"
            f"Live PnL ${live_pnl:.2f} ≤ limit ${LIVE_PNL_STOPLOSS:.2f}\n"
            f"All positions closed. Was in {current_mode.upper()} mode.\n"
            f"Watching for trend change before re-entering..."
        )
        append_log("HARD_STOP", {"live_pnl": live_pnl, "limit": LIVE_PNL_STOPLOSS,
                                  "price": price, "stopped_mode": current_mode})
        return

    # ── Daily loss auto-disable ──────────────────────────────────────────────
    daily_pnl = state.get("daily_pnl") or 0.0
    if daily_pnl <= -2.0 and state.get("trading_enabled", True) and not state.get("waiting_for_trend_change"):
        current_mode = state.get("grid_mode", "neutral")
        state["trading_enabled"]          = False
        state["waiting_for_trend_change"] = True
        state["stopped_mode"]             = current_mode
        save_state(state)
        send_telegram(
            f"⏸ <b>DAILY LOSS LIMIT</b>\n"
            f"Daily PnL ${daily_pnl:.2f} — paused in {current_mode.upper()} mode.\n"
            f"Watching for trend change before re-entering..."
        )
        return

    candles = get_candles("60", 100)
    if len(candles) < 30:
        send_telegram(f"⚠️ Not enough candle data: {len(candles)}")
        return

    mode, regime_reason, indicators = detect_regime(candles, price)
    prev_mode = state.get("grid_mode", "neutral")

    # ── Breakeven SL: once position profit >= threshold, move SL to entry ────
    pos = get_position()
    if pos:
        entry   = pos["entry_price"]
        size    = pos["size"]
        upnl    = pos["live_pnl"]
        pos_idx = 1 if pos["side"] == "Buy" else 2
        if upnl >= BREAKEVEN_THRESHOLD:
            be_res = set_position_stop_loss(pos_idx, entry)
            if be_res.get("retCode") == 0:
                append_log("BREAKEVEN_SET", {"entry": entry, "upnl": upnl, "pos_idx": pos_idx})
                send_telegram(
                    f"🔒 <b>Breakeven SL set</b>\n"
                    f"Position profit ${upnl:.2f} ≥ ${BREAKEVEN_THRESHOLD:.2f}\n"
                    f"SL moved to entry ${entry:.2f} — trade can no longer lose."
                )

    # ── Layer 3: ADX extreme exit — violent market move ──────────────────────
    if indicators["adx"] >= ADX_EXTREME_EXIT:
        cancel_all_orders()
        close_all_positions()
        state["grid_active"]    = False
        state["tracked_orders"] = {}
        state["counter_placed"] = {}
        save_state(state)
        send_telegram(
            f"⚡ <b>EXTREME ADX EXIT</b>\n"
            f"ADX {indicators['adx']} ≥ {ADX_EXTREME_EXIT} — violent market move detected.\n"
            f"All positions closed. Will resume next cycle."
        )
        append_log("EXTREME_EXIT", {"adx": indicators["adx"], "price": price})
        return

    # ── Position size gate: stop new grid if already at max exposure ─────────
    total_position = get_total_position_size()
    at_max_position = total_position >= MAX_POSITION_ETH
    if at_max_position:
        send_telegram(
            f"⚠️ <b>MAX POSITION REACHED</b>\n"
            f"Open: {total_position} ETH ≥ limit {MAX_POSITION_ETH} ETH\n"
            f"Waiting for TPs to fill before placing new grid."
        )
        append_log("MAX_POSITION", {"size": total_position, "limit": MAX_POSITION_ETH})

    # ── Mode switch: cancel grid orders if regime changed (keep TPs) ─────────
    if state.get("grid_active") and mode != prev_mode:
        cancel_grid_orders_only(state)
        state["grid_active"]  = False
        state["center_price"] = None
        save_state(state)
        send_telegram(
            f"🔀 <b>REGIME CHANGE</b> {prev_mode.upper()} → {mode.upper()}\n"
            f"Price: ${price:.2f} | {regime_reason}\nGrid orders cancelled — TP orders kept."
        )
        append_log("REGIME_CHANGE", {"from": prev_mode, "to": mode, "price": price})

    state["grid_mode"] = mode

    # ── TP/SL monitoring: always runs if there are tracked orders OR open position
    # Intentionally NOT gated on grid_active — TPs must be managed even when
    # the grid is paused (e.g. max position reached, trailing re-center, etc.)
    has_tracked = bool(state.get("tracked_orders"))
    has_position = (state.get("position_side") is not None)

    if has_tracked or has_position:
        state = check_and_place_counter_orders(state, price)

        # Safety net: if there's an open position with no TP in tracked orders,
        # place one now (covers cases where TP was missed or never set)
        tp_ids = {oid for oid, info in state.get("tracked_orders", {}).items()
                  if info.get("is_counter")}
        pos = get_position()
        if pos and not tp_ids:
            fill_price  = pos["entry_price"]
            qty         = pos["size"]
            pos_side    = pos["side"]
            spacing_abs = fill_price * GRID_SPACING_PCT
            tp_distance = max(TARGET_PROFIT_USD / qty, spacing_abs)

            if pos_side == "Buy":
                pos_idx  = 1
                tp_price = round(fill_price + tp_distance, 2)
                sl_price = round(fill_price - tp_distance, 2)
                res = place_limit_order("Sell", tp_price, qty, pos_idx=pos_idx)
            else:
                pos_idx  = 2
                tp_price = round(fill_price - tp_distance, 2)
                sl_price = round(fill_price + tp_distance, 2)
                res = place_limit_order("Buy", tp_price, qty, pos_idx=pos_idx)

            if res.get("retCode") == 0:
                new_oid = res["result"]["orderId"]
                tracked = state.get("tracked_orders", {})
                tracked[new_oid] = {
                    "side":       "Sell" if pos_side == "Buy" else "Buy",
                    "price":      tp_price, "qty": qty,
                    "pos_idx":    pos_idx, "center": fill_price, "is_counter": True,
                }
                state["tracked_orders"] = tracked
                set_position_stop_loss(pos_idx, sl_price)
                send_telegram(
                    f"🔧 <b>TP/SL restored</b>\n"
                    f"Open {pos_side} @ ${fill_price:.2f} ({qty} ETH)\n"
                    f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f}\n"
                    f"Target: ~${round(tp_distance * qty, 2)}"
                )
                append_log("TP_RESTORED", {"pos_side": pos_side, "entry": fill_price,
                                           "tp": tp_price, "sl": sl_price})

        save_state(state)

    # ── Trailing: re-center if price left grid range (keep TP orders) ────────
    trail, trail_reason = should_trail(state, price, mode)
    if trail:
        cancel_grid_orders_only(state)
        state["grid_active"]  = False
        state["center_price"] = None
        save_state(state)
        send_telegram(
            f"🔄 <b>TRAILING RE-CENTER</b> ({mode})\n"
            f"{trail_reason}\nGrid orders cancelled — TP orders preserved."
        )
        append_log("TRAIL_RECENTER", {"mode": mode, "price": price, "reason": trail_reason})

    # ── Claude decision ──────────────────────────────────────────────────────
    open_orders = get_open_orders()
    open_count  = len(open_orders)

    decision, reason, center_price = ask_claude_grid(price, indicators, open_count, state, mode)

    # Override PLACE to SKIP if position size is maxed out
    if decision == "PLACE" and at_max_position:
        decision = "SKIP"
        reason   = f"Position size {total_position} ETH at max {MAX_POSITION_ETH} ETH — waiting for TPs."

    if decision == "CANCEL":
        cancel_grid_orders_only(state)
        state["grid_active"]  = False
        state["center_price"] = None
        save_state(state)
        send_telegram(f"🚫 <b>GRID CANCELLED by Claude</b>\nPrice: ${price:.2f}\n{reason}\nTP orders preserved.")
        append_log("GRID_CANCEL_CLAUDE", {"reason": reason, "price": price, "mode": mode})

    elif decision == "PLACE":
        if open_count > 0:
            cancel_grid_orders_only(state)
            time.sleep(1)

        set_leverage()

        # Grid bounds
        grid_upper = round(center_price * (1 + GRID_LEVELS * GRID_SPACING_PCT), 2)
        grid_lower = round(center_price * (1 - GRID_LEVELS * GRID_SPACING_PCT), 2)

        if mode == "neutral":
            placed_buys, placed_sells, state = place_neutral_grid(center_price, state)
            orders_summary = f"Buys: {placed_buys}\nSells: {placed_sells}"

        elif mode == "uptrend":
            placed_buys, state = place_uptrend_grid(center_price, state)
            orders_summary = f"Buy orders (counter sells on fill): {placed_buys}"
            grid_upper = round(center_price * (1 + GRID_LEVELS * GRID_SPACING_PCT * 2), 2)  # wider upper for trailing

        else:  # downtrend
            placed_sells, state = place_downtrend_grid(center_price, state)
            orders_summary = f"Sell orders (counter buys on fill): {placed_sells}"
            grid_lower = round(center_price * (1 - GRID_LEVELS * GRID_SPACING_PCT * 2), 2)  # wider lower for trailing

        state["grid_active"]  = True
        state["center_price"] = center_price
        state["grid_upper"]   = grid_upper
        state["grid_lower"]   = grid_lower
        state["last_placed"]  = now
        save_state(state)

        mode_emoji = {"neutral": "🟢", "uptrend": "📈", "downtrend": "📉"}.get(mode, "🟢")
        send_telegram(
            f"{mode_emoji} <b>GRID PLACED ({mode.upper()})</b>\n"
            f"Center: ${center_price:.2f} | {GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}%\n"
            f"Range: ${grid_lower} — ${grid_upper}\n"
            f"{orders_summary}\n"
            f"ADX: {indicators['adx']} | DI+: {indicators.get('plus_di')} | DI-: {indicators.get('minus_di')}\n"
            f"BB: {indicators['bb_width_pct']}%\n"
            f"Claude: {reason}"
        )
        append_log("GRID_PLACED", {
            "mode":        mode,
            "center":      center_price,
            "grid_upper":  grid_upper,
            "grid_lower":  grid_lower,
            "adx":         indicators["adx"],
            "plus_di":     indicators.get("plus_di"),
            "minus_di":    indicators.get("minus_di"),
            "bb_width":    indicators["bb_width_pct"],
        })

    else:  # SKIP
        send_telegram(
            f"✅ <b>GRID ACTIVE ({mode.upper()})</b> | ${price:.2f}\n"
            f"Center: ${state.get('center_price', 'N/A')} | Orders: {open_count}\n"
            f"Range: ${state.get('grid_lower','?')} — ${state.get('grid_upper','?')}\n"
            f"ADX: {indicators['adx']} | DI+: {indicators.get('plus_di')} | DI-: {indicators.get('minus_di')}\n"
            f"Session profit: ${state.get('total_profit', 0):.4f} | Daily: ${state.get('daily_pnl', 0):.4f}\n"
            f"Fills: {state.get('total_fills', 0)}\n"
            f"Claude: {reason}"
        )


# ── run loop ──────────────────────────────────────────────────────────────────

def run_loop():
    clear_stop()
    send_telegram(
        f"🤖 <b>ETH Grid Bot Started</b>\n"
        f"Checking for existing orders and positions..."
    )
    try:
        recover_on_startup()
    except Exception as e:
        send_telegram(f"⚠️ Startup recovery error: {e}")
        append_log("RECOVERY_ERROR", {"error": str(e)})
    send_telegram(
        f"🤖 <b>ETH Grid Bot Ready</b>\n"
        f"Modes: neutral / uptrend / downtrend (auto-detected)\n"
        f"{GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}% | {QTY_PER_LEVEL} ETH/order\n"
        f"{LEVERAGE}x leverage | interval: {CHECK_INTERVAL//60}min\n"
        f"ADX sideways < {ADX_SIDEWAYS_MAX} | trend > {ADX_TREND_MIN}"
    )
    append_log("START", {
        "symbol": SYMBOL, "leverage": LEVERAGE,
        "grid_levels": GRID_LEVELS, "spacing_pct": GRID_SPACING_PCT,
        "qty_per_level": QTY_PER_LEVEL, "adx_sideways_max": ADX_SIDEWAYS_MAX,
        "adx_trend_min": ADX_TREND_MIN,
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
