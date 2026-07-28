# Prompt: BTC Trading Bot Android Monitor App

Build a native Android application in **Jetpack Compose** that serves as a real-time monitoring dashboard for a live BTC/USDT perpetual futures trading bot. The app connects to a REST API at `https://tbot.zeabur.app/` and displays live trading data with a dark, professional trading-terminal aesthetic.

---

## API

**Base URL:** `https://tbot.zeabur.app/`

### GET /status  — primary endpoint, poll every 10 seconds
Returns JSON:
```json
{
  "status": "ok",
  "paper_mode": false,
  "symbol": "BTCUSDT",
  "leverage": 25,
  "btc_qty": "0.004",
  "balance": {
    "usdt": 1234.56,
    "btc": 0.001,
    "equity": 1280.00
  },
  "live_position": {
    "in_position": true,
    "side": "LONG",
    "entry_price": 65000.00,
    "qty": 0.004,
    "sl_price": 64500.00,
    "tp_price": 66500.00,
    "liq_price": 62000.00,
    "unrealised_pnl": 12.50,
    "entry_time": "2026-07-28T10:00:00Z"
  },
  "state": {
    "in_position": true,
    "side": "LONG",
    "entry_price": 65000.00,
    "qty": 0.004,
    "sl_price": 64500.00,
    "tp_price": 66500.00,
    "entry_time": "2026-07-28T10:00:00Z",
    "pending_signal": null,
    "daily_pnl_usdt": 45.20,
    "total_pnl_usdt": 312.80,
    "trade_count_today": 2,
    "total_trades": 18,
    "last_action": "LONG",
    "last_reason": "H1 directional candle → M5 fib retracement entry.",
    "_last_price": 65180.00
  },
  "recent_trades": [
    {
      "time": "2026-07-28T08:30:00Z",
      "side": "LONG",
      "entry": 64200.00,
      "exit": 65100.00,
      "qty": 0.004,
      "leverage": 25,
      "pnl": 36.00
    }
  ]
}
```

### GET /healthz — health check
### GET /history — full trade history array

---

## Design System

Match this exact colour palette and typography:

| Token | Value |
|-------|-------|
| Background | `#080A0F` |
| Surface | `#0F1219` |
| Surface2 | `#161B25` |
| Border | `#1E2533` |
| Text primary | `#E2E8F0` |
| Text muted | `#8892A4` |
| Text dim | `#4A5568` |
| BTC Orange | `#F7931A` |
| Green (profit/long) | `#16C784` |
| Red (loss/short) | `#EA3943` |
| Yellow (warning/paper) | `#F0B90B` |
| Purple (signal) | `#7B61FF` |

**Fonts:** Use `Roboto Mono` for all numbers, prices, and monospaced values. Use `Inter` (or system default) for labels and body text.

---

## Screens & Layout

### 1. Main Dashboard Screen (single scrollable screen)

#### A. Top App Bar
- Title: **"BTC BOT"** in BTC orange, bold, monospace font
- Subtitle: `BTCUSDT · 25× · H1→M5` in dim text, smaller
- Right side: animated status dot (green pulse when connected, red when error) + last-updated timestamp
- Mode badge: `PAPER MODE` (yellow outlined pill) or `● LIVE` (green outlined pill)

#### B. Price Hero Card
Full-width card with dark surface background and orange accent border-left (4dp).
- **BTC price** — huge monospace number (34sp), BTC orange: `$65,180.00`
- Below in a 2-column row:
  - Left column: `Leverage` label → `25×` value in orange badge style
  - Right: `Qty` label → `0.004 BTC`
- Second row:
  - `Daily P&L` → coloured value (green if positive, red if negative): `+$45.20`
  - `Total P&L` → coloured value: `+$312.80`

#### C. Account Balance Card
Full-width card with 4 equal columns in a single row (wrap to 2×2 on small screens):
- **Wallet Balance** — `$1,234.56` (white, 16sp mono bold)
- **Equity** — `$1,280.00`
- **BTC Holdings** — `0.0010 BTC`
- **Mode** — `LIVE` (green) or `PAPER` (yellow)

#### D. Strategy Phase Indicator
Horizontal stepper with 4 phases. Active phase has orange circle + orange label. Done phases have green check circle. Inactive are grey.
```
① Watching H1  ──  ② Signal Armed  ──  ③ M5 Waiting  ──  ④ In Position
```
Determine active phase from state:
- `in_position=true` → phase 4
- `pending_signal != null` → phase 3
- `last_action == "SIGNAL"` → phase 2
- else → phase 1

#### E. Position Card
Card titled **"Position"** with age in header right (e.g. `2h ago`).

**When FLAT (no position):**
- Grey pill: `● FLAT — NO POSITION`
- 2-column metrics: `Trades Today` / `Total Trades`

**When in a position (LONG or SHORT):**
- Animated pill: pulsing green dot + `LONG` (green) OR pulsing red dot + `SHORT` (red)
- 2×2 metrics grid:
  - Entry price (orange)
  - Qty
  - Stop Loss (red)
  - Take Profit (green)
