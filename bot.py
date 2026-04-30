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
TRADE_SIZE = 80  # USD
STATE_FILE = "/tmp/bot_state.json"

# Ghana is UTC+0 (no daylight saving)
# 3pm Ghana = 15:00 UTC, 6pm Ghana = 18:00 UTC
ENTRY_HOUR = 15
CHECK_HOUR = 18

def send_telegram(message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

def get_price():
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles():
    r = requests.get(f"https://api.bybit.com/v5/market/kline?category=spot&symbol={SYMBOL}&interval=60&limit=200", timeout=10)
    candles = r.json()["result"]["list"]
    return [{"open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]} for c in reversed(candles)]

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()["data"][0]
        return f"{data['value']} ({data['value_classification']})"
    except:
        return "unavailable"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"position": None, "date": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1000000))

def place_order(side, qty_usdt=None, qty_eth=None):
    timestamp = get_server_time()
    if side == "Buy":
        params = {"category": "spot", "symbol": SYMBOL, "side": "Buy",
                  "orderType": "Market", "qty": str(qty_usdt), "marketUnit": "quoteCoin"}
    else:
        params = {"category": "spot", "symbol": SYMBOL, "side": "Sell",
                  "orderType": "Market", "qty": str(round(qty_eth, 6)), "marketUnit": "baseCoin"}
    param_str = timestamp + BYBIT_API_KEY + "5000" + json.dumps(params, separators=(',', ':'))
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": timestamp,
               "X-BAPI-SIGN": sign, "X-BAPI-RECV-WINDOW": "5000"}
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

def run_entry():
    """3pm Ghana: Claude analyzes and decides whether to buy"""
    price = get_price()
    candles = get_candles()
    fear_greed = get_fear_greed()

    recent = candles[-24:]
    highs = [float(c["high"]) for c in recent]
    lows = [float(c["low"]) for c in recent]
    closes = [float(c["close"]) for c in recent]
    volumes = [float(c["volume"]) for c in recent]

    prompt = f"""You are a professional crypto trader. Analyze ETH/USDT and decide whether to BUY or SKIP.

Current price: ${price}
Fear & Greed Index: {fear_greed}
Last 24h — High: ${max(highs):.2f}, Low: ${min(lows):.2f}
Recent closes (last 6h): {closes[-6:]}
Recent volumes (last 6h): {volumes[-6:]}

Rules:
- Only BUY if there is a clear short-term upside opportunity
- SKIP if trend is unclear or risky
- Trade size is $80 USDT spot

Respond in this exact format:
DECISION: BUY or SKIP
REASON: (2 sentences max)
TARGET: $X.XX (if BUY)
STOPLOSS: $X.XX (if BUY)"""

    response = ask_claude(prompt)
    lines = response.strip().split("\n")
    decision = "SKIP"
    for line in lines:
        if line.startswith("DECISION:"):
            decision = line.replace("DECISION:", "").strip()

    state = load_state()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    if decision == "BUY":
        result = place_order("Buy", qty_usdt=TRADE_SIZE)
        if result.get("retCode") == 0:
            state["position"] = {"price": price, "qty": TRADE_SIZE / price, "date": today}
            state["date"] = today
            save_state(state)
            send_telegram(f"🔵 <b>CLAUDE BOUGHT ETH</b>\nPrice: ${price:.2f}\n\n{response}")
        else:
            send_telegram(f"❌ Buy failed: {result.get('retMsg')}\n\nClaude wanted to buy:\n{response}")
    else:
        send_telegram(f"⏭ <b>CLAUDE SKIPPED</b>\nPrice: ${price:.2f}\n\n{response}")

def run_check():
    """6pm Ghana: Claude checks open position and decides to hold or sell"""
    state = load_state()
    if not state.get("position"):
        send_telegram("ℹ️ <b>6PM Check</b>: No open position.")
        return

    price = get_price()
    position = state["position"]
    buy_price = position["price"]
    qty_eth = position["qty"]
    pnl_pct = ((price - buy_price) / buy_price) * 100
    pnl_usd = (price - buy_price) * qty_eth

    prompt = f"""You are a professional crypto trader managing an open ETH/USDT spot position.

Entry price: ${buy_price:.2f}
Current price: ${price:.2f}
PnL: {pnl_pct:.2f}% (${pnl_usd:.4f})
Position size: ~$80 USDT

Should you HOLD or SELL?
- SELL if profit is good enough or trend is weakening
- SELL if loss is getting worse
- HOLD only if momentum is clearly still positive

Respond in this exact format:
DECISION: HOLD or SELL
REASON: (2 sentences max)"""

    response = ask_claude(prompt)
    lines = response.strip().split("\n")
    decision = "HOLD"
    for line in lines:
        if line.startswith("DECISION:"):
            decision = line.replace("DECISION:", "").strip()

    if decision == "SELL":
        result = place_order("Sell", qty_eth=qty_eth)
        if result.get("retCode") == 0:
            state["position"] = None
            save_state(state)
            send_telegram(f"💰 <b>CLAUDE SOLD ETH</b>\nEntry: ${buy_price:.2f} → Exit: ${price:.2f}\nPnL: {pnl_pct:.2f}% (${pnl_usd:.4f})\n\n{response}")
        else:
            send_telegram(f"❌ Sell failed: {result.get('retMsg')}\n\nClaude wanted to sell:\n{response}")
    else:
        send_telegram(f"🟡 <b>CLAUDE HOLDING</b>\nPrice: ${price:.2f} | PnL: {pnl_pct:.2f}%\n\n{response}")

def main():
    send_telegram("🤖 <b>ETH Trader Bot Started</b>\nWaiting for 3PM (entry) and 6PM (check) Ghana time...")
    print("Bot running...")
    while True:
        now = datetime.utcnow()
        hour = now.hour
        minute = now.minute

        if hour == ENTRY_HOUR and minute == 0:
            print("Running entry check...")
            run_entry()
            time.sleep(61)
        elif hour == CHECK_HOUR and minute == 0:
            print("Running position check...")
            run_check()
            time.sleep(61)
        else:
            time.sleep(30)

if __name__ == "__main__":
    main()
