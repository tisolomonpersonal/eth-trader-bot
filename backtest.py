"""
Walk-forward backtester for the BTC scalping engine.

Runs the EXACT same scalp_signal / scalp_risk code paths the live bot uses, so
what you measure here is what you deploy. Nothing is reimplemented.

    python backtest.py --days 30
    python backtest.py --days 30 --fees 0.055 --json

Read the caveats at the bottom of the output before trusting any number.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
import indicators as ind_calc
import scalp_signal
import scalp_risk


# ── Historical data ───────────────────────────────────────────────────────────

def fetch_history(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """
    Page backwards through Bybit's kline endpoint (1000 bars max per request)
    until `days` of history is collected. Public endpoint — no API key needed,
    which means you can validate the strategy before funding anything.
    """
    from pybit.unified_trading import HTTP
    client = HTTP(testnet=False)

    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    bars_needed = int(days * 24 * 60 / int(interval))
    frames = []
    fetched = 0

    while fetched < bars_needed:
        resp = client.get_kline(category="spot", symbol=symbol,
                                interval=interval, limit=1000, end=end)
        rows = resp["result"]["list"]
        if not rows:
            break

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
        frames.append(df)
        fetched += len(rows)

        # Bybit returns newest-first; step `end` back past the oldest bar.
        end = int(df["ts"].astype("int64").min()) - 1
        print(f"  fetched {fetched}/{bars_needed} bars…", end="\r", file=sys.stderr)
        time.sleep(0.1)   # stay well inside the public rate limit

    if not frames:
        raise RuntimeError("No historical data returned")

    out = pd.concat(frames, ignore_index=True)
    out = out.astype({"ts": "int64", "open": "float64", "high": "float64",
                      "low": "float64", "close": "float64", "vol": "float64"})
    out["ts"] = pd.to_datetime(out["ts"], unit="ms", utc=True)
    out = out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    print(f"  fetched {len(out)} bars total          ", file=sys.stderr)
    return out


def resample_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Build the 5m higher-timeframe series from 1m bars."""
    d = df_1m.set_index("ts")
    out = d.resample("5min").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "vol": "sum"}).dropna()
    return out.reset_index()


# ── Simulation ────────────────────────────────────────────────────────────────

