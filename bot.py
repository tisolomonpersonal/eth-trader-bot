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
MAX_RISK_USD = 5.0
MIN_EARLY_PROFIT = 1.0
CIRCUIT_BREAKER_LOSS = 5.0
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

def set_leverage(leverage):
    params = {"category": "linear", "symbol": SYMBOL, "buyLeverage": str(leverage), "sellLeverage": str(leverage)}
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

def place_order(side, sl, tp, qty):
    position_idx = 1 if side == "Buy" else 2
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
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

def calculate_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def detect_market_regime(closes, highs, lows, atr, bb_upper, bb_lower):
    bb_width = (bb_upper - bb_lower) / closes[-1] * 100
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema_spread = abs(ema20 - ema50) / closes[-1] * 100
    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    range_pct = (recent_high - recent_low) / closes[-1] * 100
    if bb_width > 3.0 and ema_spread > 0.5 and range_pct > 3.0:
        return "TRENDING"
    else:
        return "SIDEWAYS"

def calculate_safe_leverage(price, sl_price, qty):
    sl_distance_pct = abs(price - sl_price) / price
    if sl_distance_pct == 0:
        return 25
    position_value = qty * price
    max_leverage = MAX_RISK_USD / (position_value * sl_distance_pct)
    leverage = max(25, min(45, int(max_leverage)))
    return leverage

def liquidation_price(side, entry, leverage):
    if side == "Buy":
        return round(entry * (1 - 1 / leverage), 2)
    else:
        return round(entry * (1 + 1 / leverage), 2)

def smart_tp(side, price, bb_upper, bb_lower, bb_mid, regime):
    bb_range = bb_upper - bb_lower
    multiplier = 2.0 if regime == "TRENDING" else 1.0
    if side == "Buy":
        tp = round(bb_mid + (bb_range * multiplier), 2)
        return max(tp, bb_upper)
    else:
        tp = round(bb_mid - (bb_range * multiplier), 2)
        return min(tp, bb_lower)

