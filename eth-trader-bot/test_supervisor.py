"""Offline checks for the supervisor — no network, no keys, no database."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sup_test_")
os.environ["SUPERVISOR_ENABLED"] = "true"

import json

import grid_config as gc
import grid_strategy as gs
import supervisor as sup
import supervisor_config as sc

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILS.append(label)


def base_metrics(**kw):
    m = {
        "price": 64800.0, "bias": "long", "ema_fast": 64500.0, "ema_slow": 64400.0,
        "atr": 110.0, "step": 55.0, "levels_per_side": 2,
        "long_size": 0.0, "short_size": 0.0, "open_orders": 4,
        "realised_today": 0.0, "equity": 9.79, "halted": False,
    }
    m.update(kw)
    return m


print("\n== override file round-trip ==")
if sc.GRID_OVERRIDE_FILE.exists():
    sc.GRID_OVERRIDE_FILE.unlink()
check("no override -> configured width", sup.current_levels() == 2, sup.current_levels())
sup.write_override(1, "test")
check("override applies", sup.current_levels() == 1, sup.current_levels())
check("grid sees it too", gs.effective_levels() == (1, 1), gs.effective_levels())

print("\n== grid builds fewer levels under override ==")
below, above = gs.build_levels(64800, 55)
check("1 level each side", len(below) == 1 and len(above) == 1, (below, above))
sup.write_override(2, "test")
below, above = gs.build_levels(64800, 55)
check("2 levels each side", len(below) == 2 and len(above) == 2, (below, above))

print("\n== malformed overrides fail safe ==")
for bad in ['{"levels_per_side": 99}', '{"levels_per_side": 0}',
            '{"levels_per_side": "two"}', 'not json at all', '{}']:
    sc.GRID_OVERRIDE_FILE.write_text(bad)
    check(f"ignored: {bad[:28]}", gs.effective_levels() == (2, 2), gs.effective_levels())
sc.GRID_OVERRIDE_FILE.unlink()

print("\n== rules: protective cases ==")
lv, why = sup.decide_by_rules(base_metrics(halted=True), {})
check("halted -> narrow", lv == 1, why)

loss = -abs(gc.GRID_MAX_DAILY_LOSS_USDT) * sc.SUPERVISOR_DRAWDOWN_FRACTION - 0.01
lv, why = sup.decide_by_rules(base_metrics(realised_today=loss), {})
check("drawdown -> narrow", lv == 1, why)

lv, why = sup.decide_by_rules(base_metrics(bias="neutral"), {})
check("neutral bias -> narrow", lv == 1, why)

lv, why = sup.decide_by_rules(base_metrics(long_size=gc.GRID_MAX_POSITION_BTC), {})
check("at position cap -> narrow", lv == 1, why)

lv, why = sup.decide_by_rules(base_metrics(), {})
check("normal -> wide", lv == 2, why)

print("\n== rules: learning from history ==")
perf_bad = {2: {"trades": 10, "pnl": -3.2, "wins": 2, "win_rate": 0.2},
            1: {"trades": 8, "pnl": 1.1, "wins": 5, "win_rate": 0.625}}
lv, why = sup.decide_by_rules(base_metrics(), perf_bad)
check("wide losing, narrow winning -> narrow", lv == 1, why)

perf_thin = {2: {"trades": 2, "pnl": -3.0, "wins": 0, "win_rate": 0.0}}
lv, why = sup.decide_by_rules(base_metrics(), perf_thin)
check("too few trades -> ignore history", lv == 2, why)

perf_good = {1: {"trades": 9, "pnl": 2.4, "wins": 6, "win_rate": 0.667}}
lv, why = sup.decide_by_rules(base_metrics(), perf_good)
check("narrow proven, wide untried -> widen", lv == 2, why)

print("\n== model reply parsing ==")
check("plain json", sup._parse_model_reply('{"levels":1,"confidence":80,"reason":"x"}')["levels"] == 1)
check("json in prose", sup._parse_model_reply('Sure!\n{"levels": 2, "reason": "y"}\nHope that helps')["levels"] == 2)
check("clamped high", sup._parse_model_reply('{"levels": 47}')["levels"] == 2)
check("clamped low", sup._parse_model_reply('{"levels": -3}')["levels"] == 1)
check("garbage -> None", sup._parse_model_reply("no json here") is None)
check("missing key -> None", sup._parse_model_reply('{"confidence": 90}') is None)

print("\n== model is advisory ==")
sup.write_override(2, "setup")
_real = sup.ask_model
sup.ask_model = lambda m, p: {"levels": 1, "confidence": 99, "reason": "model says narrow"}
d = sup.decide(base_metrics(), {})          # rules say wide, model says narrow
check("rules win over model", d["levels_after"] == 2, d)
check("source records the override", d["source"] == "rules_override_llm", d["source"])
check("model suggestion still recorded", d["llm_suggestion"] == 1, d)

sup.ask_model = lambda m, p: None
d = sup.decide(base_metrics(), {})
check("no model -> rules alone", d["source"] == "rules" and d["levels_after"] == 2, d)

sup.ask_model = lambda m, p: {"levels": 2, "confidence": 70, "reason": "agree"}
d = sup.decide(base_metrics(), {})
check("agreement noted", "model agrees" in d["reason"], d["reason"])
sup.ask_model = _real

print("\n== decide() shape is insertable ==")
d = sup.decide(base_metrics(), {})
for k in ("decided_at", "levels_before", "levels_after", "changed",
          "source", "reason", "llm_suggestion", "llm_reason", "metrics"):
    check(f"has {k}", k in d)
check("metrics json-serialisable", isinstance(json.dumps(d["metrics"], default=str), str))

print("\n" + "=" * 50)
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("all checks passed")
