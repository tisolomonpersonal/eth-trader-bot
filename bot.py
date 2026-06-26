"""
BNB/USDT Spot Trading Bot — Bybit
Cycle: every 1 minute
Alerts: immediate on trade/SL/TP/error, hourly summary
"""
import os, json, math, time, logging, traceback, requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
BYBIT_API_KEY     = os.environ.get("BYBIT_API_KEY",     "")
BYBIT_API_SECRET  = os.environ.get("BYBIT_API_SECRET",  "")
BYBIT_TESTNET     = os.environ.get("BYBIT_TESTNET",     "false").lower() == "true"

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID",   "")

SYMBOL         = "BNBUSDT"
CATEGORY       = "spot"
INTERVAL       = "1"    # 1-minute candles
CANDLE_LIMIT   = 100    # enough for RSI(14), MACD(26), BB(20)

STOP_LOSS_PCT      = float(os.environ.get("STOP_LOSS_PCT",     "2.0"))
TAKE_PROFIT_PCT    = float(os.environ.get("TAKE_PROFIT_PCT",   "4.0"))
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT","5.0"))
TRADE_USDT         = float(os.environ.get("TRADE_AMOUNT_USDT", "20.0"))
GHC_RATE           = float(os.environ.get("GHC_RATE",          "15.5"))  # 1 USD ≈ GHC

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",    "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL",   "qwen2.5:3b")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY",   "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")

PAPER_MODE = not bool(BYBIT_API_KEY and BYBIT_API_SECRET)
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/bot_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bnb-bot")

# ── State persistence ─────────────────────────────────────────────────────────
_DEFAULTS = {
    "in_position":      False,
    "entry_price":      0.0,
    "entry_time":       None,
    "qty":              0.0,
    "entry_usdt":       0.0,
    "sl_price":         0.0,
    "tp_price":         0.0,
    "daily_pnl_usdt":   0.0,
    "daily_reset_date": "",
    "trade_count_today":0,
    "last_action":      "NONE",
    "last_reason":      "",
    "total_pnl_usdt":   0.0,
}

def load_state() -> dict:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text())
            return {**_DEFAULTS, **saved}
    except Exception as e:
        log.error(f"State load error: {e}")
    return _DEFAULTS.copy()

def save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"State save error: {e}")

def get_state() -> dict:
    return load_state()

def reset_daily_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pnl_usdt"]    = 0.0
        state["trade_count_today"] = 0
        state["daily_reset_date"]  = today
    return state

# ── Bybit client ──────────────────────────────────────────────────────────────
_bybit_client = None

def _client():
    global _bybit_client
    if _bybit_client is None:
        from pybit.unified_trading import HTTP
        _bybit_client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY   or None,
            api_secret=BYBIT_API_SECRET or None,
        )
    return _bybit_client

