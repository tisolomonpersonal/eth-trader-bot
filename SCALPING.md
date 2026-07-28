# BTC Perpetual Scalping Mode

BTC only, derivatives only. `SCALP_MODE=true` runs a deterministic, rule-based
scalper on the **BTCUSDT linear perpetual**. `SCALP_MODE=false` leaves the
original AI-led spot swing bot untouched.

> **Nothing here has been executed.** There was no Python available on the
> machine this was written on. Run `backtest.py` first — it will surface any
> import or logic errors before money is involved.

## Read this first: minimum position size

BTCUSDT perp has a **0.001 BTC minimum order**. At a six-figure BTC price that
is **~$100+ of notional**, which at 1x leverage needs ~$100 of margin.

**A $10 account cannot open the minimum position at 1x.** It would need roughly
10x leverage just to place one trade — and at 10x, a ~10% adverse move
liquidates you. BTC moves 10% intraday several times a year.

That is a genuine constraint, not a tuning problem. `scheduler._check_account_viable()`
checks this at startup and logs + Telegrams a clear message rather than letting
you discover it through rejected orders. Your options:

- Fund to roughly the price of 0.001 BTC to trade at **1x**, or
- Paper-trade until then (no keys set = paper mode, costs nothing), or
- Accept leverage, understanding it converts a sizing problem into a
  liquidation problem.

Raising `LEVERAGE` to force a position through is not a fix.

## Why perps rather than spot

Fees, and the ability to short.

| | Spot | Perp |
|---|---|---|
| Taker | 0.10% | **0.055%** |
| Round trip (2 market orders) | 0.20% | **0.11%** |
| Shorts | ✗ | ✓ |
| Liquidation risk | none | **yes** |

The round-trip cost sets a hard floor on any target, because a scalp that
doesn't clear fees is a loss no matter how good the entry was:

```
minimum take-profit = ROUND_TRIP_FEE_PCT × MIN_EDGE_FEE_MULT
                    = 0.11% × 2.5  =  0.275%      (perp)
                      0.20% × 2.5  =  0.500%      (spot)
```

What that difference does to the same strategy, with a 0.35% stop:

| Win rate | Target | Spot (0.20%) | Perp (0.11%) |
|---|---|---|---|
| 55% | 0.5% | −0.08% ❌ | +0.01% |
| 60% | 0.5% | −0.04% ❌ | **+0.05%** ✅ |
| 65% | 0.5% | ±0.00% | **+0.09%** ✅ |

A 60%-win-rate scalper is **net negative on spot and positive on perps**. Same
rules, opposite sign. That is the whole argument.

## Strategy: regime-switched, both directions

ADX picks which engine is live — mean reversion and breakout fail in opposite
conditions, so each covers the other's weakness.

| Regime | Condition | Setup | Long | Short |
|---|---|---|---|---|
| **RANGE** | ADX ≤ 20 | Fade to VWAP/band-mid | %B ≤ 0.05, RSI ≤ 30 | %B ≥ 0.95, RSI ≥ 70 |
| **TREND** | ADX ≥ 25 | Squeeze breakout | break of recent high | break of recent low |
| **TREND** | ADX ≥ 25 | Pullback continuation | hold EMA21, reclaim EMA9 | reject EMA21 |

All filtered by a 5-minute EMA50 bias: never fade into a higher-timeframe
trend, never short into a rally. Volume confirmation is required on breakouts —
an unconfirmed break is usually a fakeout.

Shorts matter more than they sound. BTC downtrends are faster and more violent
than uptrends, because leveraged longs dominate open interest and liquidation
cascades are one-directional. A long-only scalper sits out half the market.

## Risk envelope

The entries are the smaller half of the system. What keeps a scalper solvent:

- **Exchange-held stops.** SL/TP attach to the entry order and live on Bybit,
  so the position stays protected if the bot crashes or Zeabur restarts. The
  old spot path checked stops in Python once a cycle — protection that lasted
  only as long as the process did. On a leveraged account that difference is
  how a small loss becomes the whole account.
