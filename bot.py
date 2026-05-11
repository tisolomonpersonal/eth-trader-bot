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
CHECK_INTERVAL = 1800
MIN_LIQ_BUFFER = 0.10  # SL must be at least 30% away from liquidation

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
    r = requests.get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}&interval={interval}&limit={limit}", timeout=10)
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

def calculate_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def get_liquidation_price(side, entry, leverage):
    """Approximate liquidation price at 45x leverage"""
    if side == "Buy":
        return round(entry * (1 - 1 / leverage), 2)
    else:
        return round(entry * (1 + 1 / leverage), 2)

def validate_sl(side, entry, sl, leverage):
    """
    Check SL is safe distance from liquidation.
    Returns (is_safe, liq_price, buffer_pct, adjusted_sl)
    """
    liq_price = get_liquidation_price(side, entry, leverage)

    if side == "Buy":
        # SL must be ABOVE liquidation by at least MIN_LIQ_BUFFER
        liq_to_sl = (sl - liq_price) / liq_price
        is_safe = liq_to_sl >= MIN_LIQ_BUFFER
        if not is_safe:
            # Auto-adjust SL to be MIN_LIQ_BUFFER above liquidation
            adjusted_sl = round(liq_price * (1 + MIN_LIQ_BUFFER), 2)
        else:
            adjusted_sl = sl
    else:
        # SL must be BELOW liquidation by at least MIN_LIQ_BUFFER
        liq_to_sl = (liq_price - sl) / liq_price
        is_safe = liq_to_sl >= MIN_LIQ_BUFFER
        if not is_safe:
            # Auto-adjust SL to be MIN_LIQ_BUFFER below liquidation
            adjusted_sl = round(liq_price * (1 - MIN_LIQ_BUFFER), 2)
        else:
            adjusted_sl = sl

    return is_safe, liq_price, round(liq_to_sl * 100, 2), adjusted_sl

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
    candles_1h = get_candles("60", 100)
    candles_4h = get_candles("240", 100)
    fear_greed = get_fear_greed()
    position = get_position()

    # --- 1H indicators ---
    closes_1h = [c["close"] for c in candles_1h]
    highs_1h = [c["high"] for c in candles_1h]
    lows_1h = [c["low"] for c in candles_1h]
    volumes_1h = [c["volume"] for c in candles_1h]
    rsi_1h = calculate_rsi(closes_1h)
    macd_1h, signal_1h, hist_1h = calculate_macd(closes_1h)
    bb_mid_1h, bb_upper_1h, bb_lower_1h = calculate_bollinger(closes_1h)
    ema20_1h = calculate_ema(closes_1h, 20)
    ema50_1h = calculate_ema(closes_1h, 50)
    atr_1h = calculate_atr(candles_1h)
    trend_1h = "UPTREND" if ema20_1h > ema50_1h else "DOWNTREND"

    # --- 4H indicators ---
    closes_4h = [c["close"] for c in candles_4h]
    highs_4h = [c["high"] for c in candles_4h]
    lows_4h = [c["low"] for c in candles_4h]
    volumes_4h = [c["volume"] for c in candles_4h]
    rsi_4h = calculate_rsi(closes_4h)
    macd_4h, signal_4h, hist_4h = calculate_macd(closes_4h)
    bb_mid_4h, bb_upper_4h, bb_lower_4h = calculate_bollinger(closes_4h)
    ema20_4h = calculate_ema(closes_4h, 20)
    ema50_4h = calculate_ema(closes_4h, 50)
    atr_4h = calculate_atr(candles_4h)
    trend_4h = "UPTREND" if ema20_4h > ema50_4h else "DOWNTREND"

    # Liquidation prices for context
    liq_long = get_liquidation_price("Buy", price, LEVERAGE)
    liq_short = get_liquidation_price("Sell", price, LEVERAGE)

    recent_highs_4h = sorted(highs_4h[-20:], reverse=True)[:5]
    recent_lows_4h = sorted(lows_4h[-20:])[:5]
    recent_highs_1h = sorted(highs_1h[-20:], reverse=True)[:5]
    recent_lows_1h = sorted(lows_1h[-20:])[:5]

    # If already in a position, just report status
    if position:
        side = position["side"]
        entry = float(position["avgPrice"])
        pnl = float(position["unrealisedPnl"])
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
        liq = get_liquidation_price(side, entry, LEVERAGE)
        liq_distance = abs(price - liq) / price * 100
        send_telegram(f"📊 <b>ACTIVE {side}</b> | ${price:.2f}\nEntry: ${entry:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%)\nLiq: ${liq} ({liq_distance:.2f}% away)\n1H: {trend_1h} | 4H: {trend_4h}\nWaiting for SL/TP...")
        return

    # No position — ask Claude
    prompt = f"""You are an expert Elliott Wave trader analyzing ETH/USDT perpetual futures.

Time: {now}
Current price: ${price:.2f}
Fear & Greed: {fear_greed}
Liquidation if LONG at current price: ${liq_long} (2.2% below entry)
Liquidation if SHORT at current price: ${liq_short} (2.2% above entry)

=== 4H TIMEFRAME (Higher Timeframe Trend) ===
- Trend: {trend_4h}
- RSI(14): {rsi_4h}
- MACD: {macd_4h} | Signal: {signal_4h} | Histogram: {hist_4h}
- Bollinger Bands: Upper ${bb_upper_4h} | Mid ${bb_mid_4h} | Lower ${bb_lower_4h}
- EMA20: ${ema20_4h} | EMA50: ${ema50_4h}
- ATR: {atr_4h}
- Recent 4H Swing Highs: {recent_highs_4h}
- Recent 4H Swing Lows: {recent_lows_4h}
- Last 10 closes: {closes_4h[-10:]}
- Last 10 volumes: {volumes_4h[-10:]}

=== 1H TIMEFRAME (Entry Timeframe) ===
- Trend: {trend_1h}
- RSI(14): {rsi_1h}
- MACD: {macd_1h} | Signal: {signal_1h} | Histogram: {hist_1h}
- Bollinger Bands: Upper ${bb_upper_1h} | Mid ${bb_mid_1h} | Lower ${bb_lower_1h}
- EMA20: ${ema20_1h} | EMA50: ${ema50_1h}
- ATR: {atr_1h}
- Recent 1H Swing Highs: {recent_highs_1h}
- Recent 1H Swing Lows: {recent_lows_1h}
- Last 10 closes: {closes_1h[-10:]}
- Last 10 volumes: {volumes_1h[-10:]}

=== ELLIOTT WAVE ANALYSIS INSTRUCTIONS ===
Using the 4H data as your primary wave structure:

1. Identify the current Elliott Wave count (1-2-3-4-5 impulse or A-B-C correction)
2. Wave validation rules:
   - Wave 2 never retraces more than 100% of Wave 1
   - Wave 3 is never the shortest impulse wave
   - Wave 4 never overlaps Wave 1 price territory
   - Wave 3 has highest volume and strongest momentum

3. ONLY enter if confident we are at the START of Wave 3:
   - Wave 1 completed: clear impulse move
   - Wave 2 completed: 38-62% Fibonacci retracement of Wave 1
   - Wave 3 starting: RSI turning from neutral, MACD crossing, volume increasing
   - 4H and 1H trends must AGREE

4. Entry rules:
   - LONG: 4H UPTREND + Wave 3 up starting + 1H confirms bullish
   - SHORT: 4H DOWNTREND + Wave 3 down starting + 1H confirms bearish
   - SKIP: wave count unclear, wrong wave, or timeframes disagree

5. SL placement:
   - LONG: below Wave 2 low
   - SHORT: above Wave 2 high
   - CRITICAL: SL must be at least 30% away from liquidation price
   - LONG liquidation: ${liq_long} → SL must be above ${round(liq_long * 1.30, 2)}
   - SHORT liquidation: ${liq_short} → SL must be below ${round(liq_short * 0.70, 2)}

6. TP: Wave 1 length × 1.618 projected from Wave 2 end

Respond in this exact format:
WAVE_COUNT: (2-3 sentence wave analysis)
4H_TREND: BULLISH or BEARISH or NEUTRAL
1H_TREND: BULLISH or BEARISH or NEUTRAL
WAVE_3_CONFIRMED: YES or NO
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

    # Hard rule — only enter if Wave 3 confirmed
    if decision in ("LONG", "SHORT") and wave3_confirmed != "YES":
        decision = "SKIP"
        response += "\n[Override: Wave 3 not confirmed — skipping]"

    if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
        side = "Buy" if decision == "LONG" else "Sell"

        # --- LIQUIDATION SAFETY CHECK ---
        is_safe, liq_price, buffer_pct, adjusted_sl = validate_sl(side, price, sl, LEVERAGE)

        if not is_safe:
            if adjusted_sl > 0:
                # Auto-adjust SL and warn
                send_telegram(f"⚠️ SL adjusted for safety\nOriginal SL: ${sl} was too close to liq ${liq_price}\nAdjusted SL: ${adjusted_sl} ({buffer_pct:.1f}% from liq)")
                sl = adjusted_sl
            else:
                # Can't make it safe — block trade
                send_telegram(f"🚫 <b>TRADE BLOCKED</b> — SL unsafe\nClaude wanted: {decision} | SL: ${sl}\nLiquidation: ${liq_price} | Buffer: {buffer_pct:.1f}%\nMinimum buffer required: {MIN_LIQ_BUFFER*100:.0f}%")
                return

        set_leverage()
        result = place_order(side, sl, tp)
        if result.get("retCode") == 0:
            _, liq_price, buffer_pct, _ = validate_sl(side, price, sl, LEVERAGE)
            send_telegram(f"🌊 <b>WAVE 3 {decision}</b>\nPrice: ${price:.2f} | SL: ${sl} | TP: ${tp}\nLiq: ${liq_price} | SL buffer: {buffer_pct:.1f}%\n4H: {trend_4h} | 1H: {trend_1h}\n\n{wave_count}\n\n{response}")
        else:
            send_telegram(f"❌ Order failed: {result.get('retMsg')}\n\nClaude wanted: {decision}")

    elif decision == "SKIP":
        send_telegram(f"⏭ <b>SKIP</b> — ${price:.2f}\n4H: {trend_4h} | 1H: {trend_1h}\nWave 3: {wave3_confirmed}\n{response}")
    else:
        send_telegram(f"⚠️ Incomplete data, skipping.\n{response}")

def main():
    send_telegram("🌊 <b>ETH Wave 3 Bot Started</b>\n45x | 0.04 ETH | Hourly | Elliott Wave + 4H + Liq Protection")
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