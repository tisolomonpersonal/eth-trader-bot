"""
Multi-timeframe test: 15-minute entries, 4-hour trend filter.

The 200MA fails on a 15m chart because 200 candles there is ~2 days - noise,
not trend. On 4h it spans ~33 days and is meaningful. So take the trend filter
from the 4h chart and the entries from the 15m chart.

LOOKAHEAD IS THE WHOLE RISK HERE. A 4h candle beginning at time T is not
finished until T+4h, so its moving-average value must not be visible to any
15m bar before then. The higher-timeframe series is therefore shifted one bar
before being mapped down, so a 15m bar at time T only ever sees 4h data that
had already closed. Getting this wrong invents an edge out of nothing.
"""
import numpy as np
import pandas as pd

import bb_ma28_backtest as bt


def add_htf_trend(df15: pd.DataFrame, htf: str = "4h", length: int = 200) -> pd.DataFrame:
    """Attach the higher-timeframe trend MA to 15m bars, without lookahead."""
    d = df15.set_index("ts")
    agg = d.resample(htf, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"}
    ).dropna()

    # rolling mean of CLOSED higher-TF candles, then shift so the value for the
    # bar starting at T reflects only candles that finished at or before T.
    htf_ma = agg["close"].rolling(length).mean().shift(1)

    mapped = htf_ma.reindex(d.index, method="ffill")
    out = df15.copy()
    out["trend_ma"] = mapped.values
    return out


def run_case(sym, days, entry_tf, htf, fee, use_htf):
    df = bt.fetch(sym, entry_tf, days)
    # MA28 / bands / ATR all from the entry timeframe.
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    if use_htf:
        d = add_htf_trend(d, htf=htf, length=200)   # overwrite the trend column
    r = bt.run(d, fee, 0.0, False, True, 0, confirm_bars=1,
               use_trend_filter=True, stop_cap_atr=1.5)
    return r


IN_SAMPLE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HELD_OUT = ["BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]

CASES = [
    ("15m entry, 15m 200MA", "15", None, False),
    ("15m entry, 4h  200MA", "15", "4h", True),
    ("15m entry, 1D  200MA", "15", "1D", True),
    ("4h  entry, 4h  200MA", "240", None, False),
]

for fee in (0.0, 0.055):
    print("=" * 82)
    print(f"{'ZERO FEES' if fee == 0 else 'WITH FEES'}")
    print("=" * 82)
    for group, syms in (("tuned on", IN_SAMPLE), ("held out", HELD_OUT)):
        print(f"\n-- {group} --")
        print(f"{'setup':<24}{'n/asset':>9}{'win%':>7}{'net%':>9}"
              f"{'PF':>7}{'expR':>8}{'DD%':>7}{'+assets':>9}")
        for label, etf, htf, use in CASES:
            days = 180 if etf == "15" else 720
            ns, wins, nets, pfs, exps, dds = [], [], [], [], [], []
            for sym in syms:
                try:
                    r = run_case(sym, days, etf, htf, fee, use)
                except Exception:
                    continue
                if not r.get("trades"):
                    continue
                ns.append(r["trades"]); wins.append(r["win_rate_pct"])
                nets.append(r["total_net_pct"]); exps.append(r["expectancy_R"])
                dds.append(r["max_drawdown_pct"])
                if r["profit_factor"]:
                    pfs.append(r["profit_factor"])
            if not ns:
                print(f"{label:<24} no data")
                continue
            pos = sum(1 for x in nets if x > 0)
            print(f"{label:<24}{int(np.mean(ns)):>9}{np.mean(wins):>7.1f}"
                  f"{np.mean(nets):>9.2f}{np.mean(pfs) if pfs else 0:>7.2f}"
                  f"{np.mean(exps):>8.3f}{np.mean(dds):>7.1f}"
                  f"{f'{pos}/{len(nets)}':>9}")
    print()
