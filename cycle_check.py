"""Where BTC sits in the halving cycle, and what the trend has actually done."""
import pandas as pd

import bb_ma28_backtest as bt

df = bt.fetch("BTCUSDT", "D", 3000).set_index("ts")
c = df["close"]

print(f"Data: {c.index[0].date()} -> {c.index[-1].date()}   price now ${c.iloc[-1]:,.0f}")
print()

for h, lbl in (("2020-05-11", "2020 halving"), ("2024-04-20", "2024 halving")):
    hd = pd.Timestamp(h, tz="UTC")
    if hd < c.index[0]:
        print(f"{lbl}: before data starts\n")
        continue
    p0 = float(c.asof(hd))
    print(f"{lbl} ({h})   price ${p0:,.0f}")
    for mth in (6, 12, 18, 24, 27):
        t = hd + pd.DateOffset(months=mth)
        if t > c.index[-1]:
            break
        p = float(c.asof(t))
        print(f"    +{mth:>2}mo  ${p:>10,.0f}   {(p/p0-1)*100:>+8.1f}%")
    print()

now = c.index[-1]
hd = pd.Timestamp("2024-04-20", tz="UTC")
print(f"Months since the 2024 halving: {(now - hd).days / 30.44:.1f}")
print()

print("Recent trend:")
for w, lbl in ((30, "1 month"), (90, "3 months"), (180, "6 months"), (365, "1 year")):
    p = float(c.iloc[-w]) if len(c) > w else float(c.iloc[0])
    print(f"  last {lbl:<9}: {(float(c.iloc[-1]) / p - 1) * 100:>+8.1f}%")
print()

ma200 = c.rolling(200).mean()
above = "ABOVE" if c.iloc[-1] > ma200.iloc[-1] else "BELOW"
print(f"Price vs 200-day MA: ${float(c.iloc[-1]):,.0f} vs ${float(ma200.iloc[-1]):,.0f} "
      f"({(float(c.iloc[-1]) / float(ma200.iloc[-1]) - 1) * 100:+.1f}%) -> {above}")
print(f"Peak in data: ${float(c.max()):,.0f} on {c.idxmax().date()} "
      f"(now {(float(c.iloc[-1]) / float(c.max()) - 1) * 100:+.1f}% from it)")
