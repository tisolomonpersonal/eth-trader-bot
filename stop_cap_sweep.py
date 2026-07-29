"""
Find where tightening the stop stops helping.

If results improve monotonically as the cap approaches zero, the "optimum" is
an artifact rather than a real setting - so push it until it turns over.
"""
import numpy as np

import bb_ma28_backtest as bt

CAPS = [0.0, 2.0, 1.5, 1.0, 0.75, 0.5, 0.35, 0.25]
SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]

data = {}
for sym, days in SYMS:
    df = bt.fetch(sym, "240", days)
    data[sym] = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)

print(f"{'cap':>7}", end="")
for sym, _ in SYMS:
    print(f"{sym[:3]+' L':>9}{sym[:3]+' S':>9}", end="")
print(f"{'TOTAL':>10}{'avg win%':>10}")

for cap in CAPS:
    row, total, wins = [], 0.0, []
    for sym, _ in SYMS:
        capobj = {}
        orig = bt.summarise
        bt.summarise = lambda t, x, f, s: (capobj.__setitem__("t", t), orig(t, x, f, s))[1]
        bt.run(data[sym], 0.0, 0.0, True, True, 0, confirm_bars=3,
               use_trend_filter=True, stop_cap_atr=cap)
        bt.summarise = orig
        tr = capobj.get("t", [])
        for side in ("LONG", "SHORT"):
            s = [t["gross_pct"] for t in tr if t["side"] == side]
            net = sum(s)
            row.append(net)
            total += net
        wins.append(np.mean([t["gross_pct"] > 0 for t in tr]) * 100 if tr else 0)

    lbl = "none" if cap == 0 else f"{cap:g}ATR"
    print(f"{lbl:>7}", end="")
    for v in row:
        print(f"{v:>9.2f}", end="")
    print(f"{total:>10.2f}{np.mean(wins):>10.1f}")
