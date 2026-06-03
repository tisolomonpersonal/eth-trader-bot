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
LEVERAGE          = int(os.environ.get("LEVERAGE", "10"))
CHECK_INTERVAL    = int(os.environ.get("CHECK_INTERVAL", "1800"))  # 30 min

# Grid settings
GRID_LEVELS       = int(os.environ.get("GRID_LEVELS", "5"))
GRID_SPACING_PCT  = float(os.environ.get("GRID_SPACING_PCT", "0.004"))   # 0.4% per level
QTY_PER_LEVEL     = float(os.environ.get("QTY_PER_LEVEL", "0.01"))       # ETH per order

# Regime thresholds
ADX_PERIOD        = int(os.environ.get("ADX_PERIOD", "14"))
ADX_SIDEWAYS_MAX  = float(os.environ.get("ADX_SIDEWAYS_MAX", "25"))      # below = sideways
ADX_TREND_MIN     = float(os.environ.get("ADX_TREND_MIN", "25"))         # above = trend active
BB_WIDTH_MIN      = float(os.environ.get("BB_WIDTH_MIN", "0.005"))
BB_WIDTH_MAX      = float(os.environ.get("BB_WIDTH_MAX", "0.025"))

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

    indicators = {
        "adx":         adx,
        "plus_di":     plus_di,
        "minus_di":    minus_di,
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
    For directional grids:
    - Uptrend: when a buy order fills, place a sell above it (close long for profit).
    - Downtrend: when a sell order fills, place a buy below it (close short for profit).
    Detects fills by comparing tracked order IDs against currently open orders.
    """
    grid_mode = state.get("grid_mode", "neutral")
    if grid_mode == "neutral":
        return state

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

        if grid_mode == "uptrend" and info.get("side") == "Buy":
            # Buy filled → place sell above to close the long for profit
            counter_price = round(fill_price + spacing_abs, 2)
            res = place_limit_order("Sell", counter_price, qty, pos_idx=pos_idx)
            if res.get("retCode") == 0:
                counter_id = res["result"]["orderId"]
                tracked[counter_id] = {
                    "side":    "Sell",
                    "price":   counter_price,
                    "qty":     qty,
                    "pos_idx": pos_idx,
                    "center":  center,
                    "is_counter": True,
                }
                countered[order_id] = True
                new_counter_count += 1
                append_log("COUNTER_SELL", {"fill_buy": fill_price, "counter_sell": counter_price})
                send_telegram(
                    f"🔁 <b>Counter SELL placed</b> (uptrend fill)\n"
                    f"Buy filled @ ${fill_price:.2f} → Sell @ ${counter_price:.2f}\n"
                    f"Profit target: +{GRID_SPACING_PCT*100:.2f}%"
                )
            else:
                print(f"[counter] sell failed: {res.get('retMsg')}")

        elif grid_mode == "downtrend" and info.get("side") == "Sell":
            # Sell filled → place buy below to close the short for profit
            counter_price = round(fill_price - spacing_abs, 2)
            res = place_limit_order("Buy", counter_price, qty, pos_idx=pos_idx)
            if res.get("retCode") == 0:
                counter_id = res["result"]["orderId"]
                tracked[counter_id] = {
                    "side":    "Buy",
                    "price":   counter_price,
                    "qty":     qty,
                    "pos_idx": pos_idx,
                    "center":  center,
                    "is_counter": True,
                }
                countered[order_id] = True
                new_counter_count += 1
                append_log("COUNTER_BUY", {"fill_sell": fill_price, "counter_buy": counter_price})
                send_telegram(
                    f"🔁 <b>Counter BUY placed</b> (downtrend fill)\n"
                    f"Sell filled @ ${fill_price:.2f} → Buy @ ${counter_price:.2f}\n"
                    f"Profit target: +{GRID_SPACING_PCT*100:.2f}%"
                )
            else:
                print(f"[counter] buy failed: {res.get('retMsg')}")

    state["tracked_orders"] = tracked
    state["counter_placed"] = countered
    return state


# ── grid placement functions ──────────────────────────────────────────────────

def place_neutral_grid(center_price, state):
    """
    Standard symmetric grid: buy orders below, sell orders above.
    Used in sideways markets.
    """
    tracked = {}
    placed_buys, placed_sells = [], []

    for i in range(1, GRID_LEVELS + 1):
        buy_price  = center_price * (1 - i * GRID_SPACING_PCT)
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)

        res = place_limit_order("Buy", buy_price, QTY_PER_LEVEL, pos_idx=1)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_buys.append(round(buy_price, 2))
            tracked[oid] = {"side": "Buy", "price": round(buy_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 1, "center": center_price}
        else:
            send_telegram(f"❌ Neutral buy failed ${buy_price:.2f}: {res.get('retMsg')}")

        res = place_limit_order("Sell", sell_price, QTY_PER_LEVEL, pos_idx=2)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_sells.append(round(sell_price, 2))
            tracked[oid] = {"side": "Sell", "price": round(sell_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 2, "center": center_price}
        else:
            send_telegram(f"❌ Neutral sell failed ${sell_price:.2f}: {res.get('retMsg')}")

        time.sleep(0.15)

    state["tracked_orders"] = tracked
    state["counter_placed"] = {}
    return placed_buys, placed_sells, state


def place_uptrend_grid(center_price, state):
    """
    Directional grid for uptrend: buy orders only, stacked below current price.
    When a buy fills, a counter sell is placed above it (see check_and_place_counter_orders).
    Uses long hedge position (positionIdx=1).
    """
    tracked = {}
    placed_buys = []

    for i in range(1, GRID_LEVELS + 1):
        buy_price = center_price * (1 - i * GRID_SPACING_PCT)
        res = place_limit_order("Buy", buy_price, QTY_PER_LEVEL, pos_idx=1)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_buys.append(round(buy_price, 2))
            tracked[oid] = {"side": "Buy", "price": round(buy_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 1, "center": center_price}
        else:
            send_telegram(f"❌ Uptrend buy failed ${buy_price:.2f}: {res.get('retMsg')}")
        time.sleep(0.15)

    state["tracked_orders"] = tracked
    state["counter_placed"] = {}
    return placed_buys, state


def place_downtrend_grid(center_price, state):
    """
    Directional grid for downtrend: sell orders only, stacked above current price.
    When a sell fills, a counter buy is placed below it (see check_and_place_counter_orders).
    Uses short hedge position (positionIdx=2).
    """
    tracked = {}
    placed_sells = []

    for i in range(1, GRID_LEVELS + 1):
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)
        res = place_limit_order("Sell", sell_price, QTY_PER_LEVEL, pos_idx=2)
        if res.get("retCode") == 0:
            oid = res["result"]["orderId"]
            placed_sells.append(round(sell_price, 2))
            tracked[oid] = {"side": "Sell", "price": round(sell_price, 2),
                            "qty": QTY_PER_LEVEL, "pos_idx": 2, "center": center_price}
        else:
            send_telegram(f"❌ Downtrend sell failed ${sell_price:.2f}: {res.get('retMsg')}")
        time.sleep(0.15)

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
- ADX: {indicators['adx']} | DI+: {indicators.get('plus_di', 'n/a')} | DI-: {indicators.get('minus_di', 'n/a')}
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

    if not state.get("trading_enabled", True):
        send_telegram(f"⏹ Trading disabled by dashboard. Price: ${price:.2f}")
        return

    candles = get_candles("60", 100)
    if len(candles) < 30:
        send_telegram(f"⚠️ Not enough candle data: {len(candles)}")
        return

    mode, regime_reason, indicators = detect_regime(candles, price)
    prev_mode = state.get("grid_mode", "neutral")

    # ── Mode switch: cancel existing grid if regime changed ──────────────────
    if state.get("grid_active") and mode != prev_mode:
        cancel_all_orders()
        state["grid_active"]     = False
        state["tracked_orders"]  = {}
        state["counter_placed"]  = {}
        state["center_price"]    = None
        save_state(state)
        send_telegram(
            f"🔀 <b>REGIME CHANGE</b> {prev_mode.upper()} → {mode.upper()}\n"
            f"Price: ${price:.2f} | {regime_reason}\nGrid cancelled — will re-place."
        )
        append_log("REGIME_CHANGE", {"from": prev_mode, "to": mode, "price": price})

    state["grid_mode"] = mode

    # ── Counter-order placement for directional fills ────────────────────────
    if state.get("grid_active") and mode in ("uptrend", "downtrend"):
        state = check_and_place_counter_orders(state, price)
        save_state(state)

    # ── Trailing: re-center if price left grid range ─────────────────────────
    trail, trail_reason = should_trail(state, price, mode)
    if trail:
        cancel_all_orders()
        state["grid_active"]    = False
        state["tracked_orders"] = {}
        state["counter_placed"] = {}
        save_state(state)
        send_telegram(
            f"🔄 <b>TRAILING RE-CENTER</b> ({mode})\n"
            f"{trail_reason}"
        )
        append_log("TRAIL_RECENTER", {"mode": mode, "price": price, "reason": trail_reason})

    # ── Claude decision ──────────────────────────────────────────────────────
    open_orders = get_open_orders()
    open_count  = len(open_orders)

    decision, reason, center_price = ask_claude_grid(price, indicators, open_count, state, mode)

    if decision == "CANCEL":
        if open_count > 0:
            cancel_all_orders()
        state["grid_active"]    = False
        state["tracked_orders"] = {}
        state["counter_placed"] = {}
        save_state(state)
        send_telegram(f"🚫 <b>GRID CANCELLED by Claude</b>\nPrice: ${price:.2f}\n{reason}")
        append_log("GRID_CANCEL_CLAUDE", {"reason": reason, "price": price, "mode": mode})

    elif decision == "PLACE":
        if open_count > 0:
            cancel_all_orders()
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
        f"Modes: neutral / uptrend / downtrend (auto-detected)\n"
        f"{GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}% | {QTY_PER_LEVEL} ETH/order\n"
        f"{LEVERAGE}x leverage | checks every {CHECK_INTERVAL//60}min\n"
        f"ADX threshold: {ADX_SIDEWAYS_MAX} (sideways) / {ADX_TREND_MIN} (trend)"
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
