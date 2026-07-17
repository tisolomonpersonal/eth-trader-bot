"""
Bybit TradFi client (stocks / forex / metals / indices CFD-style perpetuals).

TradFi is NOT a separate API — it runs through the same V5 endpoints as
crypto derivatives, using category="linear". No new host, no new keys:
it reuses BYBIT_API_KEY / BYBIT_API_SECRET from config.py and sits under
the Unified Trading Account (UTA).

Two important differences from bybit_client.py (crypto spot):
  1. Symbol suffix depends on account mode:
       Zero-Fee Mode    -> ".s" suffix, e.g. "XAUUSD.s", "EURUSD.s"
       Tight-Spread Mode -> no suffix,   e.g. "XAUUSD",   "EURUSD"
     (Tight-Spread requires meeting an asset threshold — see Bybit's
     "TradFi Trading Account Modes" help article.)
  2. Market hours are NOT 24/7. Forex is ~24h on weekdays; indices,
     commodities, and US stocks follow their underlying market sessions.
     Outside trading hours the API can return empty orderbooks or stale
     prices — callers should check is_market_open-style signals from
     get_instrument_info() (tradingStatus) before firing orders.

This module is intentionally standalone (not wired into strategy.py /
scheduler.py). The existing bot's strategy loop is written around a single
crypto spot symbol with crypto-appropriate position sizing and always-on
market hours — bolting TradFi into that loop needs a real decision about
which instrument, sizing, and leverage to use, not a guess.
"""
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config
from logger import get_logger

log = get_logger("tradfi")

_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries

_http = None  # lazy singleton — shared client pattern, same as bybit_client.py


def _client():
    global _http
    if _http is None:
        from pybit.unified_trading import HTTP
        _http = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY    or None,
            api_secret=config.BYBIT_API_SECRET or None,
        )
        log.info(f"TradFi client ready (testnet={config.BYBIT_TESTNET})")
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


_symbol_cache: dict = {}  # base -> the variant the exchange actually accepts


def _normalize_base(sym: str) -> str:
    sym = sym.upper()
    if sym.endswith(".S"):
        sym = sym[:-2]
    return sym


def resolve_symbol(base_symbol: Optional[str] = None) -> str:
    """
    Return the symbol variant the exchange actually lists.

    TRADFI_MODE gives us a *preferred* suffix (zero_fee -> ".s", tight_spread ->
    none), but if the account mode doesn't match, the preferred variant won't
    exist and every call fails as "closed". So we probe both variants once,
    cache whichever the exchange knows, and reuse it. Falls back to the
    configured guess (no crash) if neither can be verified.
    """
    base = _normalize_base(base_symbol or config.TRADFI_SYMBOL)
    if base in _symbol_cache:
        return _symbol_cache[base]

    preferred = f"{base}.s" if config.TRADFI_MODE == "zero_fee" else base
    candidates = [preferred, base if preferred.endswith(".s") else f"{base}.s"]

    for cand in candidates:
        try:
            resp = _client().get_instruments_info(
                category=config.TRADFI_CATEGORY, symbol=cand)
            if resp.get("result", {}).get("list"):
                if cand != preferred:
                    log.warning(f"Symbol {preferred} not found — using {cand} "
                                f"(check TRADFI_MODE matches your account)")
                _symbol_cache[base] = cand
                return cand
        except Exception as e:
            log.warning(f"Symbol probe {cand} failed: {e}")

    return preferred  # unverified fallback; don't block resolution


# ── Market data (public — no auth required) ───────────────────────────────────

def get_klines(symbol: Optional[str] = None, interval: Optional[str] = None,
                limit: int = 250) -> pd.DataFrame:
    """Fetch OHLCV candles for a TradFi instrument. Returns DataFrame oldest→newest."""
    sym = resolve_symbol(symbol)
    itv = interval or config.TRADFI_INTERVAL

    def _fetch():
        resp = _client().get_kline(
            category=config.TRADFI_CATEGORY,
            symbol=sym,
            interval=itv,
            limit=limit,
        )
        rows = resp["result"]["list"]
        if not rows:
            log.warning(f"No candles returned for {sym} — market may be closed")
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

    return _retry(_fetch, "tradfi_get_klines")


def get_instrument_info(symbol: Optional[str] = None) -> dict:
    """
    Returns instrument metadata: lot size, tick size, leverage limits,
    and tradingStatus (use this to check if the market is currently open
    before placing orders — TradFi is NOT 24/7 like crypto).
    """
    sym = resolve_symbol(symbol)

    def _fetch():
        resp = _client().get_instruments_info(category=config.TRADFI_CATEGORY, symbol=sym)
        return resp["result"]["list"][0]

    return _retry(_fetch, "tradfi_instrument_info")


