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
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))

MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "2"))
MAX_CONSEC_LOSS_USD = float(os.environ.get("MAX_CONSEC_LOSS_USD", "4"))
VOL_SPIKE_ATR_PCT = float(os.environ.get("VOL_SPIKE_ATR_PCT", "0.02"))
VOL_PAUSE_SECONDS = int(os.environ.get("VOL_PAUSE_SECONDS", "1800"))

DECISION_INTERVAL = os.environ.get("DECISION_INTERVAL", "15")
DECISION_CANDLE_LIMIT = int(os.environ.get("DECISION_CANDLE_LIMIT", "500"))
MA_PERIOD = int(os.environ.get("MA_PERIOD", "200"))
MA_SLOPE_LOOKBACK = int(os.environ.get("MA_SLOPE_LOOKBACK", "10"))
MA_DISTANCE_PCT = float(os.environ.get("MA_DISTANCE_PCT", "0.003"))

MACD_FAST = int(os.environ.get("MACD_FAST", "12"))
MACD_SLOW = int(os.environ.get("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.environ.get("MACD_SIGNAL", "9"))

RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_LONG_MIN = float(os.environ.get("RSI_LONG_MIN", "50"))
RSI_LONG_MAX = float(os.environ.get("RSI_LONG_MAX", "70"))
RSI_SHORT_MIN = float(os.environ.get("RSI_SHORT_MIN", "30"))
RSI_SHORT_MAX = float(os.environ.get("RSI_SHORT_MAX", "50"))

ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_PCT_MIN = float(os.environ.get("ATR_PCT_MIN", "0.003"))
ATR_PCT_MAX = float(os.environ.get("ATR_PCT_MAX", "0.02"))

SL_ATR_MULT = float(os.environ.get("SL_ATR_MULT", "1.5"))
TP_ATR_MULT = float(os.environ.get("TP_ATR_MULT", "2.5"))

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE = Path(__file__).with_name("log.txt")

# ─── thread-stop flag (set by app.py) ───────────────────────────────────────
_stop_flag = False

def request_stop():
    global _stop_flag
    _stop_flag = True

def clear_stop():
    global _stop_flag
    _stop_flag = False

def is_stop_requested():
    return _stop_flag
# ────────────────────────────────────────────────────────────────────────────


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
        "lifetime_pnl": 0.0,
        "trade_history": [],
        "paused_until": 0,
        "pause_reason": "",
        "trading_enabled": True,
    }


def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except:
        pass


def append_log(event_type, payload):
    try:
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "event": event_type,
        }
        if isinstance(payload, dict):
            record.update(payload)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except:
        pass


def performance_summary(state):
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
        "last_pnl": round(last_pnl, 4),
    }


def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1000000))


def sign_get_request(query: str):
    timestamp = get_server_time()
    recv_window = "5000"
    param_str = timestamp + BYBIT_API_KEY + recv_window + query
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": recv_window,
    }


