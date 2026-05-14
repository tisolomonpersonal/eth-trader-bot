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
QTY = 0.04
LEVERAGE = 45
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))  # seconds (default: 1 hour, saves tokens)

# --- RISK / SAFETY GUARDS ---
# NOTE: These are *equity-based* limits (USDT). With very small accounts + 45x leverage,
# equity can swing quickly. Consider lowering leverage in production.
MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "2"))
MAX_CONSEC_LOSS_USD = float(os.environ.get("MAX_CONSEC_LOSS_USD", "4"))

# Pause after volatility spikes (15m ATR as % of price)
VOL_SPIKE_ATR_PCT = float(os.environ.get("VOL_SPIKE_ATR_PCT", "0.02"))  # 2%
VOL_PAUSE_SECONDS = int(os.environ.get("VOL_PAUSE_SECONDS", "1800"))     # 30 minutes

# --- STRATEGY (15M): Claude-led market analysis with indicator context + constraints ---
DECISION_INTERVAL = os.environ.get("DECISION_INTERVAL", "15")  # Bybit kline interval string
DECISION_CANDLE_LIMIT = int(os.environ.get("DECISION_CANDLE_LIMIT", "500"))
MA_PERIOD = int(os.environ.get("MA_PERIOD", "200"))
MA_SLOPE_LOOKBACK = int(os.environ.get("MA_SLOPE_LOOKBACK", "10"))  # candles
MA_DISTANCE_PCT = float(os.environ.get("MA_DISTANCE_PCT", "0.003"))  # 0.3%

MACD_FAST = int(os.environ.get("MACD_FAST", "12"))
MACD_SLOW = int(os.environ.get("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.environ.get("MACD_SIGNAL", "9"))

RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_LONG_MIN = float(os.environ.get("RSI_LONG_MIN", "50"))
RSI_LONG_MAX = float(os.environ.get("RSI_LONG_MAX", "70"))
RSI_SHORT_MIN = float(os.environ.get("RSI_SHORT_MIN", "30"))
RSI_SHORT_MAX = float(os.environ.get("RSI_SHORT_MAX", "50"))

ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_PCT_MIN = float(os.environ.get("ATR_PCT_MIN", "0.003"))  # 0.3% (avoid dead chop)
ATR_PCT_MAX = float(os.environ.get("ATR_PCT_MAX", "0.02"))   # 2.0% (avoid chaos)

SL_ATR_MULT = float(os.environ.get("SL_ATR_MULT", "1.5"))
TP_ATR_MULT = float(os.environ.get("TP_ATR_MULT", "2.5"))

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")  # common: UNIFIED / CONTRACT
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE = Path(__file__).with_name("log.txt")

def utc_now_ts():
    return int(datetime.now(timezone.utc).timestamp())

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {
        "day": None,
        "start_equity": None,
        "entry_equity": None,
        "entry_time": None,
        "entry_price": None,
        "entry_side": None,
        "had_position": False,
        "consecutive_loss": 0.0,
        "total_pnl": 0.0,
        "trade_history": [],
        "paused_until": 0,
        "pause_reason": ""
    }

def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def append_log(event_type, payload):
    """
    Append-only JSONL log for durable audit trail on persistent storage.
    Each line is: {"ts": "...", "event": "...", ...payload}
    """
    try:
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "event": event_type
        }
        if isinstance(payload, dict):
            record.update(payload)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except:
        pass

def performance_summary(state):
    """
    Returns a small summary dict to feed Claude each cycle (keeps prompt small).
    """
    hist = state.get("trade_history") or []
    pnls = []
    for t in hist:
        try:
            pnls.append(float(t.get("pnl") or 0.0))
        except:
            pass
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = len(pnls)
    winrate = (len(wins) / total) * 100 if total else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    last_pnl = pnls[-1] if pnls else 0.0
    return {
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(winrate, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "last_pnl": round(last_pnl, 4)
    }

def sign_get_request(query: str):
    """
    Bybit v5 GET signing: sign = HMAC_SHA256(secret, timestamp + apiKey + recvWindow + queryString)
    """
    timestamp = get_server_time()
    recv_window = "5000"
    param_str = timestamp + BYBIT_API_KEY + recv_window + query
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": recv_window
    }
    return headers

def get_wallet_equity_usdt():
    """
    Returns total equity in USDT (best-effort parsing across Bybit response shapes).
    """
    query = f"accountType={BYBIT_ACCOUNT_TYPE}&coin=USDT"
    headers = sign_get_request(query)
    r = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}", headers=headers, timeout=10)
    data = r.json()
    if not data.get("result") or not data["result"].get("list"):
        return None
    item = data["result"]["list"][0]

    # Shape A: top-level totalEquity string
    if "totalEquity" in item and item["totalEquity"] not in (None, ""):
        try:
            return float(item["totalEquity"])
        except:
            pass

    # Shape B: item["coin"] list with USDT element
    coins = item.get("coin") or []
    for c in coins:
        if (c.get("coin") or "").upper() == "USDT":
            for k in ("equity", "walletBalance", "availableToWithdraw", "availableBalance"):
                if k in c and c[k] not in (None, ""):
                    try:
                        return float(c[k])
                    except:
                        continue

    # Last resort: try common keys
    for k in ("totalWalletBalance", "totalMarginBalance"):
        if k in item and item[k] not in (None, ""):
            try:
                return float(item[k])
            except:
                pass
    return None

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

