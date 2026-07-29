"""
What would this strategy actually do to a $10 account in one month?

Uses the OUT-OF-SAMPLE trade outcomes - the honest ones, where profit factor
was 1.06 - rather than the flattering in-sample numbers. Monte Carlo over the
real distribution of trade results, with the sizing constraints a $10 account
actually faces on Bybit.
"""
import numpy as np

import bb_ma28_backtest as bt

ACCOUNT = 10.0
HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]

# --- Collect the real out-of-sample trade returns --------------------------
pool, per_year = [], []
for sym, days in HELD_OUT:
    cap = {}
    orig = bt.summarise
    bt.summarise = lambda t, x, f, s: (cap.__setitem__("t", t), orig(t, x, f, s))[1]
    d = bt.add_indicators(bt.fetch(sym, "240", days), 20, 2.0, 28, "sma", 200)
    bt.run(d, 0.055, 0.0, False, True, 0, confirm_bars=1,
           use_trend_filter=True, stop_cap_atr=1.5)
    bt.summarise = orig
    tr = cap.get("t", [])
    pool += [t["net_pct"] for t in tr]
    per_year.append(len(tr) / (days / 365))

pool = np.array(pool)
tpm = np.mean(per_year) / 12          # trades per month
print(f"Out-of-sample trade pool: {len(pool)} trades")
print(f"  mean {pool.mean():+.3f}%  median {np.median(pool):+.3f}%  "
      f"stdev {pool.std():.3f}%")
print(f"  win rate {(pool > 0).mean() * 100:.1f}%")
print(f"  trades per month: {tpm:.1f}")
print()

# --- Sizing reality --------------------------------------------------------
price = float(bt.fetch("BTCUSDT", "D", 30)["close"].iloc[-1])
min_notional = 0.001 * price
lev_needed = min_notional / ACCOUNT
print(f"BTC ${price:,.0f} | minimum position 0.001 BTC = ${min_notional:,.2f} notional")
print(f"A ${ACCOUNT:.0f} account needs {lev_needed:.1f}x leverage to open one position.")
print(f"So each 1% move in BTC = {lev_needed:.1f}% of your account.")
liq = 100 / lev_needed
print(f"Approx liquidation distance: {liq:.1f}% adverse move (before fees/margin buffer).")
print()

# --- Monte Carlo -----------------------------------------------------------
rng = np.random.default_rng(7)
N = 100_000
finals, ruined = [], 0
for _ in range(N):
    eq = ACCOUNT
    n = rng.poisson(tpm)
    for _ in range(n):
        if eq < 0.5:
            break
        r = rng.choice(pool) * lev_needed / 100     # leveraged return on equity
        if r <= -1.0:                                # wiped out
            eq = 0.0
            break
        eq *= (1 + r)
    if eq <= 0.5:
        ruined += 1
    finals.append(eq)

finals = np.array(finals)
print(f"Monte Carlo, {N:,} simulated months at {lev_needed:.1f}x:")
print(f"  median outcome : ${np.median(finals):.2f}")
print(f"  mean outcome   : ${finals.mean():.2f}")
print()
for p in (5, 25, 50, 75, 95):
    print(f"  {p:>2}th percentile: ${np.percentile(finals, p):>6.2f}")
print()
print(f"  chance you END UP AHEAD : {(finals > ACCOUNT).mean() * 100:.1f}%")
print(f"  chance you lose > half   : {(finals < ACCOUNT / 2).mean() * 100:.1f}%")
print(f"  chance of ~total loss     : {ruined / N * 100:.1f}%")
print(f"  chance of no trades at all: {np.exp(-tpm) * 100:.1f}%")
