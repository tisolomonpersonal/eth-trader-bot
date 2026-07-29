"""
Where BTC sits relative to the long-horizon moving averages used as cycle markers.

Note on the 200-MONTH MA: it needs 200 monthly candles = 16.7 years. BTC has
only traded since ~2010 and this dataset starts in 2020 (76 months), so it
cannot be computed. The widely-used long-horizon cycle marker is the 200-WEEK
MA (~3.8 years), which IS computable here.
"""
import pandas as pd

import bb_ma28_backtest as bt

daily = bt.fetch("BTCUSDT", "D", 3000).set_index("ts")
c = daily["close"]
print(f"Data: {c.index[0].date()} -> {c.index[-1].date()}  "
      f"({len(c)} days / {len(c)/7:.0f} weeks / {len(c)/30.44:.0f} months)")
print(f"Price now: ${float(c.iloc[-1]):,.0f}\n")

wk = c.resample("W").last().dropna()
mo = c.resample("ME").last().dropna()
print(f"Weekly candles available : {len(wk)}")
print(f"Monthly candles available: {len(mo)}")
print(f"200-month MA computable? {'yes' if len(mo) >= 200 else 'NO - needs 200, have %d' % len(mo)}")
print(f"200-week  MA computable? {'yes' if len(wk) >= 200 else 'NO'}\n")

price = float(c.iloc[-1])


def report(series, length, label):
    if len(series) < length:
        print(f"{label:<18} n/a (needs {length}, have {len(series)})")
        return
    ma = series.rolling(length).mean()
    v = float(ma.iloc[-1])
    side = "ABOVE" if price > v else "BELOW"
    print(f"{label:<18} ${v:>10,.0f}   price is {side} by {abs(price/v-1)*100:>5.1f}%")


print("=== WEEKLY chart ===")
for n in (50, 100, 200):
    report(wk, n, f"{n}-week MA")

print("\n=== MONTHLY chart ===")
# 20/21-month are the common monthly-chart cycle markers, since 200 is impossible.
for n in (12, 20, 21, 50):
    report(mo, n, f"{n}-month MA")

print("\n=== DAILY chart (for reference) ===")
for n in (50, 200):
    report(c, n, f"{n}-day MA")

print("\n=== History vs the 200-week MA ===")
if len(wk) >= 200:
    ma200w = wk.rolling(200).mean()
    both = pd.DataFrame({"price": wk, "ma": ma200w}).dropna()
    below = both[both.price < both.ma]
    print(f"Weeks with data since the 200w MA became computable: {len(both)} "
          f"({both.index[0].date()} onward)")
    print(f"Weeks closed BELOW it: {len(below)} ({len(below)/len(both)*100:.1f}%)")
    if len(below):
        print(f"Most recent week below: {below.index[-1].date()}")
    print(f"Current distance above/below: {(price/float(ma200w.iloc[-1])-1)*100:+.1f}%")
