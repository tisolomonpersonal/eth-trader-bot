"""
Does the FINAL strategy work on 15m?

The earlier 15m failure used 2-3 confirming candles, no stop cap and both
directions. The final version is different: 200MA filter, 1 confirming candle,
stop capped at 1.5 ATR, short side only.

Run at zero fees so the question is purely whether the SIGNAL survives at
shorter timeframes.
"""
import numpy as np

import bb_ma28_backtest as bt

TFS = [("15", 180), ("60", 360), ("240", 720)]
IN_SAMPLE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HELD_OUT = ["BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def test(sym, tf, days, fee):
    try:
        df = bt.fetch(sym, tf, days)
    except Exception:
        return None
    d = bt.add_indicators(df, 20, 2.0, 28, "sma", 200)
    r = bt.run(d, fee, 0.0, False, True, 0, confirm_bars=1,
               use_trend_filter=True, stop_cap_atr=1.5)
    return r if r.get("trades") else None


for fee, flabel in ((0.0, "ZERO FEES — is the signal there?"),
                    (0.055, "WITH FEES — would it survive costs?")):
    print("=" * 78)
    print(flabel)
    print("=" * 78)
    for group, syms in (("tuned on", IN_SAMPLE), ("held out", HELD_OUT)):
        print(f"\n-- {group} --")
        print(f"{'TF':>5}{'n/asset':>9}{'win%':>7}{'net%':>9}{'PF':>7}"
              f"{'expR':>8}{'DD%':>7}{'assets +':>10}")
        for tf, days in TFS:
            ns, wins, nets, pfs, exps, dds = [], [], [], [], [], []
            for sym in syms:
                r = test(sym, tf, days, fee)
                if not r:
                    continue
                ns.append(r["trades"])
                wins.append(r["win_rate_pct"])
                nets.append(r["total_net_pct"])
                if r["profit_factor"]:
                    pfs.append(r["profit_factor"])
                exps.append(r["expectancy_R"])
                dds.append(r["max_drawdown_pct"])
            if not ns:
                continue
            pos = sum(1 for x in nets if x > 0)
            print(f"{tf+'m':>5}{int(np.mean(ns)):>9}{np.mean(wins):>7.1f}"
                  f"{np.mean(nets):>9.2f}{np.mean(pfs) if pfs else 0:>7.2f}"
                  f"{np.mean(exps):>8.3f}{np.mean(dds):>7.1f}"
                  f"{f'{pos}/{len(nets)}':>10}")
    print()
