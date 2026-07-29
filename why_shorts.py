"""
Why does the short side work and the long side not?

Three candidate mechanisms, measured rather than asserted:

  A) STOP ASYMMETRY. A long's stop is the low of the candle that touched the
     LOWER band - typically a big red panic candle. A short's stop is the high
     of the candle that touched the UPPER band - typically a smaller green one.
     Crypto falls faster than it rises, so the two are not mirror images: longs
     may be structurally handed wider stops than shorts.

  B) FOLLOW-THROUGH ASYMMETRY. Downside breaks are driven by liquidation
     cascades, which are one-directional because leveraged longs dominate open
     interest. If crashes overshoot and keep going while rallies exhaust, then
     fading a low is unreliable while fading a high is not.

  C) IT WAS JUST A BEAR MARKET. Already partly ruled out (shorts profited in
     rising quarters), but quantified again here per side.
"""
import numpy as np
import pandas as pd

import bb_ma28_backtest as bt

SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]


def collect(sym, days):
    df = bt.fetch(sym, "240", days)
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    cap = {}
    orig = bt.summarise
    bt.summarise = lambda t, x, f, s: (cap.__setitem__("t", t), orig(t, x, f, s))[1]
    bt.run(d, 0.0, 0.0, True, True, 0, confirm_bars=3, use_trend_filter=True)
    bt.summarise = orig
    return d, cap.get("t", [])


print("=" * 78)
print("A) STOP DISTANCE — is the long side handed wider stops?")
print("=" * 78)
print(f"{'symbol':<9}{'side':<7}{'n':>4}{'med risk%':>11}{'mean risk%':>12}{'p90 risk%':>11}")
all_rows = []
for sym, days in SYMS:
    d, tr = collect(sym, days)
    all_rows.append((sym, d, tr))
    for side in ("LONG", "SHORT"):
        r = [t["risk_pct"] for t in tr if t["side"] == side]
        if not r:
            continue
        print(f"{sym:<9}{side:<7}{len(r):>4}{np.median(r):>11.3f}"
              f"{np.mean(r):>12.3f}{np.percentile(r,90):>11.3f}")

print()
print("=" * 78)
print("B) FOLLOW-THROUGH — what happens after each kind of band touch?")
print("=" * 78)
print("Forward return over the next 6 bars (24h) after a band touch,")
print("measured on ALL touches, independent of the strategy's entry rules.")
print()
print(f"{'symbol':<9}{'touch':<8}{'n':>5}{'mean fwd%':>11}{'median%':>10}{'contd%':>9}")
for sym, d, tr in all_rows:
    c = d["close"].values
    fwd = pd.Series(c).shift(-6) / pd.Series(c) - 1
    for kind, mask in (("LOWER", d["touch_lower"].values),
                       ("UPPER", d["touch_upper"].values)):
        f = fwd[mask].dropna() * 100
        if not len(f):
            continue
        # "continued" = kept moving in the direction of the touch (down for a
        # lower-band touch, up for an upper-band touch) rather than reverting.
        contd = (f < 0).mean() * 100 if kind == "LOWER" else (f > 0).mean() * 100
        print(f"{sym:<9}{kind:<8}{len(f):>5}{f.mean():>11.3f}{f.median():>10.3f}{contd:>9.1f}")

print()
print("=" * 78)
print("C) OUTCOME SHAPE — where the money actually goes")
print("=" * 78)
print(f"{'symbol':<9}{'side':<7}{'n':>4}{'win%':>7}{'avgWin%':>9}{'avgLoss%':>10}"
      f"{'W/L':>7}{'net%':>9}")
for sym, d, tr in all_rows:
    for side in ("LONG", "SHORT"):
        s = [t for t in tr if t["side"] == side]
        if not s:
            continue
        w = [t["gross_pct"] for t in s if t["gross_pct"] > 0]
        l = [t["gross_pct"] for t in s if t["gross_pct"] <= 0]
        aw = np.mean(w) if w else 0
        al = np.mean(l) if l else 0
        print(f"{sym:<9}{side:<7}{len(s):>4}{len(w)/len(s)*100:>7.1f}{aw:>9.3f}"
              f"{al:>10.3f}{(aw/abs(al) if al else 0):>7.2f}"
              f"{sum(t['gross_pct'] for t in s):>9.2f}")
