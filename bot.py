import requests, json, os, time, hmac, hashlib
from datetime import datetime
import anthropic

# --- CONFIG ---
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL = "ETHUSDT"
QTY = 0.14
LEVERAGE = 25
INTERVAL_NO_POSITION = 3600
INTERVAL_IN_POSITION = 1800
STATE_FILE = "/tmp/bot_state.json"

def send_telegram(message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
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

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL, "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    headers, body = sign_request(params)
    requests.post("https://api.bybit.com/v5/position/set-leverage", data=body, headers=headers, timeout=10)

def get_price():
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles():
    r = requests.get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}&interval=60&limit=100", timeout=10)
    candles = r.json()["result"]["list"]
    return [{"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in reversed(candles)]

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()["data"][0]
        return f"{data['value']} ({data['value_classification']})"
    except:
        return "unavailable"

def get_position():
    timestamp = get_server_time()
    query = f"category=linear&symbol={SYMBOL}"
    param_str = timestamp + BYBIT_API_KEY + "5000" + query
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000"
    }
    r = requests.get(f"https://api.bybit.com/v5/position/list?{query}", headers=headers, timeout=10)
    positions = r.json()["result"]["list"]
    for p in positions:
        if float(p["size"]) > 0:
            return p
    return None

def place_order(side, sl, tp):
    position_idx = 1 if side == "Buy" else 2
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(QTY),
        "positionIdx": position_idx,
        "stopLoss": str(round(sl, 2)),
        "takeProfit": str(round(tp, 2)),
        "slTriggerBy": "MarkPrice",
        "tpTriggerBy": "MarkPrice"
    }
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create", data=body, headers=headers, timeout=10)
    return r.json()

def update_sl_tp(side, new_sl=None, new_tp=None):
    position_idx = 1 if side == "Buy" else 2
    params = {"category": "linear", "symbol": SYMBOL, "positionIdx": position_idx}
    if new_sl:
        params["stopLoss"] = str(round(new_sl, 2))
        params["slTriggerBy"] = "MarkPrice"
    if new_tp:
        params["takeProfit"] = str(round(new_tp, 2))
        params["tpTriggerBy"] = "MarkPrice"
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/position/trading-stop", data=body, headers=headers, timeout=10)
    return r.json()

def close_position(side, qty):
    close_side = "Sell" if side == "Buy" else "Buy"
    position_idx = 1 if side == "Buy" else 2
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "positionIdx": position_idx,
        "reduceOnly": "true"
    }
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create", data=body, headers=headers, timeout=10)
    return r.json()

def calculate_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(closes, period):
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def calculate_macd(closes):
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd_line = round(ema12 - ema26, 4)
    signal = round(macd_line * 0.9, 4)
    histogram = round(macd_line - signal, 4)
    return macd_line, signal, histogram

def calculate_bollinger(closes, period=20):
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((p - sma) ** 2 for p in recent) / period
    std = variance ** 0.5
    upper = round(sma + 2 * std, 2)
    lower = round(sma - 2 * std, 2)
    return round(sma, 2), upper, lower

def smart_tp(side, price, bb_upper, bb_lower, bb_mid):
    bb_range = bb_upper - bb_lower
    if side == "Buy":
        tp = round(bb_mid + (bb_range * 1.5), 2)
        return max(tp, bb_upper)
    else:
        tp = round(bb_mid - (bb_range * 1.5), 2)
        return min(tp, bb_lower)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_position": None, "total_pnl": 0, "wins": 0, "losses": 0, "be_moved": False}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def ask_claude(prompt, retries=3):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(retries):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < retries - 1:
                send_telegram(f"⏳ Anthropic overloaded, retrying in 30s... (attempt {attempt+1}/{retries})")
                time.sleep(30)
            else:
                raise e

