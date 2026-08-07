# AI Studio prompt

Paste everything below the line into Google AI Studio (Build / "Create an app").
It is self-contained — it describes the design, the live API, and the exact
response shapes, so the model does not have to guess at any of them.

Backend base URL: `https://tbot.zeabur.app`
Both endpoints allow cross-origin GETs and need no authentication.

---

Build a single-page real-time dashboard for a Bitcoin grid trading bot. It is
read-only: it displays live data and never sends orders. Plain HTML, CSS and
vanilla JavaScript in one file — no build step, no frameworks, no charting
library.

## Data source

Poll these two endpoints every 10 seconds, in parallel, with `fetch`:

- `https://tbot.zeabur.app/grid/status`
- `https://tbot.zeabur.app/supervisor/status`

Both are public GET endpoints returning JSON with CORS enabled. Never send
credentials. If a fetch throws, show a "can't reach the bot" banner and keep
retrying on the next tick — do not stop the interval.

### `/grid/status` response

```json
{
  "status": "ok",
  "paper_mode": false,
  "dry_run": false,
  "symbol": "BTCUSDT",
  "qty": 0.001,
  "leverage": 28,
  "max_per_side": 0.002,
  "atr_mult": 0.5,
  "daily_loss_limit": 3.0,
  "price": 64897.9,
  "balance": { "usdt": 9.79, "equity": 9.85 },
  "positions": {
    "long":  { "size": 0.001, "avg_price": 64842.3, "unrealised_pnl": 0.0556,
               "position_idx": 1, "side": "Buy" },
    "short": null
  },
  "orders": [
    { "order_id": "a1", "link_id": "gr-entry1-x", "side": "Buy",
      "price": 64842.3, "qty": 0.001, "position_idx": 1, "reduce_only": false }
  ],
  "state": {
    "bias": "long",
    "centre": 64897.9,
    "step": 55.6,
    "levels_below": [64842.3, 64786.8],
    "levels_above": [64953.5, 65009.0],
    "realised_today": -0.2231,
    "cycles": 47,
    "halted": false,
    "halt_reason": "",
    "updated_at": "2026-08-07T09:52:00+00:00"
  },
  "error": null
}
```

Notes on this payload:

- `status` is `"ok"`, `"halted"`, or `"disabled"`. When `"disabled"`, the whole
  bot is switched off — show a single explanatory message and nothing else.
- `positions.long` and `positions.short` are each either an object or `null`.
  Both can be non-null at once; this bot runs hedge mode and holds both sides
  simultaneously. Never assume only one exists.
- `positions` may instead be `{ "error": "..." }` if the exchange call failed.
- `error` non-null means some fields are stale; show a warning but still render.
- `price` may be `null` if the exchange read failed — fall back to `state.centre`.

### `/supervisor/status` response

```json
{
  "status": "ok",
  "observe_only": true,
  "levels_now": 2,
  "level_bounds": [1, 2],
  "memory": { "enabled": true, "reachable": true },
  "llm": { "enabled": true, "model": "qwen2.5:3b", "advisory_only": true },
  "performance_by_levels": {
    "1": { "trades": 9,  "pnl": 0.4127, "wins": 6, "win_rate": 0.667 },
    "2": { "trades": 14, "pnl": -0.8842, "wins": 5, "win_rate": 0.357 }
  },
  "recent_decisions": [
    { "decided_at": "2026-08-07T09:45:00+00:00",
      "levels_before": 2, "levels_after": 1, "changed": true,
      "source": "rules_override_llm",
      "reason": "2-level lost -0.88 while 1-level made +0.41",
      "llm_suggestion": 2,
      "llm_reason": "trend is intact, wider grid captures more" }
  ]
}
```

`status` may be `"disabled"`, in which case the supervisor panel shows an
"off" state. `performance_by_levels` and `recent_decisions` are often empty
early on — handle both gracefully with friendly placeholders, not blank space.

## Visual design

Playful and colourful, but every number stays legible at a glance.

**Palette**
- background `#0B0A1F`
- grape `#8B5CF6`, bubblegum `#FF4D9D`, tangerine `#FF8A3D`, lemon `#FFD93D`,
  mint `#2DD4A7`, sky `#38BDF8`, cherry `#FF5470`
- text `#F4F1FF`, muted `#B9AEE8`, dim `#7A6FA8`

**Fonts** (Google Fonts)
- Headings: Fredoka 600/700
- Body: Space Grotesk 500/700
- Every number, price, quantity and timestamp: JetBrains Mono 500/700

Numbers must be monospace everywhere. Digits that change width while updating
make a live dashboard hard to read.

