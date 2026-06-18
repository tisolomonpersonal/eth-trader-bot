"""
EURUSD Master Pattern Trading Bot (Bybit TradFi)
=================================================
Strategy: 3-phase master pattern (Contraction -> Expansion -> Trend)
- 4H timeframe: directional bias (where smart money has settled)
- 1H timeframe: entry signal (counter-trend expansion = entry zone)
- No hard SL: exit only when 4H bias reverses (trend change)
- TP: dynamic, placed at 1H average price (contraction midpoint)
- Single position at a time
- Dynamic lot sizing: protects account by risking RISK_PCT per trade
- Forex market hours respected (24/5, closed weekends)
"""

import requests, json, os, time, hmac, hashlib, traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except Exception:
    anthropic = None

# == CONFIG ====================================================================
BYBIT_API_KEY      = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# TradFi symbol (Zero-Fee mode uses .s suffix; Tight-Spread mode omit suffix)
SYMBOL             = os.environ.get("SYMBOL", "EURUSD.s")
# NOTE: If TradFi symbols require a different category, update CATEGORY below.
# Try "linear" first. If API returns error, check Bybit TradFi API docs.
CATEGORY           = os.environ.get("CATEGORY", "linear")

# Forex precision
PIP_SIZE           = float(os.environ.get("PIP_SIZE", "0.0001"))       # 1 pip for EURUSD
LOT_UNIT_SIZE      = int(os.environ.get("LOT_UNIT_SIZE", "100000"))    # units in 1 standard lot
PRICE_DECIMALS     = int(os.environ.get("PRICE_DECIMALS", "5"))        # EURUSD: 5 decimal places
MIN_LOT            = float(os.environ.get("MIN_LOT", "0.01"))          # minimum lot size
MAX_LOT            = float(os.environ.get("MAX_LOT", "10.0"))          # maximum lot size

# Dynamic lot sizing (protects money, scales with account)
# Lots sized so that MAX_ADVERSE_PIPS adverse move = RISK_PCT of equity
RISK_PCT           = float(os.environ.get("RISK_PCT", "0.01"))         # 1% equity at risk per trade
MAX_ADVERSE_PIPS   = int(os.environ.get("MAX_ADVERSE_PIPS", "100"))    # worst-case pip buffer for sizing

# Entry
ENTRY_OFFSET_PIPS  = int(os.environ.get("ENTRY_OFFSET_PIPS", "2"))     # pip offset for limit entry
MIN_TP_PIPS        = int(os.environ.get("MIN_TP_PIPS", "10"))          # skip entry if TP < this many pips

# Master pattern timeframes
BIAS_TF            = os.environ.get("BIAS_TF", "240")                  # 4H - directional bias
ENTRY_TF           = os.environ.get("ENTRY_TF", "60")                  # 1H - entry timing
BOX_LOOKBACK       = int(os.environ.get("BOX_LOOKBACK", "40"))         # candles to search for contraction
BOX_WINDOW         = int(os.environ.get("BOX_WINDOW", "5"))            # candles in contraction box
SETTLE_CANDLES     = int(os.environ.get("SETTLE_CANDLES", "2"))        # 4H candles needed to confirm bias

# Risk
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "300"))      # seconds between cycles
PORTFOLIO_STOPLOSS = float(os.environ.get("PORTFOLIO_STOPLOSS", "-4.0"))  # emergency close all

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")


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
        "in_trade":         False,
        "trade_side":       None,
        "entry_price":      None,
        "tp_price":         None,
        "tp_order_id":      None,
        "lot_size":         None,
        "bias_4h":          None,
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


# == MARKET HOURS ==============================================================

def is_forex_market_open():
    """
    Forex is open 24 hours Mon-Fri.
    Closes: Friday 22:00 UTC
    Opens:  Sunday 22:00 UTC
    """
    now     = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

    if weekday == 5:                              # Saturday - always closed
        return False
    if weekday == 6 and now.hour < 22:            # Sunday before 22:00 UTC
        return False
    if weekday == 4 and now.hour >= 22:           # Friday after 22:00 UTC
        return False
    return True


# == LOT SIZING ================================================================

