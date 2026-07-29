"""
Control test: is the post-impulse pullback an ELLIOTT effect, or just
mean reversion after any large rally?

A valid five-wave impulse is, by construction, a big up-move. If comparable
up-moves that VIOLATE Elliott's rules behave the same way, the rules add
nothing and the effect is ordinary mean reversion wearing a costume.
"""
import numpy as np
import pandas as pd

from elliott import SYMS, zigzag, find_impulses
import bb_ma28_backtest as bt

H = 30


def find_rallies(piv, valid_ends):
    """
    Six-pivot LHLHLH sequences that do NOT satisfy Elliott's rules - same
    shape, same general size, rules broken. The control group.
    """
    out = []
    for k in range(len(piv) - 5):
        pts = piv[k:k + 6]
        if "".join(p[2] for p in pts) != "LHLHLH":
            continue
        p0, p1, p2, p3, p4, p5 = [p[1] for p in pts]
        w1, w3, w5 = p1 - p0, p3 - p2, p5 - p4
        if min(w1, w3, w5) <= 0:
            continue
        ok = (p2 > p0) and not (w3 < w1 and w3 < w5) and (p4 > p1)
        if ok or pts[5][0] in valid_ends:
            continue                       # this one is a valid impulse
        out.append((pts[5][0], (p5 / p0 - 1) * 100))
    return out


imp_fwd, ctl_fwd, base_fwd = [], [], []
imp_size, ctl_size = [], []

for sym, days in SYMS:
    df = bt.fetch(sym, "240", days)
    piv = zigzag(df, 3.0)
    c = df["close"].values

    imps = find_impulses(piv)
    valid_ends = {e for e, _, _, _ in imps}
    ctrls = find_rallies(piv, valid_ends)

    base = pd.Series(c).pct_change(H).shift(-H).dropna().values * 100
    base_fwd.append(base.mean())

    for end_i, w1, w3, w5 in imps:
        if end_i + H < len(c):
            imp_fwd.append((c[end_i + H] / c[end_i] - 1) * 100)
    for end_i, size in ctrls:
        if end_i + H < len(c):
            ctl_fwd.append((c[end_i + H] / c[end_i] - 1) * 100)
            ctl_size.append(size)

imp, ctl = np.array(imp_fwd), np.array(ctl_fwd)

print("=" * 74)
print(f"Forward return over {H} bars after each kind of setup")
print("=" * 74)
print(f"{'group':<28}{'n':>6}{'mean%':>9}{'median%':>10}{'fell%':>8}")
print(f"{'Valid Elliott impulse':<28}{len(imp):>6}{imp.mean():>9.2f}"
      f"{np.median(imp):>10.2f}{(imp < 0).mean() * 100:>8.1f}")
if len(ctl):
    print(f"{'Same shape, rules VIOLATED':<28}{len(ctl):>6}{ctl.mean():>9.2f}"
          f"{np.median(ctl):>10.2f}{(ctl < 0).mean() * 100:>8.1f}")
print(f"{'Baseline (any moment)':<28}{'':>6}{np.mean(base_fwd):>9.2f}")
print()

if len(ctl) > 20:
    diff = imp.mean() - ctl.mean()
    se = np.sqrt(imp.var() / len(imp) + ctl.var() / len(ctl))
    t = diff / se if se else 0
    print(f"Difference (impulse - control): {diff:+.3f}%   t = {t:+.2f}")
    print()
    if abs(t) < 2:
        print("=> NOT distinguishable. Elliott's rules add nothing beyond")
        print("   'price rallied' - the pullback is ordinary mean reversion.")
    else:
        print("=> The rules do separate the two groups.")
else:
    print("Too few control cases to compare.")
