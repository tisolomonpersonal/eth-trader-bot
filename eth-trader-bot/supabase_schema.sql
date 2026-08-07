-- Supervisor memory schema.
--
-- Run once against the Supabase project, either in the SQL editor or as a
-- migration:  supabase db push
--
-- RLS note at the bottom — it matters for which key you give the bot.

-- ── Grid snapshots ───────────────────────────────────────────────────────────
create table if not exists grid_observations (
  id              bigserial primary key,
  observed_at     timestamptz not null default now(),
  price           numeric,
  bias            text,                       -- long | short | neutral
  ema_fast        numeric,
  ema_slow        numeric,
  atr             numeric,
  step            numeric,                    -- grid spacing in USD
  levels_per_side int,                        -- grid width at this moment
  long_size       numeric,
  short_size      numeric,
  open_orders     int,
  realised_today  numeric,
  equity          numeric,
  halted          boolean default false
);

create index if not exists grid_observations_observed_at_idx
  on grid_observations (observed_at desc);

-- ── Closed trades, mirrored from Bybit ───────────────────────────────────────
-- order_id is unique so re-polling the same window cannot duplicate rows.
create table if not exists grid_trades (
  id              bigserial primary key,
  order_id        text unique,
  closed_at       timestamptz not null,
  side            text,                       -- Buy | Sell
  qty             numeric,
  entry_price     numeric,
  exit_price      numeric,
  pnl             numeric,
  -- Conditions at the time, so performance can be attributed to them later.
  levels_per_side int,
  bias            text,
  atr             numeric
);

create index if not exists grid_trades_closed_at_idx on grid_trades (closed_at desc);
create index if not exists grid_trades_levels_idx    on grid_trades (levels_per_side);

-- ── Supervisor decisions ─────────────────────────────────────────────────────
-- Every considered decision is recorded, including "leave it alone", so the
-- reasoning can be reviewed rather than inferred from the effects.
create table if not exists supervisor_decisions (
  id              bigserial primary key,
  decided_at      timestamptz not null default now(),
  levels_before   int,
  levels_after    int,
  changed         boolean,
  source          text,                       -- rules | llm | rules_override_llm
  reason          text,
  llm_suggestion  int,
  llm_reason      text,
  metrics         jsonb                       -- the window the call was based on
);

create index if not exists supervisor_decisions_decided_at_idx
  on supervisor_decisions (decided_at desc);

-- ── Row Level Security ───────────────────────────────────────────────────────
-- RLS is on by default for new tables. The bot writes with whatever key is in
-- SUPABASE_KEY, so pick one:
--
--   Service role key  — bypasses RLS entirely. Simplest. Keep it server-side
--                       only; it can read and write everything.
--
--   Publishable/anon  — needs the policies below. Anyone holding the key can
--                       then write these tables, which is acceptable only
--                       because they hold no secrets and no money moves through
--                       them. Never widen this to other tables.
--
-- Uncomment if using the publishable key:
--
-- alter table grid_observations    enable row level security;
-- alter table grid_trades          enable row level security;
-- alter table supervisor_decisions enable row level security;
--
-- create policy "anon read/write observations" on grid_observations
--   for all to anon using (true) with check (true);
-- create policy "anon read/write trades" on grid_trades
--   for all to anon using (true) with check (true);
-- create policy "anon read/write decisions" on supervisor_decisions
--   for all to anon using (true) with check (true);
