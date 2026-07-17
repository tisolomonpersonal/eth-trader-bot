"""
Bybit TradFi client — MetaTrader 5 edition.

Bybit TradFi (Forex, Metals, Indices, Commodities, Stock CFDs) does NOT live on
Bybit's V5 crypto API. It runs on a separate MetaTrader 5 (MT5) system, and the
only way to automate it is to talk to an MT5 terminal. This module connects to
the `mt5-server` service (Wine + MetaTrader5 + mt5linux RPC bridge) and exposes
the SAME public interface the old V5 client had, so tradfi_strategy.py /
scheduler.py / tradfi_risk.py keep working unchanged:

    resolve_symbol, get_klines, list_symbols, get_instrument_info,
    is_market_open, get_balance, get_position, get_swap_fee,
    place_market_order, place_market_buy, place_market_sell, close_position

Symbols follow Bybit TradFi naming: Zero-Fee mode adds a ".s" suffix
(EURUSD.s, XAUUSD.s); Tight-Spread mode has no suffix. TRADFI_MODE controls it,
and resolve_symbol auto-detects whichever variant the terminal actually lists.

Connection settings (config / env):
    MT5_HOST, MT5_PORT   — where the mt5linux RPC bridge listens
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER — Bybit MT5 account credentials
When those aren't set, config.TRADFI_PAPER is True and no live orders are sent.
"""
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config
from logger import get_logger

log = get_logger("tradfi")

_MAX_RETRIES = 3
_RETRY_DELAY = 3  # seconds between retries

_mt5_proxy = None          # cached mt5linux MetaTrader5 proxy
_symbol_cache: dict = {}   # base symbol -> the variant the terminal actually lists


# ── Connection ────────────────────────────────────────────────────────────────

def _mt5():
    """Return a connected mt5linux MetaTrader5 proxy (lazy singleton)."""
    global _mt5_proxy
    if _mt5_proxy is not None:
        return _mt5_proxy

    if not config.MT5_HOST:
        raise RuntimeError("MT5_HOST not set — cannot reach the MT5 terminal.")

    from mt5linux import MetaTrader5  # imported lazily so paper/local runs don't need it

    m = MetaTrader5(host=config.MT5_HOST, port=config.MT5_PORT)

    login = int(config.MT5_LOGIN) if str(config.MT5_LOGIN).isdigit() else None
    initialized = False
    if login:
        initialized = bool(m.initialize(login=login,
                                        password=config.MT5_PASSWORD,
                                        server=config.MT5_SERVER))
    if not initialized:
        initialized = bool(m.initialize())
    if not initialized:
        raise RuntimeError(f"MT5 initialize() failed: {m.last_error()}")

    # Make sure we're actually logged in to the broker.
    if login and m.account_info() is None:
        if not m.login(login, password=config.MT5_PASSWORD, server=config.MT5_SERVER):
            raise RuntimeError(f"MT5 login failed: {m.last_error()}")

    _mt5_proxy = m
    log.info(f"MT5 connected via {config.MT5_HOST}:{config.MT5_PORT}")
    return m


def _reset_client():
    global _mt5_proxy
    _mt5_proxy = None


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


def _timeframe(m, interval):
    """Map a minutes-string interval to an MT5 TIMEFRAME_* constant."""
    mapping = {
        "1": "TIMEFRAME_M1", "3": "TIMEFRAME_M3", "5": "TIMEFRAME_M5",
        "15": "TIMEFRAME_M15", "30": "TIMEFRAME_M30", "60": "TIMEFRAME_H1",
        "120": "TIMEFRAME_H2", "240": "TIMEFRAME_H4", "D": "TIMEFRAME_D1",
        "1D": "TIMEFRAME_D1", "W": "TIMEFRAME_W1",
    }
    return getattr(m, mapping.get(str(interval), "TIMEFRAME_M15"))


# ── Symbol resolution ─────────────────────────────────────────────────────────

def _normalize_base(sym: str) -> str:
    sym = str(sym).upper()
    if sym.endswith(".S"):
        sym = sym[:-2]
    return sym


