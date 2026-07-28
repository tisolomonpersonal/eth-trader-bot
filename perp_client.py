"""
Bybit V5 linear perpetual client (BTCUSDT).

Different enough from the spot client to justify its own module:

  - Positions, not balances. You hold a *position* with a side and a size;
    "how much BTC do I own" is not the question any more.
  - Shorts are first-class. Sell-to-open is a real position, not an error.
  - Leverage exists, and with it a liquidation price.
  - Stop-loss and take-profit attach to the ORDER and live on the exchange.
    This is the single biggest safety improvement over the spot bot: the stop
    survives the bot crashing, the container restarting, or Zeabur going down.
    The spot path checked stops in Python once a cycle, which protects you
    only for as long as the process is alive.

One-way mode (positionIdx=0) throughout — one position per symbol at a time.
"""
import math
import time
from typing import Optional

import pandas as pd

import config
from logger import get_logger

log = get_logger("perp")

_MAX_RETRIES = 3
_RETRY_DELAY = 5

_http = None
_leverage_set = False
_qty_step: Optional[float] = None
_min_qty: Optional[float] = None


def _client():
    global _http
    if _http is None:
        from pybit.unified_trading import HTTP
        _http = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY or None,
            api_secret=config.BYBIT_API_SECRET or None,
        )
        log.info(f"Bybit perp client ready (testnet={config.BYBIT_TESTNET})")
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


# ── Instrument metadata ───────────────────────────────────────────────────────

def _load_instrument() -> tuple[float, float]:
    """
    Fetch (qtyStep, minOrderQty) for the symbol, cached.

    BTCUSDT perp uses a 0.001 step with a 0.001 minimum — i.e. the smallest
    position is 0.001 BTC. At a $100k BTC price that is $100 of notional, which
    at 1x leverage requires $100 of margin. This is the constraint that decides
    whether a small account can trade perps at all; see check_account_viable().
    """
    global _qty_step, _min_qty
    if _qty_step is not None and _min_qty is not None:
        return _qty_step, _min_qty

    try:
        resp = _client().get_instruments_info(category="linear", symbol=config.SYMBOL)
        f = resp["result"]["list"][0]["lotSizeFilter"]
        _qty_step = float(f["qtyStep"])
        _min_qty = float(f["minOrderQty"])
        log.info(f"{config.SYMBOL} perp: qtyStep={_qty_step} minQty={_min_qty}")
    except Exception as e:
        log.warning(f"Could not fetch instrument info, using BTCUSDT defaults: {e}")
        _qty_step, _min_qty = 0.001, 0.001

    return _qty_step, _min_qty


def round_qty(qty: float) -> float:
    """Floor a quantity to the instrument's step size."""
    step, _ = _load_instrument()
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


def min_qty() -> float:
    return _load_instrument()[1]


def check_account_viable(balance_usdt: float, price: float) -> tuple[bool, str]:
    """
    Can this account open the smallest allowed position at all?

    Worth checking explicitly and early: on a small float the answer is often
    no, and the failure mode otherwise is a stream of rejected orders rather
    than a clear message.
    """
    mq = min_qty()
    notional = mq * price
    margin_needed = notional / max(config.LEVERAGE, 1)

    if balance_usdt < margin_needed:
        return False, (
            f"Account too small for {config.SYMBOL} perp. Minimum position is "
            f"{mq} {config.BASE_COIN} = ${notional:,.2f} notional, needing "
            f"${margin_needed:,.2f} margin at {config.LEVERAGE:g}x. "
            f"Balance is ${balance_usdt:,.2f}."
        )
    return True, (f"OK — min position ${notional:,.2f} notional, "
                  f"${margin_needed:,.2f} margin at {config.LEVERAGE:g}x.")


# ── Leverage ──────────────────────────────────────────────────────────────────

def ensure_leverage() -> None:
    """
    Set leverage once per process. Bybit errors with 110043 when the value is
    already what you asked for, which is a success for our purposes.
    """
    global _leverage_set
    if _leverage_set or config.PAPER_MODE:
        return

    try:
        _client().set_leverage(
            category="linear",
            symbol=config.SYMBOL,
            buyLeverage=str(config.LEVERAGE),
            sellLeverage=str(config.LEVERAGE),
        )
        log.info(f"Leverage set to {config.LEVERAGE:g}x on {config.SYMBOL}")
    except Exception as e:
        if "110043" in str(e) or "leverage not modified" in str(e).lower():
            log.info(f"Leverage already {config.LEVERAGE:g}x")
        else:
            # Not fatal — the account keeps whatever leverage it had. Log loudly
            # because that value may not be what the risk maths assumed.
            log.error(f"Could not set leverage (position will use the account's "
                      f"existing setting, which may differ from LEVERAGE={config.LEVERAGE:g}): {e}")
    _leverage_set = True


# ── Market data ───────────────────────────────────────────────────────────────

def get_klines(interval: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """OHLCV for the perp, oldest→newest. Public endpoint."""
    def _fetch():
        resp = _client().get_kline(
            category="linear",
            symbol=config.SYMBOL,
            interval=interval or config.INTERVAL,
            limit=limit or config.CANDLE_LIMIT,
        )
        rows = resp["result"]["list"]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
        df = df.astype({"ts": "int64", "open": "float64", "high": "float64",
                        "low": "float64", "close": "float64", "vol": "float64"})
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)

    return _retry(_fetch, "get_klines")


# ── Account ───────────────────────────────────────────────────────────────────

