"""Offline checks for the grid strategy — no network, no API keys."""
import os
import tempfile

# Point persistence at a throwaway dir before grid_config reads DATA_DIR.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="grid_test_")

import numpy as np
import pandas as pd

import grid_client as gcl
import grid_config as gc
import grid_strategy as gs

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILS.append(label)


def make_df(prices):
    n = len(prices)
    close = pd.Series(prices, dtype="float64")
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "vol": pd.Series([100.0] * n),
    })


print("\n== rounding ==")
check("tick rounding", gcl.round_price(100123.4567) == 100123.5,
      gcl.round_price(100123.4567))
check("qty floors, never rounds up", gcl.round_qty(0.0019) == 0.001,
      gcl.round_qty(0.0019))
check("min qty accepted", gcl.qty_is_tradeable(0.001))
check("sub-min qty rejected", not gcl.qty_is_tradeable(0.0005))

print("\n== bias ==")
up = make_df(np.linspace(60000, 100000, 400))
down = make_df(np.linspace(100000, 60000, 400))
flat = make_df([80000 + (i % 3) for i in range(400)])

b_up, f_up, s_up = gs.compute_bias(up)
b_dn, _, _ = gs.compute_bias(down)
b_fl, f_fl, s_fl = gs.compute_bias(flat)
check("uptrend -> long", b_up == "long", b_up)
check("downtrend -> short", b_dn == "short", b_dn)
check("flat -> neutral", b_fl == "neutral", f"{b_fl} sep={abs(f_fl-s_fl):.4f}")

print("\n== geometry ==")
step = gs.compute_step(up)
check("step is positive", step > 0, step)
below, above = gs.build_levels(100000, 500)
check("2 below, descending", below == [99500.0, 99000.0], below)
check("2 above, ascending", above == [100500.0, 101000.0], above)

print("\n== desired orders: long bias, flat book ==")
o = gs.desired_orders("long", below, above, {"long": None, "short": None})
entries = [x for x in o if not x["reduce_only"] and x["position_idx"] == 1]
tps = [x for x in o if x["reduce_only"]]
hedges = [x for x in o if not x["reduce_only"] and x["position_idx"] == 2]
check("2 buy entries below", len(entries) == 2 and all(x["side"] == "Buy" for x in entries),
      entries)
check("no TPs with no position", len(tps) == 0, tps)
check("above levels become hedge shorts", len(hedges) == 2
      and all(x["side"] == "Sell" and x["position_idx"] == 2 for x in hedges), hedges)
check("total 4 orders (2 below, 2 above)", len(o) == 4, len(o))

print("\n== desired orders: long bias, 0.002 long held ==")
pos = {"long": {"size": 0.002, "avg_price": 99250, "unrealised_pnl": 0,
                "position_idx": 1, "side": "Buy"}, "short": None}
o = gs.desired_orders("long", below, above, pos)
tps = [x for x in o if x["reduce_only"] and x["position_idx"] == 1]
hedges = [x for x in o if x["position_idx"] == 2 and not x["reduce_only"]]
check("both above levels are reduce-only TPs", len(tps) == 2, tps)
check("TPs sized one qty each", all(x["qty"] == 0.001 for x in tps), tps)
check("TPs are Sell on the long", all(x["side"] == "Sell" for x in tps), tps)
check("no hedge while position covers the levels", len(hedges) == 0, hedges)

print("\n== desired orders: partial cover (0.001 long) ==")
pos = {"long": {"size": 0.001, "avg_price": 99500, "unrealised_pnl": 0,
                "position_idx": 1, "side": "Buy"}, "short": None}
o = gs.desired_orders("long", below, above, pos)
tps = [x for x in o if x["reduce_only"]]
hedges = [x for x in o if x["position_idx"] == 2 and not x["reduce_only"]]
check("nearest above level is a TP", len(tps) == 1 and tps[0]["price"] == above[0],
      tps)
check("farther above level becomes a hedge", len(hedges) == 1
      and hedges[0]["price"] == above[1], hedges)

print("\n== desired orders: position cap reached ==")
pos = {"long": {"size": gc.GRID_MAX_POSITION_BTC, "avg_price": 99000,
                "unrealised_pnl": 0, "position_idx": 1, "side": "Buy"}, "short": None}
o = gs.desired_orders("long", below, above, pos)
new_entries = [x for x in o if not x["reduce_only"] and x["position_idx"] == 1]
check("no new entries at cap", len(new_entries) == 0, new_entries)

