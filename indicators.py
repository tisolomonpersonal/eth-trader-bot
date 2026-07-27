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


# ── Scalping indicators ───────────────────────────────────────────────────────

def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full ATR series (the scalar `atr()` above only returns the last value)."""
    prev = df["close"].shift()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average Directional Index — trend *strength*, direction-agnostic.
    This is the regime switch: low ADX means range (fade setups are valid),
    high ADX means trend (fades get run over, only breakouts/pullbacks allowed).
    """
    up = df["high"].diff()
    down = -df["low"].diff()

    plus_dm = ((up > down) & (up > 0)).astype(float) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)).astype(float) * down.clip(lower=0)

    tr = atr_series(df, period)
    # Guard against a flat/zero-range window producing div-by-zero.
    tr_safe = tr.replace(0, float("nan"))

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_safe

    denom = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / denom
    value = dx.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]

    if pd.isna(value):
        return 0.0
    return round(float(value), 2)


def bollinger(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> dict:
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    # Bandwidth normalised by the mid, so it's comparable across price levels.
    bandwidth = (upper - lower) / mid.replace(0, float("nan")) * 100

    last_mid = float(mid.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    price = float(closes.iloc[-1])

    # Where price sits inside the band: 0.0 = lower band, 1.0 = upper band.
    span = last_upper - last_lower
    pct_b = (price - last_lower) / span if span > 0 else 0.5

    return {
        "bb_mid": round(last_mid, 2),
        "bb_upper": round(last_upper, 2),
        "bb_lower": round(last_lower, 2),
        "bb_bandwidth": round(float(bandwidth.iloc[-1]), 4),
        "bb_pct_b": round(float(pct_b), 4),
        "_bandwidth_series": bandwidth,
    }


def is_squeeze(bandwidth: pd.Series, lookback: int = 50, pctile: float = 25) -> bool:
    """
    True when current Bollinger bandwidth sits in the bottom `pctile`% of the
    last `lookback` bars — volatility is compressed and an expansion is likely.
    This is the arming condition for breakout scalps.
    """
    window = bandwidth.iloc[-lookback:].dropna()
    if len(window) < lookback // 2:
        return False
    threshold = window.quantile(pctile / 100)
    current = bandwidth.iloc[-1]
    if pd.isna(current) or pd.isna(threshold):
        return False
    return bool(current <= threshold)


def session_vwap(df: pd.DataFrame) -> float:
    """
    Rolling VWAP anchored to the current UTC day. Intraday mean-reversion
    targets VWAP because that's where resting institutional flow clusters.
    """
    if "ts" not in df.columns:
        return float(df["close"].iloc[-1])

    today = df["ts"].dt.date.iloc[-1]
    session = df[df["ts"].dt.date == today]
    # Very early in a UTC session there aren't enough bars for a stable VWAP —
    # fall back to the last 60 bars rather than returning a 2-bar average.
    if len(session) < 20:
        session = df.iloc[-60:]

    typical = (session["high"] + session["low"] + session["close"]) / 3
    vol = session["vol"]
    total_vol = vol.sum()
    if total_vol <= 0:
        return float(df["close"].iloc[-1])
    return round(float((typical * vol).sum() / total_vol), 2)


def calculate_scalp(df: pd.DataFrame, trend_df: pd.DataFrame | None = None) -> dict:
    """
    Full scalping indicator set off 1-minute candles, plus an optional
    higher-timeframe (5m) DataFrame used only for the directional bias filter.
    """
    import config

    closes = df["close"]
    price = round(float(closes.iloc[-1]), 2)

    bb = bollinger(closes, config.BB_PERIOD, config.BB_STD)
    bandwidth_series = bb.pop("_bandwidth_series")

    atr_val = float(atr_series(df, config.ATR_PERIOD).iloc[-1])
    atr_pct = (atr_val / price * 100) if price else 0.0

    ema9 = float(_ema(closes, 9).iloc[-1])
    ema21 = float(_ema(closes, 21).iloc[-1])

    adx_val = adx(df, config.ADX_PERIOD)
    if adx_val <= config.RANGE_ADX_MAX:
        regime = "RANGE"
    elif adx_val >= config.TREND_ADX_MIN:
        regime = "TREND"
    else:
        regime = "TRANSITION"

    # Higher-timeframe bias — never fade against a strong 5m trend.
    htf_bias = "NEUTRAL"
    if trend_df is not None and len(trend_df) >= 50:
        htf_closes = trend_df["close"]
        htf_ema50 = float(_ema(htf_closes, 50).iloc[-1])
        htf_price = float(htf_closes.iloc[-1])
        if htf_price > htf_ema50 * 1.0005:
            htf_bias = "BULLISH"
        elif htf_price < htf_ema50 * 0.9995:
            htf_bias = "BEARISH"

    return {
        "price": price,
        "atr": round(atr_val, 2),
        "atr_pct": round(atr_pct, 4),
        "adx": adx_val,
        "regime": regime,
        "squeeze": is_squeeze(bandwidth_series, config.SQUEEZE_LOOKBACK, config.SQUEEZE_PCTILE),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "vwap": session_vwap(df),
        "rsi": rsi(closes),
        "vol_trend": volume_trend(df["vol"]),
        "htf_bias": htf_bias,
        "recent_high": round(float(df["high"].iloc[-config.SQUEEZE_LOOKBACK:].max()), 2),
        "recent_low": round(float(df["low"].iloc[-config.SQUEEZE_LOOKBACK:].min()), 2),
        **bb,
        **macd(closes),
    }


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
