from flask import Flask, jsonify, request
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)

# Paths
STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE = Path(__file__).parent / "log.txt"

# Bot state for API
bot_running = False
bot_error = None

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_last_lines(n=20):
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, 'r') as f:
        lines = f.readlines()
    return lines[-n:]

# --- Bot logic (copied from bot.py, simplified for integration) ---
import requests, hmac, hashlib

def get_candles(interval, limit):
    url = f"https://api.bybit.com/v5/market/kline?symbol=ETHUSDT&interval={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        return r.json().get("result", {}).get("list", [])
    except:
        return []

def calculate_ma(candles, period):
    if len(candles) < period:
        return None
    closes = [float(c[4]) for c in candles]
    return sum(closes[-period:]) / period

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1:
        return None
    closes = [float(c[4]) for c in candles]
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(candles, fast=12, slow=26, signal=9):
    if len(candles) < slow + signal:
        return None, None, None
    closes = [float(c[4]) for c in candles]
    ema_fast = [sum(closes[:fast])/fast]
    ema_slow = [sum(closes[:slow])/slow]
    for i in range(fast, len(closes)):
        ema_fast.append((closes[i] * 2/(fast+1)) + (ema_fast[-1] * (1 - 2/(fast+1))))
    for i in range(slow, len(closes)):
        ema_slow.append((closes[i] * 2/(slow+1)) + (ema_slow[-1] * (1 - 2/(slow+1))))
    macd_line = [f - s for f, s in zip(ema_fast[slow-slow:], ema_slow)]
    signal_line = [sum(macd_line[:signal])/signal] * len(macd_line)
    for i in range(signal, len(macd_line)):
        signal_line[i] = (macd_line[i] * 2/(signal+1)) + (signal_line[i-1] * (1 - 2/(signal+1)))
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line[-1], signal_line[-1], histogram[-1]

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, c = float(candles[i][2]), float(candles[i][3]), float(candles[i][4])
        prev_c = float(candles[i-1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / period

def get_price():
    url = "https://api.bybit.com/v5/market/tickers?symbol=ETHUSDT"
    try:
        r = requests.get(url, timeout=10)
        return float(r.json()["result"]["list"][0]["markPrice"])
    except:
        return None

def get_equity():
    url = "https://api.bybit.com/v5/account/wallet-balance"
    headers = {
        "X-BA-SIGN": "",
        "X-BA-TS": "",
        "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        return float(r.json()["result"]["list"][0]["totalEquity"])
    except:
        return None

def get_position():
    url = "https://api.bybit.com/v5/position/list"
    headers = {
        "X-BA-SIGN": "",
        "X-BA-TS": "",
        "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        for p in r.json()["result"]["list"]:
            if p["symbol"] == "ETHUSDT":
                return p
        return None
    except:
        return None

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}/sendMessage"
    payload = {"chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""), "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def run_cycle():
    global bot_running, bot_error
    try:
        bot_running = True
        bot_error = None

        # Get data
        candles_15m = get_candles("15", 200)
        candles_1h = get_candles("60", 300)
        price = get_price()
        equity = get_equity()
        position = get_position()

        if not price or not candles_15m or not candles_1h:
            send_telegram("⚠️ Incomplete data, skipping.")
            return

        # Calculate indicators
        ma200_15m = calculate_ma(candles_15m, 200)
        rsi_15m = calculate_rsi(candles_15m, 14)
        atr_15m = calculate_atr(candles_15m, 14)
        macd_line, macd_signal, macd_hist = calculate_macd(candles_15m, 12, 26, 9)

        # Get state
        state = load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("day") != today:
            state = {"day": today, "equity_start": equity, "daily_pnl": 0, "consecutive_loss": 0, "trades_today": 0, "max_trades_per_day": 10, "trading_enabled": True, "paused": False, "pause_until": None, "pause_reason": None, "had_position": False, "entry_equity": None, "lifetime_pnl": state.get("lifetime_pnl", 0), "last_trade": None, "win_count": state.get("win_count", 0), "loss_count": state.get("loss_count", 0), "avg_win": state.get("avg_win", 0), "avg_loss": state.get("avg_loss", 0), "last_5_trades": []}
        state["equity"] = equity
        state["daily_pnl"] = equity - state.get("equity_start", equity)
        state["position"] = position
        save_state(state)

        # Check pause
        if state.get("paused") and state.get("pause_until") and time.time() < state.get("pause_until"):
            send_telegram(f"⏸️ <b>PAUSED</b> — ${price:.2f}\n{state.get('pause_reason', 'Paused')}")
            return

        # Check max trades
        if state.get("trades_today", 0) >= state.get("max_trades_per_day", 10):
            send_telegram(f"🚫 <b>MAX TRADES REACHED</b> — ${price:.2f}\n{state.get('trades_today', 0)} trades today")
            return

        # Check daily loss
        if state.get("daily_pnl", 0) <= -float(os.environ.get("MAX_DAILY_LOSS_USD", 2)):
            send_telegram(f"🛑 <b>DAILY LOSS LIMIT HIT</b> — ${price:.2f}\nPnL: ${state.get('daily_pnl', 0):.2f}")
            return

        # Check consecutive loss
        if state.get("consecutive_loss", 0) >= float(os.environ.get("MAX_CONSEC_LOSS_USD", 4)):
            send_telegram(f"🛑 <b>CONSECUTIVE LOSS LIMIT HIT</b> — ${price:.2f}\nLoss: ${state.get('consecutive_loss', 0):.2f}")
            return

        # Check volatility spike (15m ATR%)
        atr_pct = atr_15m / price if atr_15m and price else 0
        if atr_pct > float(os.environ.get("VOL_SPIKE_ATR_PCT", 0.02)):
            send_telegram(f"⚠️ <b>VOLATILITY SPIKE</b> — ${price:.2f}\nATR%: {atr_pct*100:.2f}%")
            state["paused"] = True
            state["pause_reason"] = f"Volatility spike (ATR%: {atr_pct*100:.2f}%)"
            state["pause_until"] = time.time() + float(os.environ.get("VOL_PAUSE_SECONDS", 1800))
            save_state(state)
            return

        # Prepare context for Claude
        last_candle = candles_15m[-1]
        price_now = float(last_candle[4])
        price_open = float(last_candle[1])
        price_high = float(last_candle[2])
        price_low = float(last_candle[3])
        volume = float(last_candle[5])

        # Get performance memory
        perf_text = f"Today PnL: ${state.get('daily_pnl', 0):.2f} | Consecutive loss: ${state.get('consecutive_loss', 0):.2f} | Lifetime PnL: ${state.get('lifetime_pnl', 0):.2f}\n"
        if state.get("last_5_trades"):
            perf_text += "Last 5 trades: " + ", ".join(state.get("last_5_trades", [])) + "\n"

        # Build prompt
        prompt = f"""You are a professional ETHUSDT futures trader. Study the market data below and decide whether to LONG, SHORT, or SKIP. You choose your own strategy — no fixed indicators are imposed. Analyze price action, structure, momentum, volatility, and any patterns you see. Only trade when you see a clear edge.

Current price: ${price_now:.2f} (open: ${price_open:.2f}, high: ${price_high:.2f}, low: ${price_low:.2f})
Volume: {volume}
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC

Indicators (15m):
- MA200: ${ma200_15m:.2f}
- RSI(14): {rsi_15m}
- ATR(14): {atr_15m:.2f} ({atr_pct*100:.2f}%)
- MACD: line={macd_line:.2f}, signal={macd_signal:.2f}, hist={macd_hist:.2f}

Performance memory:
{perf_text}

Your constraints:
- Max daily loss: ${os.environ.get("MAX_DAILY_LOSS_USD", 2)}
- Max consecutive loss: ${os.environ.get("MAX_CONSEC_LOSS_USD", 4)}
- Position size: 0.04 ETH
- Leverage: 45x
- Max trades per day: {state.get("max_trades_per_day", 10)}

Return JSON with:
- DECISION: "LONG", "SHORT", or "SKIP"
- REASON: why (1-2 sentences)
- SL: stop loss price (if trading)
- TP: take profit price (if trading)
- QUALITY: "A", "B", or "C" (how strong the setup is)
- PERSONAL_MESSAGE: optional message to user (if SKIP, explain why)

Only trade if QUALITY is A or B and you're confident."""
        state["last_prompt"] = prompt
        save_state(state)

        # Call Claude
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        claude_text = response.content[0].text

        # Parse response
        try:
            # Try to extract JSON from response
            import re
            json_match = re.search(r'\{[^{}]*\}', claude_text, re.DOTALL)
            if json_match:
                claude_json = json.loads(json_match.group())
            else:
                # Try parsing the whole response as JSON
                claude_json = json.loads(claude_text)
        except:
            claude_json = {"DECISION": "SKIP", "REASON": "Failed to parse Claude response", "PERSONAL_MESSAGE": claude_text}

        decision = claude_json.get("DECISION", "SKIP")
        reason = claude_json.get("REASON", "No reason given")
        personal_msg = claude_json.get("PERSONAL_MESSAGE", "")

        # Log to file
        log_line = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price_now,
            "decision": decision,
            "reason": reason,
            "sl": claude_json.get("SL"),
            "tp": claude_json.get("TP"),
            "quality": claude_json.get("QUALITY"),
            "equity": equity,
            "pnl": state.get("daily_pnl", 0),
            "consecutive_loss": state.get("consecutive_loss", 0),
            "paused": state.get("paused"),
            "pause_reason": state.get("pause_reason")
        })
        with open(LOG_FILE, 'a') as f:
            f.write(log_line + "\n")

        # Send Telegram
        if decision == "SKIP":
            send_telegram(
                f"⏭ <b>SKIP</b> — ${price_now:.2f}\n"
                f"MA200: ${ma200_15m:.2f} | RSI: {rsi_15m} | ATR%: {atr_pct*100:.2f}%\n"
                f"MACD hist: {macd_hist:.2f}\n"
                f"Reason: {reason}"
            )
            if personal_msg:
                send_telegram(f"💬 <i>{personal_msg}</i>")
        else:
            sl = claude_json.get("SL")
            tp = claude_json.get("TP")
            quality = claude_json.get("QUALITY", "B")
            if not sl or not tp:
                send_telegram(f"⚠️ <b>SKIP</b> — SL/TP missing\n{reason}")
                return

            # Check quality
            if quality not in ("A", "B"):
                send_telegram(f"⚠️ <b>SKIP</b> — Quality {quality} below A/B\n{reason}")
                return

            # Check R:R
            sl_dist = abs(price_now - sl)
            tp_dist = abs(tp - price_now)
            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
            if rr < 1.5:
                send_telegram(f"⚠️ <b>SKIP</b> — R:R {rr} < 1.5\n{reason}")
                return

            # Place order
            side = "Buy" if decision == "LONG" else "Sell"
            qty = float(os.environ.get("QTY", "0.04"))
            url = "https://api.bybit.com/v5/order/create"
            headers = {
                "X-BA-SIGN": "",
                "X-BA-TS": "",
                "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
            }
            payload = {
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": side,
                "orderType": "Market",
                "qty": qty,
                "sl": str(sl),
                "tp": str(tp),
                "leverage": int(os.environ.get("LEVERAGE", "45"))
            }
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=10)
                result = r.json()
                if result.get("retCode") == 0:
                    liq = sl * (1 + (1/45)) if side == "Buy" else sl * (1 - (1/45))
                    send_telegram(
                        f"🎯 <b>{decision}</b> — ${price_now:.2f}\n"
                        f"SL: ${sl} | TP: ${tp}\n"
                        f"R:R: 1:{rr} | Liq: ${liq:.2f}\n"
                        f"Quality: {quality}\n"
                        f"Reason: {reason}"
                    )
                    # Update state
                    state["trades_today"] = state.get("trades_today", 0) + 1
                    state["had_position"] = True
                    state["entry_equity"] = equity
                    save_state(state)
                else:
                    send_telegram(f"❌ Order failed: {result.get('retMsg')}")
            except Exception as e:
                send_telegram(f"❌ Order error: {str(e)}")

    except Exception as e:
        bot_error = str(e)
        send_telegram(f"❌ <b>ERROR</b>\n{str(e)}\n{traceback.format_exc()}")

def bot_thread():
    global bot_running, bot_error
    while True:
        try:
            run_cycle()
        except Exception as e:
            bot_error = str(e)
        # Sleep for CHECK_INTERVAL (default 1 hour)
        time.sleep(int(os.environ.get("CHECK_INTERVAL", "3600")))

# Start bot in background thread
bot_thread_instance = threading.Thread(target=bot_thread, daemon=True)
bot_thread_instance.start()

@app.route('/')
def index():
    state = load_state()
    return jsonify({
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_running": bot_running,
        "bot_error": bot_error,
        "state": state
    })

@app.route('/api/status')
def api_status():
    state = load_state()
    return jsonify({
        "equity": state.get("equity"),
        "daily_pnl": state.get("daily_pnl"),
        "consecutive_loss": state.get("consecutive_loss"),
        "lifetime_pnl": state.get("lifetime_pnl"),
        "paused": state.get("paused"),
        "pause_reason": state.get("pause_reason"),
        "position": state.get("position"),
        "last_trade": state.get("last_trade"),
        "trades_today": state.get("trades_today"),
        "max_trades_per_day": state.get("max_trades_per_day"),
        "bot_running": bot_running,
        "bot_error": bot_error
    })

@app.route('/api/pause', methods=['POST'])
def api_pause():
    data = request.get_json() or {}
    minutes = int(data.get("minutes", 30))
    reason = data.get("reason", "User requested pause")
    state = load_state()
    state["paused"] = True
    state["pause_reason"] = reason
    state["pause_until"] = (datetime.now(timezone.utc).timestamp() + minutes * 60)
    save_state(state)
    return jsonify({"status": "paused", "minutes": minutes, "reason": reason})

@app.route('/api/resume', methods=['POST'])
def api_resume():
    state = load_state()
    state["paused"] = False
    state["pause_reason"] = None
    state["pause_until"] = None
    save_state(state)
    return jsonify({"status": "resumed"})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    state = load_state()
    state["trading_enabled"] = False
    save_state(state)
    return jsonify({"status": "trading stopped"})

@app.route('/api/start', methods=['POST'])
def api_start():
    state = load_state()
    state["trading_enabled"] = True
    save_state(state)
    return jsonify({"status": "trading started"})

@app.route('/api/log')
def api_log():
    lines = get_last_lines(50)
    return jsonify({"log": lines})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify({
            "max_daily_loss_usd": os.environ.get("MAX_DAILY_LOSS_USD", "2"),
            "max_consec_loss_usd": os.environ.get("MAX_CONSEC_LOSS_USD", "4"),
            "check_interval": os.environ.get("CHECK_INTERVAL", "3600"),
            "qty": os.environ.get("QTY", "0.04"),
            "leverage": os.environ.get("LEVERAGE", "45")
        })
    else:
        data = request.get_json() or {}
        return jsonify({"status": "config update not implemented (use Zeabur env vars)"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