def is_market_open(symbol: Optional[str] = None) -> bool:
    """
    Open == the exchange is still producing fresh candles for this symbol.

    This is far more reliable than the instrument "status" field, which reports
    the *listing* status ("Trading" = the instrument is listed), NOT whether the
    session is currently open. During real market hours candles keep advancing;
    over the weekend / session close they go stale. We consider the market open
    when the newest candle is younger than ~3 intervals.
    """
    try:
        df = get_klines(symbol, limit=2)
        if df is None or df.empty:
            return False
        last_ts = df["ts"].iloc[-1].to_pydatetime()
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        try:
            interval_min = int(config.TRADFI_INTERVAL)
        except (TypeError, ValueError):
            interval_min = 15  # non-numeric intervals (e.g. "D") -> daily-ish
        age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60.0
        fresh_window = max(3 * interval_min, 10)
        is_open = age_min <= fresh_window
        if not is_open:
            log.info(f"Market looks closed: newest candle {age_min:.0f}m old "
                     f"(> {fresh_window}m window)")
        return is_open
    except Exception as e:
        log.warning(f"Could not determine market status: {e}")
        return False


# ── Account data (requires auth) ──────────────────────────────────────────────

def get_balance() -> dict:
    """Returns {'usdt': float} available in the Unified Trading Account backing TradFi."""
    if config.PAPER_MODE:
        return {"usdt": config.MAX_INVESTMENT_USDT * 2}

    def _fetch():
        resp = _client().get_wallet_balance(accountType=config.TRADFI_ACCOUNT_TYPE)
        coins = resp["result"]["list"][0]["coin"]
        bal = {"usdt": 0.0}
        for c in coins:
            if c["coin"].upper() == "USDT":
                bal["usdt"] = float(c.get("walletBalance") or 0)
        return bal

    return _retry(_fetch, "tradfi_get_balance")


def get_position(symbol: Optional[str] = None) -> Optional[dict]:
    """Returns the open TradFi position for symbol, or None if flat."""
    sym = resolve_symbol(symbol)

    def _fetch():
        resp = _client().get_positions(category=config.TRADFI_CATEGORY, symbol=sym)
        rows = resp["result"]["list"]
        if not rows or float(rows[0].get("size") or 0) == 0:
            return None
        return rows[0]

    return _retry(_fetch, "tradfi_get_position")


def get_swap_fee(symbol: Optional[str] = None) -> Optional[dict]:
    """
    Overnight swap/financing fee for holding a TradFi position past daily
    market close. Check this before sizing positions you intend to hold overnight.
    """
    sym = resolve_symbol(symbol)

    def _fetch():
        # Bybit exposes this under the funding-rate style endpoint for linear
        # instruments; TradFi swap fee mirrors crypto funding-rate history.
        resp = _client().get_funding_rate_history(category=config.TRADFI_CATEGORY, symbol=sym, limit=1)
        rows = resp["result"]["list"]
        return rows[0] if rows else None

    try:
        return _retry(_fetch, "tradfi_swap_fee")
    except Exception as e:
        log.warning(f"Swap fee lookup failed for {sym}: {e}")
        return None


# ── Order execution ────────────────────────────────────────────────────────────

def place_market_order(side: str, qty: float, symbol: Optional[str] = None,
                        reduce_only: bool = False) -> dict:
    """
    Place a market order on a TradFi instrument.
    side: "Buy" or "Sell". qty is in lots/contracts per the instrument's lotSizeFilter
    (call get_instrument_info() first to get the correct qtyStep/minOrderQty).

    NOTE: TradFi does not use isolated/cross/portfolio margin modes like
    crypto derivatives — margin behaves like a cross-margin variant
    automatically. There is no separate leverage call needed for most
    symbols; each symbol has fixed/unique leverage set by Bybit.
    """
    sym = resolve_symbol(symbol)

    if side not in ("Buy", "Sell"):
        raise ValueError(f"side must be 'Buy' or 'Sell', got {side!r}")

    if config.PAPER_MODE:
        log.info(f"[PAPER {side.upper()}] {qty} {sym} (reduce_only={reduce_only})")
        return {"paper": True, "symbol": sym, "side": side, "qty": qty}

    if not is_market_open(sym):
        raise RuntimeError(f"{sym} market appears closed — refusing to place live order")

    def _order():
        resp = _client().place_order(
            category=config.TRADFI_CATEGORY,
            symbol=sym,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=reduce_only,
        )
        log.info(f"TradFi {side.upper()} order placed on {sym}: {resp['result']}")
        return resp["result"]

    return _retry(_order, "tradfi_place_order")


def place_market_buy(qty: float, symbol: Optional[str] = None) -> dict:
    return place_market_order("Buy", qty, symbol)


def place_market_sell(qty: float, symbol: Optional[str] = None) -> dict:
    return place_market_order("Sell", qty, symbol)


def close_position(symbol: Optional[str] = None) -> Optional[dict]:
    """Flatten any open position on symbol with a reduce-only market order."""
    pos = get_position(symbol)
    if not pos:
        log.info(f"No open TradFi position on {resolve_symbol(symbol)} to close")
        return None
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    return place_market_order(side, float(pos["size"]), symbol, reduce_only=True)
