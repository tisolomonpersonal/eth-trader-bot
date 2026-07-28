"""
Walk-forward backtester for the BTC perpetual scalping engine.

Runs the EXACT same scalp_signal / scalp_risk code paths the live bot uses, so
what you measure here is what you deploy. Nothing is reimplemented.

    python backtest.py --days 30
    python backtest.py --days 30 --fees 0.10     # what spot would have cost
    python backtest.py --days 30 --json

Read the caveats printed at the end before trusting any number.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
import indicators as ind_calc
import scalp_signal
import scalp_risk


# ── Historical data ───────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent / ".backtest_cache"


def fetch_history(symbol: str, interval: str, days: int,
                  use_cache: bool = True) -> pd.DataFrame:
    """
    Page backwards through Bybit's linear kline endpoint (1000 bars per call)
    until `days` of history is collected. Public endpoint — no API key, no
    account, nothing at risk. Validate before funding.

    Results are cached to disk per (symbol, interval, days, UTC date). Comparing
    fee scenarios means running this repeatedly over identical data, and
    refetching 43k bars each time is both slow and rude to the rate limiter.
    """
    cache_file = CACHE_DIR / (f"{symbol}_{interval}m_{days}d_"
                              f"{datetime.now(timezone.utc):%Y%m%d}.pkl")
    if use_cache and cache_file.exists():
        print(f"  using cached data: {cache_file.name}", file=sys.stderr)
        return pd.read_pickle(cache_file)

    from pybit.unified_trading import HTTP
    client = HTTP(testnet=False)

    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    bars_needed = int(days * 24 * 60 / int(interval))
    frames = []
    fetched = 0

    while fetched < bars_needed:
        resp = client.get_kline(category="linear", symbol=symbol,
                                interval=interval, limit=1000, end=end)
        rows = resp["result"]["list"]
        if not rows:
            break

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
        frames.append(df)
        fetched += len(rows)
        end = int(df["ts"].astype("int64").min()) - 1
        print(f"  fetched {fetched}/{bars_needed} bars…", end="\r", file=sys.stderr)
        time.sleep(0.1)

    if not frames:
        raise RuntimeError("No historical data returned")

    out = pd.concat(frames, ignore_index=True)
    out = out.astype({"ts": "int64", "open": "float64", "high": "float64",
                      "low": "float64", "close": "float64", "vol": "float64"})
    out["ts"] = pd.to_datetime(out["ts"], unit="ms", utc=True)
    out = out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    print(f"  fetched {len(out)} bars total          ", file=sys.stderr)

    if use_cache:
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            out.to_pickle(cache_file)
        except Exception as e:
            print(f"  (could not cache: {e})", file=sys.stderr)

    return out


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate to a coarser timeframe — used to build the higher-TF bias series."""
    d = df.set_index("ts")
    out = d.resample(f"{minutes}min").agg({"open": "first", "high": "max", "low": "min",
                                           "close": "last", "vol": "sum"}).dropna()
    return out.reset_index()


# ── Simulation ────────────────────────────────────────────────────────────────

