"""
OUT-OF-SAMPLE TEST.

Every parameter in this configuration - the 1.5 ATR stop cap, 1 confirming
candle, the 200MA filter, shorts-only - was chosen while looking at BTC, ETH
and SOL. Reporting results on those same assets proves nothing.

These assets were used in no tuning decision whatsoever. Whatever they show is
the honest estimate of what this strategy does on data it has never seen.

Fees are included at Bybit perp taker rates. No parameter is changed.
"""
import numpy as np

import bb_ma28_backtest as bt

# Frozen configuration - nothing here may be adjusted based on what follows.
CFG = dict(bb_period=20, bb_std=2.0, ma=28, ma_type="sma", trend_len=200,
           confirm=1, stop_cap=1.5, fee=0.055, interval="240")

TUNED_ON = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200)]
HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]


def test(sym, days):
    try:
        df = bt.fetch(sym, CFG["interval"], days)
    except Exception as e:
        return {"err": str(e)[:40]}
    d = bt.add_indicators(df, CFG["bb_period"], CFG["bb_std"], CFG["ma"],
                          CFG["ma_type"], CFG["trend_len"])
    r = bt.run(d, CFG["fee"], 0.0, False, True, 0,
               confirm_bars=CFG["confirm"], use_trend_filter=True,
               stop_cap_atr=CFG["stop_cap"])
    return r


def show(title, syms):
    print("=" * 74)
    print(title)
    print("=" * 74)
    print(f"{'symbol':<10}{'n':>5}{'win%':>7}{'net%':>9}{'PF':>7}{'DD%':>7}{'expR':>8}")
    pfs, nets = [], []
    for sym, days in syms:
        r = test(sym, days)
        if r.get("err"):
            print(f"{sym:<10} unavailable ({r['err']})")
            continue
        if not r.get("trades"):
            print(f"{sym:<10} no trades")
            continue
        pf = r["profit_factor"]
        print(f"{sym:<10}{r['trades']:>5}{r['win_rate_pct']:>7}{r['total_net_pct']:>9.2f}"
              f"{str(pf):>7}{r['max_drawdown_pct']:>7}{r['expectancy_R']:>8.3f}")
        if pf:
            pfs.append(pf)
        nets.append(r["total_net_pct"])
    if nets:
        print(f"{'':<10}{'':>5}{'':>7}{np.mean(nets):>9.2f}{np.mean(pfs) if pfs else 0:>7.2f}"
              f"   <- mean")
        print(f"profitable on {sum(1 for x in nets if x > 0)}/{len(nets)} assets")
    print()
    return nets, pfs


print(f"Config (frozen): {CFG}\n")
show("TUNED ON THESE - results here are not evidence", TUNED_ON)
nets, pfs = show("HELD OUT - never used in any tuning decision", HELD_OUT)

if nets:
    print("=" * 74)
    pos = sum(1 for x in nets if x > 0)
    print(f"Out-of-sample: {pos}/{len(nets)} profitable, mean net {np.mean(nets):+.2f}%, "
          f"mean PF {np.mean(pfs):.2f}")
    print()
    if pos >= len(nets) * 0.7 and np.mean(pfs) > 1.2:
        print("=> Holds up on unseen assets.")
    elif pos >= len(nets) * 0.5:
        print("=> Mixed. Weaker out of sample than in - some of the in-sample")
        print("   result was fitting.")
    else:
        print("=> FAILS out of sample. The in-sample result was curve-fitting.")
