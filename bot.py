import requests, json, os, time, hmac, hashlib
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except Exception:
    anthropic = None

# --- CONFIG ---
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL           = "ETHUSDT"
LEVERAGE         = int(os.environ.get("LEVERAGE", "10"))
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "1800"))  # 30 min

# Grid settings
GRID_LEVELS      = int(os.environ.get("GRID_LEVELS", "5"))
GRID_SPACING_PCT = float(os.environ.get("GRID_SPACING_PCT", "0.004"))  # 0.4% per level
QTY_PER_LEVEL    = float(os.environ.get("QTY_PER_LEVEL", "0.01"))      # ETH per order

# Sideways filter
ADX_PERIOD        = int(os.environ.get("ADX_PERIOD", "14"))
ADX_SIDEWAYS_MAX  = float(os.environ.get("ADX_SIDEWAYS_MAX", "25"))
BB_WIDTH_MIN      = float(os.environ.get("BB_WIDTH_MIN", "0.005"))
BB_WIDTH_MAX      = float(os.environ.get("BB_WIDTH_MAX", "0.025"))

BYBIT_ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")
STATE_FILE = Path(__file__).with_name("bot_state.json")
LOG_FILE   = Path(__file__).with_name("log.txt")

# ── thread-stop flag (used by app.py) ───────────────────────────────────────
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


# ── state helpers ────────────────────────────────────────────────────────────

def load_state():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {
        "grid_active": False,
        "center_price": None,
        "grid_upper": None,
        "grid_lower": None,
        "total_profit": 0.0,
        "lifetime_pnl": 0.0,
        "total_fills": 0,
        "trade_history": [],
        "last_placed": None,
        "trading_enabled": True,
        "paused_until": 0,
        "pause_reason": "",
        "equity": None,
        "daily_pnl": None,
        "consecutive_loss": 0.0,
    }

def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def append_log(event, payload):
    try:
        record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "event": event}
        if isinstance(payload, dict):
            record.update(payload)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except:
        pass

def performance_summary(state):
    """Required by app.py."""
    hist  = state.get("trade_history") or []
    pnls  = []
    for t in hist:
        try:
            pnls.append(float(t.get("pnl") or 0.0))
        except:
            pass
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = len(pnls)
    return {
        "trades":   total,
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  round((len(wins) / total) * 100, 2) if total else 0.0,
        "avg_win":  round(sum(wins) / len(wins), 4)   if wins   else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "last_pnl": round(pnls[-1], 4) if pnls else 0.0,
    }


# ── Bybit helpers ────────────────────────────────────────────────────────────

def get_server_time():
    r = requests.get("https://api.bybit.com/v3/public/time", timeout=5)
    return str(int(float(r.json()["result"]["timeNano"]) / 1000000))

def sign_get(query):
    ts = get_server_time()
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   (ts + BYBIT_API_KEY + "5000" + query).encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000"}

def sign_post(params):
    ts   = get_server_time()
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    sig  = hmac.new(BYBIT_API_SECRET.encode(),
                    (ts + BYBIT_API_KEY + "5000" + body).encode(), hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000",
               "Content-Type": "application/json"}
    return headers, body

