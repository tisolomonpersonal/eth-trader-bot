"""
Technical indicator calculations.
All functions operate on a pandas DataFrame with columns: open, high, low, close, vol.
"""
import pandas as pd
from logger import get_logger

log = get_logger("indicators")


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
    line    = _ema(closes, 12) - _ema(closes, 26)
    signal  = _ema(line, 9)
    hist    = line - signal
    return {
        "macd_line":   round(float(line.iloc[-1]),   6),
        "macd_signal": round(float(signal.iloc[-1]), 6),
        "macd_hist":   round(float(hist.iloc[-1]),   6),
    }


def ema_values(closes: pd.Series) -> dict:
    e50  = _ema(closes, 50)
    e200 = _ema(closes, 200)
    return {
        "ema50":      round(float(e50.iloc[-1]),  4),
        "ema200":     round(float(e200.iloc[-1]), 4),
        "prev_ema50": round(float(e50.iloc[-2]),  4) if len(e50) > 1 else 0.0,
        "prev_ema200":round(float(e200.iloc[-2]), 4) if len(e200) > 1 else 0.0,
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
    closes  = df["close"]
    price   = round(float(closes.iloc[-1]), 4)

    emas    = ema_values(closes)
    e50     = emas["ema50"]
    e200    = emas["ema200"]
    pe50    = emas["prev_ema50"]
    pe200   = emas["prev_ema200"]

    # Trend from EMA relationship
    if e50 > e200 * 1.001:
        trend = "Bullish"
    elif e50 < e200 * 0.999:
        trend = "Bearish"
    else:
        trend = "Neutral"

    # Crossover detection
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
        "price":        price,
        "ema50":        e50,
        "ema200":       e200,
        "trend":        trend,
        "crossover":    crossover,
        "rsi":          rsi_val,
        "vol_trend":    vol_trend,
        "atr":          atr_val,
        **macd_vals,
    }

    log.debug(f"Indicators: price={price} ema50={e50} ema200={e200} "
              f"trend={trend} rsi={rsi_val} macd_hist={macd_vals['macd_hist']:+.6f} "
              f"vol={vol_trend} atr={atr_val}")
    return result
