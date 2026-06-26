"""
bybit_trader.py — Bybit V5 auto-execution module for ETH signals.

Called from scanner.py after each AI signal is generated.
Places market orders with pre-set Take Profit and Stop Loss.

Environment variables (set on Zeabur bot-app service):
  BYBIT_API_KEY     — Bybit API key  (required)
  BYBIT_API_SECRET  — Bybit secret   (required)
  BYBIT_TESTNET     — "true" for testnet, "false" for live (default: "false")
  BYBIT_SYMBOL      — trading pair   (default: "ETHUSDT")
  BYBIT_CATEGORY    — "linear" (USDT perpetual) or "spot" (default: "linear")
  BYBIT_LEVERAGE    — leverage multiplier for linear (default: "1")
  BYBIT_TRADE_USDT  — fixed USDT per trade, e.g. "50"  (takes priority over PCT)
  BYBIT_TRADE_PCT   — % of wallet per trade if TRADE_USDT=0 (default: "10")
"""

import os
import json
import hmac
import hashlib
import time
import math
import requests

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BYBIT_TESTNET    = os.environ.get("BYBIT_TESTNET",    "false").lower() == "true"
BYBIT_SYMBOL     = os.environ.get("BYBIT_SYMBOL",     "ETHUSDT")
BYBIT_CATEGORY   = os.environ.get("BYBIT_CATEGORY",   "linear")   # linear = USDT perpetual
BYBIT_LEVERAGE   = int(os.environ.get("BYBIT_LEVERAGE",   "1"))
BYBIT_TRADE_USDT = float(os.environ.get("BYBIT_TRADE_USDT", "0"))  # 0 = use PCT
BYBIT_TRADE_PCT  = float(os.environ.get("BYBIT_TRADE_PCT",  "10")) # 10% of wallet

QTY_STEP = 0.01   # ETHUSDT minimum qty increment on Bybit

BASE_URL = (
    "https://api-testnet.bybit.com" if BYBIT_TESTNET
    else "https://api.bybit.com"
)


def enabled() -> bool:
    """Return True if API credentials are configured."""
    return bool(BYBIT_API_KEY and BYBIT_API_SECRET)


# ── Bybit V5 HMAC-SHA256 auth ─────────────────────────────────────────────────

def _sign_headers(payload_str: str) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    sign_str = ts + BYBIT_API_KEY + recv_window + payload_str
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-SIGN":        signature,
        "X-BAPI-SIGN-TYPE":   "2",
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type":       "application/json",
    }


