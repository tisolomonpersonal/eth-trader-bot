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
QTY = 0.08
LEVERAGE = 45
CHECK_INTERVAL = 3600

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
    param_str = timestamp + BYBIT_API_KEY + "5000" + json.dumps(params, separators=(',', ':'), ensure_ascii=False)
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000"
    }
    return headers

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL, "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    timestamp = get_server_time()
    param_str = timestamp + BYBIT_API_KEY + "5000" + json.dumps(params, separators=(',', ':'), ensure_ascii=False)
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000"
    }
    requests.post("https://api.bybit.com/v5/position/set-leverage", json=params, headers=headers, timeout=10)

def get_price():
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles():
    r = requests.get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}&interval=60&limit=24", timeout=10)
    candles = r.json()["result"]["list"]
    return [{"open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]} for c in reversed(candles)]

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
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(QTY),
        "stopLoss": str(round(sl, 2)),
        "takeProfit": str(round(tp, 2)),
        "slTriggerBy": "MarkPrice",
        "tpTriggerBy": "MarkPrice"
    }
    headers = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create", json=params, headers=headers, timeout=10)
    return r.json()

def close_position(side, qty):
    close_side = "Sell" if side == "Buy" else "Buy"
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "reduceOnly": True
    }
    headers = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create", json=params, headers=headers, timeout=10)
    return r.json()

def ask_claude(prompt):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def run_cycle():
    price = get_price()
    candles = get_candles()
    fear_greed = get_fear_greed()
    position = get_position()

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if position:
        side = position["side"]
        entry = float(position["avgPrice"])
        pnl = float(position["unrealisedPnl"])
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)

        prompt = f"""You are managing an open ETH/USDT perpetual position at 45x leverage.

Time: {now}
Position: {side} | Entry: ${entry:.2f} | Current: ${price:.2f}
Unrealised PnL: ${pnl:.4f} ({pnl_pct:.2f}%)
Fear & Greed: {fear_greed}
Recent closes: {closes[-6:]}

Should you HOLD or CLOSE this position?
- CLOSE if momentum is reversing or risk is too high
- HOLD if trend is still strong

Respond in this exact format:
DECISION: HOLD or CLOSE
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
                send_telegram(f"🔴 <b>POSITION CLOSED</b>\nEntry: ${entry:.2f} → Exit: ${price:.2f}\nPnL: ${pnl:.4f} ({pnl_pct:.2f}%)\n\n{response}")
            else:
                send_telegram(f"❌ Close failed: {result.get('retMsg')}")
        else:
            send_telegram(f"🟡 <b>HOLDING {side}</b>\nPrice: ${price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%)\n\n{response}")

    else:
        prompt = f"""You are a professional crypto trader. Analyze ETH/USDT perpetual and decide to go LONG, SHORT, or SKIP.

Time: {now}
Current price: ${price:.2f}
Fear & Greed: {fear_greed}
24h High: ${max(highs):.2f} | 24h Low: ${min(lows):.2f}
Recent closes (last 6h): {closes[-6:]}
Recent volumes (last 6h): {volumes[-6:]}

Leverage: 45x — liquidation is ~2.2% against you. Be conservative.
Trade size: 0.08 ETH

You MUST provide SL and TP prices (not percentages).

Respond in this exact format:
DECISION: LONG or SHORT or SKIP
REASON: (2 sentences max)
SL: $X.XX
TP: $X.XX"""

        response = ask_claude(prompt)
        lines = response.strip().split("\n")
        decision = "SKIP"
        sl = tp = 0.0

        for line in lines:
            if line.startswith("DECISION:"):
                decision = line.replace("DECISION:", "").strip()
            elif line.startswith("SL:"):
                try:
                    sl = float(line.replace("SL:", "").replace("$", "").strip())
                except:
                    pass
            elif line.startswith("TP:"):
                try:
                    tp = float(line.replace("TP:", "").replace("$", "").strip())
                except:
                    pass

        if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
            side = "Buy" if decision == "LONG" else "Sell"
            set_leverage()
            result = place_order(side, sl, tp)
            if result.get("retCode") == 0:
                send_telegram(f"🚀 <b>{decision}</b>\nPrice: ${price:.2f} | SL: ${sl} | TP: ${tp}\n\n{response}")
            else:
                send_telegram(f"❌ Order failed: {result.get('retMsg')}\n\nClaude wanted: {decision}")
        elif decision == "SKIP":
            send_telegram(f"⏭ <b>SKIP</b> — ${price:.2f}\n{response}")
        else:
            send_telegram(f"⚠️ Claude gave incomplete data, skipping.\n{response}")

def main():
    send_telegram("🤖 <b>ETH Derivatives Bot Started</b>\n45x Leverage | 0.08 ETH | Hourly checks")
    while True:
        try:
            run_cycle()
        except Exception as e:
            send_telegram(f"⚠️ Error: {str(e)}")
            print(f"Error: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