def resolve_symbol(base_symbol: Optional[str] = None) -> str:
    """
    Return the symbol variant the terminal actually lists.

    TRADFI_MODE gives a preferred suffix (zero_fee -> ".s", tight_spread -> none).
    If the preferred variant isn't found we probe the alternate, cache whichever
    the terminal knows, and add it to Market Watch. Falls back to the configured
    guess (no crash) if the terminal is unreachable.
    """
    base = _normalize_base(base_symbol or config.TRADFI_SYMBOL)
    if base in _symbol_cache:
        return _symbol_cache[base]

    preferred = f"{base}.s" if config.TRADFI_MODE == "zero_fee" else base
    candidates = [preferred, base if preferred.endswith(".s") else f"{base}.s"]

    try:
        m = _mt5()
        for cand in candidates:
            if m.symbol_info(cand) is not None:
                m.symbol_select(cand, True)
                if cand != preferred:
                    log.warning(f"Symbol {preferred} not found — using {cand} "
                                f"(check TRADFI_MODE matches your account)")
                _symbol_cache[base] = cand
                return cand
        log.warning(f"Neither {candidates} found in terminal for base {base}")
    except Exception as e:
        log.warning(f"Symbol probe failed ({e}); using {preferred}")

    return preferred


# ── Market data ───────────────────────────────────────────────────────────────

def get_klines(symbol: Optional[str] = None, interval: Optional[str] = None,
               limit: int = 250) -> pd.DataFrame:
    """Fetch OHLCV candles. Returns a DataFrame (oldest→newest) with columns
    ts, open, high, low, close, vol — the shape indicators.calculate expects."""
    sym = resolve_symbol(symbol)
    itv = interval or config.TRADFI_INTERVAL
    cols = ["ts", "open", "high", "low", "close", "vol"]

    def _fetch():
        m = _mt5()
        m.symbol_select(sym, True)
        rates = m.copy_rates_from_pos(sym, _timeframe(m, itv), 0, limit)
        if rates is None or len(rates) == 0:
            log.warning(f"No candles returned for {sym} — market may be closed")
            return pd.DataFrame(columns=cols)

        df = pd.DataFrame(rates)
        if "tick_volume" in df.columns:
            df["vol"] = df["tick_volume"]
        elif "real_volume" in df.columns:
            df["vol"] = df["real_volume"]
        else:
            df["vol"] = 0.0
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df[cols].astype({
            "open": "float64", "high": "float64", "low": "float64",
            "close": "float64", "vol": "float64",
        })
        return df.sort_values("ts").reset_index(drop=True)

    return _retry(_fetch, "tradfi_get_klines")


def list_symbols(search: Optional[str] = None, limit: int = 1000) -> list:
    """List instrument symbols the terminal exposes, optionally filtered by a
    case-insensitive substring (e.g. search='EUR')."""
    def _fetch():
        m = _mt5()
        syms = m.symbols_get() or []
        names = [getattr(s, "name", "") for s in syms]
        if search:
            s = search.upper()
            names = [x for x in names if s in x.upper()]
        return sorted(names)[:limit]

    return _retry(_fetch, "tradfi_list_symbols")


def get_instrument_info(symbol: Optional[str] = None) -> dict:
    """
    Return instrument metadata in a shape tradfi_risk.calculate_position_qty
    understands. lotSizeFilter mirrors the old V5 keys (qtyStep/minOrderQty),
    now sourced from MT5's volume_step/volume_min. Also includes contract_size
    and margin_per_lot so position sizing can budget by real margin.
    """
    def _fetch():
        m = _mt5()
        sym = resolve_symbol(symbol)
        m.symbol_select(sym, True)
        info = m.symbol_info(sym)
        if info is None:
            raise RuntimeError(f"symbol_info({sym}) returned None")

        vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
        vstep = float(getattr(info, "volume_step", vmin) or vmin)
        vmax = float(getattr(info, "volume_max", 0) or 0)
        contract = float(getattr(info, "trade_contract_size", 1) or 1)

        margin_per_lot = None
        try:
            tick = m.symbol_info_tick(sym)
            price = float(getattr(tick, "ask", 0) or getattr(tick, "bid", 0) or 0)
            if price > 0:
                margin_per_lot = m.order_calc_margin(m.ORDER_TYPE_BUY, sym, 1.0, price)
        except Exception as e:
            log.debug(f"order_calc_margin failed for {sym}: {e}")

        return {
            "symbol": sym,
            "digits": int(getattr(info, "digits", 2) or 2),
            "contract_size": contract,
            "margin_per_lot": float(margin_per_lot) if margin_per_lot else None,
            "lotSizeFilter": {
                "qtyStep": vstep,
                "minOrderQty": vmin,
                "maxOrderQty": vmax,
            },
        }

    return _retry(_fetch, "tradfi_instrument_info")


