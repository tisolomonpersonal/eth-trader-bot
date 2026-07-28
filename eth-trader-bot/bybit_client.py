"""
Bybit Linear Perpetuals client — BTC/USDT with 25× leverage.
Supports LONG and SHORT. Fetches both H1 and M5 OHLCV candles.
"""
import math
import time
from typing import Optional, Tuple

import pandas as pd

import config
from logger import get_logger

log = get_logger("bybit")

_MAX_RETRIES = 3
_RETRY_DELAY = 5

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
        if not config.PAPER_MODE:
            _set_leverage()
    return _http


def _reset_client():
    global _http
    _http = None


def _retry(fn, label: str):
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            log.warning(f"[{label}] attempt {attempt}/{_MAX_RETRIES} failed: {e}")
            if attempt == _MAX_RETRIES:
                _reset_client()
                raise
            time.sleep(_RETRY_DELAY * attempt)


def _set_leverage():
    """Set leverage on both buy and sell side for the perpetual contract."""
    try:
        _client().set_leverage(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            buyLeverage=str(config.LEVERAGE),
            sellLeverage=str(config.LEVERAGE),
        )
        log.info(f"Leverage set to {config.LEVERAGE}× for {config.SYMBOL}")
    except Exception as e:
        # May fail if leverage is already set — not fatal
        log.warning(f"set_leverage: {e}")


# ── Market data ───────────────────────────────────────────────────────────────

def _fetch_klines(interval: str, limit: int) -> pd.DataFrame:
    def _fetch():
        resp = _client().get_kline(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            interval=interval,
            limit=limit,
        )
        rows = resp["result"]["list"]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
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

    return _retry(_fetch, f"get_klines_{interval}")


def get_klines_h1() -> pd.DataFrame:
    """Fetch H1 OHLCV candles for BTC/USDT (sorted oldest→newest)."""
    return _fetch_klines(config.H1_INTERVAL, config.H1_LIMIT)


def get_klines_m5() -> pd.DataFrame:
    """Fetch M5 OHLCV candles for BTC/USDT (sorted oldest→newest)."""
    return _fetch_klines(config.M5_INTERVAL, config.M5_LIMIT)


def get_klines() -> pd.DataFrame:
    """Alias used by legacy hourly summary — returns H1 candles."""
    return get_klines_h1()


# ── Account data ──────────────────────────────────────────────────────────────

def get_balance() -> dict:
    """Returns {'usdt': float, 'btc': float, 'equity': float}.
    Tries UNIFIED account first (standard), falls back to CONTRACT."""
    if config.PAPER_MODE:
        return {"usdt": 10000.0, "btc": 0.0, "equity": 10000.0}

    def _parse_coins(coins):
        bal = {"usdt": 0.0, "btc": 0.0, "equity": 0.0}
        for c in coins:
            sym = c["coin"].upper()
            if sym == "USDT":
                bal["usdt"]   = float(c.get("walletBalance")  or 0)
                bal["equity"] = float(c.get("equity")         or c.get("walletBalance") or 0)
            elif sym == "BTC":
                bal["btc"]    = float(c.get("walletBalance")  or 0)
        return bal

    def _fetch():
        cl = _client()
        for acct_type in ("UNIFIED", "CONTRACT"):
            try:
                resp  = cl.get_wallet_balance(accountType=acct_type)
                rows  = resp.get("result", {}).get("list", [])
                if rows and rows[0].get("coin"):
                    return _parse_coins(rows[0]["coin"])
            except Exception:
                continue
        return {"usdt": 0.0, "btc": 0.0, "equity": 0.0}

    return _retry(_fetch, "get_balance")


def get_position() -> Optional[dict]:
    """Return open position info for BTCUSDT, or None if flat."""
    if config.PAPER_MODE:
        return None

    def _fetch():
        resp = _client().get_positions(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
        )
        positions = resp["result"]["list"]
        for p in positions:
            if float(p.get("size", 0)) > 0:
                return p
        return None

    return _retry(_fetch, "get_position")


# ── Order execution ───────────────────────────────────────────────────────────

def open_long(qty: float = None, ref_price: float = 0.0) -> Tuple[float, float]:
    """
    Open a LONG position (market buy).
    Returns (qty, estimated_fill_price).
    """
    qty = qty or config.BTC_QTY

    if config.PAPER_MODE:
        log.info(f"[PAPER LONG] {qty} BTC @ ${ref_price:,.2f} | {config.LEVERAGE}×")
        return qty, ref_price

    def _order():
        if not _http:
            _client()  # ensure leverage is set
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Buy",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            positionIdx=1,  # one-way mode → 0; hedge mode → 1 (long). Use 0 for one-way.
        )
        log.info(f"LONG order placed: {resp['result']}")
        return qty, ref_price

    return _retry(_order, "open_long")


def open_short(qty: float = None, ref_price: float = 0.0) -> Tuple[float, float]:
    """
    Open a SHORT position (market sell).
    Returns (qty, estimated_fill_price).
    """
    qty = qty or config.BTC_QTY

    if config.PAPER_MODE:
        log.info(f"[PAPER SHORT] {qty} BTC @ ${ref_price:,.2f} | {config.LEVERAGE}×")
        return qty, ref_price

    def _order():
        if not _http:
            _client()
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            positionIdx=0,
        )
        log.info(f"SHORT order placed: {resp['result']}")
        return qty, ref_price

    return _retry(_order, "open_short")


def close_long(qty: float = None, ref_price: float = 0.0) -> float:
    """Close a LONG position. Returns estimated fill price."""
    qty = qty or config.BTC_QTY

    if config.PAPER_MODE:
        log.info(f"[PAPER CLOSE LONG] {qty} BTC @ ${ref_price:,.2f}")
        return ref_price

    def _order():
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=True,
            positionIdx=0,
        )
        log.info(f"CLOSE LONG placed: {resp['result']}")
        return ref_price

    return _retry(_order, "close_long")


def close_short(qty: float = None, ref_price: float = 0.0) -> float:
    """Close a SHORT position. Returns estimated fill price."""
    qty = qty or config.BTC_QTY

    if config.PAPER_MODE:
        log.info(f"[PAPER CLOSE SHORT] {qty} BTC @ ${ref_price:,.2f}")
        return ref_price

    def _order():
        resp = _client().place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side="Buy",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=True,
            positionIdx=0,
        )
        log.info(f"CLOSE SHORT placed: {resp['result']}")
        return ref_price

    return _retry(_order, "close_short")


# ── Legacy aliases (used by tradfi summary / hourly alerts) ───────────────────

def place_market_buy(usdt_amount: float, ref_price: float):
    """Legacy alias — routes to open_long with fixed BTC_QTY."""
    return open_long(config.BTC_QTY, ref_price)


def place_market_sell(qty: float, ref_price: float) -> float:
    """Legacy alias — routes to close_long."""
    return close_long(qty, ref_price)
