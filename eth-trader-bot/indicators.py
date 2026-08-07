"""
Technical indicator calculations — 4H Bollinger Band Short Strategy.

Key functions for this strategy:
  detect_bb_short_setup(df)   → setup dict or None
  get_ma28_current(df)        → float  (current MA28 — moving TP target)
  bollinger_bands(df)         → (upper, middle, lower) Series tuple
  sma(series, period)         → pd.Series

General helpers retained for the hourly summary and the grid bot:
  atr, rsi, macd, ema_values, calculate
"""
import pandas as pd
from logger import get_logger
import config

log = get_logger("indicators")


# ── Basic helpers ─────────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


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
    """Compute all general indicators from an OHLCV DataFrame. Returns a flat dict."""
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


# ── BB Short Strategy helpers ─────────────────────────────────────────────────

def bollinger_bands(
    df: pd.DataFrame,
    period: int = None,
    std: float = None,
) -> tuple:
    """
    Compute Bollinger Bands over the DataFrame's close prices.

    Returns (upper, middle, lower) as pd.Series, all same length as df.
    """
    period = period or config.BB_PERIOD
    std    = std    or config.BB_STD

    closes = df["close"]
    middle = sma(closes, period)
    sigma  = closes.rolling(window=period, min_periods=period).std(ddof=1)
    upper  = middle + std * sigma
    lower  = middle - std * sigma
    return upper, middle, lower


def get_ma28_current(df: pd.DataFrame) -> float:
    """
    Current MA28 value — used as the moving take-profit target.
    Uses the last row (which may be a forming candle; that's intentional —
    as the average drifts toward you, it gets you out before the trade sours).
    Returns 0.0 if there aren't enough candles yet.
    """
    ma = sma(df["close"], config.MA_SHORT)
    val = ma.iloc[-1]
    if pd.isna(val):
        return 0.0
    return round(float(val), 2)


def detect_bb_short_setup(df: pd.DataFrame) -> dict | None:
    """
    Scan the last two CLOSED 4H candles for a Bollinger Band short setup.

    Labels (df is sorted oldest→newest, df[-1] is still forming):
      signal_candle  = df[-3]  — must have high >= upper BB  (the "touch" candle)
      confirm_candle = df[-2]  — must be red (close < open)  (the confirming candle)

    All four conditions must be met on the CONFIRM candle's close:
      1. signal_candle.high >= upper_bb at that candle   (price spiked to upper band)
      2. confirm_candle is red                            (close < open)
      3. confirm_candle.close < MA200                    (downtrend filter)
      4. confirm_candle.close > MA28                     (price has room to fall to MA28)

    Additionally:
      - confirm_candle must immediately follow signal_candle (no gap)
      - enough history must exist for MA200 to be valid

    Returns a dict:
      signal_candle_ts  : str  — timestamp of the BB-touch candle (used for dedup)
      confirm_candle_ts : str  — timestamp of the red confirmation candle
      bb_touch_high     : float — high of the signal candle (SL anchor)
      entry_price_ref   : float — close of the confirm candle (entry reference)
      ma28              : float — MA28 at entry (initial TP reference)
      ma200             : float — MA200 at confirm candle
      upper_bb          : float — upper band value at signal candle

    Returns None if the setup is not present.
    """
    min_rows = max(config.MA_LONG, config.BB_PERIOD) + 5
    if len(df) < min_rows:
        log.warning(f"[BB setup] Not enough candles: {len(df)} < {min_rows}")
        return None

    upper_bb, _, _ = bollinger_bands(df)
    ma28_series    = sma(df["close"], config.MA_SHORT)
    ma200_series   = sma(df["close"], config.MA_LONG)

    # index -1 is forming; -2 is confirm candle; -3 is signal (BB touch) candle
    sig_idx = -3
    con_idx = -2

    sig  = df.iloc[sig_idx]
    con  = df.iloc[con_idx]

    sig_upper  = upper_bb.iloc[sig_idx]
    con_ma28   = ma28_series.iloc[con_idx]
    con_ma200  = ma200_series.iloc[con_idx]

    if any(pd.isna(v) for v in [sig_upper, con_ma28, con_ma200]):
        log.debug("[BB setup] NaN in indicators — not enough history yet")
        return None

    sig_high  = float(sig["high"])
    sig_close = float(sig["close"])
    con_open  = float(con["open"])
    con_close = float(con["close"])

    # Condition 1: signal candle touched or crossed upper band
    if sig_high < float(sig_upper):
        return None

    # Condition 2: confirm candle is red (bearish close)
    if con_close >= con_open:
        log.debug(
            f"[BB setup] Confirm candle NOT red: open={con_open:.2f} close={con_close:.2f}"
        )
        return None

    # Condition 3: confirm candle close is BELOW MA200 (downtrend)
    if con_close >= float(con_ma200):
        log.debug(
            f"[BB setup] Price NOT below MA200: close={con_close:.2f} ma200={con_ma200:.2f}"
        )
        return None

    # Condition 4: confirm candle close is ABOVE MA28 (room to drop to target)
    if con_close <= float(con_ma28):
        log.debug(
            f"[BB setup] Price NOT above MA28: close={con_close:.2f} ma28={con_ma28:.2f}"
        )
        return None

    # Immediacy check: confirm candle must directly follow signal candle (no 4H gap)
    try:
        expected_gap = pd.Timedelta(hours=4)
        actual_gap   = con["ts"] - sig["ts"]
        if abs(actual_gap - expected_gap) > pd.Timedelta(minutes=30):
            log.debug(
                f"[BB setup] Non-consecutive candles: gap={actual_gap}. Setup void."
            )
            return None
    except Exception:
        pass  # Timestamp check is best-effort; don't block the trade

    log.info(
        f"[BB Setup] VALID SHORT @ {con['ts']} | "
        f"signal_high={sig_high:.2f} upper_bb={float(sig_upper):.2f} | "
        f"confirm close={con_close:.2f} open={con_open:.2f} | "
        f"MA28={float(con_ma28):.2f} MA200={float(con_ma200):.2f}"
    )

    return {
        "signal_candle_ts":  str(sig["ts"]),
        "confirm_candle_ts": str(con["ts"]),
        "bb_touch_high":     round(sig_high, 2),
        "entry_price_ref":   round(con_close, 2),
        "ma28":              round(float(con_ma28), 2),
        "ma200":             round(float(con_ma200), 2),
        "upper_bb":          round(float(sig_upper), 2),
    }
