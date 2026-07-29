"""
Does capping the stop fix the long side?

The cap is applied SYMMETRICALLY to both directions. Fixing only longs would
be fitting to the side already known to underperform, which manufactures a
result rather than testing one.

Zero fees throughout, per request - this isolates signal from cost.
"""
import numpy as np

import bb_ma28_backtest as bt

CAPS = [0.0, 3.0, 2.0, 1.5, 1.0]
SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]


def side_stats(trades, side):
    s = [t for t in trades if t["side"] == side]
    if not s:
        return None
    w = [t["gross_pct"] for t in s if t["gross_pct"] > 0]
    l = [t["gross_pct"] for t in s if t["gross_pct"] <= 0]
    aw, al = (np.mean(w) if w else 0), (np.mean(l) if l else 0)
    return {
        "n": len(s), "win": len(w) / len(s) * 100,
        "aw": aw, "al": al, "wl": (aw / abs(al)) if al else 0,
        "net": sum(t["gross_pct"] for t in s),
        "risk": np.mean([t["risk_pct"] for t in s]),
    }


for sym, days in SYMS:
    df = bt.fetch(sym, "240", days)
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    print(f"\n===== {sym} =====")
    print(f"{'cap':<6}{'side':<7}{'n':>4}{'win%':>7}{'risk%':>8}"
          f"{'avgW%':>8}{'avgL%':>8}{'W/L':>6}{'net%':>9}")
    for cap in CAPS:
        cap_lbl = "none" if cap == 0 else f"{cap:g}ATR"
        cap_obj = {}
        orig = bt.summarise
        bt.summarise = lambda t, x, f, s: (cap_obj.__setitem__("t", t), orig(t, x, f, s))[1]
        bt.run(d, 0.0, 0.0, True, True, 0, confirm_bars=3,
               use_trend_filter=True, stop_cap_atr=cap)
        bt.summarise = orig
        tr = cap_obj.get("t", [])
        for side in ("LONG", "SHORT"):
            st = side_stats(tr, side)
            if not st:
                continue
            print(f"{cap_lbl:<6}{side:<7}{st['n']:>4}{st['win']:>7.1f}{st['risk']:>8.3f}"
                  f"{st['aw']:>8.3f}{st['al']:>8.3f}{st['wl']:>6.2f}{st['net']:>9.2f}")
        print()
