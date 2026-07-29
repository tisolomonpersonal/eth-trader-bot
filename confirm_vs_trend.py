"""
Is the 3-candle confirmation still needed once the 200MA filter is on?

Both are filters, so they may be doing the same job twice. If fewer candles
works as well with the trend filter engaged, that is a strict win: more
trades, more statistical confidence, and more opportunity.

Everything else held at the current config: 4h, BB 20/2.0, MA28 dynamic
target, stop capped at 1.5 ATR, zero fees.
"""
import numpy as np

import bb_ma28_backtest as bt

SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]
data = {}
for sym, days in SYMS:
    data[sym] = bt.add_indicators(bt.fetch(sym, "240", days), 20, 2.0, 28, "sma", 200)


def stats(sym, confirm, trend, shorts_only):
    cap = {}
    orig = bt.summarise
    bt.summarise = lambda t, x, f, s: (cap.__setitem__("t", t), orig(t, x, f, s))[1]
    bt.run(data[sym], 0.0, 0.0, not shorts_only, True, 0,
           confirm_bars=confirm, use_trend_filter=trend, stop_cap_atr=1.5)
    bt.summarise = orig
    tr = [t for t in cap.get("t", []) if (t["side"] == "SHORT") or not shorts_only]
    if not tr:
        return None
    g = [t["gross_pct"] for t in tr]
    return {"n": len(tr), "win": np.mean([x > 0 for x in g]) * 100,
            "net": sum(g), "exp": np.mean(g),
            "expR": np.mean([t["gross_pct"] / t["risk_pct"] for t in tr if t["risk_pct"]])}


for shorts_only in (True, False):
    print("=" * 84)
    print("SHORTS ONLY" if shorts_only else "BOTH SIDES")
    print("=" * 84)
    print(f"{'cnf':>4}{'200MA':>7}", end="")
    for sym, _ in SYMS:
        print(f"{sym[:3]+' n':>8}{sym[:3]+' net':>10}", end="")
    print(f"{'TOT net':>10}{'TOT n':>8}{'avg expR':>10}")

    for confirm in (1, 2, 3, 4):
        for trend in (False, True):
            tot, totn, exps = 0.0, 0, []
            cells = []
            for sym, _ in SYMS:
                s = stats(sym, confirm, trend, shorts_only)
                if s is None:
                    cells.append((0, 0.0))
                    continue
                cells.append((s["n"], s["net"]))
                tot += s["net"]
                totn += s["n"]
                exps.append(s["expR"])
            print(f"{confirm:>4}{('ON' if trend else 'off'):>7}", end="")
            for n, net in cells:
                print(f"{n:>8}{net:>10.2f}", end="")
            print(f"{tot:>10.2f}{totn:>8}{np.mean(exps) if exps else 0:>10.3f}")
        print()
