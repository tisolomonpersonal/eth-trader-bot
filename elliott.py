"""
Testing the mechanizable parts of Elliott Wave.

Full Elliott Wave cannot be tested: wave counts are subjective, several valid
counts coexist for any series, and an alternate count is always available when
the primary fails. That makes it unfalsifiable rather than wrong.

What CAN be tested deterministically:

  1. FIBONACCI RETRACEMENTS. After a swing, do the 0.382 / 0.5 / 0.618 levels
     act as support or resistance more often than arbitrary levels? This is
     the decisive test - if Fib levels are not special, the whole retracement
     apparatus is decoration. Compared against control levels (0.25/0.45/0.70)
     that no one watches.

  2. IMPULSE STRUCTURE. Detect five-wave sequences satisfying Elliott's three
     hard rules, then measure what actually happens next. Theory says a
     completed five-wave move is followed by a three-wave correction against
     it, so forward returns after a completed impulse should be negative
     (for an up-impulse) relative to baseline.

  3. WAVE 3 EXTENSION. Elliott holds wave 3 is usually the longest and never
     the shortest. That is a checkable claim about detected impulses.
"""
import numpy as np
import pandas as pd

import bb_ma28_backtest as bt

SYMS = [("BTCUSDT", 1600), ("ETHUSDT", 1600), ("SOLUSDT", 1200),
        ("BNBUSDT", 1600), ("XRPUSDT", 1600)]


def zigzag(df: pd.DataFrame, pct: float = 3.0):
    """
    Deterministic pivot detector: a new pivot forms when price reverses by
    `pct` from the running extreme. This is what makes swing identification
    objective instead of eyeballed - the whole point, since Elliott's
    subjectivity is exactly what makes it untestable.

    Strict two-state machine. An earlier version let both branches run when
    direction was unset, so the extreme flipped between the bar's high and low
    every iteration and no pivot was ever recorded.

    Returns [(index, price, 'H'|'L'), ...].
    """
    highs, lows = df["high"].values, df["low"].values
    piv = []
    direction = 1                       # +1 = tracking a high, -1 = a low
    ext_i, ext_p = 0, highs[0]

    for i in range(1, len(df)):
        if direction == 1:
            if highs[i] > ext_p:
                ext_i, ext_p = i, highs[i]
            elif ext_p > 0 and (ext_p - lows[i]) / ext_p * 100 >= pct:
                piv.append((ext_i, ext_p, "H"))
                direction = -1
                ext_i, ext_p = i, lows[i]
        else:
            if lows[i] < ext_p:
                ext_i, ext_p = i, lows[i]
            elif ext_p > 0 and (highs[i] - ext_p) / ext_p * 100 >= pct:
                piv.append((ext_i, ext_p, "L"))
                direction = 1
                ext_i, ext_p = i, highs[i]
    return piv


# ── Test 1: are Fibonacci levels special? ────────────────────────────────────

FIB = [0.382, 0.5, 0.618]
CONTROL = [0.25, 0.45, 0.70]          # levels nobody watches


def retracement_test(df, piv, tol=0.025):
    """
    For each completed swing, measure how deep the following retracement ran,
    then record whether that depth landed within `tol` of each candidate level.

    Every level gets the identical tolerance window, so Fib and control levels
    are judged on exactly the same basis.
    """
    hits = {round(l, 3): 0 for l in FIB + CONTROL}
    depths, total = [], 0

    for k in range(len(piv) - 2):
        _, p0, k0 = piv[k]
        _, p1, k1 = piv[k + 1]
        _, p2, _ = piv[k + 2]
        if k0 == k1:
            continue
        swing = p1 - p0
        if swing == 0:
            continue
        # p2 is the extreme of the retracement against the p0 -> p1 swing.
        depth = (p1 - p2) / swing        # 0 = no retrace, 1 = full retrace
        if not (0 < depth < 1.2):
            continue
        total += 1
        depths.append(depth)
        for lvl in hits:
            if abs(depth - lvl) <= tol:
                hits[lvl] += 1
    return hits, total, depths


