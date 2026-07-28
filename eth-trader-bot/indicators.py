"""
Technical indicator calculations — Directional Candle Strategy.

New additions:
  detect_directional_candle(df_h1)  → signal dict or None
  fib_entry_zone(h1_high, h1_low, direction) → (fib_low, fib_high)
  check_structural_block(df_h1, h1_high, h1_low, direction) → bool
  find_swing_tp(df_m5, direction, entry, sl) → float
"""
import pandas as pd
from logger import get_logger
import config

log = get_logger("indicators")


# ── Basic helpers ─────────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    value = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(value), 2)


def macd(closes: pd.Series) -> dict:
    line   = _ema(closes, 12) - _ema(closes, 26)
    signal = _ema(line, 9)
    hist   = line - signal
    return {
        "macd_line":   round(float(line.iloc[-1]),   6),
        "macd_signal": round(float(signal.iloc[-1]), 6),
        "macd_hist":   round(float(hist.iloc[-1]),   6),
    }


def ema_values(closes: pd.Series) -> dict:
    e50  = _ema(closes, 50)
    e200 = _ema(closes, 200)
    return {
        "ema50":       round(float(e50.iloc[-1]),  4),
        "ema200":      round(float(e200.iloc[-1]), 4),
        "prev_ema50":  round(float(e50.iloc[-2]),  4) if len(e50) > 1 else 0.0,
        "prev_ema200": round(float(e200.iloc[-2]), 4) if len(e200) > 1 else 0.0,
    }


def atr(df: pd.DataFrame, period: int = 14) -> float:
    prev  = df["close"].shift()
    tr    = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    value = tr.rolling(period).mean().iloc[-1]
    return round(float(value), 4)


def volume_trend(volumes: pd.Series, lookback: int = 20) -> str:
    if len(volumes) < lookback + 1:
        return "Unknown"
    avg = volumes.iloc[-(lookback + 1):-1].mean()
    cur = float(volumes.iloc[-1])
    if avg == 0:
        return "Unknown"
    ratio = cur / avg
    if ratio >= 1.5:
        return "High"
    if ratio <= 0.5:
        return "Low"
    return "Normal"


def calculate(df: pd.DataFrame) -> dict:
    """Compute all indicators from an OHLCV DataFrame. Returns a flat dict."""
    closes = df["close"]
    price  = round(float(closes.iloc[-1]), 4)

    emas  = ema_values(closes)
    e50   = emas["ema50"]
    e200  = emas["ema200"]
    pe50  = emas["prev_ema50"]
    pe200 = emas["prev_ema200"]

    if e50 > e200 * 1.001:
        trend = "Bullish"
    elif e50 < e200 * 0.999:
        trend = "Bearish"
    else:
        trend = "Neutral"

    if pe50 <= pe200 and e50 > e200:
        crossover = "Golden Cross"
    elif pe50 >= pe200 and e50 < e200:
        crossover = "Death Cross"
    else:
        crossover = "None"

    macd_vals = macd(closes)
    rsi_val   = rsi(closes)
    atr_val   = atr(df)
    vol_trend = volume_trend(df["vol"])

    result = {
        "price":     price,
        "ema50":     e50,
        "ema200":    e200,
        "trend":     trend,
        "crossover": crossover,
        "rsi":       rsi_val,
        "vol_trend": vol_trend,
        "atr":       atr_val,
        **macd_vals,
    }

    log.debug(
        f"Indicators: price={price} ema50={e50} ema200={e200} "
        f"trend={trend} rsi={rsi_val} macd_hist={macd_vals['macd_hist']:+.6f} "
        f"vol={vol_trend} atr={atr_val}"
    )
    return result


# ── Directional Candle Strategy helpers ───────────────────────────────────────

def detect_directional_candle(df_h1: pd.DataFrame) -> dict | None:
    """
    Inspect the last CLOSED H1 candle (index -2; index -1 is still forming).

    Bearish setup:
      current_high > prev_high  AND  current_close < prev_low
      → swept above the prior high then closed below the prior low

    Bullish setup:
      current_low < prev_low  AND  current_close > prev_high
      → swept below the prior low then closed above the prior high

    Returns a dict with keys:
      direction   : 'LONG' | 'SHORT'
      h1_high     : float — high of the signal candle
      h1_low      : float — low of the signal candle
      candle_ts   : pd.Timestamp — open time of the signal candle
    or None if no signal.
    """
    if len(df_h1) < 3:
        return None

    # -1 is still forming, -2 is the last closed, -3 is the one before
    cur  = df_h1.iloc[-2]
    prev = df_h1.iloc[-3]

    c_high  = float(cur["high"])
    c_low   = float(cur["low"])
    c_close = float(cur["close"])
    p_high  = float(prev["high"])
    p_low   = float(prev["low"])

    if c_high > p_high and c_close < p_low:
        log.info(
            f"[H1 Signal] BEARISH directional candle @ {cur['ts']} | "
            f"high={c_high} swept prev_high={p_high}, close={c_close} < prev_low={p_low}"
        )
        return {
            "direction": "SHORT",
            "h1_high":   c_high,
            "h1_low":    c_low,
            "candle_ts": str(cur["ts"]),
        }

    if c_low < p_low and c_close > p_high:
        log.info(
            f"[H1 Signal] BULLISH directional candle @ {cur['ts']} | "
            f"low={c_low} swept prev_low={p_low}, close={c_close} > prev_high={p_high}"
        )
        return {
            "direction": "LONG",
            "h1_high":   c_high,
            "h1_low":    c_low,
            "candle_ts": str(cur["ts"]),
        }

    return None