def run_cycle():
    price = get_price()
    candles = get_candles()
    fear_greed = get_fear_greed()
    position = get_position()
    state = load_state()

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    rsi = calculate_rsi(closes)
    macd, signal, histogram = calculate_macd(closes)
    bb_mid, bb_upper, bb_lower = calculate_bollinger(closes)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    trend = "UPTREND" if ema20 > ema50 else "DOWNTREND"

    # --- Detect TP/SL hit ---
    if state.get("last_position") and not position:
        last = state["last_position"]
        entry = last["price"]
        side = last["side"]
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
        pnl_usd = pnl_pct / 100 * QTY * entry
        if pnl_pct > 0:
            state["wins"] = state.get("wins", 0) + 1
            state["total_pnl"] = state.get("total_pnl", 0) + pnl_usd
            send_telegram(f"🎯 <b>TP HIT!</b>\nEntry: ${entry:.2f} → ~${price:.2f}\n+${pnl_usd:.4f} ({pnl_pct:.2f}%)\nW:{state['wins']} L:{state['losses']} | Total: ${state['total_pnl']:.4f}")
        else:
            state["losses"] = state.get("losses", 0) + 1
            state["total_pnl"] = state.get("total_pnl", 0) + pnl_usd
            send_telegram(f"🛑 <b>SL HIT!</b>\nEntry: ${entry:.2f} → ~${price:.2f}\n${pnl_usd:.4f} ({pnl_pct:.2f}%)\nW:{state['wins']} L:{state['losses']} | Total: ${state['total_pnl']:.4f}")
        state["last_position"] = None
        state["be_moved"] = False
        save_state(state)

    if position:
        side = position["side"]
        entry = float(position["avgPrice"])
        pnl = float(position["unrealisedPnl"])
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
        current_tp = float(position.get("takeProfit", 0))
        be_moved = state.get("be_moved", False)
        updates = []

        # --- Trailing TP ---
        new_tp = smart_tp(side, price, bb_upper, bb_lower, bb_mid)
        if side == "Buy" and new_tp > current_tp and current_tp > 0:
            result = update_sl_tp(side, new_tp=new_tp)
            if result.get("retCode") == 0:
                updates.append(f"TP→${new_tp}")
        elif side == "Sell" and new_tp < current_tp and current_tp > 0:
            result = update_sl_tp(side, new_tp=new_tp)
            if result.get("retCode") == 0:
                updates.append(f"TP→${new_tp}")

        # --- Ask Claude ---
        prompt = f"""You are managing an open ETH/USDT perpetual position.

Time: {now}
Position: {side} | Entry: ${entry:.2f} | Current: ${price:.2f}
Unrealised PnL: ${pnl:.4f} ({pnl_pct:.2f}%)
Breakeven SL already moved: {be_moved}

Technical Indicators:
- RSI(14): {rsi} (>70 overbought, <30 oversold)
- MACD: {macd} | Signal: {signal} | Histogram: {histogram}
- Bollinger Bands: Upper ${bb_upper} | Mid ${bb_mid} | Lower ${bb_lower}
- EMA20: ${ema20} | EMA50: ${ema50} | Trend: {trend}
- Fear & Greed: {fear_greed}

Make decisions based purely on technical signals.
- CLOSE if RSI reversing, MACD crosses against position, or price hits BB extreme
- HOLD if trend and momentum still confirm direction
- MOVE_BE if profit is sufficient and you want to move SL to breakeven to protect gains — only if not already moved

Respond in this exact format:
DECISION: HOLD or CLOSE or MOVE_BE
REASON: (2 sentences max)"""

        response = ask_claude(prompt)
        decision = "HOLD"
        for line in response.strip().split("\n"):
            if line.startswith("DECISION:"):
                decision = line.replace("DECISION:", "").strip()

        if decision == "CLOSE":
            qty = float(position["size"])
            result = close_position(side, qty)
            if result.get("retCode") == 0:
                if pnl > 0:
                    state["wins"] = state.get("wins", 0) + 1
                else:
                    state["losses"] = state.get("losses", 0) + 1
                state["total_pnl"] = state.get("total_pnl", 0) + pnl
                state["last_position"] = None
                state["be_moved"] = False
                save_state(state)
                send_telegram(f"🔴 <b>CLOSED</b> | ${entry:.2f}→${price:.2f}\nPnL: ${pnl:.4f} ({pnl_pct:.2f}%) | W:{state['wins']} L:{state['losses']}\n\n{response}")
            else:
                send_telegram(f"❌ Close failed: {result.get('retMsg')}")

        elif decision == "MOVE_BE" and not be_moved:
            result = update_sl_tp(side, new_sl=entry)
            if result.get("retCode") == 0:
                state["be_moved"] = True
                save_state(state)
                updates.append(f"SL→BE (${entry:.2f})")
                send_telegram(f"🔒 <b>SL MOVED TO BREAKEVEN</b> ${entry:.2f}\nPrice: ${price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%)\n\n{response}")
            else:
                send_telegram(f"❌ BE move failed: {result.get('retMsg')}")

        else:
            update_str = " | " + " | ".join(updates) if updates else ""
            send_telegram(f"🟡 <b>HOLD {side}</b> | ${price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%){update_str}\nRSI: {rsi} | {trend}\n\n{response}")

        state["last_position"] = {"price": entry, "side": side}
        save_state(state)
        return INTERVAL_IN_POSITION

    else:
        prompt = f"""You are a professional crypto trader. Analyze ETH/USDT perpetual and decide to go LONG, SHORT, or SKIP.

Time: {now}
Current price: ${price:.2f}
24h High: ${max(highs):.2f} | 24h Low: ${min(lows):.2f}

Technical Indicators:
- RSI(14): {rsi} (>70 overbought, <30 oversold)
- MACD: {macd} | Signal: {signal} | Histogram: {histogram}
- Bollinger Bands: Upper ${bb_upper} | Mid ${bb_mid} | Lower ${bb_lower}
- EMA20: ${ema20} | EMA50: ${ema50} | Trend: {trend}
- Fear & Greed: {fear_greed}
- Recent volumes (last 6h): {volumes[-6:]}

Make your decision based purely on technical signals.
- LONG if RSI < 65, MACD bullish, price above EMA20, uptrend confirmed
- SHORT if RSI > 45, MACD bearish, price below EMA20, downtrend confirmed
- SKIP if majority of signals conflict
- SL must be outside BB bands, TP will be calculated automatically

You MUST provide SL as exact price.

Respond in this exact format:
DECISION: LONG or SHORT or SKIP
REASON: (2 sentences max)
SL: $X.XX"""

        response = ask_claude(prompt)
        lines = response.strip().split("\n")
        decision = "SKIP"
        sl = 0.0

        for line in lines:
            if line.startswith("DECISION:"):
                decision = line.replace("DECISION:", "").strip()
            elif line.startswith("SL:"):
                try:
                    sl = float(line.replace("SL:", "").replace("$", "").strip())
                except:
                    pass

        if decision in ("LONG", "SHORT") and sl > 0:
            side = "Buy" if decision == "LONG" else "Sell"
            tp = smart_tp(side, price, bb_upper, bb_lower, bb_mid)
            set_leverage()
            result = place_order(side, sl, tp)
            if result.get("retCode") == 0:
                state["last_position"] = {"price": price, "side": side}
                state["be_moved"] = False
                save_state(state)
                send_telegram(f"🚀 <b>{decision}</b> | ${price:.2f} | SL: ${sl} | TP: ${tp}\nRSI: {rsi} | MACD: {macd} | {trend}\n\n{response}")
            else:
                send_telegram(f"❌ Order failed: {result.get('retMsg')}")
        else:
            send_telegram(f"⏭ <b>SKIP</b> | ${price:.2f} | RSI: {rsi} | {trend}\n\n{response}")

        return INTERVAL_NO_POSITION

def main():
    send_telegram("🤖 <b>ETH Bot Started</b>\n25x | 0.14 ETH | 1h entry scan | 30min position monitor")
    while True:
        try:
            interval = run_cycle()
        except Exception as e:
            send_telegram(f"⚠️ Error: {str(e)}")
            print(f"Error: {e}")
            interval = INTERVAL_NO_POSITION
        time.sleep(interval)

if __name__ == "__main__":
    main()
