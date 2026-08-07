"""
Bybit v5 client for the hedged grid — hedge mode, limit orders.

Separate from bybit_client.py, which is one-way mode (positionIdx=0) and
market-only. This one owns its own HTTP session so it can use a different
API key (see grid_config docstring).

positionIdx convention (Bybit hedge mode):
    1 = long side
    2 = short side
"""
import math
import time
import uuid
from typing import Optional

import pandas as pd

import grid_config as gc
from logger import get_logger

log = get_logger("grid.client")

_MAX_RETRIES = 3
_RETRY_DELAY = 5

_http = None       # lazy singleton
_filters = None    # instrument tick/step cache


def _client():
    global _http
    if _http is None:
        from pybit.unified_trading import HTTP
        _http = HTTP(
            testnet=gc.GRID_TESTNET,
            api_key=gc.GRID_API_KEY or None,
            api_secret=gc.GRID_API_SECRET or None,
        )
        log.info(f"Grid client ready (testnet={gc.GRID_TESTNET}, paper={gc.GRID_PAPER_MODE})")
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


# ── Instrument filters ────────────────────────────────────────────────────────

def filters() -> dict:
    """Tick size / qty step / min qty for the grid symbol. Cached."""
    global _filters
    if _filters is not None:
        return _filters

    def _fetch():
        resp = _client().get_instruments_info(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
        )
        item = resp["result"]["list"][0]
        return {
            "tick_size": float(item["priceFilter"]["tickSize"]),
            "qty_step":  float(item["lotSizeFilter"]["qtyStep"]),
            "min_qty":   float(item["lotSizeFilter"]["minOrderQty"]),
            "max_qty":   float(item["lotSizeFilter"]["maxOrderQty"]),
        }

    try:
        _filters = _retry(_fetch, "get_instruments_info")
    except Exception as e:
        # Sane BTCUSDT defaults so paper mode works without network.
        log.warning(f"instrument info unavailable, using BTCUSDT defaults: {e}")
        _filters = {"tick_size": 0.1, "qty_step": 0.001, "min_qty": 0.001, "max_qty": 100.0}
    log.info(f"Filters for {gc.GRID_SYMBOL}: {_filters}")
    return _filters


def _decimals(step: float) -> int:
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def round_price(price: float) -> float:
    tick = filters()["tick_size"]
    return round(round(price / tick) * tick, _decimals(tick))


def round_qty(qty: float) -> float:
    step = filters()["qty_step"]
    # Floor, never round up — rounding up can exceed a size cap.
    return round(math.floor(qty / step) * step, _decimals(step))


def qty_is_tradeable(qty: float) -> bool:
    return round_qty(qty) >= filters()["min_qty"]


# ── Account setup ─────────────────────────────────────────────────────────────

def ensure_hedge_mode() -> bool:
    """
    Switch the symbol to hedge mode (both sides) and apply leverage.
    Returns True if the account is ready to trade the grid.
    """
    if gc.GRID_PAPER_MODE:
        log.info("[PAPER] skipping hedge-mode + leverage setup")
        return True

    ok = True
    try:
        _client().switch_position_mode(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            mode=3,  # 3 = both sides (hedge)
        )
        log.info(f"{gc.GRID_SYMBOL} set to hedge mode")
    except Exception as e:
        # 110025 = position mode not modified (already hedge) — benign.
        if "110025" in str(e):
            log.info(f"{gc.GRID_SYMBOL} already in hedge mode")
        else:
            log.error(f"switch_position_mode failed: {e}")
            ok = False

    try:
        _client().set_leverage(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            buyLeverage=str(gc.GRID_LEVERAGE),
            sellLeverage=str(gc.GRID_LEVERAGE),
        )
        log.info(f"Leverage set to {gc.GRID_LEVERAGE}x")
    except Exception as e:
        # 110043 = leverage not modified — benign.
        if "110043" in str(e):
            log.info(f"Leverage already {gc.GRID_LEVERAGE}x")
        else:
            log.warning(f"set_leverage: {e}")

    return ok


# ── Market data ───────────────────────────────────────────────────────────────