def is_market_open(symbol: Optional[str] = None) -> bool:
    """
    Open == the terminal has a live, fresh tick for the symbol and trading is
    enabled. MT5 server time may be offset from UTC by a few hours, so we treat
    a tick as "live" when it's younger than TRADFI_MARKET_MAX_TICK_AGE_HRS
    (default 6h) — enough to absorb the offset while still flagging a real
    weekend/overnight closure (many hours or days stale). Fails safe to closed.
    """
    try:
        m = _mt5()
        sym = resolve_symbol(symbol)
        info = m.symbol_info(sym)
        if info is None:
            return False
        if not getattr(info, "visible", True):
            m.symbol_select(sym, True)
            info = m.symbol_info(sym)
        if int(getattr(info, "trade_mode", 4) or 0) == 0:  # SYMBOL_TRADE_MODE_DISABLED
            log.info(f"{sym} trading disabled by broker")
            return False

        tick = m.symbol_info_tick(sym)
        if tick is None or float(getattr(tick, "time", 0) or 0) == 0 \
                or float(getattr(tick, "bid", 0) or 0) <= 0:
            return False

        age_hrs = (datetime.now(timezone.utc).timestamp() - float(tick.time)) / 3600.0
        if age_hrs > config.TRADFI_MARKET_MAX_TICK_AGE_HRS:
            log.info(f"{sym} last tick {age_hrs:.1f}h old "
                     f"(> {config.TRADFI_MARKET_MAX_TICK_AGE_HRS}h) — market closed")
            return False
        return True
    except Exception as e:
        log.warning(f"Could not determine MT5 market status: {e}")
        return False


# ── Account data ──────────────────────────────────────────────────────────────

def get_balance() -> dict:
    """Returns {'usdt': spendable_amount, ...}. In TradFi 'usdt' is really the
    account currency (usually USD); the key is kept for interface compatibility."""
    if config.TRADFI_PAPER:
        return {"usdt": config.TRADFI_MAX_INVESTMENT_USDT * 2}

    def _fetch():
        m = _mt5()
        acc = m.account_info()
        if acc is None:
            raise RuntimeError("account_info() returned None (not logged in?)")
        balance = float(getattr(acc, "balance", 0.0) or 0.0)
        free = getattr(acc, "margin_free", None)
        spendable = float(free) if (free is not None and float(free) > 0) else balance
        return {
            "usdt": spendable,
            "balance": balance,
            "equity": float(getattr(acc, "equity", balance) or balance),
            "currency": getattr(acc, "currency", "USD"),
        }

    return _retry(_fetch, "tradfi_get_balance")


def get_position(symbol: Optional[str] = None) -> Optional[dict]:
    """Return the open position for symbol as a normalized dict, or None if flat."""
    def _fetch():
        m = _mt5()
        sym = resolve_symbol(symbol)
        positions = m.positions_get(symbol=sym)
        if not positions:
            return None
        p = positions[0]
        side = "Buy" if int(getattr(p, "type", 0) or 0) == 0 else "Sell"  # 0 = POSITION_TYPE_BUY
        return {
            "side": side,
            "size": float(getattr(p, "volume", 0) or 0),
            "entry": float(getattr(p, "price_open", 0) or 0),
            "ticket": int(getattr(p, "ticket", 0) or 0),
            "profit": float(getattr(p, "profit", 0) or 0),
        }

    return _retry(_fetch, "tradfi_get_position")


def get_swap_fee(symbol: Optional[str] = None) -> Optional[dict]:
    """Overnight swap (financing) rates for holding a position, if available."""
    try:
        m = _mt5()
        info = m.symbol_info(resolve_symbol(symbol))
        if info is None:
            return None
        return {
            "swap_long": getattr(info, "swap_long", None),
            "swap_short": getattr(info, "swap_short", None),
        }
    except Exception:
        return None


# ── Order execution ────────────────────────────────────────────────────────────

def _filling_mode(m, info):
    """Pick an order filling mode the symbol accepts (IOC > FOK > RETURN)."""
    flags = int(getattr(info, "filling_mode", 0) or 0)
    if flags & 2:   # SYMBOL_FILLING_IOC
        return m.ORDER_FILLING_IOC
    if flags & 1:   # SYMBOL_FILLING_FOK
        return m.ORDER_FILLING_FOK
    return m.ORDER_FILLING_RETURN