def pip_value_usd(lot_size):
    """
    USD value of 1 pip move for given lot size.
    For EURUSD: 1 pip = PIP_SIZE * LOT_UNIT_SIZE * lot_size USD
    e.g. 0.01 lot -> 0.0001 * 100000 * 0.01 = $0.10 per pip
    """
    return PIP_SIZE * LOT_UNIT_SIZE * lot_size


def calculate_lot_size(equity):
    """
    Dynamic lot sizing that protects the account:
    - Risk RISK_PCT of equity (default 1%)
    - Sized so a MAX_ADVERSE_PIPS move against us = risk_amount
    - Automatically scales up as account grows, down if it shrinks

    Example:
      $10 equity  -> risk $0.10 -> 0.01 lots (min)
      $100 equity -> risk $1.00 -> 0.10 lots
      $500 equity -> risk $5.00 -> 0.50 lots
    """
    if not equity or equity <= 0:
        return MIN_LOT

    risk_amount          = equity * RISK_PCT
    pip_value_per_std    = PIP_SIZE * LOT_UNIT_SIZE  # pip value for 1 standard lot (e.g. $10)
    lot_size             = risk_amount / (MAX_ADVERSE_PIPS * pip_value_per_std)
    lot_size             = max(MIN_LOT, min(round(lot_size, 2), MAX_LOT))
    return lot_size


# == BYBIT AUTH ================================================================

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


# == BYBIT API =================================================================

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
        f"https://api.bybit.com/v5/market/tickers?category={CATEGORY}&symbol={SYMBOL}",
        timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles(interval, limit=100):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category={CATEGORY}"
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
        query = f"category={CATEGORY}&symbol={SYMBOL}"
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
    query = f"category={CATEGORY}&symbol={SYMBOL}&limit=50"
    try:
        r = requests.get(f"https://api.bybit.com/v5/order/realtime?{query}",
                         headers=sign_get(query), timeout=10)
        return r.json().get("result", {}).get("list", [])
    except:
        return []

def cancel_all_orders():
    params = {"category": CATEGORY, "symbol": SYMBOL}
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/cancel-all",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except:
        return {}

def cancel_order(order_id):
    params = {"category": CATEGORY, "symbol": SYMBOL, "orderId": order_id}
    headers, body = sign_post(params)
    try:
        r = requests.post("https://api.bybit.com/v5/order/cancel",
                          data=body, headers=headers, timeout=10)
        return r.json()
    except:
        return {}

def place_limit_order(side, price, qty, pos_idx=None, reduce_only=False):
    if pos_idx is None:
        pos_idx = 1 if side == "Buy" else 2
    params = {
        "category":    CATEGORY,
        "symbol":      SYMBOL,
        "side":        side,
        "orderType":   "Limit",
        "qty":         str(round(qty, 2)),                       # lots: 2 decimal places
        "price":       str(round(price, PRICE_DECIMALS)),        # forex: 5 decimal places
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
        "category":    CATEGORY,
        "symbol":      SYMBOL,
        "side":        close_side,
        "orderType":   "Market",
        "qty":         str(round(qty, 2)),
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
        query = f"category={CATEGORY}&symbol={SYMBOL}"
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
            print(f"[close_all] {pos['side']} {size} lots -> {res.get('retCode')} {res.get('retMsg')}")
            time.sleep(0.2)
    except Exception as e:
        print(f"[close_all] {e}")

def get_closed_pnl(start_time_ms=None):
    try:
        query = f"category={CATEGORY}&symbol={SYMBOL}&limit=50"
        if start_time_ms:
            query += f"&startTime={int(start_time_ms)}"
        r = requests.get(f"https://api.bybit.com/v5/position/closed-pnl?{query}",
                         headers=sign_get(query), timeout=10)
        data = r.json()
        return data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []
    except:
        return []


# == MASTER PATTERN ANALYSIS ===================================================

def find_contraction_box(candles, lookback=40, window=5):
    """
    Find the tightest price range (contraction box) in recent candles.
    Phase 1 of the master pattern - where smart money accumulates.
    Returns: {high, low, avg, range, range_pct} or None
    """
    if len(candles) < window + 2:
        return None
    recent    = candles[-min(lookback, len(candles)):]
    best_range = float('inf')
    best_high  = best_low = 0

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
        "high":      round(best_high, PRICE_DECIMALS),
        "low":       round(best_low,  PRICE_DECIMALS),
        "avg":       round(avg,       PRICE_DECIMALS),
        "range":     round(best_range, PRICE_DECIMALS),
        "range_pct": round(range_pct, 3),
    }