def calculate_ema_series(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    ema = values[0]
    out = [ema]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out

def calculate_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def calculate_macd(closes):
    """
    Returns (macd_line, signal_line, histogram) series lists (floats).
    """
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 5:
        return [], [], []
    ema_fast = calculate_ema_series(closes, MACD_FAST)
    ema_slow = calculate_ema_series(closes, MACD_SLOW)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema_series(macd_line, MACD_SIGNAL)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist

def get_trade_decision_1h(candles_1h):
    """
    Deterministic strategy:
    - Trend filter: close above/below MA200 + MA200 slope
    - Anti-whipsaw: distance from MA200 (pct)
    - Momentum trigger: MACD histogram crosses 0 on candle close
    - RSI filter to avoid extremes
    - ATR% filter to avoid dead chop / chaos
    Returns: (decision, reason, indicators_dict)
    """
    closes = [c["close"] for c in candles_1h]
    if len(closes) < MA_PERIOD + MA_SLOPE_LOOKBACK + 5:
        return "SKIP", "Not enough 1H candle history for MA200/slope.", {}

    close = closes[-1]
    ma200 = calculate_sma(closes, MA_PERIOD)
    ma200_prev = sum(closes[-(MA_PERIOD + MA_SLOPE_LOOKBACK):-MA_SLOPE_LOOKBACK]) / MA_PERIOD
    slope = ma200 - ma200_prev
    slope_dir = "UP" if slope > 0 else "DOWN" if slope < 0 else "FLAT"

    dist_pct = abs(close - ma200) / ma200 if ma200 else 0

    rsi = calculate_rsi(closes, period=RSI_PERIOD)

    macd_line, sig_line, hist = calculate_macd(closes)
    if len(hist) < 3:
        return "SKIP", "Not enough data for MACD.", {}

    # candle-close confirmation: look at the last two closed values
    macd_cross_up = hist[-1] > 0 and hist[-2] <= 0
    macd_cross_down = hist[-1] < 0 and hist[-2] >= 0

    atr = calculate_atr(candles_1h, period=ATR_PERIOD_1H)
    atr_pct = (atr / close) if close > 0 else 0

    # Filters first (avoid whipsaw)
    if dist_pct < MA_DISTANCE_PCT:
        return "SKIP", f"Too close to MA{MA_PERIOD} (distance {dist_pct*100:.2f}%).", {
            "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
        }

    if atr_pct < ATR_PCT_MIN_1H:
        return "SKIP", f"ATR too low (ATR% {atr_pct*100:.2f}%), likely chop.", {
            "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
        }

    if atr_pct > ATR_PCT_MAX_1H:
        return "SKIP", f"ATR too high (ATR% {atr_pct*100:.2f}%), too volatile.", {
            "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
        }

    # LONG setup
    if close > ma200 and slope > 0 and macd_cross_up and (RSI_LONG_MIN <= rsi <= RSI_LONG_MAX):
        return "LONG", "Price above rising MA200 + MACD up-cross + RSI filter passed.", {
            "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
        }

    # SHORT setup
    if close < ma200 and slope < 0 and macd_cross_down and (RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX):
        return "SHORT", "Price below falling MA200 + MACD down-cross + RSI filter passed.", {
            "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
        }

    # Otherwise skip with a short reason
    if close > ma200 and slope <= 0:
        reason = "Above MA200 but MA200 slope not up (possible range)."
    elif close < ma200 and slope >= 0:
        reason = "Below MA200 but MA200 slope not down (possible range)."
    elif close > ma200 and not macd_cross_up:
        reason = "Above MA200 but no MACD up-cross on close."
    elif close < ma200 and not macd_cross_down:
        reason = "Below MA200 but no MACD down-cross on close."
    else:
        reason = "No valid setup."

    return "SKIP", reason, {
        "ma200": ma200, "slope_dir": slope_dir, "dist_pct": dist_pct, "rsi": rsi, "atr": atr, "atr_pct": atr_pct
    }

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

def find_swing_points(candles, lookback=30):
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
    if len(candles) < 3:
        return "NONE", 0
    last = candles[-1]
    prev = candles[-2]
    for high in swing_highs:
        if prev["high"] > high and prev["close"] < high:
            return "BEARISH_SWEEP", high
        if last["high"] > high and last["close"] < high:
            return "BEARISH_SWEEP", high
    for low in swing_lows:
        if prev["low"] < low and prev["close"] > low:
            return "BULLISH_SWEEP", low
        if last["low"] < low and last["close"] > low:
            return "BULLISH_SWEEP", low
    return "NONE", 0

def detect_msb(candles, sweep_type):
    if len(candles) < 5 or sweep_type == "NONE":
        return False
    recent_closes = [c["close"] for c in candles[-5:]]
    recent_highs = [c["high"] for c in candles[-5:]]
    recent_lows = [c["low"] for c in candles[-5:]]
    if sweep_type == "BULLISH_SWEEP":
        prev_high = max(recent_highs[:-1])
        return recent_closes[-1] > prev_high
    if sweep_type == "BEARISH_SWEEP":
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
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text
        except Exception as e:
            # transient overloads happen occasionally
            if "overloaded" in str(e).lower() and attempt < retries - 1:
                time.sleep(20)
            else:
                raise e

def parse_claude_trade_response(text):
    """
    Expected format:
      DECISION: LONG|SHORT|SKIP
      REASON: ...
      SL: $X.XX
      TP: $X.XX
    """
    decision = "SKIP"
    reason = ""
    sl = 0.0
    tp = 0.0
    personal_message = ""
    for raw in (text or "").strip().splitlines():
        line = raw.strip()
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.upper().startswith("PERSONAL_MESSAGE:"):
            personal_message = line.split(":", 1)[1].strip()
        elif line.upper().startswith("SL:"):
            try:
                sl = float(line.split(":", 1)[1].replace("$", "").strip())
            except:
                pass
        elif line.upper().startswith("TP:"):
            try:
                tp = float(line.split(":", 1)[1].replace("$", "").strip())
            except:
                pass
    if decision not in ("LONG", "SHORT", "SKIP"):
        decision = "SKIP"
    return decision, reason, sl, tp, personal_message

def run_cycle():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now(timezone.utc).date().isoformat()

    price = get_price()
    fear_greed = get_fear_greed()
    position = get_position()
    equity = get_wallet_equity_usdt()

    # Claude runs the decision-making (and proposes SL/TP). If there's no key, never enter.
    if not ANTHROPIC_API_KEY and not position:
        send_telegram("⚠️ ANTHROPIC_API_KEY is missing. Skipping entries this cycle.")
        return

    # If we can't fetch equity, we can't enforce the $-based risk limits safely.
    # We'll still report an active position, but we will not open new ones.
    if equity is None and not position:
        send_telegram("⚠️ Could not fetch wallet equity (USDT). Skipping entries this cycle.")
        return

    state = load_state()
    if state.get("day") != today or state.get("start_equity") is None:
        trade_history = state.get("trade_history") or []
        total_pnl = float(state.get("total_pnl") or 0.0)
        state = {
            "day": today,
            "start_equity": equity,
            "entry_equity": None,
            "entry_time": None,
            "entry_price": None,
            "entry_side": None,
            "had_position": False,
            "consecutive_loss": 0.0,
            "total_pnl": total_pnl,
            "trade_history": trade_history[-50:],
            "paused_until": 0,
            "pause_reason": ""
        }
        save_state(state)

    candles = get_candles(DECISION_INTERVAL, DECISION_CANDLE_LIMIT)
    if len(candles) < (MA_PERIOD + MA_SLOPE_LOOKBACK + 5):
        send_telegram(f"⚠️ Not enough candle data\n{DECISION_INTERVAL}M: {len(candles)}")
        return

    # Dashboard Stop/Start control. Existing positions are still reported, but no new
    # entries are opened while trading is disabled.
    if not state.get("trading_enabled", True) and not position:
        send_telegram(f"STOPPED - ${price:.2f}\nTrading is disabled from the dashboard.")
        append_log("STOPPED_SKIP", {"price": round(price, 2), "reason": "trading_enabled=false"})
        return

    # ATR used for volatility pause (same timeframe as decision by default)
    atr_decision = calculate_atr(candles, period=ATR_PERIOD)

    liq_long = get_liquidation_price("Buy", price, LEVERAGE)
    liq_short = get_liquidation_price("Sell", price, LEVERAGE)

    # --- VOLATILITY PAUSE ---
    atr_pct = (atr_decision / price) if price > 0 else 0
    if atr_pct >= VOL_SPIKE_ATR_PCT:
        pause_until = utc_now_ts() + VOL_PAUSE_SECONDS
        if pause_until > int(state.get("paused_until") or 0):
            state["paused_until"] = pause_until
            state["pause_reason"] = f"VOL_SPIKE (15m ATR%={atr_pct*100:.2f}%)"
            save_state(state)
            send_telegram(
                f"⏸ <b>PAUSE</b> — Volatility spike\n"
                f"{DECISION_INTERVAL}m ATR: {atr_decision} ({atr_pct*100:.2f}%) | Price: ${price:.2f}\n"
                f"Pausing for {int(VOL_PAUSE_SECONDS/60)}m."
            )
            append_log("PAUSE", {
                "reason": state.get("pause_reason"),
                "paused_until": state.get("paused_until"),
                "price": round(price, 2),
                "atr": atr_decision,
                "atr_pct": round(atr_pct, 6),
                "interval": DECISION_INTERVAL
            })

    # --- POSITION TRANSITION TRACKING (for consecutive loss) ---
    # If we *had* a position previously and now it's closed, update consecutive loss.
    if state.get("had_position") and not position:
        if equity is not None and state.get("entry_equity") is not None:
            trade_pnl = float(equity) - float(state["entry_equity"])
            if trade_pnl < 0:
                state["consecutive_loss"] = round(float(state.get("consecutive_loss", 0.0)) + abs(trade_pnl), 4)
            else:
                state["consecutive_loss"] = 0.0
            # performance memory
            state["total_pnl"] = round(float(state.get("total_pnl", 0.0)) + trade_pnl, 4)
            hist = state.get("trade_history") or []
            hist.append({
                "entry_time": state.get("entry_time"),
                "exit_time": now,
                "side": state.get("entry_side"),
                "entry_price": state.get("entry_price"),
                "exit_price": price,
                "pnl": round(trade_pnl, 4)
            })
            state["trade_history"] = hist[-50:]
            append_log("CLOSE", {
                "side": state.get("entry_side"),
                "entry_time": state.get("entry_time"),
                "exit_time": now,
                "entry_price": state.get("entry_price"),
                "exit_price": round(price, 2),
                "pnl": round(trade_pnl, 4),
                "equity": round(float(equity), 4) if equity is not None else None,
                "consecutive_loss": state.get("consecutive_loss"),
                "total_pnl": state.get("total_pnl")
            })
        state["had_position"] = False
        state["entry_equity"] = None
        state["entry_time"] = None
        state["entry_price"] = None
        state["entry_side"] = None
        save_state(state)

    # --- DAILY LOSS LIMIT (equity-based) ---
    if equity is not None and state.get("start_equity") is not None:
        daily_loss = max(0.0, float(state["start_equity"]) - float(equity))
        if daily_loss >= MAX_DAILY_LOSS_USD:
            # Pause until next UTC day
            tomorrow = datetime.now(timezone.utc).date().toordinal() + 1
            pause_until = int(datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc).timestamp())
            state["paused_until"] = max(int(state.get("paused_until") or 0), pause_until)
            state["pause_reason"] = f"DAILY_LOSS_LIMIT (down ${daily_loss:.2f} / ${MAX_DAILY_LOSS_USD:.2f})"
            save_state(state)

    # --- CONSECUTIVE LOSS LIMIT (cumulative since last win) ---
    if float(state.get("consecutive_loss") or 0.0) >= MAX_CONSEC_LOSS_USD:
        tomorrow = datetime.now(timezone.utc).date().toordinal() + 1
        pause_until = int(datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc).timestamp())
        state["paused_until"] = max(int(state.get("paused_until") or 0), pause_until)
        state["pause_reason"] = f"CONSEC_LOSS_LIMIT (${state['consecutive_loss']:.2f} / ${MAX_CONSEC_LOSS_USD:.2f})"
        save_state(state)

    # If paused, do not enter new trades
    if utc_now_ts() < int(state.get("paused_until") or 0) and not position:
        reason = state.get("pause_reason") or "PAUSED"
        eq_txt = f"${equity:.2f}" if equity is not None else "n/a"
        send_telegram(
            f"⏸ <b>PAUSED</b> — ${price:.2f}\n"
            f"Reason: {reason}\n"
            f"Equity: {eq_txt} | Start: {state.get('start_equity')}\n"
            f"{DECISION_INTERVAL}m ATR%: {atr_pct*100:.2f}%"
        )
        append_log("PAUSED_SKIP", {
            "reason": reason,
            "paused_until": state.get("paused_until"),
            "price": round(price, 2),
            "equity": round(float(equity), 4) if equity is not None else None
        })
        return

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
            f"Waiting for SL/TP..."
        )
        # Ensure state knows we have a position (for later PnL tracking). If the bot restarts
        # mid-position, this sets a baseline equity (best effort).
        if not state.get("had_position"):
            state["had_position"] = True
            if state.get("entry_equity") is None and equity is not None:
                state["entry_equity"] = float(equity)
            state["entry_time"] = state.get("entry_time") or now
            state["entry_price"] = state.get("entry_price") or entry
            state["entry_side"] = state.get("entry_side") or side
            save_state(state)
        return

    # --- Claude in charge: compute indicators, send to Claude, execute if allowed ---
    closes_tf = [c["close"] for c in candles]
    close_tf = closes_tf[-1]
    ma200 = calculate_sma(closes_tf, MA_PERIOD)
    ma200_prev = sum(closes_tf[-(MA_PERIOD + MA_SLOPE_LOOKBACK):-MA_SLOPE_LOOKBACK]) / MA_PERIOD
    slope = ma200 - ma200_prev
    slope_dir = "UP" if slope > 0 else "DOWN" if slope < 0 else "FLAT"
    dist_pct = abs(close_tf - ma200) / ma200 if ma200 else 0

    rsi_tf = calculate_rsi(closes_tf, period=RSI_PERIOD)
    macd_line, sig_line, hist = calculate_macd(closes_tf)
    macd_hist_last = hist[-1] if hist else 0
    macd_hist_prev = hist[-2] if len(hist) >= 2 else 0
    # Easier trigger (higher frequency): momentum direction + histogram sign,
    # not only a 0-cross event.
    macd_momentum = "UP" if macd_hist_last > macd_hist_prev else "DOWN" if macd_hist_last < macd_hist_prev else "FLAT"

    atr_tf = calculate_atr(candles, period=ATR_PERIOD)
    atr_tf_pct = (atr_tf / close_tf) if close_tf > 0 else 0

    # Performance context for Claude (summary + last 5 trades)
    hist = state.get("trade_history") or []
    last5 = hist[-5:]
    perf_lines = []
    for t in last5:
        try:
            perf_lines.append(f"{t.get('side')} pnl={t.get('pnl')} at {t.get('exit_time')}")
        except:
            pass
    perf_text = "\n".join(perf_lines) if perf_lines else "No closed trades recorded yet."
    daily_pnl = (float(equity) - float(state.get("start_equity"))) if (equity is not None and state.get("start_equity") is not None) else 0.0
    perf = performance_summary(state)

    last_candles = candles[-10:]
    last_candles_summary = [
        f"C{i+1}: O{c['open']:.2f} H{c['high']:.2f} L{c['low']:.2f} C{c['close']:.2f} V{c['volume']:.0f}"
        for i, c in enumerate(last_candles)
    ]

    prompt = f"""You are Claude, an autonomous crypto trader and account manager.
Your job: study the market and decide whether to enter a trade right now.

SYMBOL: {SYMBOL} perpetual (Bybit)
Time: {now}
Current price: ${price:.2f}
Fear & Greed: {fear_greed}

=== PERFORMANCE MEMORY (context) ===
Today's PnL (equity-based): ${daily_pnl:.2f}
Consecutive loss (equity-based): ${float(state.get("consecutive_loss") or 0.0):.2f}
Lifetime PnL (equity-based): ${float(state.get("total_pnl") or 0.0):.2f}
Stats: trades={perf['trades']} winrate={perf['winrate']}% avg_win={perf['avg_win']} avg_loss={perf['avg_loss']} last_pnl={perf['last_pnl']}
Last trades:
{perf_text}

=== {DECISION_INTERVAL}M INDICATORS ===
Close: ${close_tf:.2f}
MA{MA_PERIOD}: ${ma200:.2f}
MA{MA_PERIOD} slope ({MA_SLOPE_LOOKBACK} bars): {slope_dir} (delta {slope:.4f})
Distance from MA{MA_PERIOD}: {dist_pct*100:.2f}%
RSI({RSI_PERIOD}): {rsi_tf}
MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) histogram: prev={macd_hist_prev:.6f} last={macd_hist_last:.6f} momentum={macd_momentum}
ATR({ATR_PERIOD}): {atr_tf} ({atr_tf_pct*100:.2f}%)

Last 10 candles:
{last_candles_summary[0]}
{last_candles_summary[1]}
{last_candles_summary[2]}
{last_candles_summary[3]}
{last_candles_summary[4]}
{last_candles_summary[5]}
{last_candles_summary[6]}
{last_candles_summary[7]}
{last_candles_summary[8]}
{last_candles_summary[9]}

=== HARD CONSTRAINTS (the system will block trades that violate these) ===
1) You MUST output a clear DECISION: LONG, SHORT, or SKIP.
2) If LONG/SHORT, you MUST provide SL and TP (2 decimals).
3) Aim for minimum R:R >= 1.5 (TP distance / SL distance).
4) Avoid liquidation risk (45x leverage): keep SL far enough from liquidation.
5) If today's PnL is near the daily limit or consecutive losses are high, be more conservative.

You may use any analysis method you want (trend, range, structure, momentum, mean reversion).
Only propose a trade if you believe the setup has a real edge; otherwise SKIP.

Respond ONLY in this exact format:
DECISION: LONG or SHORT or SKIP
REASON: (max 2 sentences)
PERSONAL_MESSAGE: (one short sentence to the user, optional)
SL: $X.XX
TP: $X.XX
"""

    claude_text = ask_claude(prompt)
    decision, reason, sl, tp, personal_message = parse_claude_trade_response(claude_text)

    # Always notify every check (decision + reason)
    send_telegram(
        f"🧠 <b>CLAUDE CHECK</b> — {now}\n"
        f"Price: ${price:.2f} | Decision: <b>{decision}</b>\n"
        f"Reason: {reason if reason else '(none)'}\n"
        f"{('Message: ' + personal_message + chr(10)) if personal_message else ''}"
        f"MA{MA_PERIOD}: ${ma200:.2f} | Dist: {dist_pct*100:.2f}% | Slope: {slope_dir}\n"
        f"RSI: {rsi_tf} | MACD momentum: {macd_momentum} | ATR%: {atr_tf_pct*100:.2f}%\n"
        f"Proposed SL/TP: ${sl:.2f} / ${tp:.2f}"
    )
    append_log("CHECK", {
        "now": now,
        "price": round(price, 2),
        "equity": round(float(equity), 4) if equity is not None else None,
        "decision": decision,
        "reason": reason,
        "personal_message": personal_message,
        "sl": round(sl, 2) if sl else 0.0,
        "tp": round(tp, 2) if tp else 0.0,
        "ma200": round(ma200, 4) if ma200 is not None else None,
        "ma_slope": slope_dir,
        "ma_dist_pct": round(dist_pct, 6),
        "rsi": rsi_tf,
        "macd_momentum": macd_momentum,
        "macd_hist_prev": round(macd_hist_prev, 8),
        "macd_hist_last": round(macd_hist_last, 8),
        "atr": atr_tf,
        "atr_pct": round(atr_tf_pct, 6),
        "interval": DECISION_INTERVAL,
        "daily_pnl": round(daily_pnl, 4),
        "consecutive_loss": float(state.get("consecutive_loss") or 0.0),
        "total_pnl": float(state.get("total_pnl") or 0.0)
    })

    if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
        side = "Buy" if decision == "LONG" else "Sell"

        if not sl_is_safe(side, price, sl, LEVERAGE):
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nSL ${sl} too close to liq ${liq}\nClaude: {decision} | {reason}")
            append_log("BLOCK", {"type": "SL_TOO_CLOSE_TO_LIQ", "decision": decision, "reason": reason, "sl": sl, "liq": liq})
            return

        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
        if rr < 1.5:
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nR:R {rr} below 1.5 minimum\nClaude: {decision} | {reason}")
            append_log("BLOCK", {"type": "RR_TOO_LOW", "decision": decision, "reason": reason, "rr": rr, "sl": sl, "tp": tp})
            return

        set_leverage()
        equity_before = equity
        result = place_order(side, sl, tp)
        if result.get("retCode") == 0:
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(
                f"🎯 <b>CLAUDE {decision}</b>\n"
                f"Time: {now}\n"
                f"Price: ${price:.2f} | SL: ${sl} | TP: ${tp}\n"
                f"R:R: 1:{rr} | Liq: ${liq}\n"
                f"Reason: {reason}\n\n"
                f"{claude_text}"
            )
            state["had_position"] = True
            if equity_before is not None:
                state["entry_equity"] = float(equity_before)
            state["entry_time"] = now
            state["entry_price"] = price
            state["entry_side"] = side
            save_state(state)
            append_log("ORDER", {
                "side": side,
                "decision": decision,
                "price": round(price, 2),
                "sl": sl,
                "tp": tp,
                "rr": rr,
                "liq": liq,
                "equity_before": round(float(equity_before), 4) if equity_before is not None else None
            })
        else:
            send_telegram(f"❌ Order failed: {result.get('retMsg')}")
            append_log("ORDER_FAIL", {
                "side": side,
                "decision": decision,
                "price": round(price, 2),
                "sl": sl,
                "tp": tp,
                "retCode": result.get("retCode"),
                "retMsg": result.get("retMsg")
            })
    elif decision == "SKIP":
        return
    else:
        send_telegram(f"⚠️ Claude output incomplete; skipping.\n{claude_text}")

def main():
    send_telegram(f"📈 <b>ETH Bot Started</b>\n45x | 0.04 ETH | {int(CHECK_INTERVAL/60)}m checks | Claude in charge ({DECISION_INTERVAL}m MACD/RSI/MA{MA_PERIOD})")
    append_log("START", {"symbol": SYMBOL, "qty": QTY, "leverage": LEVERAGE, "check_interval": CHECK_INTERVAL})
    while True:
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {str(e)}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
            append_log("ERROR", {"error": err, "traceback": traceback.format_exc()})
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