def place_market_order(side: str, qty: float, symbol: Optional[str] = None,
                       reduce_only: bool = False,
                       position_ticket: Optional[int] = None) -> dict:
    """
    Send a market order via MT5. side is 'Buy'/'Sell'; qty is in LOTS.
    Pass position_ticket to close/reduce a specific open position.
    """
    sym = resolve_symbol(symbol)
    if side not in ("Buy", "Sell"):
        raise ValueError(f"side must be 'Buy' or 'Sell', got {side!r}")

    if config.TRADFI_PAPER:
        log.info(f"[PAPER {side.upper()}] {qty} {sym} (reduce_only={reduce_only})")
        return {"paper": True, "symbol": sym, "side": side, "qty": qty}

    def _order():
        m = _mt5()
        m.symbol_select(sym, True)
        info = m.symbol_info(sym)
        tick = m.symbol_info_tick(sym)
        if info is None or tick is None:
            raise RuntimeError(f"No symbol/tick data for {sym}")

        order_type = m.ORDER_TYPE_BUY if side == "Buy" else m.ORDER_TYPE_SELL
        price = float(getattr(tick, "ask") if side == "Buy" else getattr(tick, "bid"))

        request = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(qty),
            "type": order_type,
            "price": price,
            "deviation": config.MT5_DEVIATION,
            "magic": config.MT5_MAGIC,
            "comment": "eth-trader-bot",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": _filling_mode(m, info),
        }
        if position_ticket:
            request["position"] = int(position_ticket)

        res = m.order_send(request)
        retcode = getattr(res, "retcode", None)
        if res is None or retcode != m.TRADE_RETCODE_DONE:
            raise RuntimeError(
                f"order_send rejected: retcode={retcode} "
                f"comment={getattr(res, 'comment', '')}")
        log.info(f"TradFi {side.upper()} {qty} {sym} filled @ "
                 f"{getattr(res, 'price', price)} (order {getattr(res, 'order', '?')})")
        return {
            "order": getattr(res, "order", None),
            "price": float(getattr(res, "price", price) or price),
            "volume": float(getattr(res, "volume", qty) or qty),
            "retcode": retcode,
        }

    return _retry(_order, "tradfi_place_order")


def place_market_buy(qty: float, symbol: Optional[str] = None) -> dict:
    return place_market_order("Buy", qty, symbol)


def place_market_sell(qty: float, symbol: Optional[str] = None) -> dict:
    return place_market_order("Sell", qty, symbol)


def close_position(symbol: Optional[str] = None) -> Optional[dict]:
    """Flatten every open position on symbol by sending offsetting market orders
    referencing each position ticket (the MT5 way to close)."""
    sym = resolve_symbol(symbol)

    if config.TRADFI_PAPER:
        log.info(f"[PAPER CLOSE] {sym}")
        return {"paper": True, "symbol": sym}

    def _close():
        m = _mt5()
        positions = m.positions_get(symbol=sym)
        if not positions:
            log.info(f"No open TradFi position on {sym} to close")
            return None
        results = []
        for p in positions:
            side = "Sell" if int(getattr(p, "type", 0) or 0) == 0 else "Buy"
            results.append(place_market_order(
                side, float(getattr(p, "volume", 0) or 0), symbol,
                reduce_only=True, position_ticket=int(getattr(p, "ticket", 0) or 0)))
        return results

    return _retry(_close, "tradfi_close_position")


# ── Diagnostics ────────────────────────────────────────────────────────────────

def diagnose(symbol: Optional[str] = None) -> dict:
    """One-shot health check for the MT5 link — surfaces exactly why a symbol
    reads as closed. Consumed by the /tradfi/debug endpoint."""
    d = {
        "mt5_host": config.MT5_HOST or "(not set)",
        "mt5_port": config.MT5_PORT,
        "creds_set": bool(config.MT5_LOGIN and config.MT5_PASSWORD and config.MT5_SERVER),
        "tradfi_paper": config.TRADFI_PAPER,
    }
    try:
        m = _mt5()
        d["connected"] = True
    except Exception as e:
        d["connected"] = False
        d["connect_error"] = str(e)
        return d

    try:
        acc = m.account_info()
        d["account"] = None if acc is None else {
            "login": getattr(acc, "login", None),
            "server": getattr(acc, "server", None),
            "balance": getattr(acc, "balance", None),
            "currency": getattr(acc, "currency", None),
        }
    except Exception as e:
        d["account_error"] = str(e)

    try:
        sym = resolve_symbol(symbol)
        d["resolved_symbol"] = sym
        info = m.symbol_info(sym)
        d["symbol_found"] = info is not None
        if info is not None:
            d["trade_mode"] = getattr(info, "trade_mode", None)
            d["visible"] = getattr(info, "visible", None)
            tick = m.symbol_info_tick(sym)
            if tick is not None:
                age = datetime.now(timezone.utc).timestamp() - float(getattr(tick, "time", 0) or 0)
                d["tick"] = {
                    "bid": getattr(tick, "bid", None),
                    "ask": getattr(tick, "ask", None),
                    "age_hours": round(age / 3600, 2),
                }
    except Exception as e:
        d["symbol_error"] = str(e)

    try:
        d["is_market_open"] = is_market_open(symbol)
    except Exception as e:
        d["market_open_error"] = str(e)
    return d