def fetch_klines() -> pd.DataFrame:
    resp = _client().get_kline(
        category=CATEGORY, symbol=SYMBOL,
        interval=INTERVAL, limit=CANDLE_LIMIT,
    )
    rows = resp["result"]["list"]
    # Bybit returns: [timestamp, open, high, low, close, volume, turnover] newest-first
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","turn"])
    df = df.astype({"ts":"int64","open":"float64","high":"float64",
                    "low":"float64","close":"float64","vol":"float64"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.sort_values("ts").reset_index(drop=True)

def fetch_balance() -> dict:
    if PAPER_MODE:
        return {"usdt": TRADE_USDT * 10, "bnb": 0.0}
    try:
        # Try Unified account first, fall back to SPOT
        for acc_type in ("UNIFIED", "SPOT"):
            try:
                resp = _client().get_wallet_balance(accountType=acc_type)
                coins = resp["result"]["list"][0]["coin"]
                bal = {"usdt": 0.0, "bnb": 0.0}
                for c in coins:
                    sym = c["coin"].upper()
                    if sym == "USDT": bal["usdt"] = float(c.get("walletBalance") or 0)
                    if sym == "BNB":  bal["bnb"]  = float(c.get("walletBalance") or 0)
                return bal
            except Exception:
                continue
    except Exception as e:
        log.error(f"fetch_balance: {e}")
    return {"usdt": 0.0, "bnb": 0.0}

def _bnb_qty_precision() -> int:
    try:
        resp = _client().get_instruments_info(category=CATEGORY, symbol=SYMBOL)
        step = float(resp["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        if step >= 1: return 0
        return max(0, len(str(step).rstrip("0").split(".")[-1]))
    except Exception:
        return 3  # BNB default on Bybit spot

def place_buy(price: float) -> tuple[float, float]:
    """Place market BUY. Returns (qty, filled_price)."""
    prec = _bnb_qty_precision()
    qty  = math.floor((TRADE_USDT / price) * 10**prec) / 10**prec
    if PAPER_MODE:
        log.info(f"[PAPER BUY] {qty} BNB @ ${price}")
        return qty, price
    resp = _client().place_order(
        category=CATEGORY, symbol=SYMBOL,
        side="Buy", orderType="Market",
        qty=str(qty), timeInForce="IOC",
    )
    log.info(f"BUY placed: {resp['result']}")
    return qty, price

def place_sell(qty: float, price: float) -> float:
    """Place market SELL. Returns filled_price."""
    if PAPER_MODE:
        log.info(f"[PAPER SELL] {qty} BNB @ ${price}")
        return price
    resp = _client().place_order(
        category=CATEGORY, symbol=SYMBOL,
        side="Sell", orderType="Market",
        qty=str(qty), timeInForce="IOC",
    )
    log.info(f"SELL placed: {resp['result']}")
    return price

# ── Technical indicators ──────────────────────────────────────────────────────
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rsi(closes: pd.Series, n: int = 14) -> float:
    d    = closes.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    rs   = gain / loss.replace(0, float("nan"))
    return round(float((100 - 100 / (1 + rs)).iloc[-1]), 2)

def _macd(closes: pd.Series) -> tuple[float, float, float]:
    line = _ema(closes, 12) - _ema(closes, 26)
    sig  = _ema(line, 9)
    hist = line - sig
    return (round(float(line.iloc[-1]), 6),
            round(float(sig.iloc[-1]), 6),
            round(float(hist.iloc[-1]), 6))

def _bb(closes: pd.Series, n: int = 20, k: float = 2.0) -> tuple[float, float, float, float]:
    sma  = closes.rolling(n).mean()
    std  = closes.rolling(n).std()
    up   = float((sma + k * std).iloc[-1])
    lo   = float((sma - k * std).iloc[-1])
    mid  = float(sma.iloc[-1])
    p    = float(closes.iloc[-1])
    pos  = (p - lo) / (up - lo) if (up - lo) > 0 else 0.5
    return round(up, 4), round(mid, 4), round(lo, 4), round(pos, 3)

def _atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["close"].shift()
    tr   = pd.concat([df["high"] - df["low"],
                      (df["high"] - prev).abs(),
                      (df["low"]  - prev).abs()], axis=1).max(axis=1)
    return round(float(tr.rolling(n).mean().iloc[-1]), 4)

def calculate_indicators(df: pd.DataFrame) -> dict:
    c      = df["close"]
    price  = round(float(c.iloc[-1]), 4)
    rsi    = _rsi(c)
    ml, ms, mh = _macd(c)
    bbu, bbm, bbl, bbp = _bb(c)
    e20    = round(float(_ema(c, 20).iloc[-1]), 4)
    e50    = round(float(_ema(c, 50).iloc[-1]), 4)
    atr    = _atr(df)
    trend  = ("Bullish" if e20 > e50 * 1.001 else
              "Bearish" if e20 < e50 * 0.999 else "Neutral")
    return {
        "price": price, "rsi": rsi,
        "macd_line": ml, "macd_sig": ms, "macd_hist": mh,
        "bb_upper": bbu, "bb_mid": bbm, "bb_lower": bbl, "bb_pos": bbp,
        "ema20": e20, "ema50": e50, "atr": atr, "trend": trend,
    }

# ── AI signal ─────────────────────────────────────────────────────────────────
def _prompt(ind: dict, in_pos: bool) -> str:
    pos_note = ("You are IN a long position — consider SELL if bearish or reversal."
                if in_pos else
                "You are NOT in a position — consider BUY if conditions are bullish.")
    return f"""You are a BNB/USDT spot trading analyst. Reply ONLY with a JSON object.

BNB/USDT 1-min data:
Price: ${ind['price']}
RSI(14): {ind['rsi']} {'— overbought' if ind['rsi']>70 else '— oversold' if ind['rsi']<30 else '— neutral'}
MACD hist: {ind['macd_hist']:+.6f} {'— bullish' if ind['macd_hist']>0 else '— bearish'}
BB position: {ind['bb_pos']*100:.1f}% {'— near upper band' if ind['bb_pos']>0.8 else '— near lower band' if ind['bb_pos']<0.2 else '— mid range'}
EMA20={ind['ema20']} vs EMA50={ind['ema50']} — Trend: {ind['trend']}
ATR(14): {ind['atr']}
{pos_note}

BUY rules: not in position + RSI<55 + MACD hist positive + bullish/neutral trend
SELL rules: in position + (RSI>65 + MACD turning negative) OR strong bearish signal
HOLD: when conditions are unclear or marginal

Reply with ONLY valid JSON, one of:
{{"action":"BUY","reasoning":"<one sentence>"}}
{{"action":"SELL","reasoning":"<one sentence>"}}
{{"action":"HOLD","reasoning":"<one sentence>"}}"""

def _parse_action(raw: dict) -> dict:
    action = str(raw.get("action", "HOLD")).upper().strip()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    return {"action": action, "reasoning": str(raw.get("reasoning", "")).strip()}

def _ollama(prompt: str) -> dict:
    r = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
              "format": "json", "options": {"temperature": 0.1, "num_predict": 80}},
        timeout=30,
    )
    r.raise_for_status()
    return _parse_action(json.loads(r.json()["response"]))

def _groq(prompt: str) -> dict:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": "llama-3.1-8b-instant",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1, "max_tokens": 100,
              "response_format": {"type": "json_object"}},
        timeout=15,
    )
    r.raise_for_status()
    return _parse_action(json.loads(r.json()["choices"][0]["message"]["content"]))

