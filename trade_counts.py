"""
Exactly how many trades, over how long, produced the 4h results - per asset.
BNB included prominently since it was the weakest of the held-out set.
"""
import numpy as np

import bb_ma28_backtest as bt

ASSETS = [
    ("BTCUSDT", 1600, "tuned on"),
    ("ETHUSDT", 1600, "tuned on"),
    ("SOLUSDT", 1200, "tuned on"),
    ("BNBUSDT", 1600, "held out"),
    ("XRPUSDT", 1600, "held out"),
    ("ADAUSDT", 1600, "held out"),
    ("DOGEUSDT", 1600, "held out"),
    ("LINKUSDT", 1600, "held out"),
    ("LTCUSDT", 1600, "held out"),
    ("AVAXUSDT", 1400, "held out"),
    ("DOTUSDT", 1600, "held out"),
]

for fee, flab in ((0.0, "ZERO FEES"), (0.055, "WITH FEES")):
    print("=" * 92)
    print(f"4h chart, 200MA filter, 1 confirm, 1.5 ATR stop cap, shorts only — {flab}")
    print("=" * 92)
    print(f"{'symbol':<10}{'group':<10}{'days':>6}{'years':>7}{'trades':>8}"
          f"{'tr/yr':>7}{'win%':>7}{'net%':>9}{'PF':>7}{'DD%':>7}")
    rows = []
    for sym, days, group in ASSETS:
        try:
            df = bt.fetch(sym, "240", days)
        except Exception as e:
            print(f"{sym:<10}{group:<10} unavailable")
            continue
        d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
        r = bt.run(d, fee, 0.0, False, True, 0, confirm_bars=1,
                   use_trend_filter=True, stop_cap_atr=1.5)
        if not r.get("trades"):
            print(f"{sym:<10}{group:<10} no trades")
            continue
        yrs = r["days"] / 365
        print(f"{sym:<10}{group:<10}{r['days']:>6.0f}{yrs:>7.1f}{r['trades']:>8}"
              f"{r['trades']/yrs:>7.1f}{r['win_rate_pct']:>7.1f}"
              f"{r['total_net_pct']:>9.2f}{str(r['profit_factor']):>7}"
              f"{r['max_drawdown_pct']:>7.1f}")
        rows.append((group, r["trades"], r["total_net_pct"],
                     r["profit_factor"] or 0, yrs))

    for group in ("tuned on", "held out"):
        g = [x for x in rows if x[0] == group]
        if not g:
            continue
        tot = sum(x[1] for x in g)
        print(f"  {group}: {tot} trades total across {len(g)} assets, "
              f"mean net {np.mean([x[2] for x in g]):+.2f}%, "
              f"mean PF {np.mean([x[3] for x in g]):.2f}, "
              f"{sum(1 for x in g if x[2] > 0)}/{len(g)} profitable")
    print()