def run(df: pd.DataFrame, df5: pd.DataFrame, warmup: int = 250) -> dict:
    """
    Bar-by-bar walk forward. At bar i the engine sees only bars 0..i — the
    indicator window is sliced, never the full frame, so there is no lookahead.
    """
    state = {
        "in_position": False, "side": None, "entry_price": 0.0, "entry_time": None,
        "qty": 0.0, "sl_price": 0.0, "tp_price": 0.0, "setup": "",
        "trailing_active": False, "daily_pnl_usdt": 0.0, "trade_count_today": 0,
        "consecutive_losses": 0, "last_loss_time": None,
    }

    equity = config.MAX_INVESTMENT_USDT
    peak = equity
    max_dd = 0.0
    trades = []
    current_day = None
    fee_rate = config.TAKER_FEE_PCT / 100

    for i in range(warmup, len(df)):
        window = df.iloc[max(0, i - warmup):i + 1]
        bar = df.iloc[i]
        ts = bar["ts"]

        day = ts.date()
        if day != current_day:
            current_day = day
            state["daily_pnl_usdt"] = 0.0
            state["trade_count_today"] = 0
            state["consecutive_losses"] = 0

        w5 = df5[df5["ts"] <= ts]
        if len(w5) < 50:
            continue

        try:
            ind = ind_calc.calculate_scalp(window, w5.iloc[-100:])
        except Exception:
            continue

        # --- Manage an open position against this bar's actual range ---------
        if state["in_position"]:
            # Sequencing matters. The stop in force during THIS bar is the one
            # set at the end of the previous bar, so exits are checked against
            # that first. Moving the trail on this bar's close and then testing
            # this bar's low against the new level fires exits at prices that
            # did not exist when the low happened.
            long = state["side"] == "Buy"

            exit_price = None
            trigger = None

            # Pessimistic ordering: when a bar's range contains both the stop
            # and the target, assume the STOP filled. Optimistic ordering is
            # the most common way a backtest flatters a scalping strategy.
            if long:
                if bar["low"] <= state["sl_price"]:
                    exit_price, trigger = state["sl_price"], \
                        ("TRAIL" if state["trailing_active"] else "SL")
                elif bar["high"] >= state["tp_price"]:
                    exit_price, trigger = state["tp_price"], "TP"
            else:
                if bar["high"] >= state["sl_price"]:
                    exit_price, trigger = state["sl_price"], \
                        ("TRAIL" if state["trailing_active"] else "SL")
                elif bar["low"] <= state["tp_price"]:
                    exit_price, trigger = state["tp_price"], "TP"

            if exit_price is None:
                held = (ts - state["entry_time"]).total_seconds() / 60
                if held >= config.MAX_HOLD_MINUTES:
                    exit_price, trigger = bar["close"], "TIME"
                else:
                    sig = scalp_signal.get_signal(ind, state)
                    if sig.action == "CLOSE":
                        exit_price, trigger = bar["close"], "SIGNAL"

            if exit_price is not None:
                entry, qty = state["entry_price"], state["qty"]
                gross = (exit_price - entry) * qty if long else (entry - exit_price) * qty
                fees = (entry * qty + exit_price * qty) * fee_rate
                pnl = gross - fees

                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0)

                trades.append({
                    "entry_time": state["entry_time"].isoformat(),
                    "exit_time": ts.isoformat(),
                    "direction": "LONG" if long else "SHORT",
                    "setup": state["setup"],
                    "trigger": trigger,
                    "entry": round(entry, 2),
                    "exit": round(exit_price, 2),
                    "gross": round(gross, 4),
                    "fees": round(fees, 4),
                    "pnl": round(pnl, 4),
                    "pnl_pct": round((gross / (entry * qty) * 100) if entry and qty else 0, 4),
                    "held_min": round((ts - state["entry_time"]).total_seconds() / 60, 1),
                })

                state.update({"in_position": False, "side": None, "qty": 0.0,
                              "setup": "", "trailing_active": False,
                              "entry_time": None, "sl_price": 0.0, "tp_price": 0.0})
                state["daily_pnl_usdt"] += pnl
                state["consecutive_losses"] = 0 if pnl >= 0 else state["consecutive_losses"] + 1
                continue

            # Survived the bar — now ratchet the trail off the close, so it
            # governs the NEXT bar.
            state, _ = scalp_risk.update_trailing_stop(state, bar["close"], ind["atr"])
            continue

        # --- Look for an entry ------------------------------------------------
        sig = scalp_signal.get_signal(ind, state)
        if not sig.is_entry:
            continue

        # The cooldown uses wall-clock time in live trading; skip it here rather
        # than fake it, and note the difference in the caveats.
        allowed, _ = scalp_risk.validate(sig, {**state, "last_loss_time": None},
                                         {"usdt": equity}, ind)
        if not allowed:
            continue

        entry_price = bar["close"]
        side = sig.side
        brackets = scalp_risk.compute_brackets(entry_price, ind["atr"], side, sig.target)
        qty = scalp_risk.position_qty(equity, entry_price, brackets["sl_price"])
        if qty <= 0:
            continue

        state.update({
            "in_position": True,
            "side": side,
            "entry_price": entry_price,
            "entry_time": ts,
            "qty": qty,
            "sl_price": brackets["sl_price"],
            "tp_price": brackets["tp_price"],
            "setup": sig.setup,
            "trailing_active": False,
            "trade_count_today": state["trade_count_today"] + 1,
        })

    return summarise(trades, equity, max_dd, df)


# ── Reporting ─────────────────────────────────────────────────────────────────