def _openai(prompt: str) -> dict:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1, "max_tokens": 100,
              "response_format": {"type": "json_object"}},
        timeout=15,
    )
    r.raise_for_status()
    return _parse_action(json.loads(r.json()["choices"][0]["message"]["content"]))

def _rules(ind: dict, in_pos: bool) -> dict:
    """Rule-based fallback — no AI needed."""
    rsi, hist, bbp, trend = ind["rsi"], ind["macd_hist"], ind["bb_pos"], ind["trend"]
    if not in_pos:
        if rsi < 35 and hist > 0 and bbp < 0.3:
            return {"action":"BUY",  "reasoning": f"RSI oversold ({rsi}), MACD bullish, price near BB lower band."}
        if rsi < 48 and hist > 0 and trend == "Bullish":
            return {"action":"BUY",  "reasoning": f"RSI {rsi} with positive MACD momentum and bullish trend."}
    else:
        if rsi > 72 and hist < 0 and bbp > 0.8:
            return {"action":"SELL", "reasoning": f"RSI overbought ({rsi}), MACD turning negative, price at upper BB."}
        if trend == "Bearish" and hist < 0 and rsi > 55:
            return {"action":"SELL", "reasoning": f"Trend turned bearish (EMA crossover), MACD negative."}
    return {"action":"HOLD", "reasoning": f"No clear edge. RSI={rsi}, MACD hist={hist:+.6f}, trend={trend}."}

def get_ai_signal(ind: dict, in_pos: bool) -> dict:
    """Returns dict with action, reasoning, provider."""
    prompt = _prompt(ind, in_pos)
    providers = []
    if OLLAMA_HOST:            providers.append(("ollama", _ollama))
    if GROQ_API_KEY:           providers.append(("groq",   _groq))
    if OPENAI_KEY:             providers.append(("openai", _openai))

    for name, fn in providers:
        try:
            result = fn(prompt)
            log.info(f"[AI:{name}] {result['action']} — {result['reasoning'][:60]}")
            return {**result, "provider": name}
        except Exception as e:
            log.warning(f"[AI:{name}] failed: {e}")

    result = _rules(ind, in_pos)
    return {**result, "provider": "rules"}

# ── Risk manager ──────────────────────────────────────────────────────────────
def check_sl_tp(state: dict, price: float) -> Optional[str]:
    """Returns 'SL', 'TP', or None."""
    if not state.get("in_position"):
        return None
    sl, tp = state.get("sl_price", 0), state.get("tp_price", 0)
    if sl > 0 and price <= sl: return "SL"
    if tp > 0 and price >= tp: return "TP"
    return None

def trading_allowed(state: dict, balance: dict, action: str) -> tuple[bool, str]:
    daily_pnl = state.get("daily_pnl_usdt", 0)
    loss_limit = TRADE_USDT * 5 * MAX_DAILY_LOSS_PCT / 100
    if daily_pnl < -loss_limit:
        return False, f"Daily loss limit hit ({daily_pnl:.2f} USDT today)"
    if action == "BUY":
        if state.get("in_position"):
            return False, "Already holding BNB"
        if balance["usdt"] < TRADE_USDT * 0.99:
            return False, f"Insufficient USDT ({balance['usdt']:.2f})"
    if action == "SELL" and not state.get("in_position"):
        return False, "No position to sell"
    return True, "OK"

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False

