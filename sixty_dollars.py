"""
What does $60 do differently?

At $10 the minimum position forced 6.5x effective leverage - far past Kelly,
so volatility drag pushed the median below the mean. $60 is roughly the
capital at which the minimum position IS Kelly-sized, which removes the drag.

Simulates 1 month and 12 months, and includes hosting cost, since at this
scale it is the dominant term.
"""
import sys

import numpy as np

import bb_ma28_backtest as bt

ACCOUNT = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
HOSTING_PER_MONTH = 5.0
HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]

rets, per_year = [], []
for sym, days in HELD_OUT:
    cap = {}
    orig = bt.summarise
    bt.summarise = lambda t, x, f, s: (cap.__setitem__("t", t), orig(t, x, f, s))[1]
    d = bt.add_indicators(bt.fetch(sym, "240", days), 20, 2.0, 28, "sma", 200)
    bt.run(d, 0.055, 0.0, False, True, 0, confirm_bars=1,
           use_trend_filter=True, stop_cap_atr=1.5)
    bt.summarise = orig
    tr = cap.get("t", [])
    rets += [t["net_pct"] for t in tr]
    per_year.append(len(tr) / (days / 365))

rets = np.array(rets) / 100
tpm = np.mean(per_year) / 12
mu, sd = rets.mean(), rets.std()
kelly = mu / sd ** 2

price = float(bt.fetch("BTCUSDT", "D", 30)["close"].iloc[-1])
rng = np.random.default_rng(3)
N = 40_000


def sim(qty, months):
    notional = qty * price
    if notional / 100 > ACCOUNT:          # cannot meet margin even at 100x
        return None
    finals = np.empty(N)
    for i in range(N):
        eq = ACCOUNT
        for _ in range(rng.poisson(tpm * months)):
            if eq < notional / 100:
                break
            pnl = notional * rng.choice(rets)
            if -pnl >= eq:
                eq = 0.0
                break
            eq += pnl
        finals[i] = eq
    return finals


print(f"Account ${ACCOUNT:,.0f} | BTC ${price:,.0f} | "
      f"min position 0.001 BTC = ${0.001*price:,.2f}")
print(f"Edge: {mu*100:+.4f}%/trade, stdev {sd*100:.3f}%, "
      f"{tpm:.1f} trades/month, Kelly {kelly:.2f}x")
print()

for months, label in ((1, "ONE MONTH"), (12, "ONE YEAR")):
    print("=" * 74)
    print(f"{label}")
    print("=" * 74)
    print(f"{'qty':>7}{'effLev':>8}{'vs Kelly':>10}{'median$':>10}{'mean$':>9}"
          f"{'ahead%':>8}{'ruin%':>7}{'5th%ile':>9}")
    for qty in (0.001, 0.002, 0.003, 0.005):
        f = sim(qty, months)
        if f is None:
            continue
        eff = qty * price / ACCOUNT
        print(f"{qty:>7.3f}{eff:>8.2f}{eff/kelly:>9.1f}x{np.median(f):>10.2f}"
              f"{f.mean():>9.2f}{(f > ACCOUNT).mean()*100:>8.1f}"
              f"{(f <= 0.5).mean()*100:>7.2f}{np.percentile(f, 5):>9.2f}")
    print()

# --- the term that actually decides it -------------------------------------
best = sim(0.001, 12)
profit = np.median(best) - ACCOUNT
print("=" * 74)
print("NET OF RUNNING COSTS (0.001 BTC, one year)")
print("=" * 74)
print(f"  median trading profit : ${profit:+,.2f}")
print(f"  hosting (12 x ${HOSTING_PER_MONTH:.0f})  : ${-HOSTING_PER_MONTH*12:+,.2f}")
print(f"  net                   : ${profit - HOSTING_PER_MONTH*12:+,.2f}")
print()
monthly_pct = mu * tpm * min(kelly, 0.001 * price / ACCOUNT) * 100
print(f"At Kelly-ish sizing the edge is ~{monthly_pct:.3f}%/month.")
if monthly_pct > 0:
    print(f"Capital needed for $100/month: ${100/(monthly_pct/100):,.0f}")
