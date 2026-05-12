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

def calculate_vwap(candles):
    """VWAP = sum(typical_price * volume) / sum(volume)"""
    total_tp_vol = 0
    total_vol = 0
    for c in candles:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        total_tp_vol += typical_price * c["volume"]
        total_vol += c["volume"]
    if total_vol == 0:
        return candles[-1]["close"]
    return round(total_tp_vol / total_vol, 2)

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

def find_swing_points(candles, lookback=20):
    """Find recent swing highs and lows for liquidity levels"""
    highs = [c["high"] for c in candles[-lookback:]]
    lows = [c["low"] for c in candles[-lookback:]]
    swing_highs = []
    swing_lows = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(round(highs[i], 2))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append(round(lows[i], 2))
    return sorted(swing_highs, reverse=True)[:3], sorted(swing_lows)[:3]

def detect_liquidity_sweep(candles, swing_highs, swing_lows):
    """
    Detect if last 3 candles swept a liquidity level then rejected.
    Returns: sweep_type (BEARISH_SWEEP, BULLISH_SWEEP, NONE), sweep_level
    """
    if len(candles) < 3:
        return "NONE", 0

    last = candles[-1]
    prev = candles[-2]

    # Bearish sweep: wick above swing high, closes back below it
    for high in swing_highs:
        if prev["high"] > high and prev["close"] < high:
            return "BEARISH_SWEEP", high
        if last["high"] > high and last["close"] < high:
            return "BEARISH_SWEEP", high

    # Bullish sweep: wick below swing low, closes back above it
    for low in swing_lows:
        if prev["low"] < low and prev["close"] > low:
            return "BULLISH_SWEEP", low
        if last["low"] < low and last["close"] > low:
            return "BULLISH_SWEEP", low

    return "NONE", 0