- **Reconciliation.** The exchange is the source of truth for whether a
  position is open. Local JSON drifts after a crash, a manual trade, or a
  liquidation. If Bybit shows a position the bot didn't open, it refuses to
  trade rather than stacking a second one on top.
- **ATR-scaled brackets**, direction-aware — a short's stop sits *above* entry.
- **Trailing stop**, pushed to the exchange when it moves.
- **Time stop** (45 min). A scalp that hasn't resolved has stopped being a scalp.
- **Risk-based sizing** off stop distance, not off available margin. Leverage
  makes it trivial to open a position far larger than you can afford to be
  wrong about; margin answers the wrong question.
- **Overtrading guards** — trades/day cap, halt after N consecutive losses,
  cooldown after any loss.
- **Daily loss circuit breaker** — flat until UTC midnight.

## Backtest before funding anything

```bash
python backtest.py --days 30
```

Public endpoint, no API key. Runs the identical `scalp_signal` / `scalp_risk`
code the live bot uses.

Then measure how much the perp fee advantage is actually worth to you:

```bash
python backtest.py --days 30 --fees 0.10
```

That reruns the same strategy at spot cost. The gap between the two is the
clearest number you'll get about whether this design decision was right.

Run several **disjoint** windows — a trending month, a chopping month, a crash.
A strategy that only works in one window is curve-fit. Read the printed caveats;
the no-slippage assumption flatters scalping more than any other kind.

Bar: **profit factor below ~1.3, or an edge that dies when you add 2 bps of
slippage, is not live-tradeable.**

## Running

Paper mode is automatic when no API keys are set. Run it that way first.

Endpoints:

- `/scalp/signal` — live indicators, regime, the decision and its reason,
  brackets for both directions, exchange position, account viability. First
  place to look when it isn't trading.
- `/scalp/stats` — win rate, profit factor, expectancy, fees, long/short split.

State is in `scalp_state.json` / `scalp_trade_history.json`, separate from the
swing bot's files.

## Key settings

| Var | Default | Notes |
|---|---|---|
| `SCALP_MODE` | `false` | Opt-in. This repo auto-deploys on push. |
| `LEVERAGE` | `1` | See the warning at the top before changing |
| `MAX_INVESTMENT_USDT` | from GHC | Set directly for a USDT test float |
| `TAKER_FEE_PCT` | `0.055` | Set to your real VIP tier |
| `MIN_EDGE_FEE_MULT` | `2.5` | Gross edge must beat fees by this multiple |
| `SL_ATR_MULT` / `TP_ATR_MULT` | `1.2` / `1.8` | Bracket width |
| `SCALP_RISK_PCT` | `1.0` | % of pot risked per trade |
| `MAX_TRADES_PER_DAY` | `30` | **Lower this on a small account** — see below |
| `AI_VETO_ENABLED` | `false` | LLM may only block entries, never create them |

### Trade frequency vs. account size

If position notional is a large fraction of the pot, fee drag per day is
roughly `trades × round-trip %`. At 30 trades/day and 0.11%, that's **3.3% of
the pot per day** in fees before the strategy does anything. Set
`MAX_TRADES_PER_DAY` so that number stays small relative to your expected
daily edge — 6-10 is a more defensible starting point than 30.

The LLM is off by default. A model round-trip per 15s cycle adds latency and
non-reproducible noise to a timeframe where decisions must be deterministic and
backtestable. Enabled, it can only veto.

## Not built: order flow

The genuine professional edge is order flow — book imbalance, cumulative volume
delta divergence, absorption, microprice — which reads intent before it reaches
price. It needs a WebSocket rewrite (`orderbook.50.BTCUSDT`, `publicTrade.BTCUSDT`)
and an event-driven loop, not REST polling.

Don't trade it standalone at a 1-10s horizon; that's a latency race against
colocated market makers that a cloud VM loses. Use it as an **entry timing
filter** on the setups above — wait for CVD to stop making new lows before
taking a fade — at a 30s-2min horizon where 200ms of latency is noise. That
also enables post-only limit entries, cutting the round trip from 0.11% to
~0.04%.

Sequence it after a backtest baseline exists, so you can measure whether it
actually helped.