def get_wallet_equity_usdt():
    """Required by app.py. Tries multiple account types automatically."""
    for acct in [BYBIT_ACCOUNT_TYPE, "CONTRACT", "UNIFIED", "SPOT"]:
        try:
            query   = f"accountType={acct}&coin=USDT"
            headers = sign_get(query)
            r    = requests.get(f"https://api.bybit.com/v5/account/wallet-balance?{query}",
                                headers=headers, timeout=10)
            data = r.json()
            print(f"[wallet] accountType={acct} retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
            if data.get("retCode") != 0:
                continue
            items = data.get("result", {}).get("list", [])
            if not items:
                continue
            item = items[0]
            for k in ("totalEquity", "totalWalletBalance", "totalMarginBalance"):
                v = item.get(k)
                if v not in (None, ""):
                    try:
                        eq = float(v)
                        if eq >= 0:
                            print(f"[wallet] {k}={eq} via {acct}")
                            return eq
                    except:
                        pass
            for c in (item.get("coin") or []):
                if (c.get("coin") or "").upper() == "USDT":
                    for k in ("equity", "walletBalance", "availableToWithdraw", "availableBalance"):
                        v = c.get(k)
                        if v not in (None, ""):
                            try:
                                eq = float(v)
                                if eq >= 0:
                                    print(f"[wallet] coin.{k}={eq} via {acct}")
                                    return eq
                            except:
                                pass
        except Exception as e:
            print(f"[wallet] {acct} error: {e}")
    print("[wallet] all types failed — returning None")
    return None

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except:
        pass

def get_price():
    r = requests.get(
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={SYMBOL}", timeout=10)
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_candles(interval="60", limit=100):
    try:
        r = requests.get(
            f"https://api.bybit.com/v5/market/kline?category=linear&symbol={SYMBOL}"
            f"&interval={interval}&limit={limit}", timeout=10)
        data = r.json()
        if not data.get("result") or not data["result"].get("list"):
            return []
        return [
            {"open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]),  "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(data["result"]["list"])
        ]
    except:
        return []

def get_open_orders():
    query = f"category=linear&symbol={SYMBOL}&limit=50"
    r = requests.get(f"https://api.bybit.com/v5/order/realtime?{query}",
                     headers=sign_get(query), timeout=10)
    try:
        return r.json().get("result", {}).get("list", [])
    except:
        return []

def cancel_all_orders():
    params = {"category": "linear", "symbol": SYMBOL}
    headers, body = sign_post(params)
    r = requests.post("https://api.bybit.com/v5/order/cancel-all",
                      data=body, headers=headers, timeout=10)
    return r.json()

def set_leverage():
    params = {"category": "linear", "symbol": SYMBOL,
              "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)}
    headers, body = sign_post(params)
    requests.post("https://api.bybit.com/v5/position/set-leverage",
                  data=body, headers=headers, timeout=10)

def place_limit_order(side, price_level, qty):
    params = {
        "category":    "linear",
        "symbol":      SYMBOL,
        "side":        side,
        "orderType":   "Limit",
        "qty":         str(round(qty, 3)),
        "price":       str(round(price_level, 2)),
        "positionIdx": 1 if side == "Buy" else 2,
        "timeInForce": "GTC",
    }
    headers, body = sign_post(params)
    r = requests.post("https://api.bybit.com/v5/order/create",
                      data=body, headers=headers, timeout=10)
    return r.json()


# ── indicators ───────────────────────────────────────────────────────────────

def calculate_adx(candles, period=14):
    if len(candles) < period * 2:
        return 50
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(candles)):
        hd = candles[i]["high"] - candles[i-1]["high"]
        ld = candles[i-1]["low"] - candles[i]["low"]
        plus_dm_list.append(hd if hd > ld and hd > 0 else 0)
        minus_dm_list.append(ld if ld > hd and ld > 0 else 0)
        tr_list.append(max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"]),
        ))

    def wilder(vals, p):
        s = sum(vals[:p])
        out = [s]
        for v in vals[p:]:
            s = s - s / p + v
            out.append(s)
        return out

    tr_s   = wilder(tr_list,       period)
    plus_s = wilder(plus_dm_list,  period)
    minus_s= wilder(minus_dm_list, period)
    dx_list = []
    for i in range(len(tr_s)):
        if tr_s[i] == 0:
            continue
        plus_di  = 100 * plus_s[i]  / tr_s[i]
        minus_di = 100 * minus_s[i] / tr_s[i]
        di_sum   = plus_di + minus_di
        dx_list.append(100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0)
    if len(dx_list) < period:
        return 50
    return round(sum(dx_list[-period:]) / period, 2)

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    recent = closes[-period:]
    sma = sum(recent) / period
    std = (sum((p - sma) ** 2 for p in recent) / period) ** 0.5
    return round(sma, 2), round(sma + 2*std, 2), round(sma - 2*std, 2)

def is_sideways(candles, price):
    closes = [c["close"] for c in candles]
    adx    = calculate_adx(candles, ADX_PERIOD)
    mid, upper, lower = calculate_bollinger(closes)
    bb_width = (upper - lower) / mid if mid > 0 else 0
    indicators = {"adx": adx, "bb_width_pct": round(bb_width*100, 3),
                  "bb_upper": upper, "bb_lower": lower, "bb_mid": mid}
    if adx >= ADX_SIDEWAYS_MAX:
        return False, f"ADX {adx} >= {ADX_SIDEWAYS_MAX} (trending)", indicators
    if bb_width < BB_WIDTH_MIN:
        return False, f"BB width {bb_width*100:.2f}% too tight", indicators
    if bb_width > BB_WIDTH_MAX:
        return False, f"BB width {bb_width*100:.2f}% too wide", indicators
    return True, f"ADX {adx} + BB {bb_width*100:.2f}% — sideways confirmed", indicators


# ── Claude grid decision ─────────────────────────────────────────────────────

def ask_claude_grid(price, indicators, open_count, state):
    if not ANTHROPIC_API_KEY or anthropic is None:
        # fallback: place if no active grid, skip if active
        if not state.get("grid_active") or open_count == 0:
            return "PLACE", "No Claude key — auto placing grid.", price
        return "SKIP", "No Claude key — grid already active.", price

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are managing a grid trading bot for ETH/USDT perpetual on Bybit.

Current price: ${price:.2f}
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Market indicators:
- ADX: {indicators['adx']} (< {ADX_SIDEWAYS_MAX} = sideways confirmed)
- BB Upper: ${indicators['bb_upper']} | Mid: ${indicators['bb_mid']} | Lower: ${indicators['bb_lower']}
- BB Width: {indicators['bb_width_pct']}%

Grid config: {GRID_LEVELS} levels each side | {GRID_SPACING_PCT*100:.2f}% spacing | {QTY_PER_LEVEL} ETH/order | {LEVERAGE}x
Grid range: ${price*(1-GRID_LEVELS*GRID_SPACING_PCT):.2f} — ${price*(1+GRID_LEVELS*GRID_SPACING_PCT):.2f}
Open orders: {open_count}
Grid profit so far: ${state.get('total_profit', 0):.4f} | Fills: {state.get('total_fills', 0)}

Decide:
- PLACE: place/rebuild the grid now
- SKIP: grid is working fine, leave it
- CANCEL: cancel grid (price moving out of range, market changing)

Respond ONLY in this format:
DECISION: PLACE or SKIP or CANCEL
REASON: (1-2 sentences)
CENTER_PRICE: $X.XX"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text     = msg.content[0].text
        decision = "SKIP"
        reason   = ""
        center   = price
        for line in text.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("DECISION:"):
                decision = line.split(":", 1)[1].strip().upper()
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.upper().startswith("CENTER_PRICE:"):
                try:
                    center = float(line.split(":", 1)[1].replace("$", "").strip())
                except:
                    center = price
        return decision, reason, center
    except Exception as e:
        return "SKIP", f"Claude error: {e}", price


# ── grid placement ───────────────────────────────────────────────────────────

def place_grid(center_price):
    placed_buys, placed_sells = [], []
    for i in range(1, GRID_LEVELS + 1):
        buy_price  = center_price * (1 - i * GRID_SPACING_PCT)
        sell_price = center_price * (1 + i * GRID_SPACING_PCT)

        res = place_limit_order("Buy", buy_price, QTY_PER_LEVEL)
        if res.get("retCode") == 0:
            placed_buys.append(round(buy_price, 2))
        else:
            send_telegram(f"❌ Grid buy failed ${buy_price:.2f}: {res.get('retMsg')}")

        res = place_limit_order("Sell", sell_price, QTY_PER_LEVEL)
        if res.get("retCode") == 0:
            placed_sells.append(round(sell_price, 2))
        else:
            send_telegram(f"❌ Grid sell failed ${sell_price:.2f}: {res.get('retMsg')}")

        time.sleep(0.15)

    return placed_buys, placed_sells


# ── main cycle ───────────────────────────────────────────────────────────────

def run_cycle():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price = get_price()
    state = load_state()

    # Refresh equity for dashboard
    equity = get_wallet_equity_usdt()
    state["equity"] = equity
    state["price"]  = price
    save_state(state)

    # Dashboard kill-switch
    if not state.get("trading_enabled", True):
        send_telegram(f"⏹ Trading disabled by dashboard. Price: ${price:.2f}")
        return

    candles = get_candles("60", 100)
    if len(candles) < 30:
        send_telegram(f"⚠️ Not enough candle data: {len(candles)}")
        return

    sideways_ok, regime_reason, indicators = is_sideways(candles, price)

    # ── TRENDING: cancel grid ────────────────────────────────────────────────
    if not sideways_ok:
        if state.get("grid_active"):
            cancel_all_orders()
            state["grid_active"]   = False
            state["center_price"]  = None
            save_state(state)
            send_telegram(
                f"🚫 <b>GRID CANCELLED</b> — Market trending\n"
                f"Price: ${price:.2f}\nReason: {regime_reason}"
            )
            append_log("GRID_CANCEL", {"reason": regime_reason, "price": price})
        else:
            send_telegram(
                f"⏭ <b>SKIP</b> — Trending market\n"
                f"Price: ${price:.2f}\n{regime_reason}\n"
                f"ADX: {indicators['adx']} | BB: {indicators['bb_width_pct']}%"
            )
        return

    # ── SIDEWAYS ─────────────────────────────────────────────────────────────
    open_orders = get_open_orders()
    open_count  = len(open_orders)

    # Rebuild if price left grid range
    if state.get("grid_active") and state.get("grid_upper") and state.get("grid_lower"):
        if price > state["grid_upper"] or price < state["grid_lower"]:
            cancel_all_orders()
            state["grid_active"] = False
            open_count = 0
            save_state(state)
            send_telegram(
                f"🔄 <b>GRID REBUILD</b> — Price left range\n"
                f"Price: ${price:.2f} | Range: ${state['grid_lower']:.2f}–${state['grid_upper']:.2f}"
            )

    decision, reason, center_price = ask_claude_grid(price, indicators, open_count, state)

    if decision == "CANCEL":
        if open_count > 0:
            cancel_all_orders()
        state["grid_active"] = False
        save_state(state)
        send_telegram(f"🚫 <b>GRID CANCELLED by Claude</b>\nPrice: ${price:.2f}\n{reason}")
        append_log("GRID_CANCEL_CLAUDE", {"reason": reason, "price": price})

    elif decision == "PLACE":
        if open_count > 0:
            cancel_all_orders()
            time.sleep(1)

        set_leverage()
        placed_buys, placed_sells = place_grid(center_price)

        grid_upper = round(center_price * (1 + GRID_LEVELS * GRID_SPACING_PCT), 2)
        grid_lower = round(center_price * (1 - GRID_LEVELS * GRID_SPACING_PCT), 2)

        state["grid_active"]  = True
        state["center_price"] = center_price
        state["grid_upper"]   = grid_upper
        state["grid_lower"]   = grid_lower
        state["last_placed"]  = now
        save_state(state)

        send_telegram(
            f"🟢 <b>GRID PLACED</b>\n"
            f"Center: ${center_price:.2f} | {GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}%\n"
            f"Range: ${grid_lower} — ${grid_upper}\n"
            f"Buys:  {placed_buys}\n"
            f"Sells: {placed_sells}\n"
            f"ADX: {indicators['adx']} | BB: {indicators['bb_width_pct']}%\n"
            f"Claude: {reason}"
        )
        append_log("GRID_PLACED", {
            "center": center_price, "grid_upper": grid_upper, "grid_lower": grid_lower,
            "buys": placed_buys, "sells": placed_sells,
            "adx": indicators["adx"], "bb_width": indicators["bb_width_pct"],
        })

    else:  # SKIP
        send_telegram(
            f"✅ <b>GRID ACTIVE</b> | ${price:.2f}\n"
            f"Center: ${state.get('center_price', 'N/A')} | Orders: {open_count}\n"
            f"Range: ${state.get('grid_lower','?')} — ${state.get('grid_upper','?')}\n"
            f"ADX: {indicators['adx']} | BB: {indicators['bb_width_pct']}%\n"
            f"Profit: ${state.get('total_profit', 0):.4f} | Fills: {state.get('total_fills', 0)}\n"
            f"Claude: {reason}"
        )


# ── run loop (called by app.py thread) ───────────────────────────────────────

def run_loop():
    clear_stop()
    send_telegram(
        f"🤖 <b>ETH Grid Bot Started</b>\n"
        f"{GRID_LEVELS} levels × {GRID_SPACING_PCT*100:.2f}% | {QTY_PER_LEVEL} ETH/order\n"
        f"{LEVERAGE}x leverage | checks every {CHECK_INTERVAL//60}min\n"
        f"Sideways filter: ADX < {ADX_SIDEWAYS_MAX}"
    )
    append_log("START", {
        "symbol": SYMBOL, "leverage": LEVERAGE,
        "grid_levels": GRID_LEVELS, "spacing_pct": GRID_SPACING_PCT,
        "qty_per_level": QTY_PER_LEVEL, "adx_max": ADX_SIDEWAYS_MAX,
    })
    while not is_stop_requested():
        try:
            run_cycle()
        except Exception as e:
            err = f"⚠️ Error: {type(e).__name__}: {e}"
            send_telegram(err)
            print(err)
            print(traceback.format_exc())
            append_log("ERROR", {"error": err})
        for _ in range(CHECK_INTERVAL):
            if is_stop_requested():
                break
            time.sleep(1)
    append_log("STOP", {"reason": "stop_flag"})


def main():
    run_loop()


if __name__ == "__main__":
    main()