def get_wallet_equity_usdt():
    # Try both account types automatically
    for acct_type in [BYBIT_ACCOUNT_TYPE, "CONTRACT", "UNIFIED", "SPOT"]:
        try:
            query = f"accountType={acct_type}&coin=USDT"
            headers = sign_get_request(query)
            r = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}", headers=headers, timeout=10)
            data = r.json()
            print(f"[wallet] accountType={acct_type} retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

            if data.get("retCode") != 0:
                continue
            result_list = data.get("result", {}).get("list", [])
            if not result_list:
                continue

            item = result_list[0]

            # Shape A: top-level totalEquity
            val = item.get("totalEquity")
            if val not in (None, ""):
                try:
                    equity = float(val)
                    if equity > 0:
                        print(f"[wallet] found totalEquity={equity} via accountType={acct_type}")
                        return equity
                except:
                    pass

            # Shape B: totalWalletBalance / totalMarginBalance
            for k in ("totalWalletBalance", "totalMarginBalance"):
                val = item.get(k)
                if val not in (None, ""):
                    try:
                        equity = float(val)
                        if equity > 0:
                            print(f"[wallet] found {k}={equity} via accountType={acct_type}")
                            return equity
                    except:
                        pass

            # Shape C: coin list
            coins = item.get("coin") or []
            for c in coins:
                if (c.get("coin") or "").upper() == "USDT":
                    for k in ("equity", "walletBalance", "availableToWithdraw", "availableBalance"):
                        val = c.get(k)
                        if val not in (None, ""):
                            try:
                                equity = float(val)
                                if equity >= 0:
                                    print(f"[wallet] found coin.{k}={equity} via accountType={acct_type}")
                                    return equity
                            except:
                                pass

            print(f"[wallet] accountType={acct_type} — no usable equity field. item keys: {list(item.keys())}")

        except Exception as e:
            print(f"[wallet] accountType={acct_type} error: {e}")
            continue

    print("[wallet] all account types exhausted — returning None")
    return None


def send_telegram(message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except:
        pass


def sign_request(params):
    timestamp = get_server_time()
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    param_str = timestamp + BYBIT_API_KEY + "5000" + body
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json",
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
            timeout=10,
        )
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        candles = data["result"]["list"]
        if not candles:
            return []
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(candles)
        ]
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
    sign = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000",
    }
    try:
        r = requests.get(f"https://api.bybit.com/v5/position/list?{query}", headers=headers, timeout=10)
        data = r.json()
        positions = data.get("result", {}).get("list", [])
        for p in positions:
            if float(p.get("size", 0)) > 0:
                return p
    except Exception as e:
        print(f"get_position error: {e}")
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
        "tpTriggerBy": "MarkPrice",
    }
    headers, body = sign_request(params)
    r = requests.post("https://api.bybit.com/v5/order/create", data=body, headers=headers, timeout=10)
    return r.json()


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


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_macd(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 5:
        return [], [], []
    ema_fast = calculate_ema_series(closes, MACD_FAST)
    ema_slow = calculate_ema_series(closes, MACD_SLOW)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema_series(macd_line, MACD_SIGNAL)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
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
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < retries - 1:
                time.sleep(20)
            else:
                raise e


def parse_claude_trade_response(text):
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

    if not ANTHROPIC_API_KEY and not position:
        send_telegram("⚠️ ANTHROPIC_API_KEY is missing. Skipping entries this cycle.")
        return

    if equity is None and not position:
        send_telegram("⚠️ Could not fetch wallet equity (USDT). Skipping entries this cycle.")
        return

    state = load_state()

    # Reset daily state
    if state.get("day") != today or state.get("start_equity") is None:
        trade_history = state.get("trade_history") or []
        total_pnl = float(state.get("total_pnl") or 0.0)
        lifetime_pnl = float(state.get("lifetime_pnl") or total_pnl)
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
            "lifetime_pnl": lifetime_pnl,
            "trade_history": trade_history[-50:],
            "paused_until": 0,
            "pause_reason": "",
            "trading_enabled": True,
        }
        save_state(state)

    # Update live fields for dashboard
    state["equity"] = equity
    state["price"] = price
    # Live PnL for active position
    if position:
        state["live_pnl"] = float(position.get("unrealisedPnl", 0))
        state["position_side"] = position.get("side")
        state["entry_price"] = float(position.get("avgPrice", 0))
        state["mark_price"] = price
    else:
        state["live_pnl"] = None
        state["position_side"] = None
        state["entry_price"] = None
        state["mark_price"] = None

    if equity is not None and state.get("start_equity") is not None:
        state["daily_pnl"] = float(equity) - float(state["start_equity"])
    else:
        state["daily_pnl"] = None

    state["trades_today"] = len([
        t for t in state.get("trade_history", [])
        if str(t.get("exit_time", "")).startswith(today)
    ])
    save_state(state)

    candles = get_candles(DECISION_INTERVAL, DECISION_CANDLE_LIMIT)
    if len(candles) < (MA_PERIOD + MA_SLOPE_LOOKBACK + 5):
        send_telegram(f"⚠️ Not enough candle data\n{DECISION_INTERVAL}M: {len(candles)}")
        return

    # Dashboard stop control
    if not state.get("trading_enabled", True) and not position:
        send_telegram(f"STOPPED - ${price:.2f}\nTrading is disabled from the dashboard.")
        append_log("STOPPED_SKIP", {"price": round(price, 2), "reason": "trading_enabled=false"})
        return

    atr_decision = calculate_atr(candles, period=ATR_PERIOD)
    liq_long = get_liquidation_price("Buy", price, LEVERAGE)
    liq_short = get_liquidation_price("Sell", price, LEVERAGE)

    # Volatility pause
    atr_pct = (atr_decision / price) if price > 0 else 0
    if atr_pct >= VOL_SPIKE_ATR_PCT:
        pause_until = utc_now_ts() + VOL_PAUSE_SECONDS
        if pause_until > int(state.get("paused_until") or 0):
            state["paused_until"] = pause_until
            state["pause_reason"] = f"VOL_SPIKE (ATR%={atr_pct*100:.2f}%)"
            save_state(state)
            send_telegram(
                f"⏸ <b>PAUSE</b> — Volatility spike\n"
                f"ATR: {atr_decision} ({atr_pct*100:.2f}%) | Price: ${price:.2f}\n"
                f"Pausing for {int(VOL_PAUSE_SECONDS/60)}m."
            )
            append_log("PAUSE", {"reason": state.get("pause_reason"), "price": round(price, 2)})

    # Position closed detection
    if state.get("had_position") and not position:
        if equity is not None and state.get("entry_equity") is not None:
            trade_pnl = float(equity) - float(state["entry_equity"])
            if trade_pnl < 0:
                state["consecutive_loss"] = round(float(state.get("consecutive_loss", 0.0)) + abs(trade_pnl), 4)
            else:
                state["consecutive_loss"] = 0.0
            state["total_pnl"] = round(float(state.get("total_pnl", 0.0)) + trade_pnl, 4)
            state["lifetime_pnl"] = round(float(state.get("lifetime_pnl", 0.0)) + trade_pnl, 4)
            hist = state.get("trade_history") or []
            hist.append({
                "entry_time": state.get("entry_time"),
                "exit_time": now,
                "side": state.get("entry_side"),
                "entry_price": state.get("entry_price"),
                "exit_price": price,
                "pnl": round(trade_pnl, 4),
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
            })
        state["had_position"] = False
        state["entry_equity"] = None
        state["entry_time"] = None
        state["entry_price"] = None
        state["entry_side"] = None
        save_state(state)

    # Daily loss limit
    if equity is not None and state.get("start_equity") is not None:
        daily_loss = max(0.0, float(state["start_equity"]) - float(equity))
        if daily_loss >= MAX_DAILY_LOSS_USD:
            tomorrow = datetime.now(timezone.utc).date().toordinal() + 1
            pause_until = int(datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc).timestamp())
            state["paused_until"] = max(int(state.get("paused_until") or 0), pause_until)
            state["pause_reason"] = f"DAILY_LOSS_LIMIT (down ${daily_loss:.2f})"
            save_state(state)

    # Consecutive loss limit
    if float(state.get("consecutive_loss") or 0.0) >= MAX_CONSEC_LOSS_USD:
        tomorrow = datetime.now(timezone.utc).date().toordinal() + 1
        pause_until = int(datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc).timestamp())
        state["paused_until"] = max(int(state.get("paused_until") or 0), pause_until)
        state["pause_reason"] = f"CONSEC_LOSS_LIMIT (${state['consecutive_loss']:.2f})"
        save_state(state)

    # Paused check
    if utc_now_ts() < int(state.get("paused_until") or 0) and not position:
        reason = state.get("pause_reason") or "PAUSED"
        eq_txt = f"${equity:.2f}" if equity is not None else "n/a"
        send_telegram(
            f"⏸ <b>PAUSED</b> — ${price:.2f}\n"
            f"Reason: {reason}\n"
            f"Equity: {eq_txt}"
        )
        append_log("PAUSED_SKIP", {"reason": reason, "price": round(price, 2)})
        return

    # Active position — just report
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
        if not state.get("had_position"):
            state["had_position"] = True
            if state.get("entry_equity") is None and equity is not None:
                state["entry_equity"] = float(equity)
            state["entry_time"] = state.get("entry_time") or now
            state["entry_price"] = state.get("entry_price") or entry
            state["entry_side"] = state.get("entry_side") or side
            save_state(state)
        return

    # --- Build Claude prompt ---
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
    macd_momentum = "UP" if macd_hist_last > macd_hist_prev else "DOWN" if macd_hist_last < macd_hist_prev else "FLAT"

    atr_tf = calculate_atr(candles, period=ATR_PERIOD)
    atr_tf_pct = (atr_tf / close_tf) if close_tf > 0 else 0

    hist_list = state.get("trade_history") or []
    last5 = hist_list[-5:]
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

