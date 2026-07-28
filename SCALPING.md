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

## Measured results — read before deploying anything

Backtested on 30 days of real BTCUSDT perp data, $2,000 pot (sizing constraint
removed so the strategy could be judged on its own), perp taker fees.

| Base TF | trades | win rate | return | profit factor |
|---|---|---|---|---|
| **1m** | 68 | 36.8% | **−7.35%** | 0.31 |
| **5m** | 91 | 37.4% | **−7.90%** | 0.43 |
| **15m** | 31 | 41.9% | −3.57% | 0.45 |
| **30m** | 15 | 60.0% | +0.40% | 1.13 |
| 30m + 2bps slippage | 11 | 63.6% | +0.49% | 1.25 |
| 60m | 6 | 50.0% | −1.09% | 0.47 |

**As a 1-minute scalper this strategy loses money. That result is solid** — 68
and 91 trades are meaningful samples, and both are decisively negative.

The 30m rows are NOT evidence of an edge. Eleven to fifteen trades is noise
around zero, and 60m turning negative again shows there's no stable trend past
30m. Do not read "+0.49% with slippage" as a working strategy.

### Why 1m fails — the mechanism

```
BTC 1-minute ATR (30d):  median 0.0483%,  p90 0.0976%
Fee floor (0.11% × 2.5): 0.275%  =  5.70 ATR
```

BTC's 1-minute bars are far too small relative to fixed costs. The consequence
is that the ATR-scaled brackets **never actually engage** — `MIN_SL_PCT` and the
fee floor clamp every trade to the same fixed 0.25% stop / 0.275% target:

```
effective R:R          1.1   (config intends 1.50)
break-even win rate    47.6%
achieved win rate      36.8%
```

So the bot was structurally losing before any setup logic ran. The fix is not
parameter tuning — it is making the fee floor small relative to ATR, which
means either a coarser timeframe (ATR scales with √time) or a lower fee tier.

| Base TF | est. ATR | fee floor in ATR |
|---|---|---|
| 1m | 0.048% | 5.7 |
| 5m | 0.108% | 2.5 |
| 15m | 0.187% | 1.5 |
| 30m | 0.265% | 1.04 |

You want that right column at ~1 or below.

### The lever that actually matters

Maker-only execution (post-only limit entry and exit) costs **0.04% round trip**
instead of 0.11%, dropping the fee floor from 0.275% to **0.10%** — which makes
**5m** viable (0.93 ATR) while preserving something recognisable as scalping.

That is why the order-flow work described at the bottom of this document is not
an optional enhancement. It is the thing that makes maker fills achievable
without adverse selection, and therefore the only route to a 1-5m strategy that
clears costs.

### Other findings

- `MEAN_REVERSION` fired **zero times in 30 days** at 1m. The setup billed as
  the highest-frequency, highest-win-rate edge is gated too tightly
  (`%B≤0.05` AND `RSI≤30` AND below VWAP AND RANGE regime AND HTF not bearish).
  The whole book ran on the two low-win-rate setups.
- Shorts underperformed badly at 1m (18.8% win rate vs 52.8% for longs) over a
  window where BTC rose. Expected, but it means the long/short split needs
  testing across a down month before drawing conclusions.
- Trailing-stop geometry was wrong: arming at +1.0 ATR and trailing 0.7 ATR
  behind, against a 1.2-ATR stop, caps winners below losses by construction.
  Only 3 of 68 trades reached take-profit.

**No parameter in this repo has been tuned against these results.** Doing so
would curve-fit to a single 30-day window of a single regime. Any change to the
trail or the thresholds needs validating on windows other than the one that
motivated it.

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