def is_moving_toward_tp(side, entry, current_price, tp):
    """Check if price is moving toward TP not away from it"""
    if side == "Buy":
        return current_price > entry
    else:
        return current_price < entry

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
                max_tokens=700,
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
    atr = calculate_atr(candles)
    trend = "UPTREND" if ema20 > ema50 else "DOWNTREND"
    regime = detect_market_regime(closes, highs, lows, atr, bb_upper, bb_lower)

    # --- Detect TP/SL hit (only if we didn't manually close) ---
    if state.get("last_position") and not position:
        last = state["last_position"]
        # Only report if we didn't already report a manual close
        if not last.get("manually_closed", False):
            entry = last["price"]
            side = last["side"]
            qty = last.get("qty", 0.08)
            pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
            pnl_usd = pnl_pct / 100 * qty * entry
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
        qty = float(position["size"])
        tp = float(position.get("takeProfit", 0))
        pnl_pct = ((price - entry) / entry) * 100 * (1 if side == "Buy" else -1)
        current_tp = float(position.get("takeProfit", 0))
        be_moved = state.get("be_moved", False)
        updates = []

        # --- CIRCUIT BREAKER: force close if loss >= $5 ---
        if pnl <= -CIRCUIT_BREAKER_LOSS:
            result = close_position(side, qty)
            if result.get("retCode") == 0:
                state["losses"] = state.get("losses", 0) + 1
                state["total_pnl"] = state.get("total_pnl", 0) + pnl
                state["last_position"] = {"price": entry, "side": side, "qty": qty, "manually_closed": True}
                state["be_moved"] = False
                save_state(state)
                send_telegram(f"🚨 <b>CIRCUIT BREAKER</b> — Force closed!\nLoss hit -${CIRCUIT_BREAKER_LOSS}\nEntry: ${entry:.2f} → ${price:.2f}\nPnL: ${pnl:.4f} | W:{state['wins']} L:{state['losses']}")
            else:
                send_telegram(f"🚨 Circuit breaker failed to close: {result.get('retMsg')}")
            return INTERVAL_NO_POSITION

        # --- Trailing TP ---
        new_tp = smart_tp(side, price, bb_upper, bb_lower, bb_mid, regime)
        if side == "Buy" and new_tp > current_tp and current_tp > 0:
            result = update_sl_tp(side, new_tp=new_tp)
            if result.get("retCode") == 0:
                updates.append(f"TP→${new_tp}")
        elif side == "Sell" and new_tp < current_tp and current_tp > 0:
            result = update_sl_tp(side, new_tp=new_tp)
            if result.get("retCode") == 0:
                updates.append(f"TP→${new_tp}")

        # --- Check if price is moving toward TP ---
        heading_to_tp = is_moving_toward_tp(side, entry, price, tp)

        prompt = f"""You are managing an open ETH/USDT perpetual position.

Time: {now}
Position: {side} | Entry: ${entry:.2f} | Current: ${price:.2f}
Unrealised PnL: ${pnl:.4f} ({pnl_pct:.2f}%)
Price moving toward TP: {heading_to_tp}
Market Regime: {regime}
Breakeven SL already moved: {be_moved}

Technical Indicators:
- RSI(14): {rsi} (>70 overbought, <30 oversold)
- MACD: {macd} | Signal: {signal} | Histogram: {histogram}
- Bollinger Bands: Upper ${bb_upper} | Mid ${bb_mid} | Lower ${bb_lower}
- EMA20: ${ema20} | EMA50: ${ema50} | Trend: {trend}
- ATR(14): {atr}
- Fear & Greed: {fear_greed}

Rules — follow strictly:
- CLOSE only if price is moving AWAY from TP and reversal is confirmed by multiple indicators
- CLOSE_EARLY only if ALL of these are true: PnL >= ${MIN_EARLY_PROFIT}, price is moving AWAY from TP, AND 60-70%+ reversal probability confirmed by RSI + MACD + BB together
- DO NOT close early if price is still moving toward TP — let it run
- HOLD if price is moving toward TP or trend still confirms direction
- MOVE_BE only when profit is clear, momentum still strong, not too early

Respond in this exact format:
DECISION: HOLD or CLOSE or CLOSE_EARLY or MOVE_BE
REASON: (2 sentences max)"""

        response = ask_claude(prompt)
        decision = "HOLD"
        for line in response.strip().split("\n"):
            if line.startswith("DECISION:"):
                decision = line.replace("DECISION:", "").strip()

        # --- Enforce minimum profit rule for CLOSE_EARLY ---
        if decision == "CLOSE_EARLY" and pnl < MIN_EARLY_PROFIT:
            decision = "HOLD"
            response += f"\n[Override: PnL ${pnl:.4f} below ${MIN_EARLY_PROFIT} minimum — holding]"

        # --- Enforce: don't close if heading to TP ---
        if decision in ("CLOSE", "CLOSE_EARLY") and heading_to_tp and pnl > 0:
            decision = "HOLD"
            response += "\n[Override: Price heading toward TP — holding]"

        if decision in ("CLOSE", "CLOSE_EARLY"):
            result = close_position(side, qty)
            if result.get("retCode") == 0:
                if pnl > 0:
                    state["wins"] = state.get("wins", 0) + 1
                else:
                    state["losses"] = state.get("losses", 0) + 1
                state["total_pnl"] = state.get("total_pnl", 0) + pnl
                state["last_position"] = {"price": entry, "side": side, "qty": qty, "manually_closed": True}
                state["be_moved"] = False
                save_state(state)
                label = "💰 EARLY EXIT" if decision == "CLOSE_EARLY" else "🔴 CLOSED"
                send_telegram(f"<b>{label}</b> | ${entry:.2f}→${price:.2f}\nPnL: ${pnl:.4f} ({pnl_pct:.2f}%) | W:{state['wins']} L:{state['losses']}\n\n{response}")
            else:
                send_telegram(f"❌ Close failed: {result.get('retMsg')}")

        elif decision == "MOVE_BE" and not be_moved:
            result = update_sl_tp(side, new_sl=entry)
            if result.get("retCode") == 0:
                state["be_moved"] = True
                save_state(state)
                updates.append(f"SL→BE (${entry:.2f})")
                send_telegram(f"🔒 <b>SL→BREAKEVEN</b> ${entry:.2f}\nPrice: ${price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%)\n\n{response}")
            else:
                send_telegram(f"❌ BE move failed: {result.get('retMsg')}")

        else:
            update_str = " | " + " | ".join(updates) if updates else ""
            send_telegram(f"🟡 <b>HOLD {side}</b> | ${price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:.2f}%){update_str}\nRSI: {rsi} | {regime} | {trend}\n\n{response}")

        state["last_position"] = {"price": entry, "side": side, "qty": qty, "manually_closed": False}
        save_state(state)
        return INTERVAL_IN_POSITION

    else:
        prompt = f"""You are a professional crypto trader. Analyze ETH/USDT perpetual and decide to go LONG, SHORT, or SKIP.

Time: {now}
Current price: ${price:.2f}
24h High: ${max(highs):.2f} | 24h Low: ${min(lows):.2f}
Market Regime: {regime}

Technical Indicators:
- RSI(14): {rsi} (>70 overbought, <30 oversold)
- MACD: {macd} | Signal: {signal} | Histogram: {histogram}
- Bollinger Bands: Upper ${bb_upper} | Mid ${bb_mid} | Lower ${bb_lower}
- EMA20: ${ema20} | EMA50: ${ema50} | Trend: {trend}
- ATR(14): {atr}
- Fear & Greed: {fear_greed}
- Recent volumes (last 6h): {volumes[-6:]}

Strategy guide:
- TRENDING market: follow the trend, wider SL, larger TP, favor momentum entries
- SIDEWAYS market: fade extremes, buy near BB lower, sell near BB upper, tighter TP

Risk rules:
- Max risk per trade: $5.00
- Choose ETH quantity between 0.08 and 0.14
- Choose leverage between 25x and 45x so that if SL is hit, loss does not exceed $5.00
- SL must be at least 50% away from liquidation price
- SL must be outside BB bands

Entry rules:
- LONG if RSI < 65, MACD bullish, price above EMA20, uptrend confirmed
- SHORT if RSI > 45, MACD bearish, price below EMA20, downtrend confirmed
- SKIP if majority of signals conflict

Respond in this exact format:
DECISION: LONG or SHORT or SKIP
REASON: (2 sentences max)
SL: $X.XX
TP: $X.XX
LEVERAGE: X
QTY: X.XX"""

        response = ask_claude(prompt)
        lines = response.strip().split("\n")
        decision = "SKIP"
        sl = tp = 0.0
        claude_leverage = None
        claude_qty = None

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
            elif line.startswith("LEVERAGE:"):
                try:
                    claude_leverage = int(line.replace("LEVERAGE:", "").replace("x", "").strip())
                except:
                    pass
            elif line.startswith("QTY:"):
                try:
                    claude_qty = float(line.replace("QTY:", "").strip())
                except:
                    pass

        if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
            side = "Buy" if decision == "LONG" else "Sell"

            qty = max(0.08, min(0.14, claude_qty)) if claude_qty else 0.08
            safe_leverage = calculate_safe_leverage(price, sl, qty)
            if claude_leverage:
                final_leverage = min(claude_leverage, safe_leverage)
                final_leverage = max(25, min(45, final_leverage))
            else:
                final_leverage = safe_leverage

            # Verify SL 50%+ from liquidation
            for lev in range(final_leverage, 24, -1):
                liq = liquidation_price(side, price, lev)
                if side == "Buy":
                    gap = (sl - liq) / liq * 100
                else:
                    gap = (liq - sl) / liq * 100
                if gap >= 50:
                    final_leverage = lev
                    break

            set_leverage(final_leverage)
            result = place_order(side, sl, tp, qty)
            if result.get("retCode") == 0:
                state["last_position"] = {"price": price, "side": side, "qty": qty, "manually_closed": False}
                state["be_moved"] = False
                save_state(state)
                send_telegram(f"🚀 <b>{decision}</b> | ${price:.2f} | {regime}\nQTY: {qty} ETH | Lev: {final_leverage}x | Max loss: ~$5\nSL: ${sl} | TP: ${tp}\nRSI: {rsi} | {trend}\n\n{response}")
            else:
                send_telegram(f"❌ Order failed: {result.get('retMsg')}")
        else:
            send_telegram(f"⏭ <b>SKIP</b> | ${price:.2f} | {regime} | RSI: {rsi} | {trend}\n\n{response}")

        return INTERVAL_NO_POSITION

def main():
    send_telegram("🤖 <b>ETH Bot Started</b>\n25-45x | 0.08-0.14 ETH | $5 Circuit Breaker | Smart Exit")
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
