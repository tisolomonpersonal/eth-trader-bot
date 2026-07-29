"""
Does banking part-way to the MA beat riding it all the way?

Closer targets get hit more often but pay less. Reported separately for the
assets used in tuning and the held-out ones, because a change that only helps
the former is a fit, not an improvement.
"""
import numpy as np

import bb_ma28_backtest as bt

FRACTIONS = [0.25, 0.4, 0.5, 0.6, 0.75, 1.0]
IN_SAMPLE = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]
HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]

cache = {}
for sym, days in IN_SAMPLE + HELD_OUT:
    cache[sym] = bt.add_indicators(bt.fetch(sym, "240", days), 20, 2.0, 28, "sma", 200)


def evaluate(syms, frac, fee):
    wins, nets, pfs, exps, dds, ns = [], [], [], [], [], []
    for sym, _ in syms:
        r = bt.run(cache[sym], fee, 0.0, False, True, 0, confirm_bars=1,
                   use_trend_filter=True, stop_cap_atr=1.5, tp_fraction=frac)
        if not r.get("trades"):
            continue
        ns.append(r["trades"]); wins.append(r["win_rate_pct"])
        nets.append(r["total_net_pct"]); exps.append(r["expectancy_R"])
        dds.append(r["max_drawdown_pct"])
        if r["profit_factor"]:
            pfs.append(r["profit_factor"])
    if not ns:
        return None
    return {"n": int(np.mean(ns)), "win": np.mean(wins), "net": np.mean(nets),
            "pf": np.mean(pfs) if pfs else 0, "expR": np.mean(exps),
            "dd": np.mean(dds), "pos": sum(1 for x in nets if x > 0), "tot": len(nets)}


for fee, flab in ((0.0, "ZERO FEES"), (0.055, "WITH FEES")):
    print("=" * 80)
    print(flab)
    print("=" * 80)
    for label, syms in (("TUNED ON (not evidence)", IN_SAMPLE),
                        ("HELD OUT (the real test)", HELD_OUT)):
        print(f"\n-- {label} --")
        print(f"{'tp frac':>8}{'n':>6}{'win%':>7}{'net%':>9}{'PF':>7}"
              f"{'expR':>8}{'DD%':>7}{'+assets':>9}")
        for frac in FRACTIONS:
            r = evaluate(syms, frac, fee)
            if not r:
                continue
            mark = "  <- as specified" if frac == 1.0 else ""
            ratio = f"{r['pos']}/{r['tot']}"
            print(f"{frac:>8.2f}{r['n']:>6}{r['win']:>7.1f}{r['net']:>9.2f}"
                  f"{r['pf']:>7.2f}{r['expR']:>8.3f}{r['dd']:>7.1f}"
                  f"{ratio:>9}{mark}")
    print()