def ghc(usdt: float) -> str:
    return f"{usdt * GHC_RATE:,.0f} GHC"

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")

def alert_trade(action: str, price: float, qty: float, usdt_val: float,
                reason: str, pnl: Optional[float] = None) -> str:
    emoji = {"BUY":"🟢","SELL":"🔴","SL":"🛑","TP":"✅"}.get(action, "⚪")
    label = {"SL":"STOP LOSS TRIGGERED","TP":"TAKE PROFIT REACHED"}.get(
        action, "TRADE EXECUTED" + (" [PAPER]" if PAPER_MODE else ""))
    pnl_line = f"\nP&L: <b>${pnl:+.2f}</b> ({pnl/usdt_val*100:+.1f}%)" if pnl is not None else ""
    return (
        f"{emoji} <b>{label}</b>\n"
        f"{'─'*26}\n"
        f"Action: <b>{action}</b>\n"
        f"Pair: BNB/USDT Spot\n"
        f"Amount: {ghc(usdt_val)} (~${usdt_val:.2f})\n"
        f"Price: ${price:,.4f}\n"
        f"Qty: {qty:.4f} BNB"
        f"{pnl_line}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )

def alert_hourly(state: dict, ind: dict, balance: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    action = state.get("last_action", "HOLD")
    reason = state.get("last_reason", "Monitoring markets.")

    if state.get("in_position"):
        entry = state["entry_price"]
        qty   = state["qty"]
        pnl_u = (ind["price"] - entry) * qty
        pnl_p = (ind["price"] - entry) / entry * 100 if entry > 0 else 0
        pos_line = f"{qty:.4f} BNB @ ${entry:,.4f} (P&L: ${pnl_u:+.2f} / {pnl_p:+.2f}%)"
        sl_tp = f"SL: ${state['sl_price']:,.4f} | TP: ${state['tp_price']:,.4f}"
    else:
        pos_line = "None"
        sl_tp = "—"

    mode = "📄 PAPER | " if PAPER_MODE else ""
    return (
        f"📊 <b>{mode}BNB Bot Report</b>\n"
        f"Time: {now}\n"
        f"{'─'*26}\n"
        f"Price: <b>${ind['price']:,.4f}</b>\n"
        f"Trend: {ind['trend']}\n"
        f"RSI: {ind['rsi']} | MACD: {ind['macd_hist']:+.5f} | BB: {ind['bb_pos']*100:.0f}%\n"
        f"Action: <b>{action}</b>\n"
        f"Balance: {ghc(balance['usdt'])} (~${balance['usdt']:.2f})\n"
        f"Position: {pos_line}\n"
        f"{sl_tp}\n"
        f"Daily P&L: ${state.get('daily_pnl_usdt',0):+.2f} | "
        f"Total P&L: ${state.get('total_pnl_usdt',0):+.2f}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )

# ── Main cycle ─────────────────────────────────────────────────────────────────
def run_cycle(state: dict) -> dict:
    df    = fetch_klines()
    ind   = calculate_indicators(df)
    price = ind["price"]
    bal   = fetch_balance()

    # ── SL / TP check (always runs before AI) ────────────────────────────────
    exit_trigger = check_sl_tp(state, price)
    if exit_trigger:
        qty   = state["qty"]
        entry = state["entry_price"]
        try:
            place_sell(qty, price)
        except Exception as e:
            log.error(f"Exit sell failed: {e}")
            send_telegram(f"⚠️ <b>ORDER ERROR</b>\nCould not execute {exit_trigger} exit: {e}")
            return state

        usdt_val = qty * price
        pnl      = (price - entry) * qty
        reason   = (f"{'Stop loss' if exit_trigger=='SL' else 'Take profit'} hit. "
                    f"Entry ${entry:,.4f} → Exit ${price:,.4f}. P&L ${pnl:+.2f}.")
        state.update({
            "in_position": False, "qty": 0.0, "entry_price": 0.0,
            "entry_time": None, "sl_price": 0.0, "tp_price": 0.0,
            "last_action": exit_trigger, "last_reason": reason,
            "daily_pnl_usdt": state["daily_pnl_usdt"] + pnl,
            "total_pnl_usdt": state["total_pnl_usdt"] + pnl,
        })
        send_telegram(alert_trade(exit_trigger, price, qty, usdt_val, reason, pnl))
        log.info(f"[{exit_trigger}] qty={qty} price={price} pnl={pnl:+.4f}")
        return state

    # ── AI signal ─────────────────────────────────────────────────────────────
    sig      = get_ai_signal(ind, state.get("in_position", False))
    action   = sig["action"]
    reason   = sig["reasoning"]
    provider = sig["provider"]
    log.info(f"[cycle] ${price} RSI={ind['rsi']} trend={ind['trend']} "
             f"AI({provider})={action}")

    # ── Risk check ────────────────────────────────────────────────────────────
    allowed, block_reason = trading_allowed(state, bal, action)
    if not allowed:
        log.info(f"[risk] {action} blocked: {block_reason}")
        state["last_action"] = "HOLD"
        state["last_reason"] = f"{action} signal blocked: {block_reason}"
        return state

    # ── Execute ───────────────────────────────────────────────────────────────
    if action == "BUY":
        try:
            qty, filled = place_buy(price)
        except Exception as e:
            log.error(f"BUY failed: {e}")
            send_telegram(f"⚠️ <b>ORDER ERROR</b>\nBUY failed: {e}")
            return state

        sl = round(filled * (1 - STOP_LOSS_PCT / 100), 4)
        tp = round(filled * (1 + TAKE_PROFIT_PCT / 100), 4)
        state.update({
            "in_position": True,
            "entry_price": filled, "entry_time": datetime.now(timezone.utc).isoformat(),
            "qty": qty, "entry_usdt": TRADE_USDT,
            "sl_price": sl, "tp_price": tp,
            "last_action": "BUY", "last_reason": reason,
            "trade_count_today": state["trade_count_today"] + 1,
        })
        send_telegram(alert_trade("BUY", filled, qty, TRADE_USDT, reason))
        log.info(f"[BUY] {qty} BNB @ {filled} SL={sl} TP={tp}")

    elif action == "SELL":
        qty   = state["qty"]
        entry = state["entry_price"]
        try:
            filled = place_sell(qty, price)
        except Exception as e:
            log.error(f"SELL failed: {e}")
            send_telegram(f"⚠️ <b>ORDER ERROR</b>\nSELL failed: {e}")
            return state

        usdt_val = qty * filled
        pnl      = (filled - entry) * qty
        state.update({
            "in_position": False, "qty": 0.0, "entry_price": 0.0,
            "entry_time": None, "sl_price": 0.0, "tp_price": 0.0,
            "last_action": "SELL", "last_reason": reason,
            "daily_pnl_usdt": state["daily_pnl_usdt"] + pnl,
            "total_pnl_usdt": state["total_pnl_usdt"] + pnl,
        })
        send_telegram(alert_trade("SELL", filled, qty, usdt_val, reason, pnl))
        log.info(f"[SELL] {qty} BNB @ {filled} pnl={pnl:+.4f}")

    else:  # HOLD
        state["last_action"] = "HOLD"
        state["last_reason"] = reason

    return state

# ── Bot runner ────────────────────────────────────────────────────────────────
def run_bot():
    log.info(f"Starting BNB/USDT bot | paper={PAPER_MODE} | testnet={BYBIT_TESTNET}")
    send_telegram(
        f"🤖 <b>BNB/USDT Bot Started</b>\n"
        f"{'─'*26}\n"
        f"Exchange: Bybit {'(Testnet)' if BYBIT_TESTNET else '(Live Spot)'}\n"
        f"Mode: {'📄 Paper trading' if PAPER_MODE else '💰 Live trading'}\n"
        f"Trade size: {ghc(TRADE_USDT)} (~${TRADE_USDT:.0f} USDT)\n"
        f"Stop loss: {STOP_LOSS_PCT}% | Take profit: {TAKE_PROFIT_PCT}%\n"
        f"Scanning every 1 minute."
    )

    state     = load_state()
    last_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    while True:
        t0 = time.time()
        try:
            state = reset_daily_if_needed(state)
            state = run_cycle(state)
            save_state(state)
        except Exception as e:
            err = str(e)[:300]
            log.error(f"Cycle error: {e}\n{traceback.format_exc()}")
            send_telegram(f"⚠️ <b>BOT ERROR</b>\n{err}\n\nRetrying in 30s.")
            time.sleep(30)
            continue

        # Hourly summary
        now = datetime.now(timezone.utc)
        if (now - last_hour).total_seconds() >= 3600:
            try:
                df  = fetch_klines()
                ind = calculate_indicators(df)
                bal = fetch_balance()
                send_telegram(alert_hourly(state, ind, bal))
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"Hourly summary error: {e}")

        # Sleep the rest of the minute
        elapsed  = time.time() - t0
        sleep_s  = max(5, 60 - elapsed)
        time.sleep(sleep_s)