def run(df: pd.DataFrame, df5: pd.DataFrame, warmup: int = 250) -> dict:
    """
    Bar-by-bar walk forward. At bar i the engine sees only bars 0..i — the
    indicator window is sliced, never the full frame, so there is no lookahead.
    """
    state = {
        "in_position": False, "entry_price": 0.0, "entry_time": None,
        "qty": 0.0, "sl_price": 0.0, "tp_price": 0.0, "setup": "",
        "trailing_active": False, "daily_pnl_usdt": 0.0, "trade_count_today": 0,
        "consecutive_losses": 0, "last_loss_time": None,
    }

    equity = config.MAX_INVESTMENT_USDT
    peak = equity
    max_dd = 0.0
    trades = []
    equity_curve = []
    current_day = None

    fee_rate = config.TAKER_FEE_PCT / 100

    for i in range(warmup, len(df)):
        window = df.iloc[max(0, i - warmup):i + 1]
        bar = df.iloc[i]
        ts = bar["ts"]

        # Daily counter reset, mirroring reset_daily_if_needed().
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
            state = scalp_risk.update_trailing_stop(state, bar["close"], ind["atr"])

            exit_price = None
            trigger = None

            # Pessimistic ordering: if both the stop and target fall inside the
            # same bar we assume the stop filled first. Optimistic ordering is
            # the most common way a backtest flatters a scalping strategy.
            if bar["low"] <= state["sl_price"]:
                exit_price = state["sl_price"]
                trigger = "TRAIL" if state["trailing_active"] else "SL"
            elif bar["high"] >= state["tp_price"]:
                exit_price = state["tp_price"]
                trigger = "TP"
            else:
                held = (ts - state["entry_time"]).total_seconds() / 60
                if held >= config.MAX_HOLD_MINUTES:
                    exit_price = bar["close"]
                    trigger = "TIME"

            if exit_price is None:
                sig = scalp_signal.get_signal(ind, state)
                if sig.action == "SELL":
                    exit_price = bar["close"]
                    trigger = "SIGNAL"

            if exit_price is not None:
                entry = state["entry_price"]
                qty = state["qty"]
                gross = (exit_price - entry) * qty
                fees = (entry * qty + exit_price * qty) * fee_rate
                pnl = gross - fees

                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0)

                trades.append({
                    "entry_time": state["entry_time"].isoformat(),
                    "exit_time": ts.isoformat(),
                    "setup": state["setup"],
                    "trigger": trigger,
                    "entry": round(entry, 2),
                    "exit": round(exit_price, 2),
                    "gross": round(gross, 4),
                    "fees": round(fees, 4),
                    "pnl": round(pnl, 4),
                    "pnl_pct": round((exit_price - entry) / entry * 100, 4),
                    "held_min": round((ts - state["entry_time"]).total_seconds() / 60, 1),
                })

                state.update({"in_position": False, "qty": 0.0, "setup": "",
                              "trailing_active": False, "entry_time": None,
                              "sl_price": 0.0, "tp_price": 0.0})
                state["daily_pnl_usdt"] += pnl
                if pnl < 0:
                    state["consecutive_losses"] += 1
                else:
                    state["consecutive_losses"] = 0
                equity_curve.append({"ts": ts.isoformat(), "equity": round(equity, 2)})
                continue

        # --- Look for an entry ------------------------------------------------
        sig = scalp_signal.get_signal(ind, state)
        if sig.action != "BUY":
            continue

        balance = {"usdt": equity}
        # Cooldown uses wall-clock in live trading; skip it here rather than
        # fake it, and note the difference in the caveats.
        allowed, _ = scalp_risk.validate(sig, {**state, "last_loss_time": None},
                                         balance, ind)
        if not allowed:
            continue

        entry_price = bar["close"]
        brackets = scalp_risk.compute_brackets(entry_price, ind["atr"], sig.target)
        notional = scalp_risk.position_size(equity, entry_price, brackets["sl_price"])
        if notional < scalp_risk.MIN_NOTIONAL_USDT:
            continue

        state.update({
            "in_position": True,
            "entry_price": entry_price,
            "entry_time": ts,
            "qty": notional / entry_price,
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
        return {"trades": 0, "note": "No trades triggered in this period.",
                "bars": len(df)}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    total_fees = sum(t["fees"] for t in trades)

    by_setup = {}
    for t in trades:
        s = by_setup.setdefault(t["setup"], {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += 1 if t["pnl"] > 0 else 0
        s["pnl"] += t["pnl"]
    for s in by_setup.values():
        s["win_rate"] = round(s["wins"] / s["n"] * 100, 1)
        s["pnl"] = round(s["pnl"], 2)

    by_trigger = {}
    for t in trades:
        by_trigger[t["trigger"]] = by_trigger.get(t["trigger"], 0) + 1

    days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400

    return {
        "trades": len(trades),
        "days": round(days, 1),
        "trades_per_day": round(len(trades) / days, 1) if days else 0,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "net_pnl_usdt": round(equity - start, 2),
        "return_pct": round((equity - start) / start * 100, 2),
        "total_fees_usdt": round(total_fees, 2),
        # Fees as a share of gross winnings — the number that tells you whether
        # this is a strategy or a rebate program for Bybit.
        "fees_vs_gross_wins_pct": round(total_fees / gross_win * 100, 1) if gross_win else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_usdt": round((equity - start) / len(trades), 4),
        "avg_win": round(gross_win / len(wins), 4) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else 0,
        "max_drawdown_pct": round(max_dd, 2),
        "avg_hold_min": round(sum(t["held_min"] for t in trades) / len(trades), 1),
        "by_setup": by_setup,
        "by_exit_trigger": by_trigger,
        "final_equity": round(equity, 2),
    }


CAVEATS = """
─── Read before believing any of the above ──────────────────────────────────

1. NO SLIPPAGE MODELLED. Fills are assumed at the exact stop/target price.
   Real market orders slip, and slippage is worst precisely when you need the
   stop — during the fast move that triggered it. Budget 1-3 bps per side and
   re-check whether the edge survives.

2. STOP-BEFORE-TARGET ASSUMPTION. When a bar's range contains both levels this
   assumes the stop filled. That is conservative but crude — only tick data
   resolves the true sequence.

3. THE COOLDOWN TIMER IS NOT SIMULATED, so this takes trades the live bot would
   skip. Live results should show fewer trades than this report.

4. SURVIVORSHIP / REGIME BIAS. One 30-day window is one sample of one market
   regime. Run several disjoint windows — a trending month, a chopping month,
   a crash — before concluding anything. A strategy that only works in the
   window you tuned it on is curve-fit, not profitable.

5. NO FUNDING COSTS (irrelevant on spot, but material if you migrate to perps).

A profit factor under ~1.3 or an edge that vanishes when you add 2 bps of
slippage is not a live-tradeable strategy. Paper-trade before funding.
"""


def main():
    ap = argparse.ArgumentParser(description="Backtest the BTC scalping engine")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbol", default=config.SYMBOL)
    ap.add_argument("--fees", type=float, help="Override round-trip fee %% per side")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    if args.fees is not None:
        config.TAKER_FEE_PCT = args.fees
        config.MAKER_FEE_PCT = args.fees
        config.ROUND_TRIP_FEE_PCT = args.fees * 2

    print(f"Fetching {args.days}d of {args.symbol} 1m data…", file=sys.stderr)
    df = fetch_history(args.symbol, "1", args.days)
    df5 = resample_5m(df)

    print(f"Simulating {len(df)} bars "
          f"(round-trip fees {config.ROUND_TRIP_FEE_PCT:.3f}%, "
          f"min TP {config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT:.3f}%)…",
          file=sys.stderr)
    result = run(df, df5)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n=== BTC Scalping Backtest ===")
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