def summarise(trades, equity, max_dd, df) -> dict:
    start = config.MAX_INVESTMENT_USDT
    if not trades:
        return {"trades": 0, "bars": len(df),
                "note": "No trades triggered. Filters may be too strict for this period."}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    total_fees = sum(t["fees"] for t in trades)

    def _group(key):
        out = {}
        for t in trades:
            g = out.setdefault(t[key], {"n": 0, "wins": 0, "pnl": 0.0})
            g["n"] += 1
            g["wins"] += 1 if t["pnl"] > 0 else 0
            g["pnl"] += t["pnl"]
        for g in out.values():
            g["win_rate"] = round(g["wins"] / g["n"] * 100, 1)
            g["pnl"] = round(g["pnl"], 2)
        return out

    days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400

    return {
        "trades": len(trades),
        "days": round(days, 1),
        "trades_per_day": round(len(trades) / days, 1) if days else 0,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "net_pnl_usdt": round(equity - start, 2),
        "return_pct": round((equity - start) / start * 100, 2),
        "total_fees_usdt": round(total_fees, 2),
        # Fees as a share of gross winnings — the number that says whether this
        # is a strategy or a rebate programme for Bybit.
        "fees_vs_gross_wins_pct": round(total_fees / gross_win * 100, 1) if gross_win else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_usdt": round((equity - start) / len(trades), 4),
        "avg_win": round(gross_win / len(wins), 4) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else 0,
        "max_drawdown_pct": round(max_dd, 2),
        "avg_hold_min": round(sum(t["held_min"] for t in trades) / len(trades), 1),
        "by_direction": _group("direction"),
        "by_setup": _group("setup"),
        "by_exit_trigger": {k: v["n"] for k, v in _group("trigger").items()},
        "final_equity": round(equity, 2),
    }


CAVEATS = """
─── Read before believing any of the above ──────────────────────────────────

1. NO SLIPPAGE MODELLED. Fills are assumed at the exact stop/target price.
   Real market orders slip, and slippage is worst precisely when you need the
   stop — during the fast move that triggered it. Budget 1-3 bps per side and
   re-check whether the edge survives.

2. STOP-BEFORE-TARGET ASSUMPTION. When a bar's range contains both levels this
   assumes the stop filled. Conservative but crude — only tick data resolves
   the true sequence.

3. THE COOLDOWN TIMER IS NOT SIMULATED, so this takes trades the live bot would
   skip. Live should show fewer trades than this report.

4. NO FUNDING COSTS. Perps pay/receive funding every 8h. Scalps rarely span a
   funding stamp, but a run of trades held through them will drift from this.

5. NO LIQUIDATION MODELLING. At LEVERAGE=1 that is fine. Above ~5x it is not,
   and this backtest will happily show a profitable run through a move that
   would have liquidated the real account.

6. SURVIVORSHIP / REGIME BIAS. One 30-day window is one sample of one market
   regime. Run several disjoint windows — a trending month, a chopping month,
   a crash — before concluding anything. A strategy that only works in the
   window you tuned it on is curve-fit, not profitable.

A profit factor under ~1.3, or an edge that vanishes when you add 2 bps of
slippage, is not a live-tradeable strategy. Paper-trade before funding.
"""


def main():
    ap = argparse.ArgumentParser(description="Backtest the BTC perp scalping engine")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbol", default=config.SYMBOL)
    ap.add_argument("--fees", type=float,
                    help="Override taker fee %% per side (perp 0.055, spot 0.10)")
    ap.add_argument("--balance", type=float, help="Override starting pot in USDT")
    ap.add_argument("--interval", type=int, default=1,
                    help="Base timeframe in minutes. BTC's 1m ATR (~0.05%%) is "
                         "far smaller than the fee floor, so a coarser base is "
                         "often the only way a target clears costs.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.fees is not None:
        config.TAKER_FEE_PCT = args.fees
        config.ROUND_TRIP_FEE_PCT = args.fees * 2
    if args.balance is not None:
        config.MAX_INVESTMENT_USDT = args.balance
    if args.interval > 1:
        # The time stop is expressed in minutes but its purpose is "N bars".
        # Left at 45 on a 15m base it would close every trade after 3 bars.
        config.MAX_HOLD_MINUTES = config.MAX_HOLD_MINUTES * args.interval

    print(f"Fetching {args.days}d of {args.symbol} perp 1m data…", file=sys.stderr)
    df = fetch_history(args.symbol, "1", args.days)
    if args.interval > 1:
        df = resample(df, args.interval)
        print(f"  resampled to {args.interval}m: {len(df)} bars", file=sys.stderr)
    # Higher-timeframe bias always runs at 5x the base timeframe.
    df5 = resample(df, args.interval * 5)

    print(f"Simulating {len(df)} bars @ {args.interval}m | "
          f"pot ${config.MAX_INVESTMENT_USDT:,.2f} | "
          f"round-trip fees {config.ROUND_TRIP_FEE_PCT:.3f}% | "
          f"min TP {config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT:.3f}%…",
          file=sys.stderr)
    result = run(df, df5)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n=== BTC Perp Scalping Backtest ===")
        for k, v in result.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"    {kk}: {vv}")
            else:
                print(f"{k}: {v}")
        print(CAVEATS)


if __name__ == "__main__":
    main()