print("\n== desired orders: short bias mirrors ==")
o = gs.desired_orders("short", below, above, {"long": None, "short": None})
entries = [x for x in o if not x["reduce_only"] and x["position_idx"] == 2]
check("entries are Sell above", len(entries) == 2
      and all(x["side"] == "Sell" and x["price"] in above for x in entries), entries)
hedges = [x for x in o if x["position_idx"] == 1 and not x["reduce_only"]]
check("hedges are Buy below", len(hedges) == 2
      and all(x["side"] == "Buy" and x["price"] in below for x in hedges), hedges)

print("\n== desired orders: neutral ==")
check("neutral places nothing", gs.desired_orders("neutral", below, above,
                                                  {"long": None, "short": None}) == [])

print("\n== hedge gets an exit ==")
pos = {"long": None, "short": {"size": 0.002, "avg_price": 100600,
                               "unrealised_pnl": 0, "position_idx": 2, "side": "Sell"}}
o = gs.desired_orders("long", below, above, pos)
hedge_tp = [x for x in o if x["tag"] == "hedge_tp"]
check("hedge has a reduce-only exit", len(hedge_tp) == 1
      and hedge_tp[0]["reduce_only"] and hedge_tp[0]["position_idx"] == 2, hedge_tp)
check("hedge exit is a Buy back", hedge_tp and hedge_tp[0]["side"] == "Buy", hedge_tp)

print("\n== reconcile ==")
desired = gs.desired_orders("long", below, above, {"long": None, "short": None})


def as_existing(spec, oid):
    return {"order_id": oid, "link_id": f"gr-x-{oid}", "side": spec["side"],
            "price": spec["price"], "qty": gcl.round_qty(spec["qty"]),
            "position_idx": spec["position_idx"], "reduce_only": spec["reduce_only"]}


existing = [as_existing(d, f"id{i}") for i, d in enumerate(desired)]
r = gs.reconcile(desired, existing)
check("identical book is a no-op", r["placed"] == 0 and r["cancelled"] == 0
      and r["kept"] == 4, r)

stale = existing[:2] + [{"order_id": "zzz", "link_id": "gr-x-zzz", "side": "Buy",
                         "price": 12345.0, "qty": 0.001, "position_idx": 1,
                         "reduce_only": False}]
r = gs.reconcile(desired, stale)
check("stale order cancelled", r["cancelled"] == 1, r)
check("missing orders detected", r["kept"] == 2, r)

wrong_qty = [dict(e, qty=0.005) for e in existing]
r = gs.reconcile(desired, wrong_qty)
check("qty mismatch forces replace", r["cancelled"] == 4 and r["kept"] == 0, r)

tol = [dict(e, price=e["price"] + 0.05) for e in existing]
r = gs.reconcile(desired, tol)
check("sub-tick drift kept, no churn", r["kept"] == 4 and r["cancelled"] == 0, r)

print("\n== rebuild triggers ==")
st = gs._empty_state()
check("no grid -> rebuild", gs.needs_rebuild(st, 100000, "long", 500) != "")
st.update({"grid_built_at": "now", "centre": 100000, "step": 500, "bias": "long"})
check("stable -> no rebuild", gs.needs_rebuild(st, 100100, "long", 500) == "",
      gs.needs_rebuild(st, 100100, "long", 500))
check("bias flip -> rebuild", "bias" in gs.needs_rebuild(st, 100100, "short", 500))
# step 500 at 0.5x mult => ATR 1000; recentre at 1.5 ATR = 1500
check("drift past 1.5 ATR -> rebuild",
      "drifted" in gs.needs_rebuild(st, 101600, "long", 500),
      gs.needs_rebuild(st, 101600, "long", 500))
check("drift within band -> no rebuild", gs.needs_rebuild(st, 101400, "long", 500) == "")
check("ATR regime shift -> rebuild", "ATR step" in gs.needs_rebuild(st, 100100, "long", 1200))

print("\n== state round-trip ==")
st = gs._empty_state()
st["centre"] = 99999.9
gs.save_state(st)
loaded = gs.load_state()
check("state persists", loaded["centre"] == 99999.9, loaded.get("centre"))
check("unknown-key forward compat", "cycles" in loaded)

print("\n" + "=" * 50)
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("all checks passed")
