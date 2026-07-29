"""
$10 account, 25x leverage, 0.004 BTC per trade.

Same out-of-sample trade distribution as ten_dollars.py (profit factor 1.06).
Only the sizing changes. Liquidation is modelled explicitly, because at this
size it is reachable inside a single trade.
"""
import numpy as np

import bb_ma28_backtest as bt

import sys

ACCOUNT = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
QTY = 0.004
LEVERAGE = 25.0
MAINT_MARGIN = 0.005          # Bybit BTCUSDT maintenance margin, ~0.5%

HELD_OUT = [("BNBUSDT", 1600), ("XRPUSDT", 1600), ("ADAUSDT", 1600),
            ("DOGEUSDT", 1600), ("LINKUSDT", 1600), ("LTCUSDT", 1600),
            ("AVAXUSDT", 1400), ("DOTUSDT", 1600)]

pool_ret, pool_risk, per_year = [], [], []
for sym, days in HELD_OUT:
    cap = {}
    orig = bt.summarise
    bt.summarise = lambda t, x, f, s: (cap.__setitem__("t", t), orig(t, x, f, s))[1]
    d = bt.add_indicators(bt.fetch(sym, "240", days), 20, 2.0, 28, "sma", 200)
    bt.run(d, 0.055, 0.0, False, True, 0, confirm_bars=1,
           use_trend_filter=True, stop_cap_atr=1.5)
    bt.summarise = orig
    tr = cap.get("t", [])
    pool_ret += [t["net_pct"] for t in tr]
    pool_risk += [t["risk_pct"] for t in tr]
    per_year.append(len(tr) / (days / 365))

pool_ret = np.array(pool_ret)
pool_risk = np.array(pool_risk)
tpm = np.mean(per_year) / 12

price = float(bt.fetch("BTCUSDT", "D", 30)["close"].iloc[-1])
notional = QTY * price
margin = notional / LEVERAGE
eff_lev = notional / ACCOUNT
liq_move = (1 / LEVERAGE - MAINT_MARGIN) * 100     # % adverse move to liquidation
acct_liq_move = ACCOUNT / notional * 100           # % move that equals the whole account

print("=" * 70)
print("POSITION MATHS")
print("=" * 70)
print(f"BTC price            : ${price:,.0f}")
print(f"Position             : {QTY} BTC = ${notional:,.2f} notional")
print(f"Margin at {LEVERAGE:g}x       : ${margin:,.2f}")
print(f"Your account         : ${ACCOUNT:,.2f}"
      f"{'   <-- NOT ENOUGH, order would be rejected' if margin > ACCOUNT else ''}")
print()
print(f"Effective leverage on your account: {eff_lev:.1f}x")
print(f"  => every 1% BTC move = {eff_lev:.1f}% of your account")
print(f"Liquidation at ~{liq_move:.2f}% adverse move "
      f"(exchange), or {acct_liq_move:.2f}% = your whole account")
print()
print(f"Strategy's average stop distance: {pool_risk.mean():.3f}%")
print(f"  => one normal stop-out loses {pool_risk.mean() * eff_lev:.1f}% of the account")
print(f"Trades whose stop sits BEYOND liquidation: "
      f"{(pool_risk > min(liq_move, acct_liq_move)).mean() * 100:.1f}% "
      f"(these liquidate before the stop can fire)")
print()

# --- Monte Carlo -----------------------------------------------------------
rng = np.random.default_rng(7)
N = 100_000
finals, ruined, months_survived = [], 0, []
idx = np.arange(len(pool_ret))

for _ in range(N):
    eq = ACCOUNT
    n = rng.poisson(tpm)
    for k in range(n):
        # Can we still meet margin for the next trade?
        if eq < margin:
            break
        j = rng.choice(idx)
        r = pool_ret[j] / 100
        # Loss capped at liquidation: you cannot lose more than the account.
        pnl = notional * r
        if -pnl >= eq:
            eq = 0.0
            break
        eq += pnl
    finals.append(eq)
    if eq <= 0.5:
        ruined += 1

finals = np.array(finals)
print("=" * 70)
print(f"MONTE CARLO - {N:,} simulated months, {tpm:.1f} trades/month")
print("=" * 70)
print(f"  median outcome : ${np.median(finals):.2f}")
print(f"  mean outcome   : ${finals.mean():.2f}")
print()
for p in (1, 5, 25, 50, 75, 95, 99):
    print(f"  {p:>2}th percentile: ${np.percentile(finals, p):>7.2f}")
print()
print(f"  chance you END UP AHEAD  : {(finals > ACCOUNT).mean() * 100:.1f}%")
print(f"  chance you lose > half    : {(finals < ACCOUNT / 2).mean() * 100:.1f}%")
print(f"  chance of near-total loss : {ruined / N * 100:.1f}%")