def get_directional_bias(price):
    """
    4H directional bias using master pattern average price.
    Bias confirmed when SETTLE_CANDLES consecutive 4H closes settle above/below avg.
    Returns: (bias, avg_price_4h, box_4h)
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
    1H entry signal using master pattern.
    Entry zone = when price is on the COUNTER-TREND side of 1H average price.
    This is the expansion phase on the smaller timeframe.

    Returns: (signal, tp_price, avg_1h, box_1h)
    """
    candles = get_candles(ENTRY_TF, 60)
    if len(candles) < 10:
        return False, None, None, None

    box = find_contraction_box(candles, lookback=20, window=3)
    if not box:
        return False, None, None, None

    avg = box["avg"]

    if bias == "short":
        if price > avg:
            tp         = avg
            pip_dist   = (price - tp) / PIP_SIZE
            if pip_dist >= MIN_TP_PIPS:
                return True, round(tp, PRICE_DECIMALS), avg, box

    elif bias == "long":
        if price < avg:
            tp         = avg
            pip_dist   = (tp - price) / PIP_SIZE
            if pip_dist >= MIN_TP_PIPS:
                return True, round(tp, PRICE_DECIMALS), avg, box

    return False, None, avg, box


# == TRADE EXECUTION ===========================================================

def enter_trade(bias, price, tp_price, state):
    """
    Enter a single position in the direction of the 4H bias.
    - Lot size: calculated dynamically based on account equity (protects money)
    - Entry: limit order ENTRY_OFFSET_PIPS from current price for better fill
    - TP: limit order at 1H average price (no hard SL; exit on bias reversal)
    """
    equity   = get_wallet_equity_usdt() or 10.0
    lot_size = calculate_lot_size(equity)

    pip_dist    = abs(price - tp_price) / PIP_SIZE
    profit_est  = pip_dist * pip_value_usd(lot_size)

    if bias == "short":
        entry_price = round(price + ENTRY_OFFSET_PIPS * PIP_SIZE, PRICE_DECIMALS)
        pos_idx     = 2
        tp_side     = "Buy"
    else:
        entry_price = round(price - ENTRY_OFFSET_PIPS * PIP_SIZE, PRICE_DECIMALS)
        pos_idx     = 1
        tp_side     = "Sell"

    # Place entry order
    res = place_limit_order(
        "Sell" if bias == "short" else "Buy",
        entry_price, lot_size, pos_idx=pos_idx
    )
    if res.get("retCode") != 0:
        send_telegram(f"Entry failed: {res.get('retMsg')}")
        append_log("ENTRY_FAIL", {"bias": bias, "price": entry_price, "msg": res.get("retMsg")})
        return state

    # Place TP order (reduce-only, closes when price returns to 1H avg)
    tp_res = place_limit_order(tp_side, tp_price, lot_size, pos_idx=pos_idx, reduce_only=True)
    tp_oid = tp_res["result"]["orderId"] if tp_res.get("retCode") == 0 else None

    state["in_trade"]    = True
    state["trade_side"]  = bias
    state["entry_price"] = entry_price
    state["tp_price"]    = tp_price
    state["tp_order_id"] = tp_oid
    state["lot_size"]    = lot_size

    send_telegram(
        f"{'Short' if bias == 'short' else 'Long'} <b>TRADE ENTERED [{bias.upper()}]</b>\n"
        f"Entry: {entry_price:.{PRICE_DECIMALS}f} | TP: {tp_price:.{PRICE_DECIMALS}f}\n"
        f"TP distance: {pip_dist:.1f} pips | Est. profit: ~${profit_est:.2f}\n"
        f"Lot size: {lot_size} (equity: ${equity:.2f}, risk: {RISK_PCT*100:.0f}%)\n"
        f"Exit: TP fill OR 4H bias reversal"
    )
    append_log("ENTRY", {
        "bias": bias, "entry": entry_price, "tp": tp_price,
        "pips": round(pip_dist, 1), "profit_est": round(profit_est, 4),
        "lots": lot_size, "equity": round(equity, 2)
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
    state["lot_size"]    = None

    send_telegram(
        f"TRADE CLOSED\n"
        f"Reason: {reason}\n"
        f"Price: {price:.{PRICE_DECIMALS}f}"
    )
    append_log("EXIT", {"reason": reason, "price": price})
    return state


# == PnL & POSITION TRACKING ===================================================

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
        if state.get("in_trade"):
            state["in_trade"]    = False
            state["trade_side"]  = None
            state["tp_order_id"] = None
    return state

def update_fills_and_pnl(state):
    now_ms         = int(time.time() * 1000)
    last_check_ms  = int(state.get("last_fill_check") or 0)
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
            f"<b>{new_fills} fill(s)</b>\n"
            f"Session: ${state['total_profit']:.4f} | Daily: ${state['daily_pnl']:.4f}"
        )
    return state


# == STARTUP RECOVERY ==========================================================

def recover_on_startup(state):
    """
    On restart: check for open positions and orders.
    Restore trade state and place missing TP if needed.
    """
    open_orders = get_open_orders()
    pos         = get_position()

    if not pos and not open_orders:
        send_telegram("Startup: clean slate - no open positions or orders.")
        append_log("RECOVERY", {"result": "clean"})
        return state

    if pos:
        state["in_trade"]      = True
        state["position_side"] = pos["side"]
        state["entry_price"]   = round(pos["entry_price"], PRICE_DECIMALS)
        state["trade_side"]    = "short" if pos["side"] == "Sell" else "long"
        state["live_pnl"]      = round(pos["live_pnl"], 4)

        tp_exists = any(
            o.get("reduceOnly") or o.get("side") != pos["side"]
            for o in open_orders
        )

        if not tp_exists:
            candles_1h = get_candles(ENTRY_TF, 60)
            box_1h     = find_contraction_box(candles_1h, lookback=20, window=3) if candles_1h else None
            if box_1h:
                tp_price = box_1h["avg"]
                tp_side  = "Buy" if pos["side"] == "Sell" else "Sell"
                pos_idx  = 2 if pos["side"] == "Sell" else 1
                equity   = get_wallet_equity_usdt() or 10.0
                lot_size = calculate_lot_size(equity)
                res      = place_limit_order(tp_side, tp_price, lot_size,
                                             pos_idx=pos_idx, reduce_only=True)
                if res.get("retCode") == 0:
                    state["tp_order_id"] = res["result"]["orderId"]
                    state["tp_price"]    = tp_price
                    state["lot_size"]    = lot_size
                    send_telegram(
                        f"Recovery: TP placed\n"
                        f"Open {pos['side']} @ {pos['entry_price']:.{PRICE_DECIMALS}f}\n"
                        f"TP: {tp_price:.{PRICE_DECIMALS}f} (1H avg)"
                    )
                    append_log("RECOVERY_TP", {"entry": pos["entry_price"], "tp": tp_price})
        else:
            for o in open_orders:
                if o.get("side") != pos["side"]:
                    state["tp_order_id"] = o.get("orderId")
                    state["tp_price"]    = float(o.get("price") or 0)
                    break
            send_telegram(
                f"Recovery: trade restored\n"
                f"Open {pos['side']} @ {pos['entry_price']:.{PRICE_DECIMALS}f}\n"
                f"TP order found @ {state.get('tp_price', '?')}"
            )

    save_state(state)
    return state


# == MAIN CYCLE ================================================================

def run_cycle():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()

    # Forex market hours guard
    if not is_forex_market_open():
        state["equity"] = get_wallet_equity_usdt()
        save_state(state)
        append_log("MARKET_CLOSED", {"time": now})
        return  # Market closed, skip cycle silently

    price = get_price()

    # Refresh live data
    state["equity"] = get_wallet_equity_usdt()
    state["price"]  = price
    state = update_live_position(state)
    state = update_fills_and_pnl(state)
    save_state(state)

    # Dashboard kill-switch
    if not state.get("trading_enabled", True):
        send_telegram(f"Trading disabled. Price: {price:.{PRICE_DECIMALS}f}")
        return

    # == Emergency portfolio stop ==============================================
    live_pnl = state.get("live_pnl") or 0.0
    if live_pnl <= PORTFOLIO_STOPLOSS:
        cancel_all_orders()
        close_all_positions()
        state["in_trade"]        = False
        state["trading_enabled"] = False
        save_state(state)
        send_telegram(
            f"EMERGENCY STOP\n"
            f"Live PnL ${live_pnl:.2f} <= ${PORTFOLIO_STOPLOSS:.2f}\n"
            f"All positions closed. Re-enable from dashboard."
        )
        append_log("EMERGENCY_STOP", {"live_pnl": live_pnl, "price": price})
        return

    # == 4H directional bias (master pattern) =================================
    bias_4h, avg_4h, box_4h = get_directional_bias(price)
    state["bias_4h"]      = bias_4h
    state["avg_price_4h"] = avg_4h

    # == If in a trade: check exit conditions =================================
    if state.get("in_trade"):
        trade_side = state.get("trade_side")
        pos        = get_position()

        if not pos:
            state["in_trade"]   = False
            state["trade_side"] = None
            save_state(state)
            send_telegram(
                f"Position closed (TP hit or filled)\n"
                f"Price: {price:.{PRICE_DECIMALS}f} | Daily PnL: ${state.get('daily_pnl', 0):.4f}"
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

        # Hold - report status
        pips_from_entry = abs(price - (state.get("entry_price") or price)) / PIP_SIZE
        send_telegram(
            f"{'Short' if trade_side == 'short' else 'Long'} <b>HOLDING [{trade_side.upper()}]</b>\n"
            f"Entry: {state.get('entry_price', '?')} | TP: {state.get('tp_price', '?')}\n"
            f"Current: {price:.{PRICE_DECIMALS}f} | PnL: ${live_pnl:.4f}\n"
            f"Pips from entry: {pips_from_entry:.1f} | Lots: {state.get('lot_size', '?')}\n"
            f"4H bias: {bias_4h.upper()}"
        )
        return

    # == Not in trade: look for entry =========================================
    if bias_4h == "neutral":
        send_telegram(
            f"WAITING - 4H neutral, no bias yet\n"
            f"Price: {price:.{PRICE_DECIMALS}f}"
            + (f" | 4H avg: {avg_4h:.{PRICE_DECIMALS}f}" if avg_4h else "")
        )
        return

    # Check 1H for entry signal
    signal, tp_price, avg_1h, box_1h = get_entry_signal(bias_4h, price)
    state["avg_price_1h"] = avg_1h

    if signal and tp_price:
        state = enter_trade(bias_4h, price, tp_price, state)
    else:
        direction = "below" if bias_4h == "short" else "above"
        avg_str   = f"{avg_1h:.{PRICE_DECIMALS}f}" if avg_1h else "N/A"
        send_telegram(
            f"WATCHING [{bias_4h.upper()}]\n"
            f"4H bias confirmed | Waiting for 1H expansion\n"
            f"Price: {price:.{PRICE_DECIMALS}f} | 1H avg: {avg_str}\n"
            f"Need price {direction} 1H avg to enter"
        )

    save_state(state)


# == RUN LOOP ==================================================================

def run_loop():
    clear_stop()
    send_telegram(
        f"<b>EURUSD Master Pattern Bot</b>\n"
        f"Symbol: {SYMBOL} | Category: {CATEGORY}\n"
        f"Risk: {RISK_PCT*100:.0f}% equity/trade | Max adverse: {MAX_ADVERSE_PIPS} pips\n"
        f"Checking existing positions..."
    )

    state = load_state()
    try:
        state = recover_on_startup(state)
    except Exception as e:
        send_telegram(f"Recovery error: {e}")
        append_log("RECOVERY_ERROR", {"error": str(e)})

    equity = get_wallet_equity_usdt() or 0
    lot_example = calculate_lot_size(equity)
    send_telegram(
        f"<b>Bot Ready</b>\n"
        f"Equity: ${equity:.2f} | Starting lot size: {lot_example}\n"
        f"4H bias TF: {BIAS_TF} | 1H entry TF: {ENTRY_TF}\n"
        f"Emergency stop: ${PORTFOLIO_STOPLOSS} | Exit: TP or 4H bias reversal"
    )
    append_log("START", {
        "symbol": SYMBOL, "risk_pct": RISK_PCT,
        "bias_tf": BIAS_TF, "entry_tf": ENTRY_TF, "equity": equity
    })

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
