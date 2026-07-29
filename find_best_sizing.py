"""
Find the leverage and position size that produce tangible profit on $10.

Searches every combination that a $10 account can actually place, scores each
by simulated one-month outcome over the out-of-sample trade distribution, and
compares against Kelly-optimal sizing.

Key distinction the exchange makes, which decides the answer:

  * POSITION SIZE sets your real risk. Effective leverage is notional/account,
    and that is what multiplies every price move into your P&L.
  * The LEVERAGE SETTING only decides how much margin is reserved, i.e.
    whether the order is permitted at all, and in isolated margin how far away
    liquidation sits.

So you cannot reduce risk by lowering the leverage setting. Only a smaller
position does that - and Bybit's 0.001 BTC minimum puts a floor on how small
the position can be.
"""
import numpy as np

import bb_ma28_backtest as bt

ACCOUNT = 10.0
MAINT = 0.005
HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]

rets, risks, per_year = [], [], []
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
    risks += [t["risk_pct"] for t in tr]
    per_year.append(len(tr) / (days / 365))

rets = np.array(rets) / 100
risks = np.array(risks)
tpm = np.mean(per_year) / 12
mu, sd = rets.mean(), rets.std()

price = float(bt.fetch("BTCUSDT", "D", 30)["close"].iloc[-1])
MIN_QTY = 0.001

print("=" * 72)
print("THE EDGE")
print("=" * 72)
print(f"mean return per trade : {mu*100:+.4f}%")
print(f"stdev per trade       : {sd*100:.4f}%")
kelly = mu / (sd ** 2)
print(f"Kelly-optimal leverage: {kelly:.2f}x")
print(f"Growth turns NEGATIVE beyond ~{2*kelly:.2f}x (2x Kelly)")
print()
print("=" * 72)
print("THE CONSTRAINT")
print("=" * 72)
print(f"BTC ${price:,.0f}, minimum order {MIN_QTY} BTC = ${MIN_QTY*price:,.2f} notional")
min_eff = MIN_QTY * price / ACCOUNT
print(f"Smallest position a ${ACCOUNT:.0f} account can hold = {min_eff:.2f}x effective leverage")
print(f"That is {min_eff/kelly:.1f}x the Kelly-optimal size, before any choice is made.")
print()

rng = np.random.default_rng(11)
N = 30_000


def sim(qty, lev):
    notional = qty * price
    margin = notional / lev
    if margin > ACCOUNT:
        return None                      # order would be rejected
    finals = np.empty(N)
    for i in range(N):
        eq = ACCOUNT
        for _ in range(rng.poisson(tpm)):
            if eq < margin:
                break
            pnl = notional * rng.choice(rets)
            if -pnl >= eq:
                eq = 0.0
                break
            eq += pnl
        finals[i] = eq
    return {
        "eff": notional / ACCOUNT,
        "median": np.median(finals),
        "mean": finals.mean(),
        "ahead": (finals > ACCOUNT).mean() * 100,
        "ruin": (finals <= 0.5).mean() * 100,
        "stop_loss_pct": risks.mean() * notional / ACCOUNT,
        "unprotected": (risks > ACCOUNT / notional * 100).mean() * 100,
    }


print("=" * 72)
print("EVERY PLACEABLE COMBINATION (cross margin, one month)")
print("=" * 72)
print(f"{'qty':>7}{'lev':>6}{'effLev':>8}{'median$':>9}{'mean$':>8}"
      f"{'ahead%':>8}{'ruin%':>7}{'1 stop':>8}{'unprot%':>9}")

best = None
for qty in (0.001, 0.002, 0.003, 0.004, 0.005):
    for lev in (5, 7, 10, 25, 50, 75, 100):
        r = sim(qty, lev)
        if r is None:
            continue
        print(f"{qty:>7.3f}{lev:>6}{r['eff']:>8.1f}{r['median']:>9.2f}{r['mean']:>8.2f}"
              f"{r['ahead']:>8.1f}{r['ruin']:>7.2f}{-r['stop_loss_pct']:>7.1f}%{r['unprotected']:>9.1f}")
        if best is None or r["median"] > best[1]["median"]:
            best = ((qty, lev), r)

print()
print("=" * 72)
if best and best[1]["median"] > ACCOUNT:
    print(f"Best median outcome: {best[0]} -> ${best[1]['median']:.2f}")
else:
    print("NO COMBINATION HAS A MEDIAN ABOVE $10.")
    print(f"Least-bad: qty {best[0][0]}, leverage {best[0][1]}x "
          f"-> median ${best[1]['median']:.2f}")
print()
cap_needed = MIN_QTY * price / kelly
print(f"Capital required to hold the MINIMUM position at Kelly-optimal size:")
print(f"  ${MIN_QTY*price:,.2f} notional / {kelly:.2f}x = ${cap_needed:,.2f}")