def get_klines() -> pd.DataFrame:
    """OHLCV for the grid timeframe, oldest to newest."""
    def _fetch():
        resp = _client().get_kline(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            interval=gc.GRID_INTERVAL,
            limit=gc.GRID_KLINE_LIMIT,
        )
        rows = resp["result"]["list"]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
        df = df.astype({
            "ts": "int64", "open": "float64", "high": "float64",
            "low": "float64", "close": "float64", "vol": "float64",
        })
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)

    return _retry(_fetch, "grid_get_kline")


def get_mark_price() -> float:
    def _fetch():
        resp = _client().get_tickers(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
        )
        return float(resp["result"]["list"][0]["markPrice"])

    return _retry(_fetch, "get_mark_price")


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions() -> dict:
    """
    Both sides of the hedged position.

    Returns {"long": {...} | None, "short": {...} | None} where each entry has
    size / avg_price / unrealised_pnl / leverage.
    """
    empty = {"long": None, "short": None}
    if gc.GRID_PAPER_MODE:
        return empty

    def _fetch():
        resp = _client().get_positions(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
        )
        out = {"long": None, "short": None}
        for p in resp["result"]["list"]:
            size = float(p.get("size") or 0)
            if size <= 0:
                continue
            entry = {
                "size":            size,
                "avg_price":       float(p.get("avgPrice") or 0),
                "unrealised_pnl":  float(p.get("unrealisedPnl") or 0),
                "position_idx":    int(p.get("positionIdx") or 0),
                "side":            p.get("side", ""),
            }
            if entry["position_idx"] == 1:
                out["long"] = entry
            elif entry["position_idx"] == 2:
                out["short"] = entry
        return out

    return _retry(_fetch, "grid_get_positions")


def position_size(positions: dict, side: str) -> float:
    p = positions.get(side)
    return p["size"] if p else 0.0


# ── Orders ────────────────────────────────────────────────────────────────────

def _link_id(tag: str) -> str:
    """Order link id carrying our prefix so reconciliation can identify it."""
    return f"{gc.GRID_ORDER_PREFIX}-{tag}-{uuid.uuid4().hex[:10]}"


def is_ours(order: dict) -> bool:
    return str(order.get("orderLinkId", "")).startswith(f"{gc.GRID_ORDER_PREFIX}-")


def get_open_orders() -> list:
    """Open orders for the symbol that this bot placed. Others are ignored."""
    if gc.GRID_PAPER_MODE:
        return []

    def _fetch():
        resp = _client().get_open_orders(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            openOnly=0,
            limit=50,
        )
        out = []
        for o in resp["result"]["list"]:
            if not is_ours(o):
                continue
            out.append({
                "order_id":     o["orderId"],
                "link_id":      o["orderLinkId"],
                "side":         o["side"],
                "price":        float(o.get("price") or 0),
                "qty":          float(o.get("qty") or 0),
                "position_idx": int(o.get("positionIdx") or 0),
                "reduce_only":  bool(o.get("reduceOnly")),
            })
        return out

    return _retry(_fetch, "grid_get_open_orders")


def place_limit(side: str, qty: float, price: float, position_idx: int,
                reduce_only: bool = False, tag: str = "lvl") -> Optional[str]:
    """
    Post-only limit order. Returns orderId, or None if it was not placed.

    PostOnly matters: the grid is a maker strategy, and a level that would
    cross the book is a level that has already been overtaken by price —
    letting it fill as taker would enter at a worse price than the grid
    assumes. Bybit rejects rather than fills such an order, which is correct
    here; the next cycle re-derives the level.
    """
    qty = round_qty(qty)
    price = round_price(price)

    if not qty_is_tradeable(qty):
        log.debug(f"skip {side} {qty} @ {price}: below min qty")
        return None

    if gc.GRID_DRY_RUN or gc.GRID_PAPER_MODE:
        mode = "DRY" if gc.GRID_DRY_RUN else "PAPER"
        log.info(
            f"[{mode}] {side} LIMIT {qty} @ ${price:,.1f} "
            f"idx={position_idx} reduce_only={reduce_only} tag={tag}"
        )
        return None

    def _order():
        resp = _client().place_order(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="PostOnly",
            positionIdx=position_idx,
            reduceOnly=reduce_only,
            orderLinkId=_link_id(tag),
        )
        oid = resp["result"]["orderId"]
        log.info(
            f"{side} LIMIT {qty} @ ${price:,.1f} idx={position_idx} "
            f"reduce_only={reduce_only} tag={tag} id={oid[:8]}"
        )
        return oid

    try:
        return _retry(_order, f"place_limit_{tag}")
    except Exception as e:
        # PostOnly rejection is expected when a level sits the wrong side of
        # the book — do not escalate it, the next cycle re-derives levels.
        if "30208" in str(e) or "PostOnly" in str(e):
            log.debug(f"PostOnly rejected {side} @ {price}: level crossed")
            return None
        raise


