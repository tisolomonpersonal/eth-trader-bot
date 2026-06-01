import requests, json, os, time, hmac, hashlib
import traceback
from datetime import datetime, timezone
import anthropic
from pathlib import Path

# --- CONFIG ---
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL = "ETHUSDT"
LEVERAGE = 10
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "1800"))  # 30 min

# Grid settings
GRID_LEVELS = int(os.environ.get("GRID_LEVELS", "5"))           # number of grid levels each side
GRID_SPACING_PCT = float(os.environ.get("GRID_SPACING_PCT", "0.004"))  # 0.4% between levels
QTY_PER_LEVEL = float(os.environ.get("QTY_PER_LEVEL", "0.01"))  # ETH per grid order

# Trend filter — only run grid when market is NOT trending strongly
ADX_PERIOD = int(os.environ.get("ADX_PERIOD", "14"))
ADX_SIDEWAYS_MAX = float(os.environ.get("ADX_SIDEWAYS_MAX", "25"))  # ADX < 25 = sideways
BB_WIDTH_MIN = float(os.environ.get("BB_WIDTH_MIN", "0.005"))   # BB width > 0.5% = enough movement
BB_WIDTH_MAX = float(os.environ.get("BB_WIDTH_MAX", "0.025"))   # BB width < 2.5% = not chaotic

STATE_FILE = Path(__file__).with_name("grid_state.json")
LOG_FILE = Path(__file__).with_name("grid_log.txt")

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

def append_log(event, payload):
    try:
        record = {"ts": utc_now(), "event": event}
        if isinstance(payload, dict):
            record.update(payload)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except:
        pass

def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1000000))

def sign_request(params):
    timestamp = get_server_time()
    body = json.dumps(params, separators=(',', ':'), ensure_ascii=False)
    param_str = timestamp + BYBIT_API_KEY + "5000" + body
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }
    return headers, body

def sign_get(query):
    timestamp = get_server_time()
    param_str = timestamp + BYBIT_API_KEY + "5000" + query
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode('utf-8'), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000"
    }

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL,
              "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    headers, body = sign_request(params)
    requests.post("https://api.bybit.com/v5/position/set-leverage",
                  data=body, headers=headers, timeout=10)

def get_price():
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles(interval="60", limit=100):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}&interval={interval}&limit={limit}",
            timeout=10
        )
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        return [{"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                 "close": float(c[4]), "volume": float(c[5])}
                for c in reversed(data["result"]["list"])]
    except:
        return []

def get_open_orders():
    query = f"category=linear&symbol={SYMBOL}&limit=50"
    headers = sign_get(query)
    r = requests.get(f"https://api.bybit.com/v5/order/realtime?{query}",
                     headers=headers, timeout=10)
    data = r.json()
    if data.get("result") and data["result"].get("list"):
        return data["result"]["list"]
    return []

def cancel_all_orders():
    params = {"category": "linear", "symbol": SYMBOL}
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/cancel-all",
                      data=body, headers=headers, timeout=10)
    return r.json()

def place_limit_order(side, price_level, qty):
    position_idx = 1 if side == "Buy" else 2
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Limit",
        "qty": str(round(qty, 3)),
        "price": str(round(price_level, 2)),
        "positionIdx": position_idx,
        "timeInForce": "GTC"
    }
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create",
                      data=body, headers=headers, timeout=10)
    return r.json()

def calculate_ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 6)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"] - candles[i-1]["close"])
        )
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def calculate_adx(candles, period=14):
    """Calculate ADX — measures trend strength. Low ADX = sideways."""
    if len(candles) < period * 2:
        return 50  # assume trending if not enough data
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(candles)):
        high_diff = candles[i]["high"] - candles[i-1]["high"]
        low_diff = candles[i-1]["low"] - candles[i]["low"]
        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"] - candles[i-1]["close"])
        )
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    # Smooth with Wilder's moving average
    def wilder_smooth(values, p):
        smooth = sum(values[:p])
        result = [smooth]
        for v in values[p:]:
            smooth = smooth - smooth / p + v
            result.append(smooth)
        return result

    tr_smooth = wilder_smooth(tr_list, period)
    plus_smooth = wilder_smooth(plus_dm_list, period)
    minus_smooth = wilder_smooth(minus_dm_list, period)

    dx_list = []
    for i in range(len(tr_smooth)):
        if tr_smooth[i] == 0:
            continue
        plus_di = 100 * plus_smooth[i] / tr_smooth[i]
        minus_di = 100 * minus_smooth[i] / tr_smooth[i]
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_list.append(dx)

    if len(dx_list) < period:
        return 50
    adx = sum(dx_list[-period:]) / period
    return round(adx, 2)

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    recent = closes[-period:]
    sma = sum(recent) / period
    std = (sum((p - sma) ** 2 for p in recent) / period) ** 0.5
    return round(sma, 2), round(sma + 2 * std, 2), round(sma - 2 * std, 2)