=== PERFORMANCE MEMORY ===
Today's PnL (equity-based): ${daily_pnl:.2f}
Consecutive loss: ${float(state.get("consecutive_loss") or 0.0):.2f}
Lifetime PnL: ${float(state.get("lifetime_pnl") or 0.0):.2f}
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
{chr(10).join(last_candles_summary)}

=== HARD CONSTRAINTS ===
1) You MUST output a clear DECISION: LONG, SHORT, or SKIP.
2) If LONG/SHORT, you MUST provide SL and TP (2 decimals).
3) Aim for minimum R:R >= 1.0.
4) Avoid liquidation risk (45x leverage): keep SL far from liquidation.
5) If today's PnL is near the daily limit or consecutive losses are high, be conservative.

Respond ONLY in this exact format:
DECISION: LONG or SHORT or SKIP
REASON: (max 2 sentences)
PERSONAL_MESSAGE: (one short sentence to the user, optional)
SL: $X.XX
TP: $X.XX
"""

    claude_text = ask_claude(prompt)
    decision, reason, sl, tp, personal_message = parse_claude_trade_response(claude_text)

    send_telegram(
        f"🧠 <b>CLAUDE CHECK</b> — {now}\n"
        f"Price: ${price:.2f} | Decision: <b>{decision}</b>\n"
        f"Reason: {reason if reason else '(none)'}\n"
        f"{('Message: ' + personal_message + chr(10)) if personal_message else ''}"
        f"MA{MA_PERIOD}: ${ma200:.2f} | Dist: {dist_pct*100:.2f}% | Slope: {slope_dir}\n"
        f"RSI: {rsi_tf} | MACD: {macd_momentum} | ATR%: {atr_tf_pct*100:.2f}%\n"
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
        "rsi": rsi_tf,
        "macd_momentum": macd_momentum,
        "atr_pct": round(atr_tf_pct, 6),
        "daily_pnl": round(daily_pnl, 4),
        "consecutive_loss": float(state.get("consecutive_loss") or 0.0),
        "lifetime_pnl": float(state.get("lifetime_pnl") or 0.0),
    })

    if decision in ("LONG", "SHORT") and sl > 0 and tp > 0:
        side = "Buy" if decision == "LONG" else "Sell"

        if not sl_is_safe(side, price, sl, LEVERAGE):
            liq = get_liquidation_price(side, price, LEVERAGE)
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nSL ${sl} too close to liq ${liq}\nClaude: {decision} | {reason}")
            append_log("BLOCK", {"type": "SL_TOO_CLOSE_TO_LIQ", "decision": decision, "sl": sl, "liq": liq})
            return

        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
        if rr < 1.0:
            send_telegram(f"🚫 <b>TRADE BLOCKED</b>\nR:R {rr} below 1.0 minimum\nClaude: {decision} | {reason}")
            append_log("BLOCK", {"type": "RR_TOO_LOW", "rr": rr, "sl": sl, "tp": tp})
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
                f"Reason: {reason}\n\n{claude_text}"
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
            })
        else:
            send_telegram(f"❌ Order failed: {result.get('retMsg')}")
            append_log("ORDER_FAIL", {
                "side": side,
                "retCode": result.get("retCode"),
                "retMsg": result.get("retMsg"),
            })
    elif decision == "SKIP":
        return
    else:
        send_telegram(f"⚠️ Claude output incomplete; skipping.\n{claude_text}")


def run_loop():
    """Called by app.py in a background thread."""
    clear_stop()
    send_telegram(
        f"📈 <b>ETH Bot Started</b>\n"
        f"45x | {QTY} ETH | {int(CHECK_INTERVAL/60)}m checks | Claude ({DECISION_INTERVAL}m MA{MA_PERIOD}/MACD/RSI)"
    )
    append_log("START", {"symbol": SYMBOL, "qty": QTY, "leverage": LEVERAGE, "check_interval": CHECK_INTERVAL})
    while not is_stop_requested():
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {str(e)}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
            append_log("ERROR", {"error": err, "traceback": traceback.format_exc()})
        # Sleep in small chunks so stop flag is checked promptly
        for _ in range(CHECK_INTERVAL):
            if is_stop_requested():
                break
            time.sleep(1)
    append_log("STOP", {"reason": "stop_flag"})


def main():
    """Standalone entry point (python bot.py)."""
    run_loop()


if __name__ == "__main__":
    main()
