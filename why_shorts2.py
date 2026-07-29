"""
Follow-up: the outcome data says shorts win BIGGER, not more often.
Test the likely cause - the 200MA filter puts shorts into high-volatility
regimes and longs into low-volatility ones, because crypto downtrends are
more volatile than uptrends.
"""
import numpy as np

import bb_ma28_backtest as bt

for sym, days in (("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)):
    df = bt.fetch(sym, "240", days)
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    d = d.dropna(subset=["trend_ma"])

    rng = (d["high"] - d["low"]) / d["close"] * 100      # per-bar range, %
    above = d["close"] > d["trend_ma"]

    # How far is price from the MA28 when a setup could fire? That distance IS
    # the reward, since the MA28 is the target.
    dist = (d["close"] - d["ma"]).abs() / d["close"] * 100
    long_ctx = above & (d["close"] < d["ma"])            # long setup context
    short_ctx = (~above) & (d["close"] > d["ma"])        # short setup context

    print(f"=== {sym} ===")
    print(f"  bar range %      above 200MA: {rng[above].mean():.3f}   "
          f"below 200MA: {rng[~above].mean():.3f}   "
          f"ratio {rng[~above].mean()/rng[above].mean():.2f}x")
    print(f"  dist to MA28 %   long ctx:    {dist[long_ctx].mean():.3f}   "
          f"short ctx:   {dist[short_ctx].mean():.3f}   "
          f"ratio {dist[short_ctx].mean()/dist[long_ctx].mean():.2f}x")
    print(f"  time below 200MA: {(~above).mean()*100:.1f}%")
    print()