def fib_entry_zone(h1_high: float, h1_low: float, direction: str) -> tuple[float, float]:
    """
    Calculate the 61.8%–70.5% Fibonacci retracement entry zone
    for the completed H1 directional candle.

    After a BULLISH signal, price retraces DOWN from the close (≈ h1_high)
    back into the candle. The golden zone is:
      fib_low  = h1_high - 0.705 * range
      fib_high = h1_high - 0.618 * range

    After a BEARISH signal, price retraces UP from the close (≈ h1_low)
    back into the candle:
      fib_low  = h1_low + 0.618 * range
      fib_high = h1_low + 0.705 * range

    Returns (fib_low, fib_high).
    """
    rng = h1_high - h1_low

    if direction == "LONG":
        fib_low  = round(h1_high - config.FIB_ENTRY_HIGH * rng, 2)
        fib_high = round(h1_high - config.FIB_ENTRY_LOW  * rng, 2)
    else:  # SHORT
        fib_low  = round(h1_low  + config.FIB_ENTRY_LOW  * rng, 2)
        fib_high = round(h1_low  + config.FIB_ENTRY_HIGH * rng, 2)

    return fib_low, fib_high


def check_structural_block(
    df_h1: pd.DataFrame,
    h1_high: float,
    h1_low: float,
    direction: str,
) -> bool:
    """
    Return True (block the trade) if the signal candle's extreme is
    within STRUCTURAL_FILTER_PCT % of the 50-bar lookback extreme.

    Logic: if we're a bearish candle slamming into a 50-bar high,
    there's no runway — abort. Similarly for longs at 50-bar lows.
    """
    lookback = df_h1.iloc[-52:-2]  # 50 bars before the signal candle
    if len(lookback) < 10:
        return False

    if direction == "SHORT":
        extreme = float(lookback["high"].max())
        pct_diff = abs(h1_high - extreme) / extreme * 100
        if pct_diff <= config.STRUCTURAL_FILTER_PCT:
            log.info(
                f"[StructuralFilter] BLOCKED SHORT — signal high {h1_high:.2f} "
                f"is within {pct_diff:.3f}% of 50-bar high {extreme:.2f}"
            )
            return True

    else:  # LONG
        extreme = float(lookback["low"].min())
        pct_diff = abs(h1_low - extreme) / extreme * 100
        if pct_diff <= config.STRUCTURAL_FILTER_PCT:
            log.info(
                f"[StructuralFilter] BLOCKED LONG — signal low {h1_low:.2f} "
                f"is within {pct_diff:.3f}% of 50-bar low {extreme:.2f}"
            )
            return True

    return False


def find_swing_tp(
    df_m5: pd.DataFrame,
    direction: str,
    entry_price: float,
    sl_price: float,
) -> float:
    """
    Find the nearest M5 structural swing high (for shorts) or swing low (for longs)
    that satisfies the minimum RR. Falls back to DEFAULT_RR if no valid swing found.

    A swing high = bar whose high > both neighbours (simple 1-bar pivot).
    A swing low  = bar whose low  < both neighbours.
    """
    risk    = abs(entry_price - sl_price)
    min_rr  = config.MIN_RR
    def_rr  = config.DEFAULT_RR

    if direction == "LONG":
        min_tp = entry_price + risk * min_rr
        # Look for the nearest swing HIGH above min_tp
        for i in range(len(df_m5) - 2, 1, -1):
            h   = float(df_m5.iloc[i]["high"])
            h_l = float(df_m5.iloc[i - 1]["high"])
            h_r = float(df_m5.iloc[i + 1]["high"]) if i + 1 < len(df_m5) else 0.0
            if h > h_l and h > h_r and h > min_tp:
                log.info(f"[SwingTP] LONG swing high TP found: {h:.2f}")
                return round(h, 2)
        # Fallback: DEFAULT_RR
        tp = round(entry_price + risk * def_rr, 2)
        log.info(f"[SwingTP] LONG fallback TP ({def_rr}:1): {tp:.2f}")
        return tp

    else:  # SHORT
        max_tp = entry_price - risk * min_rr
        # Look for the nearest swing LOW below max_tp
        for i in range(len(df_m5) - 2, 1, -1):
            l   = float(df_m5.iloc[i]["low"])
            l_l = float(df_m5.iloc[i - 1]["low"])
            l_r = float(df_m5.iloc[i + 1]["low"]) if i + 1 < len(df_m5) else float("inf")
            if l < l_l and l < l_r and l < max_tp:
                log.info(f"[SwingTP] SHORT swing low TP found: {l:.2f}")
                return round(l, 2)
        tp = round(entry_price - risk * def_rr, 2)
        log.info(f"[SwingTP] SHORT fallback TP ({def_rr}:1): {tp:.2f}")
        return tp


def detect_fvg(df_m5: pd.DataFrame, direction: str) -> bool:
    """
    Optional confluence: check for a Fair Value Gap (FVG) in the last 10 M5 bars
    in the direction of the trade.

    Bullish FVG: gap[i-1].high < gap[i+1].low  (gap skipped upward)
    Bearish FVG: gap[i-1].low  > gap[i+1].high (gap skipped downward)

    Returns True if a matching FVG is present (confluence confirmed).
    """
    window = df_m5.iloc[-12:-1]
    for i in range(1, len(window) - 1):
        if direction == "LONG":
            if float(window.iloc[i - 1]["high"]) < float(window.iloc[i + 1]["low"]):
                return True
        else:  # SHORT
            if float(window.iloc[i - 1]["low"]) > float(window.iloc[i + 1]["high"]):
                return True
    return False
