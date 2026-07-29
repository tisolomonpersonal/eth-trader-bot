"""
Does the strategy have an edge, or is it just riding the market's direction?

Fees are set to zero here on purpose: the question is whether the SIGNAL is
real, separated from what it costs to trade. (A zero-fee result tells you about
edge, not about what an account would actually do.)

The test that matters: split history into rising and falling periods, then ask
whether longs work when the market rises and shorts work when it falls -- and
critically, compare against simply holding that direction the whole time. If
buy-and-hold does as well, the strategy is adding nothing.
"""
import pandas as pd

import bb_ma28_backtest as bt

SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]


def phase_label(sub: pd.DataFrame) -> str:
    """Rising or falling, by where price ended vs started."""
    chg = float(sub["close"].iloc[-1]) / float(sub["close"].iloc[0]) - 1
    return "RISING" if chg > 0 else "FALLING"


for sym, days in SYMS:
    df = bt.fetch(sym, "240", days)
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    n = len(d)

    print(f"\n===== {sym} =====")
    print(f"{'period':<10}{'mkt%':>9}{'phase':>9}"
          f"{'LONG n':>8}{'LONG%':>9}{'SHORT n':>9}{'SHORT%':>9}"
          f"{'hold-long%':>12}{'hold-short%':>13}")

    # Four equal chunks so each covers a different stretch of the cycle.
    for k in range(4):
        a, b = k * n // 4, (k + 1) * n // 4
        sub = d.iloc[a:b].reset_index(drop=True)
        mkt = (float(sub["close"].iloc[-1]) / float(sub["close"].iloc[0]) - 1) * 100

        rl = bt.run(sub, 0.0, 0.0, True, False, 0, confirm_bars=3, use_trend_filter=True)
        rs = bt.run(sub, 0.0, 0.0, False, True, 0, confirm_bars=3, use_trend_filter=True)

        ln = rl.get("trades", 0)
        lp = rl.get("total_net_pct", 0.0) if ln else 0.0
        sn = rs.get("trades", 0)
        sp = rs.get("total_net_pct", 0.0) if sn else 0.0

        print(f"{'Q'+str(k+1):<10}{mkt:>9.1f}{phase_label(sub):>9}"
              f"{ln:>8}{lp:>9.2f}{sn:>9}{sp:>9.2f}"
              f"{mkt:>12.1f}{-mkt:>13.1f}")