def detect_msb(candles, sweep_type):
    """
    After a sweep, detect Market Structure Break.
    BULLISH_SWEEP → look for price breaking above recent high (MSB up)
    BEARISH_SWEEP → look for price breaking below recent low (MSB down)
    """
    if len(candles) < 5 or sweep_type == "NONE":
        return False

    recent_closes = [c["close"] for c in candles[-5:]]
    recent_highs = [c["high"] for c in candles[-5:]]
    recent_lows = [c["low"] for c in candles[-5:]]

    if sweep_type == "BULLISH_SWEEP":
        # Price should break above a recent high after the sweep
        prev_high = max(recent_highs[:-1])
        return recent_closes[-1] > prev_high

    if sweep_type == "BEARISH_SWEEP":
        # Price should break below a recent low after the sweep
        prev_low = min(recent_lows[:-1])
        return recent_closes[-1] < prev_low

    return False

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

    candles_15m = get_candles("15", 200)
    candles_1h = get_candles("60", 50)

    if len(candles_15m) < 30 or len(candles_1h) < 20:
        send_telegram(
            f"⚠️ Not enough candle data\n"
            f"15M: {len(candles_15m)} | 1H: {len(candles_1h)}"
        )
        return

    # --- 15M indicators ---
    closes_15m = [c["close"] for c in candles_15m]
    highs_15m = [c["high"] for c in candles_15m]
    lows_15m = [c["low"] for c in candles_15m]
    volumes_15m = [c["volume"] for c in candles_15m]

    rsi_15m = calculate_rsi(closes_15m)
    ema8_15m = calculate_ema(closes_15m, 8)
    ema21_15m = calculate_ema(closes_15m, 21)
    atr_15m = calculate_atr(candles_15m)
    trend_15m = "UPTREND" if ema8_15m > ema21_15m else "DOWNTREND"
    vwap_15m = calculate_vwap(candles_15m[-50:])

    avg_volume = sum(volumes_15m[-20:]) / 20
    last_volume = volumes_15m[-1]
    volume_surge = round(last_volume / avg_volume, 2) if avg_volume > 0 else 0

    # Swing points and liquidity detection
    swing_highs, swing_lows = find_swing_points(candles_15m, lookback=30)
    sweep_type, sweep_level = detect_liquidity_sweep(candles_15m, swing_highs, swing_lows)
    msb_confirmed = detect_msb(candles_15m, sweep_type)

    # Last 3 candles for Claude context
    last_3 = candles_15m[-3:]
    last_3_summary = [
        f"C{i+1}: O${c['open']:.2f} H${c['high']:.2f} L${c['low']:.2f} Cl${c['close']:.2f} V{c['volume']:.0f}"
        for i, c in enumerate(last_3)
    ]

    # --- 1H indicators ---
    closes_1h = [c["close"] for c in candles_1h]
    ema20_1h = calculate_ema(closes_1h, 20)
    ema50_1h = calculate_ema(closes_1h, 50)
    rsi_1h = calculate_rsi(closes_1h)
    vwap_1h = calculate_vwap(candles_1h)
    trend_1h = "UPTREND" if ema20_1h > ema50_1h else "DOWNTREND"
    price_vs_vwap_1h = "ABOVE" if price > vwap_1h else "BELOW"

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
            f"VWAP: ${vwap_1h} | Price: {price_vs_vwap_1h}\n"
            f"Waiting for SL/TP..."
        )
        return

    prompt = f"""You are a professional scalp trader using Market Structure Break (MSB) + VWAP strategy on ETH/USDT perpetual.

Time: {now}
Current price: ${price:.2f}
Fear & Greed: {fear_greed}

=== 1H TIMEFRAME (Bias Filter) ===
- Trend: {trend_1h}
- VWAP: ${vwap_1h} | Price is {price_vs_vwap_1h} VWAP
- RSI(14): {rsi_1h}
- EMA20: ${ema20_1h} | EMA50: ${ema50_1h}
- Rule: ONLY LONG if price ABOVE 1H VWAP | ONLY SHORT if price BELOW 1H VWAP

=== 15M TIMEFRAME (Entry) ===
- Trend: {trend_15m}
- VWAP: ${vwap_15m}
- RSI(14): {rsi_15m}
- EMA8: ${ema8_15m} | EMA21: ${ema21_15m}
- ATR: {atr_15m}
- Volume surge: {volume_surge}x avg
- Recent swing highs: {swing_highs}
- Recent swing lows: {swing_lows}
- Liquidity sweep detected: {sweep_type} at ${sweep_level}
- MSB confirmed by code: {msb_confirmed}
- Last 3 candles:
  {last_3_summary[0]}
  {last_3_summary[1]}
  {last_3_summary[2]}

=== LIQUIDATION SAFETY ===
- LONG liq: ${liq_long} → SL must be above ${round(liq_long * 1.01, 2)}
- SHORT liq: ${liq_short} → SL must be below ${round(liq_short * 0.99, 2)}

=== MSB + VWAP STRATEGY ===

SETUP RULES:
1. VWAP Bias (1H):
   - Price ABOVE 1H VWAP → only consider LONG
   - Price BELOW 1H VWAP → only consider SHORT
   - Never trade against VWAP bias

2. Liquidity Sweep (15M):
   - BULLISH_SWEEP: price wicked below swing low then closed above → smart money grabbed stops below, now going up
   - BEARISH_SWEEP: price wicked above swing high then closed below → smart money grabbed stops above, now going down
   - No sweep detected → SKIP

3. Market Structure Break (15M):
   - After BULLISH_SWEEP: price breaks above recent high → confirmed LONG
   - After BEARISH_SWEEP: price breaks below recent low → confirmed SHORT
   - No MSB → SKIP

4. Entry confirmation:
   - Volume surge > 1.2x on confirmation candle
   - RSI not extreme (30-70 range)
   - EMA8/21 aligned with trade direction

5. SL/TP:
   - LONG SL: below the sweep wick low (just below the liquidity grab candle)
   - SHORT SL: above the sweep wick high
   - TP: next significant swing high (LONG) or swing low (SHORT)
   - Minimum R:R 1:2

DECISION RULES:
- LONG: price ABOVE 1H VWAP + BULLISH_SWEEP + MSB up + volume surge
- SHORT: price BELOW 1H VWAP + BEARISH_SWEEP + MSB down + volume surge
- SKIP: no sweep, no MSB, wrong VWAP side, weak volume, or R:R < 1:2

Respond in this exact format:
SWEEP: BULLISH or BEARISH or NONE
MSB: YES or NO
VWAP_BIAS: LONG or SHORT
SETUP_QUALITY: STRONG or WEAK or NONE
DECISION: LONG or SHORT or SKIP
REASON: (2 sentences max)
SL: $X.XX
TP: $X.XX"""

    response = ask_claude(prompt)
    lines = response.strip().split("\n")
    decision = "SKIP"
    sl = tp = 0.0
    sweep_result = "NONE"
    msb_result = "NO"
    setup_quality = "NONE"
    vwap_bias = "NONE"

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
        elif line.startswith("SWEEP:"):
            sweep_result = line.replace("SWEEP:", "").strip()
        elif line.startswith("MSB:"):
            msb_result = line.replace("MSB:", "").strip()
        elif line.startswith("SETUP_QUALITY:"):
            setup_quality = line.replace("SETUP_QUALITY:", "").strip()
        elif line.startswith("VWAP_BIAS:"):
            vwap_bias = line.replace("VWAP_BIAS:", "").strip()

    # Hard rules
    if decision in ("LONG", "SHORT") and setup_quality != "STRONG":
        decision = "SKIP"
        response += "\n[Override: Setup quality not STRONG]"

    if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
        side = "Buy" if decision == "LONG" else "Sell"

        if not sl_is_safe(side, price, sl, LEVERAGE):
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nSL ${sl} too close to liq ${liq}")
            return

        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
        if rr < 2:
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nR:R {rr} below 1:2 minimum")
            return

        set_leverage()
        result = place_order(side, sl, tp)
        if result.get("retCode") == 0:
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(
                f"🎯 <b>MSB SCALP {decision}</b>\n"
                f"Price: ${price:.2f} | SL: ${sl} | TP: ${tp}\n"
                f"R:R: 1:{rr} | Liq: ${liq}\n"
                f"Sweep: {sweep_result} | MSB: {msb_result}\n"
                f"VWAP bias: {vwap_bias} | Vol: {volume_surge}x\n\n"
                f"{response}"
            )
        else:
            send_telegram(f"❌ Order failed: {result.get('retMsg')}")

    elif decision == "SKIP":
        send_telegram(
            f"⏭ <b>SKIP</b> — ${price:.2f}\n"
            f"VWAP: ${vwap_1h} ({price_vs_vwap_1h}) | 1H: {trend_1h}\n"
            f"Sweep: {sweep_result} | MSB: {msb_result} | Quality: {setup_quality}\n"
            f"{response}"
        )
    else:
        send_telegram(f"⚠️ Incomplete data, skipping.\n{response}")

def main():
    send_telegram("🎯 <b>ETH MSB Scalp Bot Started</b>\n45x | 0.04 ETH | 15min | MSB + VWAP")
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