import requests, json, os, time, hmac, hashlib
import traceback
from datetime import datetime, timezone
import anthropic

# --- CONFIG ---
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL = "ETHUSDT"
QTY = 0.04
LEVERAGE = 45
CHECK_INTERVAL = 600

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

def get_candles(interval, limit):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}&interval={interval}&limit={limit}",
            timeout=10
        )
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        candles = data["result"]["list"]
        if len(candles) == 0:
            return []
        return [{"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in reversed(candles)]
    except Exception as e:
        print(f"Candle fetch error ({interval}): {e}")
        return []

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

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
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
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def calculate_macd(closes):
    if len(closes) < 26:
        return 0, 0, 0
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd_line = round(ema12 - ema26, 4)
    signal = round(macd_line * 0.9, 4)
    histogram = round(macd_line - signal, 4)
    return macd_line, signal, histogram

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((p - sma) ** 2 for p in recent) / period
    std = variance ** 0.5
    upper = round(sma + 2 * std, 2)
    lower = round(sma - 2 * std, 2)
    return round(sma, 2), upper, lower

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def get_liquidation_price(side, entry, leverage):
    if side == "Buy":
        return round(entry * (1 - 1 / leverage), 2)
    else:
        return round(entry * (1 + 1 / leverage), 2)

def sl_is_safe(side, entry, sl, leverage):
    liq = get_liquidation_price(side, entry, leverage)
    if side == "Buy":
        return sl > liq * 1.01
    else:
        return sl < liq * 0.99

def ask_claude(prompt, retries=3):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(retries):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=800,
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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    price = get_price()
    fear_greed = get_fear_greed()
    position = get_position()

    # Fetch candles with debug info
    candles_10m = get_candles("10", 200)
    candles_1h = get_candles("60", 50)

    # Safety check with debug counts
    if len(candles_10m) < 30 or len(candles_1h) < 20:
        send_telegram(
            f"⚠️ Not enough candle data\n"
            f"10M candles: {len(candles_10m)}\n"
            f"1H candles: {len(candles_1h)}\n"
            f"Retrying next cycle..."
        )
        return

    # --- 10M indicators ---
    closes_10m = [c["close"] for c in candles_10m]
    highs_10m = [c["high"] for c in candles_10m]
    lows_10m = [c["low"] for c in candles_10m]
    volumes_10m = [c["volume"] for c in candles_10m]
    rsi_10m = calculate_rsi(closes_10m)
    macd_10m, signal_10m, hist_10m = calculate_macd(closes_10m)
    bb_mid_10m, bb_upper_10m, bb_lower_10m = calculate_bollinger(closes_10m)
    ema8_10m = calculate_ema(closes_10m, 8)
    ema21_10m = calculate_ema(closes_10m, 21)
    ema50_10m = calculate_ema(closes_10m, 50)
    atr_10m = calculate_atr(candles_10m)
    trend_10m = "UPTREND" if ema8_10m > ema21_10m else "DOWNTREND"

    recent_highs_10m = sorted(highs_10m[-30:], reverse=True)[:5]
    recent_lows_10m = sorted(lows_10m[-30:])[:5]
    avg_volume_10m = sum(volumes_10m[-20:]) / 20 if len(volumes_10m) >= 20 else sum(volumes_10m) / len(volumes_10m)
    last_volume_10m = volumes_10m[-1] if volumes_10m else 0
    volume_surge = round(last_volume_10m / avg_volume_10m, 2) if avg_volume_10m > 0 else 0

    # --- 1H indicators ---
    closes_1h = [c["close"] for c in candles_1h]
    ema20_1h = calculate_ema(closes_1h, 20)
    ema50_1h = calculate_ema(closes_1h, 50)
    rsi_1h = calculate_rsi(closes_1h)
    trend_1h = "UPTREND" if ema20_1h > ema50_1h else "DOWNTREND"

    # Liquidation prices
    liq_long = get_liquidation_price("Buy", price, LEVERAGE)
    liq_short = get_liquidation_price("Sell", price, LEVERAGE)

    # If already in position, report and wait
    if position:
        side = position["side"]
        entry = float(position["avgPrice"])
        pnl = float(position["unrealisedPnl"])
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
        liq = get_liquidation_price(side, entry, LEVERAGE)
        liq_dist = round(abs(price - liq) / price * 100, 2)
        send_telegram(
            f"📊 <b>ACTIVE {side}</b> | ${price:.2f}\n"
            f"Entry: ${entry:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%)\n"
            f"Liq: ${liq} ({liq_dist}% away)\n"
            f"10M: {trend_10m} | 1H: {trend_1h}\n"
            f"Waiting for SL/TP..."
        )
        return

    prompt = f"""You are an expert Elliott Wave scalper trading ETH/USDT perpetual on 10-minute charts.

Time: {now}
Current price: ${price:.2f}
Fear & Greed: {fear_greed}

=== 1H TIMEFRAME (Trend Filter) ===
- Trend: {trend_1h}
- RSI(14): {rsi_1h}
- EMA20: ${ema20_1h} | EMA50: ${ema50_1h}

=== 10M TIMEFRAME (Scalping Timeframe) ===
- Trend: {trend_10m}
- RSI(14): {rsi_10m}
- MACD: {macd_10m} | Signal: {signal_10m} | Histogram: {hist_10m}
- Bollinger Bands: Upper ${bb_upper_10m} | Mid ${bb_mid_10m} | Lower ${bb_lower_10m}
- EMA8: ${ema8_10m} | EMA21: ${ema21_10m} | EMA50: ${ema50_10m}
- ATR(14): {atr_10m}
- Volume surge vs 20-candle avg: {volume_surge}x
- Recent 10M Swing Highs: {recent_highs_10m}
- Recent 10M Swing Lows: {recent_lows_10m}
- Last 15 closes: {closes_10m[-15:]}
- Last 15 volumes: {volumes_10m[-15:]}

=== LIQUIDATION SAFETY ===
- LONG liquidation: ${liq_long} → SL must be above ${round(liq_long * 1.01, 2)}
- SHORT liquidation: ${liq_short} → SL must be below ${round(liq_short * 0.99, 2)}

=== SCALPING STRATEGY ===

STEP 1 — Elliott Wave on 10M:
- Identify wave structure using swing highs/lows and last 15 closes
- Wave 3 characteristics: strongest momentum, volume surge > 1.3x, MACD peak
- Wave 2 retracement: 38-62% of Wave 1 (Fibonacci)
- ONLY enter at START of Wave 3

STEP 2 — Momentum Confirmation:
- EMA8 crossing above/below EMA21 in trade direction
- MACD histogram turning positive (LONG) or negative (SHORT)
- RSI between 40-70 for LONG, 30-60 for SHORT
- Volume surge > 1.2x on entry candle

STEP 3 — Trend Filter:
- 1H trend must agree with 10M direction
- Never trade against 1H trend

STEP 4 — Scalp SL/TP:
- SL: just below/above last 10M swing low/high (0.2% to 0.5% from entry)
- TP: 0.5% to 1.5% from entry (Wave 3 = 1.618 x Wave 1)
- SL must be above liquidation + 1% buffer
- Risk:Reward must be at least 1:2

ENTRY RULES:
- LONG: 1H UPTREND + Wave 3 up + EMA8 > EMA21 + MACD bullish + volume surge
- SHORT: 1H DOWNTREND + Wave 3 down + EMA8 < EMA21 + MACD bearish + volume surge
- SKIP: wave unclear, momentum weak, trends disagree, volume flat, R:R < 1:2

Respond in this exact format:
WAVE_COUNT: (2-3 sentence wave analysis on 10M)
MOMENTUM: STRONG_BULL or STRONG_BEAR or WEAK or NEUTRAL
WAVE_3_CONFIRMED: YES or NO
1H_TREND: BULLISH or BEARISH
10M_TREND: BULLISH or BEARISH
DECISION: LONG or SHORT or SKIP
REASON: (2 sentences max)
SL: $X.XX
TP: $X.XX"""

    response = ask_claude(prompt)
    lines = response.strip().split("\n")
    decision = "SKIP"
    sl = tp = 0.0
    wave_count = ""
    wave3_confirmed = "NO"
    momentum = "NEUTRAL"

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
        elif line.startswith("WAVE_COUNT:"):
            wave_count = line.replace("WAVE_COUNT:", "").strip()
        elif line.startswith("WAVE_3_CONFIRMED:"):
            wave3_confirmed = line.replace("WAVE_3_CONFIRMED:", "").strip()
        elif line.startswith("MOMENTUM:"):
            momentum = line.replace("MOMENTUM:", "").strip()

    # Hard rules
    if decision in ("LONG", "SHORT") and wave3_confirmed != "YES":
        decision = "SKIP"
        response += "\n[Override: Wave 3 not confirmed]"

    if decision in ("LONG", "SHORT") and momentum not in ("STRONG_BULL", "STRONG_BEAR"):
        decision = "SKIP"
        response += "\n[Override: Momentum not strong enough]"

    if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
        side = "Buy" if decision == "LONG" else "Sell"

        if not sl_is_safe(side, price, sl, LEVERAGE):
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nSL ${sl} too close to liquidation ${liq}\nSkipping.")
            return

        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
        if rr < 2:
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nR:R {rr} below 1:2 minimum\nSkipping.")
            return

        set_leverage()
        result = place_order(side, sl, tp)
        if result.get("retCode") == 0:
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(
                f"🌊 <b>WAVE 3 SCALP {decision}</b>\n"
                f"Price: ${price:.2f} | SL: ${sl} | TP: ${tp}\n"
                f"R:R: 1:{rr} | Liq: ${liq}\n"
                f"Momentum: {momentum} | Vol surge: {volume_surge}x\n"
                f"1H: {trend_1h} | 10M: {trend_10m}\n\n"
                f"{wave_count}\n\n{response}"
            )
        else:
            send_telegram(f"❌ Order failed: {result.get('retMsg')}\n\nClaude wanted: {decision}")

    elif decision == "SKIP":
        send_telegram(
            f"⏭ <b>SKIP</b> — ${price:.2f}\n"
            f"1H: {trend_1h} | 10M: {trend_10m}\n"
            f"Wave 3: {wave3_confirmed} | Momentum: {momentum}\n"
            f"{response}"
        )
    else:
        send_telegram(f"⚠️ Incomplete data, skipping.\n{response}")

def main():
    send_telegram("🌊 <b>ETH Scalp Bot Started</b>\n45x | 0.04 ETH | 10min | Elliott Wave + Momentum")
    while True:
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {str(e)}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()