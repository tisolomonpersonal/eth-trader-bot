# Trend-following hedged grid — BTC perpetual

A grid that leans with the trend instead of fading it. An EMA cross sets a
directional bias; the grid accumulates on pullbacks in that direction, takes
profit into strength, and carries an opposite-side hedge when price runs past
the grid rather than reverting.

## Read this before enabling

**It cannot share an API key with the BB-short strategy.** That strategy runs
the account in Bybit **one-way mode** (`positionIdx=0`, hardcoded in
`bybit_client.py`). A hedged grid needs **hedge mode**, which is an
account+symbol level setting. Turning one on breaks the other.

Run the grid on a **separate Bybit sub-account** and give it `GRID_API_KEY` /
`GRID_API_SECRET`. If those are unset it falls back to `BYBIT_API_KEY` /
`BYBIT_API_SECRET`, which is only safe if the BB strategy is not running.

`startup()` will refuse to start if Bybit rejects the hedge-mode switch —
usually because a one-way position is already open on the symbol.

## How a cycle works

Every `GRID_CYCLE_SECONDS` the bot recomputes everything from scratch and
reconciles the order book against what it wants. There is no fill-event
bookkeeping, so a restart, a dropped message, or a missed fill costs nothing —
the next cycle repairs the book.

1. Pull klines, compute EMA bias and ATR.
2. Kill switch: if realised PnL for the UTC day breached the limit, flatten and
   halt until the next day.
3. Backstop stop: if either side is more than `GRID_STOP_ATR_MULT` ATRs
   underwater from its average entry, close it.
4. On a bias flip, optionally market-close the side that is now counter-trend.
5. Recentre the grid if the bias flipped, price drifted more than
   `GRID_RECENTER_ATR` ATRs from centre, or the ATR regime shifted materially.
6. Diff desired orders against resting ones; place what is missing, cancel what
   is stale.

### What the levels mean

With `bias = long` (short bias mirrors everything):

| Level | Order | positionIdx | Meaning |
|---|---|---|---|
| 2 below | Buy limit | 1 | Open/add to the long on a pullback |
| 2 above | Sell limit, reduce-only | 1 | Take profit on the long, nearest level first |
| 2 above, once the long is fully covered | Sell limit | 2 | Open a hedge short |
| nearest below | Buy limit, reduce-only | 2 | Exit for the hedge if price reverts |

Orders are **PostOnly**. A level that would cross the book has already been
overtaken by price, and filling it as taker would enter worse than the grid
assumes — Bybit rejects it instead, and the next cycle re-derives the level.

Every order carries an `orderLinkId` prefixed `gr-`. Reconciliation only ever
cancels orders it can prove are its own, so another strategy on the same symbol
is never disturbed.

## Configuration

All of it lives in `grid_config.py`, all env-driven.

| Variable | Default | Meaning |
|---|---|---|
| `GRID_ENABLED` | `false` | Master switch. Nothing runs without it. |
| `GRID_DRY_RUN` | `false` | Log intended orders, send none. |
| `GRID_API_KEY` / `GRID_API_SECRET` | falls back to `BYBIT_*` | Use a sub-account. |
| `GRID_TESTNET` | inherits `BYBIT_TESTNET` | Endpoint selection. |
| `GRID_SYMBOL` | `BTCUSDT` | |
| `GRID_INTERVAL` | `15` | Minutes, for EMA + ATR. |
| `GRID_QTY` | `0.001` | BTC per level. |
| `GRID_LEVERAGE` | `28` | |
| `GRID_LEVELS_BELOW` / `GRID_LEVELS_ABOVE` | `2` / `2` | |
| `GRID_ATR_PERIOD` | `14` | |
| `GRID_ATR_MULT` | `0.5` | Level spacing = this x ATR. |
| `GRID_RECENTER_ATR` | `1.5` | Drift in ATRs before rebuilding. |
| `GRID_EMA_FAST` / `GRID_EMA_SLOW` | `50` / `200` | |
| `GRID_EMA_MIN_SEP_PCT` | `0.05` | Neutral band; below this the bot goes flat on new orders. |
| `GRID_CLOSE_COUNTER_ON_FLIP` | `true` | Close the now-counter-trend side on a flip. |
| `GRID_MAX_POSITION_BTC` | `0.008` | Hard cap per side. |
| `GRID_MAX_DAILY_LOSS_USDT` | `25.0` | Realised-loss kill switch, UTC day. |
| `GRID_STOP_ATR_MULT` | `4.0` | Backstop stop in ATRs. `0` disables. |
| `GRID_CYCLE_SECONDS` | `30` | |

### Sizing at the defaults

`GRID_QTY=0.001` BTC is Bybit's minimum order size for BTCUSDT, so levels
cannot be made smaller. At a $100k BTC price that is ~$100 notional per level,
~$3.60 of margin at 28x. The `0.008` per-side cap is 8 levels' worth — roughly
$800 notional and ~$29 margin per side, both sides at once in the worst case.

At 28x, a **~3.6% adverse move against a fully-loaded side is a liquidation**.
`GRID_STOP_ATR_MULT` exists to close well before that, but it is a market
order on a polling loop, not a guarantee — a gap can jump straight through it.
Size the account so a full loss of both sides is survivable.

## Running

```bash
GRID_ENABLED=true GRID_DRY_RUN=true python app.py
```

Offline checks, no network or keys needed:

```bash
python test_grid.py
```

Status endpoint: `GET /grid/status`.

## State

- `$DATA_DIR/grid_state.json` — bias, centre, levels, halt status, daily PnL
- `$DATA_DIR/grid_history.json` — rebuilds, flips, stops, flattens (last 500)

On Zeabur `DATA_DIR` is `/data`, backed by the `bot-data` volume, so state and
the daily kill switch survive redeploys.

## Shutdown

`scheduler.stop()` runs from gunicorn's `worker_exit` hook and cancels resting
grid orders synchronously. The loop thread is a daemon and may be killed before
it can clean up after itself; without this a redeploy would leave leveraged
limit orders on the book with nothing supervising them.

**Positions are not closed on shutdown** — only orders are cancelled. A
redeploy that takes minutes leaves open positions unmanaged. If that matters
for your deployment, flatten manually before redeploying.