def _get(endpoint: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    resp = requests.get(
        f"{BASE_URL}{endpoint}",
        headers=_sign_headers(qs),
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, body: dict) -> dict:
    body_str = json.dumps(body, separators=(",", ":"))
    resp = requests.post(
        f"{BASE_URL}{endpoint}",
        headers=_sign_headers(body_str),
        data=body_str,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── Account / position helpers ────────────────────────────────────────────────

def get_wallet_balance() -> float:
    """Return available USDT balance from Unified account."""
    data = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    for acct in data.get("result", {}).get("list", []):
        for coin in acct.get("coin", []):
            if coin.get("coin") == "USDT":
                avail = coin.get("availableToWithdraw") or coin.get("walletBalance") or "0"
                return float(avail)
    return 0.0


def get_position() -> dict | None:
    """Return current open ETHUSDT linear position, or None if flat."""
    if BYBIT_CATEGORY != "linear":
        return None
    data = _get("/v5/position/list", {"category": "linear", "symbol": BYBIT_SYMBOL})
    for p in data.get("result", {}).get("list", []):
        if float(p.get("size", 0)) != 0:
            return p
    return None


def _set_leverage() -> None:
    """Set leverage on linear symbol. Silent on failure (already set / cross-margin)."""
    if BYBIT_CATEGORY != "linear":
        return
    try:
        _post("/v5/position/set-leverage", {
            "category":     "linear",
            "symbol":       BYBIT_SYMBOL,
            "buyLeverage":  str(BYBIT_LEVERAGE),
            "sellLeverage": str(BYBIT_LEVERAGE),
        })
    except Exception:
        pass


def _round_qty(qty: float) -> float:
    """Floor to Bybit's minimum qty step (0.01 ETH)."""
    return math.floor(qty / QTY_STEP) * QTY_STEP


def close_position(position: dict) -> dict:
    """Close an existing position with a reduceOnly market order."""
    close_side = "Sell" if position.get("side") == "Buy" else "Buy"
    qty = position.get("size", "0")
    print(f"[bybit] Closing {position.get('side')} {qty} {BYBIT_SYMBOL}")
    return _post("/v5/order/create", {
        "category":   BYBIT_CATEGORY,
        "symbol":     BYBIT_SYMBOL,
        "side":       close_side,
        "orderType":  "Market",
        "qty":        str(qty),
        "reduceOnly": True,
    })


def place_order(side: str, qty: float, tp: float, sl: float) -> dict:
    """
    Place a market order with TP and SL.
      side: "Buy" | "Sell"
      qty:  ETH quantity
      tp:   take-profit price
      sl:   stop-loss price
    """
    body: dict = {
        "category":    BYBIT_CATEGORY,
        "symbol":      BYBIT_SYMBOL,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(round(qty, 2)),
        "takeProfit":  str(round(tp, 4)),
        "stopLoss":    str(round(sl, 4)),
        "tpTriggerBy": "MarkPrice",
        "slTriggerBy": "MarkPrice",
        "timeInForce": "GTC",
    }
    if BYBIT_CATEGORY == "linear":
        body["positionIdx"] = 0   # one-way mode
    return _post("/v5/order/create", body)


# ── Main entry point ──────────────────────────────────────────────────────────

def execute_trade(signal: dict) -> dict:
    """
    Called from scanner.py after a signal is generated.
    Returns a result dict: status / order_id / qty / side / reason.
    """
    if not enabled():
        return {"status": "skipped", "reason": "BYBIT_API_KEY / BYBIT_API_SECRET not configured"}

    direction = (signal.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return {"status": "skipped", "reason": f"unknown direction: {direction!r}"}

    entry = float(signal.get("entry",       0) or 0)
    tp    = float(signal.get("take_profit", 0) or 0)
    sl    = float(signal.get("stop_loss",   0) or 0)

    if entry <= 0 or tp <= 0 or sl <= 0:
        return {"status": "skipped",
                "reason": f"zero prices: entry={entry} tp={tp} sl={sl}"}

    bybit_side = "Buy" if direction == "BUY" else "Sell"

    try:
        # 1. Set leverage (no-op for spot)
        _set_leverage()

        # 2. Handle existing position (linear only)
        if BYBIT_CATEGORY == "linear":
            position = get_position()
            if position:
                pos_side = position.get("side", "")
                if pos_side == bybit_side:
                    return {
                        "status": "skipped",
                        "reason": f"already in {pos_side} — waiting for exit",
                    }
                # Opposite signal → close first, then re-enter
                close_position(position)
                time.sleep(2)

        # 3. Size the position
        balance = get_wallet_balance()
        if balance < 5:
            return {"status": "skipped",
                    "reason": f"USDT balance too low: {balance:.2f} (min 5)"}

        if BYBIT_TRADE_USDT > 0:
            trade_usdt = min(BYBIT_TRADE_USDT, balance * 0.95)
        else:
            trade_usdt = balance * (BYBIT_TRADE_PCT / 100.0)

        qty = _round_qty((trade_usdt * BYBIT_LEVERAGE) / entry)
        if qty < QTY_STEP:
            return {"status": "skipped",
                    "reason": f"qty too small: {qty} ETH (min {QTY_STEP}, need {trade_usdt:.1f} USDT)"}

        # 4. Place the order
        env = "TESTNET" if BYBIT_TESTNET else "LIVE"
        print(f"[bybit] [{env}] {bybit_side} {qty:.2f} {BYBIT_SYMBOL} "
              f"entry≈{entry:.2f}  TP={tp:.2f}  SL={sl:.2f}  "
              f"({trade_usdt:.1f} USDT × {BYBIT_LEVERAGE}x)")

        result = place_order(bybit_side, qty, tp, sl)

        ret_code = result.get("retCode", -1)
        if ret_code == 0:
            order_id = result.get("result", {}).get("orderId", "?")
            print(f"[bybit] ✅ Order filled — orderId={order_id}")
            return {
                "status":      "executed",
                "order_id":    order_id,
                "side":        bybit_side,
                "qty":         qty,
                "tp":          tp,
                "sl":          sl,
                "trade_usdt":  trade_usdt,
                "balance":     balance,
                "env":         env,
            }
        else:
            msg = result.get("retMsg", "unknown")
            print(f"[bybit] ❌ Order rejected: {ret_code} — {msg}")
            return {"status": "error", "code": ret_code, "msg": msg}

    except Exception as exc:
        print(f"[bybit] Exception during execute_trade: {exc}")
        return {"status": "error", "msg": str(exc)}


def format_trade_result(result: dict) -> str:
    """Format a Bybit execution result as a Telegram HTML message."""
    status = result.get("status")
    if status == "executed":
        env_tag = " [TESTNET]" if result.get("env") == "TESTNET" else ""
        return (
            f"\n🤖 <b>Bybit Auto-Trade{env_tag}</b>\n"
            f"✅ <b>Order executed</b>\n"
            f"ID: <code>{result.get('order_id')}</code>\n"
            f"Side: <b>{result.get('side')}</b>  "
            f"Qty: <b>{result.get('qty'):.2f} ETH</b>\n"
            f"Size: {result.get('trade_usdt', 0):.1f} USDT"
        )
    elif status == "skipped":
        return f"\n⏭️ <b>Bybit:</b> {result.get('reason', 'skipped')}"
    else:
        return f"\n⚠️ <b>Bybit error:</b> {result.get('msg', 'unknown')}"