def test_fib():
    print("=" * 78)
    print("TEST 1 — Do Fibonacci retracement levels do anything special?")
    print("=" * 78)
    print("Reversal depths clustering near each level, vs controls nobody watches.\n")

    agg_hits, agg_total, all_depths = {}, 0, []
    for sym, days in SYMS:
        df = bt.fetch(sym, "240", days)
        piv = zigzag(df, 3.0)
        hits, total, depths = retracement_test(df, piv)
        agg_total += total
        all_depths += depths
        for k, v in hits.items():
            agg_hits[k] = agg_hits.get(k, 0) + v

    if not agg_total:
        print("No swings detected.")
        return

    print(f"{'level':>8}{'type':>10}{'hits':>8}{'% of swings':>13}")
    for lvl in sorted(agg_hits):
        typ = "FIB" if lvl in FIB else "control"
        print(f"{lvl:>8}{typ:>10}{agg_hits[lvl]:>8}"
              f"{agg_hits[lvl] / agg_total * 100:>12.1f}%")

    fib_rate = np.mean([agg_hits[l] for l in FIB]) / agg_total * 100
    ctl_rate = np.mean([agg_hits[l] for l in CONTROL]) / agg_total * 100
    print(f"\nSwings analysed: {agg_total}")
    print(f"Mean hit rate  FIB levels: {fib_rate:.1f}%")
    print(f"Mean hit rate  controls  : {ctl_rate:.1f}%")
    print(f"=> Fib advantage: {fib_rate - ctl_rate:+.1f} percentage points")

    d = np.array(all_depths)
    print(f"\nRetracement depth distribution: median {np.median(d):.3f}, "
          f"mean {d.mean():.3f}")
    print("Deciles:", " ".join(f"{np.percentile(d, q):.2f}" for q in range(10, 100, 10)))


# ── Test 2: does a completed 5-wave impulse predict a reversal? ──────────────

def find_impulses(piv):
    """
    Five pivots forming an up-impulse that satisfies Elliott's three hard rules:
      - wave 2 does not retrace beyond the start of wave 1
      - wave 3 is not the shortest of waves 1, 3, 5
      - wave 4 does not overlap wave 1's territory
    Returns list of (end_index, w1, w3, w5 lengths).
    """
    out = []
    for k in range(len(piv) - 5):
        pts = piv[k:k + 6]
        kinds = "".join(p[2] for p in pts)
        if kinds != "LHLHLH":
            continue
        p0, p1, p2, p3, p4, p5 = [p[1] for p in pts]
        w1, w3, w5 = p1 - p0, p3 - p2, p5 - p4
        if min(w1, w3, w5) <= 0:
            continue
        if p2 <= p0:                       # rule 1
            continue
        if w3 < w1 and w3 < w5:            # rule 2
            continue
        if p4 <= p1:                       # rule 3
            continue
        out.append((pts[5][0], w1, w3, w5))
    return out


def test_impulse():
    print()
    print("=" * 78)
    print("TEST 2 — After a valid 5-wave up-impulse, does price actually fall?")
    print("=" * 78)

    fwd_all, base_all, w3_longest, n_imp = [], [], 0, 0
    H = 30                                  # 30 bars = 5 days on 4h
    for sym, days in SYMS:
        df = bt.fetch(sym, "240", days)
        piv = zigzag(df, 3.0)
        imps = find_impulses(piv)
        c = df["close"].values
        base = pd.Series(c).pct_change(H).shift(-H).dropna().values * 100
        base_all.append(base.mean())
        for end_i, w1, w3, w5 in imps:
            if end_i + H >= len(c):
                continue
            fwd_all.append((c[end_i + H] / c[end_i] - 1) * 100)
            n_imp += 1
            if w3 >= max(w1, w5):
                w3_longest += 1

    if not fwd_all:
        print("No valid impulses detected.")
        return

    f = np.array(fwd_all)
    print(f"Valid impulses found: {n_imp}")
    print(f"Mean forward return {H} bars after an impulse completes: {f.mean():+.2f}%")
    print(f"Baseline mean {H}-bar return (any moment at all)      : {np.mean(base_all):+.2f}%")
    print(f"Median forward return after an impulse               : {np.median(f):+.2f}%")
    print(f"Price fell over the next {H} bars: {(f < 0).mean() * 100:.1f}% of the time")
    print()
    print("Theory predicts a corrective move, so this should sit clearly BELOW")
    print("baseline with well over half of cases falling.")
    print()
    print(f"Wave 3 was the longest wave in {w3_longest}/{n_imp} "
          f"({w3_longest / max(n_imp, 1) * 100:.0f}%) of detected impulses")
    print("Elliott holds wave 3 is usually longest and never shortest.")


if __name__ == "__main__":
    test_fib()
    test_impulse()