**Colour means something** — do not use it decoratively:
- mint = long, profit, healthy
- cherry = short, loss, halted
- lemon = the live price marker, attention
- grape/bubblegum = supervisor and structural accents

**Style**
- Two large blurred colour blobs fixed behind the content: a grape circle at
  top-left and a bubblegum circle at bottom-right, ~90px blur, ~0.5 opacity.
- Cards are frosted glass: translucent white ~5%, 1px translucent border,
  22px radius, `backdrop-filter: blur(14px)`.
- The page title and the big price use a gradient fill via
  `background-clip: text`.
- Badges are fully rounded pills with gradient backgrounds.
- Gentle motion: the ₿ logo bobs slowly; a heartbeat dot pulses on each refresh;
  cards lift slightly on hover; the HALTED badge wobbles.
- Wrap all animation in `@media (prefers-reduced-motion: reduce)` and disable it
  there.

## Layout, top to bottom

1. **Header** — round gradient ₿ logo, gradient title "Grid Bot Command Deck",
   subtitle `{symbol} · perpetual · hedge mode`. Right side: status pills
   (HALTED and DRY RUN only when active, then LIVE or PAPER) and a pill showing
   a pulsing dot plus the last refresh time.

2. **Alert banner** — one at a time, most urgent wins, in this order:
   halted → partial data (`error`) → dry run. Hidden when all are clear.
   Halted must never be hidden behind a lesser warning.

3. **Price hero** — wide gradient card. Large gradient price on the left with a
   bias pill beside it (📈 LONG mint / 📉 SHORT cherry / 😐 NEUTRAL grey). On the
   right, four labelled figures: Leverage (`28×`), Per level (`0.001 BTC`),
   Spacing (`state.step` as dollars), Today (`state.realised_today`, signed and
   coloured mint/cherry).

4. **Stat strip** — responsive row of small cards: 💰 Equity, 💵 USDT,
   🔒 Margin used, 🛡️ Daily limit, 🔄 Cycles.
   Margin used = `(long.size + short.size) × price ÷ leverage`, shown as `$0.00`
   when flat. Daily limit displays as a negative, e.g. `−$3.00`.

5. **Two columns** (stack to one below ~880px):

   **Left — "🪜 The Ladder".** The centrepiece. Render every order in `orders`
   as a rung, sorted by price descending, and insert a highlighted "👈 you are
   here" row at the correct position — immediately before the first rung whose
   price is below the live price. If every rung is above the price, the marker
   goes last. Each rung shows the price, a label, and the quantity, with a 4px
   left border: mint for Buy, cherry for Sell. Derive the label from the order:
   - `reduce_only: true` → "🎯 take profit"
   - `position_idx: 2` → "🛡️ hedge short"
   - `position_idx: 1` → "🪝 entry long"

   Empty `orders` shows "no resting orders yet", or "🛑 halted — no resting
   orders" when halted.

   **Right, top — "⚖️ Positions".** One row for LONG and one for SHORT, always
   both. Each shows a coloured pill and either `size BTC @ avg_price` with
   signed unrealised PnL, or "flat 😴". Header note: `{used} / {max_per_side}
   per side`, where used is the larger of the two sizes.

   **Right, bottom — "🧠 Supervisor".** A big gradient rounded square showing
   `levels_now`, labelled "levels per side" with "can pick 1–2" beneath. Then
   two rows: Memory (connected ✓ / unreachable ✕ / off) and Brain (the model
   name plus "· advises" or "· decides", or "off"). Then the most recent
   decision: `2 → 1` or `held at 2`, a relative timestamp, the reason text, and
   if `llm_suggestion` is present a sky-coloured line "🤖 model said {n} —
   {llm_reason}". Header note shows "👀 watching only" or "✅ active".

6. **"🏆 Which width is winning?"** — table from `performance_by_levels` with
   columns Width, Trades, Wins, Win rate, Realised. Give the width with the
   highest PnL a 👑 and a mint highlight, but only when its PnL is positive.
   PnL coloured mint/cherry. The table must scroll inside its own card on
   narrow screens rather than widening the page.

7. **Footer** — "refreshing every 10s · last bot cycle {relative time}".

## Behaviour

- Format all money with thousands separators; prices to 1 decimal, PnL to 4.
- Show relative times as "32m ago", "2h ago", "3d ago".
- Render `—` for any missing or null value; never print "undefined" or "NaN".
- Every panel must have a friendly empty state. On first load, before trades
  exist, most panels are legitimately empty — the page should still look
  finished rather than broken.
- Must not scroll horizontally at 375px wide.
