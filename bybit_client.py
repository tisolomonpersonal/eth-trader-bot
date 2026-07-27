"""
Bybit Spot API client.
All trading is SPOT only — no futures, no margin, no leverage, no shorts.
Handles reconnect automatically on transient errors.
"""
import math
import time
from typing import Optional

import pandas as pd

import config
from logger import get_logger

log = get_logger("bybit")

_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries

_http = None  # lazy singleton


def _client():
    global _http
    if _http is None:
        from pybit.unified_trading import HTTP
        _http = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY    or None,
            api_secret=config.BYBIT_API_SECRET or None,
        )
        log.info(f"Bybit client ready (testnet={config.BYBIT_TESTNET})")
    return _http


def _reset_client():
    """Force reconnect on next call."""
    global _http
    _http = None


def _retry(fn, label: str):
    """Run fn() with up to _MAX_RETRIES attempts, exponential backoff."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            log.warning(f"[{label}] attempt {attempt}/{_MAX_RETRIES} failed: {e}")
            if attempt == _MAX_RETRIES:
                _reset_client()
                raise
            time.sleep(_RETRY_DELAY * attempt)


# ── Market data (public — no auth required) ───────────────────────────────────

def get_klines(interval: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """
    Fetch OHLCV candles for config.SYMBOL, sorted oldest→newest.

    `interval` defaults to config.INTERVAL (1m, the scalping timeframe). Pass
    config.TREND_INTERVAL to get the 5m series used for the higher-timeframe
    bias filter.
    """
    def _fetch():
        resp = _client().get_kline(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            interval=interval or config.INTERVAL,
            limit=limit or config.CANDLE_LIMIT,
        )
        rows = resp["result"]["list"]
        # Bybit returns: [timestamp, open, high, low, close, volume, turnover] newest-first
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","turn"])
        df = df.astype({
            "ts":    "int64",
            "open":  "float64",
            "high":  "float64",
            "low":   "float64",
            "close": "float64",
            "vol":   "float64",
        })
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)

    return _retry(_fetch, "get_klines")


# ── Account data (requires auth) ──────────────────────────────────────────────

def get_balance() -> dict:
    """
    Returns {'usdt': float, 'base': float}, where 'base' is config.BASE_COIN.
    The legacy 'bnb' key is kept as an alias so the older AI/swing path and the
    dashboard keep working unchanged.
    """
    base_key = config.BASE_COIN.lower()

    if config.PAPER_MODE:
        return {"usdt": config.MAX_INVESTMENT_USDT * 2, "base": 0.0,
                base_key: 0.0, "bnb": 0.0}

    def _fetch():
        for acc_type in ("UNIFIED", "SPOT"):
            try:
                resp = _client().get_wallet_balance(accountType=acc_type)
                coins = resp["result"]["list"][0]["coin"]
                bal = {"usdt": 0.0, "base": 0.0}
                for c in coins:
                    sym = c["coin"].upper()
                    if sym == "USDT":
                        bal["usdt"] = float(c.get("walletBalance") or 0)
                    elif sym == config.BASE_COIN.upper():
                        bal["base"] = float(c.get("walletBalance") or 0)
                bal[base_key] = bal["base"]
                bal.setdefault("bnb", bal["base"])   # legacy alias
                return bal
            except Exception:
                continue
        raise RuntimeError("Could not fetch balance from UNIFIED or SPOT account")

    return _retry(_fetch, "get_balance")


_precision_cache: Optional[int] = None


def _lot_precision() -> int:
    """
    Decimal places for the base-coin qty. BTC uses 6 (qtyStep 0.000001) vs
    BNB's 3 — getting this wrong silently truncates a BTC order to zero, so
    the exchange is queried once and the result cached.
    """
    global _precision_cache
    if _precision_cache is not None:
        return _precision_cache

    try:
        resp = _client().get_instruments_info(category=config.CATEGORY, symbol=config.SYMBOL)
        step = float(resp["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        prec = 0 if step >= 1 else max(0, len(str(step).rstrip("0").split(".")[-1]))
        _precision_cache = prec
        log.info(f"{config.SYMBOL} qty precision: {prec} decimals (step {step})")
        return prec
    except Exception as e:
        # 6dp is correct for BTC and safe for anything finer-grained than BNB.
        log.warning(f"Could not fetch lot precision, defaulting to 6: {e}")
        return 6


# ── Order execution (spot only) ───────────────────────────────────────────────

def place_market_buy(usdt_amount: float, ref_price: float) -> tuple[float, float]:
    """
    Place a market BUY order for ~usdt_amount USDT worth of the base coin.
    Returns (qty_base, estimated_fill_price).
    SPOT ONLY — no leverage, no futures.
    """
    prec = _lot_precision()
    qty  = math.floor((usdt_amount / ref_price) * 10**prec) / 10**prec
    if qty <= 0:
        raise ValueError(f"Calculated qty={qty} is too small for {usdt_amount} USDT @ {ref_price}")

    if config.PAPER_MODE:
        log.info(f"[PAPER BUY] {qty:.{prec}f} {config.BASE_COIN} @ ${ref_price:,.2f} (~${usdt_amount:.2f})")
        return qty, ref_price

    def _order():
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Buy",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
        )
        log.info(f"BUY order placed: {resp['result']}")
        return qty, ref_price

    return _retry(_order, "place_buy")


def place_market_sell(qty: float, ref_price: float) -> float:
    """
    Place a market SELL order for qty of the base coin.
    Returns estimated fill price.
    SPOT ONLY — no shorting.
    """
    if config.PAPER_MODE:
        log.info(f"[PAPER SELL] {qty:.6f} {config.BASE_COIN} @ ${ref_price:,.2f}")
        return ref_price

    def _order():
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
        )
        log.info(f"SELL order placed: {resp['result']}")
        return ref_price

    return _retry(_order, "place_sell")