def is_sideways_market(candles, price):
    """
    Returns (is_sideways: bool, reason: str, indicators: dict)
    Sideways = ADX < threshold AND BB width in valid range
    """
    closes = [c["close"] for c in candles]
    adx = calculate_adx(candles, ADX_PERIOD)
    bb_mid, bb_upper, bb_lower = calculate_bollinger(closes)
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0

    indicators = {
        "adx": adx,
        "bb_width_pct": round(bb_width * 100, 3),
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_mid": bb_mid
    }

    if adx >= ADX_SIDEWAYS_MAX:
        return False, f"ADX {adx} >= {ADX_SIDEWAYS_MAX} (trending)", indicators

    if bb_width < BB_WIDTH_MIN:
        return False, f"BB width {bb_width*100:.2f}% too tight (< {BB_WIDTH_MIN*100:.1f}%)", indicators

    if bb_width > BB_WIDTH_MAX:
        return False, f"BB width {bb_width*100:.2f}% too wide (> {BB_WIDTH_MAX*100:.1f}%)", indicators

    return True, f"ADX {adx} < {ADX_SIDEWAYS_MAX} + BB width {bb_width*100:.2f}% in range", indicators

def ask_claude_grid(price, indicators, open_orders_count, state):
    """Ask Claude to assess grid parameters for current market."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are managing a grid trading bot for ETH/USDT perpetual on Bybit.

Current price: ${price:.2f}
Time: {utc_now()}

Market indicators:
- ADX: {indicators['adx']} (< {ADX_SIDEWAYS_MAX} = sideways confirmed)
- BB Upper: ${indicators['bb_upper']} | Mid: ${indicators['bb_mid']} | Lower: ${indicators['bb_lower']}
- BB Width: {indicators['bb_width_pct']}%

Grid config:
- Levels each side: {GRID_LEVELS}
- Spacing: {GRID_SPACING_PCT*100:.2f}% between levels
- Qty per level: {QTY_PER_LEVEL} ETH
- Leverage: {LEVERAGE}x

Current open orders: {open_orders_count}
Grid profit today: ${state.get('total_profit', 0):.4f}
Fills today: {state.get('total_fills', 0)}

Grid range would be: ${price * (1 - GRID_LEVELS * GRID_SPACING_PCT):.2f} to ${price * (1 + GRID_LEVELS * GRID_SPACING_PCT):.2f}

Should we PLACE the grid, SKIP this cycle, or CANCEL and rebuild?
- PLACE: market is sideways and grid is not active or needs rebuild
- SKIP: grid is already placed and working fine
- CANCEL: grid needs to be cancelled (price moved outside range, market changed)

Respond in this exact format:
DECISION: PLACE or SKIP or CANCEL
REASON: (1-2 sentences)
CENTER_PRICE: $X.XX"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text
        decision = "SKIP"
        reason = ""
        center = price
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

def place_grid(center_price):
    """Place buy and sell limit orders around center price."""
    placed_buys = []
    placed_sells = []

    for i in range(1, GRID_LEVELS + 1):
        # Buy below
        buy_price = center_price * (1 - i * GRID_SPACING_PCT)
        result = place_limit_order("Buy", buy_price, QTY_PER_LEVEL)
        if result.get("retCode") == 0:
            placed_buys.append(round(buy_price, 2))
        else:
            send_telegram(f"❌ Grid buy order failed at ${buy_price:.2f}: {result.get('retMsg')}")

        # Sell above
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)
        result = place_limit_order("Sell", sell_price, QTY_PER_LEVEL)
        if result.get("retCode") == 0:
            placed_sells.append(round(sell_price, 2))
        else:
            send_telegram(f"❌ Grid sell order failed at ${sell_price:.2f}: {result.get('retMsg')}")

        time.sleep(0.1)  # rate limit safety

    return placed_buys, placed_sells

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r") as f:
                return json.load(f)
    except:
        pass
    return {
        "grid_active": False,
        "center_price": None,
        "grid_upper": None,
        "grid_lower": None,
        "total_profit": 0.0,
        "total_fills": 0,
        "last_placed": None
    }

def save_state(state):
    try:
        with STATE_FILE.open("w") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def run_cycle():
    now = utc_now()
    price = get_price()
    candles = get_candles("60", 100)
    state = load_state()

    if len(candles) < 30:
        send_telegram(f"⚠️ Not enough candle data: {len(candles)}")
        return

    # Check if market is sideways
    sideways, regime_reason, indicators = is_sideways_market(candles, price)

    if not sideways:
        # Market is trending — cancel grid if active
        if state.get("grid_active"):
            cancel_result = cancel_all_orders()
            state["grid_active"] = False
            state["center_price"] = None
            save_state(state)
            send_telegram(
                f"🚫 <b>GRID CANCELLED</b> — Market trending\n"
                f"Price: ${price:.2f}\n"
                f"Reason: {regime_reason}"
            )
            append_log("GRID_CANCEL", {"reason": regime_reason, "price": price})
        else:
            send_telegram(
                f"⏭ <b>SKIP</b> — Trending market\n"
                f"Price: ${price:.2f}\n"
                f"Reason: {regime_reason}\n"
                f"ADX: {indicators['adx']} | BB Width: {indicators['bb_width_pct']}%"
            )
        return

    # Market is sideways — check open orders
    open_orders = get_open_orders()
    open_count = len(open_orders)

    # Check if price moved outside grid range
    if state.get("grid_active") and state.get("grid_upper") and state.get("grid_lower"):
        if price > state["grid_upper"] or price < state["grid_lower"]:
            cancel_result = cancel_all_orders()
            state["grid_active"] = False
            save_state(state)
            send_telegram(
                f"🔄 <b>GRID REBUILD</b> — Price left range\n"
                f"Price: ${price:.2f} | Range: ${state['grid_lower']:.2f}-${state['grid_upper']:.2f}"
            )
            open_count = 0

    # Ask Claude what to do
    decision, reason, center_price = ask_claude_grid(price, indicators, open_count, state)

    if decision == "CANCEL":
        if open_count > 0:
            cancel_all_orders()
        state["grid_active"] = False
        save_state(state)
        send_telegram(
            f"🚫 <b>GRID CANCELLED by Claude</b>\n"
            f"Price: ${price:.2f}\n"
            f"Reason: {reason}"
        )
        append_log("GRID_CANCEL_CLAUDE", {"reason": reason, "price": price})

    elif decision == "PLACE":
        # Cancel existing first
        if open_count > 0:
            cancel_all_orders()
            time.sleep(1)

        set_leverage()
        placed_buys, placed_sells = place_grid(center_price)

        grid_upper = center_price * (1 + GRID_LEVELS * GRID_SPACING_PCT)
        grid_lower = center_price * (1 - GRID_LEVELS * GRID_SPACING_PCT)

        state["grid_active"] = True
        state["center_price"] = center_price
        state["grid_upper"] = round(grid_upper, 2)
        state["grid_lower"] = round(grid_lower, 2)
        state["last_placed"] = now
        save_state(state)

        send_telegram(
            f"🟢 <b>GRID PLACED</b>\n"
            f"Center: ${center_price:.2f} | {GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}%\n"
            f"Range: ${grid_lower:.2f} — ${grid_upper:.2f}\n"
            f"Buys: {placed_buys}\n"
            f"Sells: {placed_sells}\n"
            f"ADX: {indicators['adx']} | BB: {indicators['bb_width_pct']}%\n"
            f"Reason: {reason}"
        )
        append_log("GRID_PLACED", {
            "center": center_price,
            "grid_upper": grid_upper,
            "grid_lower": grid_lower,
            "buys": placed_buys,
            "sells": placed_sells,
            "adx": indicators["adx"],
            "bb_width": indicators["bb_width_pct"]
        })

    else:  # SKIP
        send_telegram(
            f"✅ <b>GRID ACTIVE</b> | ${price:.2f}\n"
            f"Center: ${state.get('center_price', 'N/A')} | Orders: {open_count}\n"
            f"ADX: {indicators['adx']} | BB: {indicators['bb_width_pct']}%\n"
            f"Profit: ${state.get('total_profit', 0):.4f} | Fills: {state.get('total_fills', 0)}\n"
            f"Claude: {reason}"
        )

def main():
    send_telegram(
        f"🤖 <b>ETH Grid Bot Started</b>\n"
        f"{GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}% | {QTY_PER_LEVEL} ETH/level\n"
        f"{LEVERAGE}x | Checks every {CHECK_INTERVAL//60}min\n"
        f"Sideways filter: ADX < {ADX_SIDEWAYS_MAX}"
    )
    append_log("START", {
        "symbol": SYMBOL, "leverage": LEVERAGE,
        "grid_levels": GRID_LEVELS, "spacing_pct": GRID_SPACING_PCT,
        "qty_per_level": QTY_PER_LEVEL, "adx_max": ADX_SIDEWAYS_MAX
    })
    while True:
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {str(e)}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
            append_log("ERROR", {"error": err})
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