def close_position(side: str, qty: float, ref_price: float = 0.0) -> bool:
    """Market-close `qty` of the long or short side. Used for flips and the kill switch."""
    qty = round_qty(qty)
    if not qty_is_tradeable(qty):
        return False

    position_idx = 1 if side == "long" else 2
    order_side = "Sell" if side == "long" else "Buy"

    if gc.GRID_DRY_RUN or gc.GRID_PAPER_MODE:
        mode = "DRY" if gc.GRID_DRY_RUN else "PAPER"
        log.info(f"[{mode}] CLOSE {side} {qty} @ ~${ref_price:,.1f}")
        return True

    def _order():
        resp = _client().place_order(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            side=order_side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=True,
            positionIdx=position_idx,
            orderLinkId=_link_id("close"),
        )
        log.info(f"CLOSE {side} {qty} market: {resp['result'].get('orderId', '')[:8]}")
        return True

    return _retry(_order, f"close_{side}")


def cancel_order(order_id: str) -> bool:
    if gc.GRID_DRY_RUN or gc.GRID_PAPER_MODE:
        log.info(f"[DRY] cancel {order_id[:8]}")
        return True

    def _cancel():
        _client().cancel_order(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            orderId=order_id,
        )
        return True

    try:
        return _retry(_cancel, "cancel_order")
    except Exception as e:
        # Already filled or cancelled between listing and cancelling.
        if "110001" in str(e):
            log.debug(f"cancel {order_id[:8]}: order no longer open")
            return True
        raise


def cancel_all_ours() -> int:
    """Cancel every open order this bot placed. Leaves other orders alone."""
    n = 0
    for o in get_open_orders():
        if cancel_order(o["order_id"]):
            n += 1
    if n:
        log.info(f"Cancelled {n} grid orders")
    return n


# ── Realised PnL ──────────────────────────────────────────────────────────────

def get_closed_pnl(start_ms: int) -> float:
    """Total realised PnL for the symbol since start_ms (UTC epoch millis)."""
    if gc.GRID_PAPER_MODE:
        return 0.0

    def _fetch():
        resp = _client().get_closed_pnl(
            category=gc.GRID_CATEGORY,
            symbol=gc.GRID_SYMBOL,
            startTime=start_ms,
            limit=100,
        )
        return sum(float(r.get("closedPnl") or 0) for r in resp["result"]["list"])

    try:
        return _retry(_fetch, "get_closed_pnl")
    except Exception as e:
        log.warning(f"get_closed_pnl failed, treating as 0: {e}")
        return 0.0


def get_balance() -> dict:
    if gc.GRID_PAPER_MODE:
        return {"usdt": 10000.0, "equity": 10000.0}

    def _fetch():
        resp = _client().get_wallet_balance(accountType="UNIFIED")
        rows = resp.get("result", {}).get("list", [])
        if not rows or not rows[0].get("coin"):
            return {"usdt": 0.0, "equity": 0.0}
        for c in rows[0]["coin"]:
            if c["coin"].upper() == "USDT":
                return {
                    "usdt":   float(c.get("walletBalance") or 0),
                    "equity": float(c.get("equity") or c.get("walletBalance") or 0),
                }
        return {"usdt": 0.0, "equity": 0.0}

    return _retry(_fetch, "grid_get_balance")