- **SL → TP progress bar** — full width, colour-gradient bar (red→green for LONG, green→red for SHORT). Show current price as a circle marker at the correct position along the bar. Labels: `SL $64,500` left, `RR 2.00:1` centre, `TP $66,500` right.
- Bottom 2-col row: `Unrealised P&L` (use Bybit's value from `live_position.unrealised_pnl`) coloured green/red, and `Current Price`
- If `liq_price` is set: show `Liquidation $62,000` in red below

#### F. Signal Armed Card (only visible when `pending_signal != null`)
Purple-tinted card with ⚡ icon and `LONG SIGNAL ARMED` or `SHORT SIGNAL ARMED` header.
Rows:
- H1 Candle Range: `$64,100 → $65,300`
- Fib Entry Zone (61.8–70.5%): show in purple `$64,700 – $64,900`
- Direction: `LONG` (green) or `SHORT` (red)
- Expires in: countdown `3h 42m`

#### G. Last Action Card
Dark surface card:
- Top row: action badge (e.g. `LONG` green, `SHORT` red, `HOLD` grey, `SL` red, `TP` green) + timestamp
- Below: reason text in muted colour, wrapping

#### H. Trade History
Section title **"Trade History"** with trade count badge.
List of rows, newest first. Each row:
- Side badge: `LONG` (green) or `SHORT` (red) — small rounded pill
- Entry price
- Exit price (or `open` if no exit)
- Qty
- Leverage (`25×`)
- P&L coloured green/red

Show `No trades yet` empty state with a simple icon.

---

## Behaviour & Polish

### Auto-refresh
Poll `/status` every **10 seconds**. Show a subtle progress indicator in the top bar. On each successful response, animate the heartbeat dot. On error, show `connection error` in red and keep retrying.

### Number Formatting
- Prices: always 2 decimal places with thousand separators (`$65,180.00`)
- BTC quantities: 4 decimal places (`0.0040`)
- P&L: always show sign (`+$45.20` or `-$12.00`)
- Format helper: if value is null/missing show `—`

### Animations
- Price updates: brief scale pulse on the price value when it changes
- Heartbeat dot: 1s pulse animation on each successful fetch
- Position pill: continuous 2s pulse on the coloured dot (CSS-equivalent using Compose `infiniteTransition`)
- Phase stepper: smooth colour transitions

### Pull to Refresh
Support manual pull-to-refresh on the main scroll view.

### Error State
Full-screen error card if the first load fails. Show retry button. Keep showing stale data with an error banner for subsequent failures.

### Connectivity indicator
Small snackbar/banner: `● Connected` (green) or `⚠ Reconnecting…` (yellow) shown at bottom.

---

## Architecture

Use **MVVM** pattern:
- `MainViewModel` — holds `StateFlow<DashboardState>` where `DashboardState` wraps all the API data
- `BotRepository` — handles HTTP with **Retrofit** + **OkHttp** (add `OkHttpClient` with 10s timeout)
- `DashboardUiState` sealed class: `Loading`, `Success(data)`, `Error(message)`
- Use `kotlinx.coroutines` with `viewModelScope` for polling (`repeatOnLifecycle`)
- Use **Gson** or **Moshi** for JSON parsing

**Dependencies to add to build.gradle:**
```groovy
implementation 'com.squareup.retrofit2:retrofit:2.9.0'
implementation 'com.squareup.retrofit2:converter-gson:2.9.0'
implementation 'com.squareup.okhttp3:logging-interceptor:4.12.0'
implementation 'androidx.lifecycle:lifecycle-viewmodel-compose:2.7.0'
implementation 'androidx.lifecycle:lifecycle-runtime-compose:2.7.0'
```

---

## File Structure
```
app/
  src/main/java/com/btcbot/monitor/
    data/
      api/BotApiService.kt       — Retrofit interface
      api/RetrofitClient.kt      — singleton setup
      model/StatusResponse.kt    — data classes
      repository/BotRepository.kt
    ui/
      theme/Theme.kt             — dark Material3 theme with the colour tokens above
      theme/Type.kt              — Roboto Mono for numbers
      components/
        PriceHeroCard.kt
        AccountBalanceCard.kt
        PhaseStepperRow.kt
        PositionCard.kt
        SlTpProgressBar.kt
        SignalArmedCard.kt
        LastActionCard.kt
        TradeHistorySection.kt
      screen/DashboardScreen.kt  — assembles all components in LazyColumn
    viewmodel/MainViewModel.kt
    MainActivity.kt
```

---

## Extra Notes

- `live_position` takes priority over `state` for position display. If `live_position` is not null, use its values; otherwise fall back to the same fields in `state`.
- `state._last_price` is the current BTC price — use it for the price hero and unrealised P&L calculation fallback.
- The SL/TP progress bar position: for LONG, `pct = (price - sl) / (tp - sl)`. For SHORT, `pct = (sl - price) / (sl - tp)`. Clamp to `[0, 1]`.
- Unrealised P&L: prefer `live_position.unrealised_pnl` (server-calculated, includes fees). Fallback: `(price - entry) * qty` for LONG, `(entry - price) * qty` for SHORT.
- The app is **read-only** — no trading controls, just monitoring.
- Target **API 26+**, use **Material3** components throughout.
- The app name is **"BTC Bot Monitor"**, package `com.btcbot.monitor`.
