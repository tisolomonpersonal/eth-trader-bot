# BTC Scalping Mode

Converts the bot from AI-led BNB swing trading to a deterministic, rule-based
BTC/USDT scalper. `SCALP_MODE=false` restores the original behaviour unchanged.

## Why the fee math dictates the design

Bybit spot charges **0.1% maker / 0.1% taker**. A market entry plus a market
exit costs **0.20% round trip**.

That single number invalidates most of what "scalping" is usually taken to
mean. A 0.25% scalp nets 0.05%. A 0.15% scalp is a guaranteed loss no matter
how good the entry is. So the system enforces a hard floor:

```
minimum take-profit = ROUND_TRIP_FEE_PCT × MIN_EDGE_FEE_MULT
                    = 0.20% × 2.5
                    = 0.50%
```

Any setup whose objective is nearer than that is refused by `scalp_risk.validate()`
before it can reach the exchange. Fewer trades, but each one can actually pay
for itself.

**The biggest available improvement is not in the entry rules — it's the fee
line.** Bybit linear perps run 0.02% maker / 0.055% taker, a ~5× reduction, and
allow shorts, which roughly doubles the number of valid setups. See
"Perp migration" below.

## Strategy: regime-switched

Rather than one strategy applied everywhere, ADX picks which engine is live —
these two fail in opposite conditions, so each covers the other's weakness.

| Regime | Condition | Setup | Profile |
|---|---|---|---|
| **RANGE** | ADX ≤ 20 | Mean-reversion fade to VWAP/band-mid | ~60-65% win, R:R ~0.8 |
| **TREND** | ADX ≥ 25 | Squeeze breakout (measured move) | ~40% win, R:R 2:1+ |
| **TREND** | ADX ≥ 25, 5m bullish | Pullback to EMA21, reclaim EMA9 | fallback, thinner edge |

All three are filtered by a 5-minute EMA50 bias — the bot never fades into a
higher-timeframe downtrend, which is the main way mean-reversion scalpers get
run over.

Spot is long-only. Every setup has an exact short mirror that becomes available
on perps (documented in `scalp_signal._MIRROR_NOTE`).

## Risk envelope

The entry rules are the smaller half of the system. What keeps a scalper solvent:

- **ATR-scaled brackets** — SL at 1.2×ATR, TP at 1.8×ATR, clamped to 0.25–1.20%.
  A fixed 2%/4% bracket is unrelated to what the market is doing right now.
- **Trailing stop** — ratchets once price runs 1×ATR in profit. Stops winners
  round-tripping back to break-even, which is how otherwise-decent scalp systems
  bleed out.
- **Time stop** — 45 min. A scalp that hasn't resolved has stopped being a scalp.
- **Risk-based sizing** — 1% of the pot per trade, sized off stop distance, so
  every trade risks the same amount regardless of volatility. The swing bot's
  `RISK_PER_TRADE_PCT=100` (whole pot per signal) is ruinous at scalp frequency.
- **Overtrading guards** — 30 trades/day cap, halt after 4 consecutive losses,
  10 min cooldown after any loss. These bind far sooner than on a swing bot,
  which is the point: overtrading and revenge-trading kill more scalpers than
  bad entries do.
- **Daily loss circuit breaker** — 5% of pot, then flat until UTC midnight.

## Backtest before funding anything

```bash
python backtest.py --days 30
```

Public endpoint, no API key needed. Runs the identical `scalp_signal` /
`scalp_risk` code the live bot uses — no reimplementation, so what you measure
is what you deploy.

Test across several disjoint windows — a trending month, a chopping month, a
crash. A strategy that only works in one window is curve-fit. Read the caveats
the backtester prints; the no-slippage assumption in particular flatters
scalping strategies more than any other kind.

Rough bar: **profit factor below ~1.3, or an edge that dies when you add 2 bps
of slippage, is not live-tradeable.**

Then compare against paying zero fees, to see how much of the edge is real:

```bash
python backtest.py --days 30 --fees 0.0
```

## Running

Paper mode is automatic when no API keys are set — `PAPER_MODE` is derived from
key presence in `config.py`. Run it that way for a few days first.

```bash
SCALP_MODE=true python app.py
```

Endpoints:

- `/scalp/signal` — live indicators, current regime, the decision and its
  reason, and the brackets it would use. First place to look when it isn't trading.
- `/scalp/stats` — win rate, profit factor, expectancy, fees paid, per-setup breakdown.
- `/status`, `/history` — as before, now backed by the scalp state files.

State lives in `scalp_state.json` / `scalp_trade_history.json`, separate from
the swing bot's files so the two strategies never mix P&L.

## Key settings

| Var | Default | Notes |
|---|---|---|
| `SCALP_MODE` | `true` | `false` restores AI swing mode |
| `SCALP_CYCLE_SECONDS` | `15` | Was 60 |
| `MAKER_FEE_PCT` / `TAKER_FEE_PCT` | `0.10` | Set to your actual VIP tier |
| `MIN_EDGE_FEE_MULT` | `2.5` | Gross edge must beat fees by this multiple |
| `SL_ATR_MULT` / `TP_ATR_MULT` | `1.2` / `1.8` | Bracket width |
| `SCALP_RISK_PCT` | `1.0` | % of pot risked per trade |
| `MAX_TRADES_PER_DAY` | `30` | Overtrading guard |
| `MAX_CONSECUTIVE_LOSSES` | `4` | Halt threshold |
| `AI_VETO_ENABLED` | `false` | LLM may only block entries, never create them |

The LLM is off by default. A model round-trip per 15s cycle adds latency and
non-reproducible noise to a timeframe where decisions must be deterministic and
backtestable. Enabled, it can only veto.

## Perp migration (the real upgrade)

```
MAKER_FEE_PCT=0.02
TAKER_FEE_PCT=0.055
```

Round trip drops 0.20% → 0.11% taker (0.04% maker-only), so the minimum viable
target falls from 0.50% to ~0.28%. Combined with short setups becoming
available, this is a far larger expectancy improvement than any tuning of the
entry rules.

Requires: `CATEGORY="linear"`, leverage config, and shorts plumbed through
`bybit_client` and `scalp_strategy` (currently long-only). It also introduces
liquidation risk, which spot does not have.

## Order-flow upgrade (not built)

The genuine professional edge is order flow — book imbalance, cumulative volume
delta divergence, absorption, microprice — which reads intent before it reaches
price. It needs a WebSocket rewrite (`orderbook.50.BTCUSDT`, `publicTrade.BTCUSDT`)
and an event-driven loop, not REST polling.

Do not trade it standalone at a 1-10s horizon; that is a latency race against
colocated market makers that a cloud VM loses. Use it instead as an **entry
timing filter** on the setups above — wait for CVD to stop making new lows
before taking a fade — at a 30s-2min horizon where 200ms of latency is noise.
That also allows post-only limit entries, cutting fees again.

Sequence it after the backtest baseline exists, so you can measure whether it
actually helped.