def get_balance() -> dict:
    """Available USDT margin in the Unified account."""
    if config.PAPER_MODE:
        return {"usdt": config.MAX_INVESTMENT_USDT, "equity": config.MAX_INVESTMENT_USDT}

    def _fetch():
        resp = _client().get_wallet_balance(accountType="UNIFIED", coin="USDT")
        row = resp["result"]["list"][0]
        for c in row.get("coin", []):
            if c["coin"].upper() == "USDT":
                return {
                    # availableToWithdraw excludes margin already committed to
                    # open positions, which is what we can actually deploy.
                    "usdt": float(c.get("availableToWithdraw") or c.get("walletBalance") or 0),
                    "equity": float(c.get("equity") or c.get("walletBalance") or 0),
                }
        return {"usdt": 0.0, "equity": 0.0}

    return _retry(_fetch, "get_balance")


def get_position() -> dict:
    """
    Current open position, read from the exchange rather than local state.

    Returns {'side': 'Buy'|'Sell'|None, 'size': float, 'entry': float,
             'unrealised_pnl': float, 'liq_price': float}.

    The exchange is the source of truth. Local JSON state can drift from
    reality after a crash, a manual trade, or an exchange-side liquidation —
    and on a leveraged account that drift is dangerous.
    """
    if config.PAPER_MODE:
        return {"side": None, "size": 0.0, "entry": 0.0,
                "unrealised_pnl": 0.0, "liq_price": 0.0}

    def _fetch():
        resp = _client().get_positions(category="linear", symbol=config.SYMBOL)
        rows = resp["result"]["list"]
        if not rows:
            return {"side": None, "size": 0.0, "entry": 0.0,
                    "unrealised_pnl": 0.0, "liq_price": 0.0}
        p = rows[0]
        size = float(p.get("size") or 0)
        if size <= 0:
            return {"side": None, "size": 0.0, "entry": 0.0,
                    "unrealised_pnl": 0.0, "liq_price": 0.0}
        return {
            "side": p.get("side"),
            "size": size,
            "entry": float(p.get("avgPrice") or 0),
            "unrealised_pnl": float(p.get("unrealisedPnl") or 0),
            "liq_price": float(p.get("liqPrice") or 0) if p.get("liqPrice") else 0.0,
        }

    return _retry(_fetch, "get_position")


# ── Orders ────────────────────────────────────────────────────────────────────

def open_position(side: str, qty: float, sl_price: float,
                  tp_price: float, ref_price: float) -> tuple[float, float]:
    """
    Open a position with the stop-loss and take-profit ATTACHED to the order.

    `side` is "Buy" (long) or "Sell" (short).

    Attaching the brackets is the important part. Bybit holds them server-side,
    so the position is protected even if this process dies — unlike the spot
    bot, whose stop only existed inside a Python loop. On a leveraged account
    an unprotected position is how a small loss becomes the whole account.

    Returns (qty, fill_price).
    """
    qty = round_qty(qty)
    mq = min_qty()
    if qty < mq:
        raise ValueError(f"qty {qty} is below the {mq} minimum for {config.SYMBOL}")

    if config.PAPER_MODE:
        log.info(f"[PAPER {side.upper()}] {qty} {config.BASE_COIN} @ ${ref_price:,.2f} "
                 f"SL=${sl_price:,.2f} TP=${tp_price:,.2f}")
        return qty, ref_price

    ensure_leverage()

    def _order():
        resp = _client().place_order(
            category="linear",
            symbol=config.SYMBOL,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            positionIdx=config.POSITION_IDX,
            stopLoss=str(round(sl_price, 1)),
            takeProfit=str(round(tp_price, 1)),
            tpslMode="Full",
            slTriggerBy="LastPrice",
            tpTriggerBy="LastPrice",
        )
        log.info(f"{side} order placed: {resp['result']}")
        return qty, ref_price

    return _retry(_order, f"open_{side}")


def close_position(side: str, qty: float, ref_price: float) -> float:
    """
    Close an open position with a reduce-only market order.

    `side` is the side of the POSITION being closed ("Buy" for a long), so the
    order itself is sent in the opposite direction. reduceOnly guarantees this
    can only ever shrink a position — it can never accidentally flip you into
    the opposite trade, which is the classic way a close goes wrong.
    """
    qty = round_qty(qty)
    if qty <= 0:
        raise ValueError(f"Cannot close: qty rounds to {qty}")

    if config.PAPER_MODE:
        log.info(f"[PAPER CLOSE {side}] {qty} {config.BASE_COIN} @ ${ref_price:,.2f}")
        return ref_price

    close_side = "Sell" if side == "Buy" else "Buy"

    def _order():
        resp = _client().place_order(
            category="linear",
            symbol=config.SYMBOL,
            side=close_side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            positionIdx=config.POSITION_IDX,
            reduceOnly=True,
        )
        log.info(f"Close order placed: {resp['result']}")
        return ref_price

    return _retry(_order, "close_position")


def update_stop(sl_price: float) -> bool:
    """
    Move the exchange-held stop-loss — used by the trailing stop.

    Returns True on success. A failure here is logged but not raised: the
    original stop is still live on the exchange, so the position stays
    protected at its previous level.
    """
    if config.PAPER_MODE:
        return True

    try:
        _client().set_trading_stop(
            category="linear",
            symbol=config.SYMBOL,
            stopLoss=str(round(sl_price, 1)),
            slTriggerBy="LastPrice",
            positionIdx=config.POSITION_IDX,
        )
        return True
    except Exception as e:
        if "34040" in str(e) or "not modified" in str(e).lower():
            return True
        log.error(f"Could not move stop to ${sl_price:,.2f} "
                  f"(previous stop remains active): {e}")
        return False
