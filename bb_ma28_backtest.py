"""
Backtest: Bollinger-band touch + two-candle confirmation, targeting the MA28.

Strategy as specified:

  LONG
    1. A candle touches the LOWER Bollinger band.
    2. The next two candles both close green.
    3. Price is below the MA28 at entry.
    4. Take profit = the MA28 (dynamic — re-evaluated every bar).
    5. Stop loss  = the LOW of the candle that touched the band.

  SHORT (mirror)
    1. A candle touches the UPPER Bollinger band.
    2. The next two candles both close red.
    3. Price is above the MA28 at entry.
    4. Take profit = the MA28 (dynamic).
    5. Stop loss  = the HIGH of the candle that touched the band.

Entry is at the close of the second confirming candle.

Interpretation choices (not specified in the brief — vary them with the flags):
  * "touch" = the bar's LOW pierces or reaches the lower band (HIGH for upper).
  * "immediately after" = the two confirming bars are the two bars directly
    following the touch bar. A gap disqualifies the setup.
  * green = close > open, red = close < open. A doji (close == open) is neither
    and breaks the sequence.
  * Bollinger defaults to 20/2.0; MA28 defaults to a simple MA.

Fees are charged on both legs. Slippage is configurable and OFF by default so
the raw strategy is visible first — see --slippage.

    python bb_ma28_backtest.py --days 180
    python bb_ma28_backtest.py --days 180 --ma-type ema --slippage 0.02
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).parent / ".backtest_cache"


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch(symbol: str, interval: str, days: int, use_cache: bool = True) -> pd.DataFrame:
    """Page backwards through Bybit's linear kline endpoint. Public, no auth."""
    cache = CACHE_DIR / (f"{symbol}_{interval}m_{days}d_"
                         f"{datetime.now(timezone.utc):%Y%m%d}.pkl")
    if use_cache and cache.exists():
        print(f"  cached: {cache.name}", file=sys.stderr)
        return pd.read_pickle(cache)

    from pybit.unified_trading import HTTP
    client = HTTP(testnet=False)

    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Bybit also accepts the calendar intervals D/W/M, which aren't minute counts.
    minutes = {"D": 1440, "W": 10080, "M": 43200}.get(str(interval).upper())
    if minutes is None:
        minutes = int(interval)
    need = int(days * 24 * 60 / minutes)
    frames, got = [], 0

    while got < need:
        resp = client.get_kline(category="linear", symbol=symbol,
                                interval=interval, limit=1000, end=end)
        rows = resp["result"]["list"]
        if not rows:
            break
        d = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turn"])
        frames.append(d)
        got += len(rows)
        end = int(d["ts"].astype("int64").min()) - 1
        print(f"  fetched {got}/{need}…", end="\r", file=sys.stderr)
        time.sleep(0.1)

    df = pd.concat(frames, ignore_index=True).astype(
        {"ts": "int64", "open": "float64", "high": "float64",
         "low": "float64", "close": "float64", "vol": "float64"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    print(f"  fetched {len(df)} bars total     ", file=sys.stderr)

    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        df.to_pickle(cache)
    return df


def add_indicators(df: pd.DataFrame, bb_period: int, bb_std: float,
                   ma_len: int, ma_type: str, trend_len: int = 200) -> pd.DataFrame:
    df = df.copy()
    mid = df["close"].rolling(bb_period).mean()
    sd = df["close"].rolling(bb_period).std()
    df["bb_upper"] = mid + bb_std * sd
    df["bb_lower"] = mid - bb_std * sd

    if ma_type == "ema":
        df["ma"] = df["close"].ewm(span=ma_len, adjust=False).mean()
    else:
        df["ma"] = df["close"].rolling(ma_len).mean()

    # Long-term trend filter. A band-touch fade is a counter-trend entry at the
    # short horizon, so the usual discipline is to only take the side that
    # agrees with the dominant trend: buy dips above the 200MA, sell rallies
    # below it. This is what rejects the "false positive" band touches that
    # occur while price is simply trending through the band.
    df["trend_ma"] = df["close"].rolling(trend_len).mean()

    # ATR, used only to optionally cap how far the stop may sit from entry.
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    df["green"] = df["close"] > df["open"]
    df["red"] = df["close"] < df["open"]
    df["touch_lower"] = df["low"] <= df["bb_lower"]
    df["touch_upper"] = df["high"] >= df["bb_upper"]
    return df


# ── Simulation ────────────────────────────────────────────────────────────────

def run(df: pd.DataFrame, fee_pct: float, slippage_pct: float,
        allow_long: bool, allow_short: bool, max_hold_bars: int,
        min_rr: float = 0.0, min_reward_pct: float = 0.0,
        confirm_bars: int = 2, target_mode: str = "dynamic",
        use_trend_filter: bool = False, stop_cap_atr: float = 0.0,
        tp_fraction: float = 1.0) -> dict:
    """
    Walk forward one bar at a time.

    A setup is recognised at bar i when bar i-2 touched the band and bars
    i-1, i both confirm. Entry is bar i's close. From bar i+1 onward the
    position is managed against its fixed stop and the *current* MA value.
    """
    fee = fee_pct / 100
    slip = slippage_pct / 100

    trades = []
    i = 0
    n = len(df)
    warm = max(60, 0)

    while i < n:
        row = df.iloc[i]
        if i < max(warm, confirm_bars + 1) or pd.isna(row["ma"]) or pd.isna(row["bb_lower"]):
            i += 1
            continue

        # confirm_bars = how many candles must close in our direction after the
        # touch before entering. More confirmation buys certainty but spends
        # the move — measured at 2, the median remaining target (0.35%) was
        # already smaller than the median stop (0.44%).
        touch = df.iloc[i - confirm_bars]
        confirms = [df.iloc[i - k] for k in range(confirm_bars - 1, -1, -1)]

        # Trend gate: only fade in the direction the 200MA supports.
        trend = row["trend_ma"]
        if use_trend_filter and pd.isna(trend):
            i += 1
            continue
        trend_ok_long = (not use_trend_filter) or row["close"] > trend
        trend_ok_short = (not use_trend_filter) or row["close"] < trend

        side = None
        if (allow_long and touch["touch_lower"]
                and all(c["green"] for c in confirms)
                and row["close"] < row["ma"]
                and trend_ok_long):
            side = "LONG"
            stop = float(touch["low"])
        elif (allow_short and touch["touch_upper"]
                and all(c["red"] for c in confirms)
                and row["close"] > row["ma"]
                and trend_ok_short):
            side = "SHORT"
            stop = float(touch["high"])

        if side is None:
            i += 1
            continue

        entry = float(row["close"])
        entry = entry * (1 + slip) if side == "LONG" else entry * (1 - slip)

        # Optional stop cap. The specified stop is the touch candle's extreme,
        # which is not symmetric between sides: a lower-band touch is a large
        # red candle, an upper-band touch a smaller green one, so longs are
        # handed structurally wider stops. Capping at a multiple of ATR bounds
        # the loss without changing which setups qualify. Applied to BOTH sides
        # so this is not a fit to the side known to underperform.
        if stop_cap_atr:
            atr = float(row["atr"])
            if atr > 0:
                limit = stop_cap_atr * atr
                if side == "LONG":
                    stop = max(stop, entry - limit)
                else:
                    stop = min(stop, entry + limit)

        # Reward and risk as they stand AT ENTRY. The target is the MA, which
        # the two confirming candles have already moved price toward — so a
        # large part of the intended move is routinely consumed before entry,
        # leaving a target too close to cover fees. This is the setup's
        # central weakness, so measure it explicitly.
        target_now = float(row["ma"])
        reward_pct = abs(target_now - entry) / entry * 100
        risk_pct_entry = abs(entry - stop) / entry * 100
        rr_at_entry = reward_pct / risk_pct_entry if risk_pct_entry else 0.0

        if min_rr and rr_at_entry < min_rr:
            i += 1
            continue
        if min_reward_pct and reward_pct < min_reward_pct:
            i += 1
            continue

        # The stop must actually be on the losing side of the entry, otherwise
        # the setup is malformed (the confirming candles already ran past it).
        if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            i += 1
            continue

        # --- manage the position ------------------------------------------
        exit_price = exit_trigger = None
        held = 0
        j = i + 1
        while j < n:
            b = df.iloc[j]
            held += 1
            # "dynamic" re-reads the MA every bar, as specified. Measured cost:
            # on TP exits the MA had drifted toward the entry, turning an
            # expected 0.734% into a realised 0.440%. "static" freezes the
            # target at its entry-bar value so the reward cannot erode.
            ma_now = float(b["ma"]) if target_mode == "dynamic" else target_now
            # tp_fraction scales how far toward the MA we aim. 1.0 is the
            # specified behaviour (all the way); 0.5 banks halfway. Closer
            # targets are hit more often but pay less, so which wins is an
            # empirical question, not an obvious one.
            target = entry + tp_fraction * (ma_now - entry)

            if side == "LONG":
                # Pessimistic: if a bar spans both levels, assume the stop filled.
                if b["low"] <= stop:
                    exit_price, exit_trigger = stop * (1 - slip), "SL"
                elif b["high"] >= target:
                    exit_price, exit_trigger = target * (1 - slip), "TP"
            else:
                if b["high"] >= stop:
                    exit_price, exit_trigger = stop * (1 + slip), "SL"
                elif b["low"] <= target:
                    exit_price, exit_trigger = target * (1 + slip), "TP"

            if exit_price is not None:
                break
            if max_hold_bars and held >= max_hold_bars:
                exit_price, exit_trigger = float(b["close"]), "TIME"
                break
            j += 1

        if exit_price is None:            # ran out of data still open
            break

        gross_pct = ((exit_price - entry) / entry * 100) if side == "LONG" \
            else ((entry - exit_price) / entry * 100)
        net_pct = gross_pct - fee * 2 * 100

        trades.append({
            "entry_time": row["ts"].isoformat(),
            "exit_time": df.iloc[j]["ts"].isoformat(),
            "side": side,
            "entry": round(entry, 2),
            "exit": round(exit_price, 2),
            "stop": round(stop, 2),
            "trigger": exit_trigger,
            "gross_pct": round(gross_pct, 4),
            "net_pct": round(net_pct, 4),
            "risk_pct": round(risk_pct_entry, 4),
            "reward_pct_at_entry": round(reward_pct, 4),
            "rr_at_entry": round(rr_at_entry, 3),
            "held_bars": held,
        })

        # No overlapping positions — resume scanning after the exit bar.
        i = j + 1

    return summarise(trades, df, fee_pct, slippage_pct)


def summarise(trades, df, fee_pct, slippage_pct) -> dict:
    if not trades:
        return {"trades": 0, "note": "No setups matched."}

    wins = [t for t in trades if t["net_pct"] > 0]
    losses = [t for t in trades if t["net_pct"] <= 0]
    gw = sum(t["net_pct"] for t in wins)
    gl = abs(sum(t["net_pct"] for t in losses))

    # Compounded equity, risking the whole notional each time (percent returns
    # chain multiplicatively). Reported as a percentage so it is size-agnostic.
    eq = 1.0
    peak, dd = 1.0, 0.0
    for t in trades:
        eq *= (1 + t["net_pct"] / 100)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)

    def group(key):
        out = {}
        for t in trades:
            g = out.setdefault(t[key], {"n": 0, "wins": 0, "net_pct": 0.0})
            g["n"] += 1
            g["wins"] += 1 if t["net_pct"] > 0 else 0
            g["net_pct"] += t["net_pct"]
        for g in out.values():
            g["win_rate"] = round(g["wins"] / g["n"] * 100, 1)
            g["net_pct"] = round(g["net_pct"], 2)
        return out

    days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400
    avg_r = sum(t["net_pct"] / t["risk_pct"] for t in trades if t["risk_pct"]) / len(trades)

    rewards = sorted(t["reward_pct_at_entry"] for t in trades)
    rrs = sorted(t["rr_at_entry"] for t in trades)
    round_trip = fee_pct * 2

    def pct(xs, p):
        return round(xs[int(len(xs) * p / 100)], 4)

    # How often is the target simply not far enough away to pay the fees?
    doomed = sum(1 for t in trades if t["reward_pct_at_entry"] < round_trip)

    return {
        "trades": len(trades),
        "days": round(days, 1),
        "trades_per_week": round(len(trades) / days * 7, 1) if days else 0,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "total_net_pct": round(sum(t["net_pct"] for t in trades), 2),
        "compounded_return_pct": round((eq - 1) * 100, 2),
        "profit_factor": round(gw / gl, 2) if gl else None,
        "expectancy_pct": round(sum(t["net_pct"] for t in trades) / len(trades), 4),
        "expectancy_R": round(avg_r, 3),
        "avg_win_pct": round(gw / len(wins), 4) if wins else 0,
        "avg_loss_pct": round(-gl / len(losses), 4) if losses else 0,
        "avg_risk_pct": round(sum(t["risk_pct"] for t in trades) / len(trades), 4),
        "reward_at_entry_pct": {"p10": pct(rewards, 10), "median": pct(rewards, 50),
                                "p90": pct(rewards, 90)},
        "rr_at_entry": {"p10": pct(rrs, 10), "median": pct(rrs, 50), "p90": pct(rrs, 90)},
        "setups_whose_target_cannot_cover_fees": f"{doomed} ({doomed/len(trades)*100:.1f}%)",
        "avg_hold_bars": round(sum(t["held_bars"] for t in trades) / len(trades), 1),
        "max_drawdown_pct": round(dd, 2),
        "fees_pct_per_trade": round(fee_pct * 2, 4),
        "slippage_pct_per_side": slippage_pct,
        "by_side": group("side"),
        "by_trigger": {k: v["n"] for k, v in group("trigger").items()},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15", help="minutes per bar")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--bb-period", type=int, default=20)
    ap.add_argument("--bb-std", type=float, default=2.0)
    ap.add_argument("--ma", type=int, default=28)
    ap.add_argument("--ma-type", choices=["sma", "ema"], default="sma")
    ap.add_argument("--fees", type=float, default=0.055,
                    help="taker fee %% per side (Bybit perp 0.055, spot 0.10)")
    ap.add_argument("--slippage", type=float, default=0.0, help="%% per side")
    ap.add_argument("--max-hold", type=int, default=0, help="bars; 0 = no time stop")
    ap.add_argument("--min-rr", type=float, default=0.0,
                    help="skip setups whose reward/risk at entry is below this")
    ap.add_argument("--min-reward", type=float, default=0.0,
                    help="skip setups whose target is nearer than this %% from entry")
    ap.add_argument("--longs-only", action="store_true")
    ap.add_argument("--shorts-only", action="store_true")
    ap.add_argument("--confirm", type=int, default=2,
                    help="candles that must close in-direction after the touch")
    ap.add_argument("--target", choices=["dynamic", "static"], default="dynamic",
                    help="dynamic re-reads the MA each bar (as specified); "
                         "static freezes it at the entry bar's value")
    ap.add_argument("--trend-ma", type=int, default=200,
                    help="length of the long-term trend MA")
    ap.add_argument("--tp-fraction", type=float, default=1.0,
                    help="how far toward the MA to target; 1.0 = all the way "
                         "(as specified), 0.5 = bank halfway")
    ap.add_argument("--stop-cap-atr", type=float, default=0.0,
                    help="cap the stop at this many ATR from entry; 0 = use the "
                         "touch candle's extreme as specified")
    ap.add_argument("--trend-filter", action="store_true",
                    help="only long above the trend MA, only short below it")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    df = fetch(args.symbol, args.interval, args.days)
    df = add_indicators(df, args.bb_period, args.bb_std, args.ma, args.ma_type,
                        args.trend_ma)

    res = run(df, args.fees, args.slippage,
              allow_long=not args.shorts_only,
              allow_short=not args.longs_only,
              max_hold_bars=args.max_hold,
              min_rr=args.min_rr, min_reward_pct=args.min_reward,
              confirm_bars=args.confirm, target_mode=args.target,
              use_trend_filter=args.trend_filter,
              stop_cap_atr=args.stop_cap_atr, tp_fraction=args.tp_fraction)

    res["config"] = {
        "symbol": args.symbol, "interval_min": args.interval, "days": args.days,
        "bb": f"{args.bb_period}/{args.bb_std}", "ma": f"{args.ma_type.upper()}{args.ma}",
        "confirm_bars": args.confirm, "target": args.target,
        "trend_filter": f"MA{args.trend_ma}" if args.trend_filter else "off",
        "sides": ("shorts only" if args.shorts_only else
                  "longs only" if args.longs_only else "both"),
    }

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"\n=== BB touch + 2-candle confirm → MA{args.ma} "
              f"({args.symbol} {args.interval}m) ===")
        for k, v in res.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"    {kk}: {vv}")
            else:
                print(f"{k}: {v}")


if __name__ == "__main__":
    main()
