"""
Supervisor — watches the grid, decides how wide it should be, remembers why.

It never places an order. Its only lever is the number of levels per side,
written to an override file the grid reads each cycle. Two loops of different
speeds share one thread:

  observe  (SUPERVISOR_CYCLE_SECONDS)   snapshot the grid, mirror closed trades
  decide   (SUPERVISOR_DECIDE_SECONDS)  reconsider the width

Deciding is deliberately far slower than observing. A grid needs time before its
performance means anything; re-deciding every few minutes would chase noise and
churn orders, which costs fees and proves nothing.

Ollama advises. Deterministic rules decide. The model sees a compact summary and
returns a suggested width, but its answer is clamped to the configured bounds
and discarded whenever the rules disagree — a small local model guessing at
leveraged position sizing is not something to hand the controls to. Both the
suggestion and what actually happened are recorded, so the model can be judged
on its record rather than assumed good or bad.
"""
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import grid_client as gcl
import grid_config as gc
import grid_strategy
import indicators as ind_calc
import memory
import supervisor_config as sc
from logger import get_logger

log = get_logger("supervisor")


# ── Override file: the one thing the supervisor can change ───────────────────

def read_override() -> dict:
    """Current override, or {} when none is set. Never raises."""
    try:
        if sc.GRID_OVERRIDE_FILE.exists():
            return json.loads(sc.GRID_OVERRIDE_FILE.read_text())
    except Exception as e:
        log.error(f"Could not read grid override, ignoring it: {e}")
    return {}


def write_override(levels: int, reason: str) -> bool:
    """
    Write atomically — the grid reads this file on its own schedule, and a torn
    read would hand it a nonsense width.
    """
    payload = {
        "levels_per_side": levels,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        sc.GRID_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = sc.GRID_OVERRIDE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(sc.GRID_OVERRIDE_FILE)
        return True
    except Exception as e:
        log.error(f"Could not write grid override: {e}")
        return False


def clamp(levels: int) -> int:
    return max(sc.SUPERVISOR_MIN_LEVELS, min(sc.SUPERVISOR_MAX_LEVELS, int(levels)))


def current_levels() -> int:
    """Width the grid is actually using right now."""
    ov = read_override()
    lv = ov.get("levels_per_side")
    if isinstance(lv, int) and sc.SUPERVISOR_MIN_LEVELS <= lv <= sc.SUPERVISOR_MAX_LEVELS:
        return lv
    return max(gc.GRID_LEVELS_ABOVE, gc.GRID_LEVELS_BELOW)


# ── Observation ───────────────────────────────────────────────────────────────

def collect_metrics() -> dict:
    """One snapshot of grid and market state. Raises if Bybit is unreachable."""
    df = gcl.get_klines()
    price = float(df["close"].iloc[-1])
    bias, ema_fast, ema_slow = grid_strategy.compute_bias(df)
    atr = float(ind_calc.atr(df, gc.GRID_ATR_PERIOD))

    positions = gcl.get_positions()
    state = grid_strategy.load_state()

    try:
        equity = gcl.get_balance().get("equity", 0.0)
    except Exception:
        equity = None

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "price": round(price, 2),
        "bias": bias,
        "ema_fast": round(ema_fast, 2),
        "ema_slow": round(ema_slow, 2),
        "atr": round(atr, 2),
        "step": round(atr * gc.GRID_ATR_MULT, 2),
        "levels_per_side": current_levels(),
        "long_size": gcl.position_size(positions, "long"),
        "short_size": gcl.position_size(positions, "short"),
        "open_orders": len(gcl.get_open_orders()),
        "realised_today": round(float(state.get("realised_today") or 0), 4),
        "equity": round(equity, 4) if equity is not None else None,
        "halted": bool(state.get("halted")),
    }


def mirror_closed_trades(metrics: dict) -> int:
    """
    Copy new closed trades from Bybit into memory, tagged with the conditions
    they happened under. Skips ids already stored, so polling overlapping
    windows cannot double-count.
    """
    if not memory.available():
        return 0

    state = grid_strategy.load_state()
    start_ms = int(state.get("day_start_ms") or 0)
    if not start_ms:
        return 0

    try:
        raw = gcl.get_closed_pnl_records(start_ms)
    except Exception as e:
        log.warning(f"Could not fetch closed trades: {e}")
        return 0

    known = memory.known_trade_ids()
    n = 0
    for r in raw:
        oid = r.get("orderId")
        if not oid or oid in known:
            continue
        ts = r.get("updatedTime") or r.get("createdTime")
        try:
            closed_at = datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat()
        except Exception:
            closed_at = datetime.now(timezone.utc).isoformat()
        if memory.record_trade({
            "order_id": oid,
            "closed_at": closed_at,
            "side": r.get("side"),
            "qty": float(r.get("qty") or 0),
            "entry_price": float(r.get("avgEntryPrice") or 0),
            "exit_price": float(r.get("avgExitPrice") or 0),
            "pnl": float(r.get("closedPnl") or 0),
            "levels_per_side": metrics.get("levels_per_side"),
            "bias": metrics.get("bias"),
            "atr": metrics.get("atr"),
        }):
            n += 1
    return n


# ── The model's opinion (advisory) ───────────────────────────────────────────

_PROMPT = """You tune a hedged BTC perpetual grid bot. It keeps N price levels \
above and N below the current price. Wider (N={max_lv}) captures more when price \
oscillates; narrower (N={min_lv}) risks less when the grid is losing or volatile.

Current state:
- Grid width now: {levels} level(s) per side
- Trend bias: {bias} (EMA fast {ema_fast}, slow {ema_slow})
- ATR: {atr} ({atr_pct}% of price)
- Realised PnL today: {realised} USDT
- Open position: long {long_size} BTC, short {short_size} BTC

Recent performance by grid width (last {hours}h):
{perf}

Reply with ONLY this JSON, no other text:
{{"levels": {min_lv} or {max_lv}, "confidence": 0-100, "reason": "one short sentence"}}"""


def _parse_model_reply(text: str) -> Optional[dict]:
    """Pull the JSON object out of a model reply that may have prose around it."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if "levels" not in d:
        return None
    try:
        return {
            "levels": clamp(int(d["levels"])),
            "confidence": int(d.get("confidence") or 0),
            "reason": str(d.get("reason") or "")[:300],
        }
    except Exception:
        return None


def ask_model(metrics: dict, perf: dict) -> Optional[dict]:
    """Ask Ollama for a width. Returns None on any failure — never raises."""
    if not sc.LLM_ENABLED:
        return None

    price = metrics.get("price") or 1
    prompt = _PROMPT.format(
        min_lv=sc.SUPERVISOR_MIN_LEVELS,
        max_lv=sc.SUPERVISOR_MAX_LEVELS,
        levels=metrics.get("levels_per_side"),
        bias=metrics.get("bias"),
        ema_fast=metrics.get("ema_fast"),
        ema_slow=metrics.get("ema_slow"),
        atr=metrics.get("atr"),
        atr_pct=round((metrics.get("atr") or 0) / price * 100, 2),
        realised=metrics.get("realised_today"),
        long_size=metrics.get("long_size"),
        short_size=metrics.get("short_size"),
        hours=sc.SUPERVISOR_LOOKBACK_HOURS,
        perf=json.dumps(perf) if perf else "no history yet",
    )

    try:
        r = requests.post(
            f"{sc.OLLAMA_HOST}/api/generate",
            json={
                "model": sc.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            },
            timeout=sc.OLLAMA_TIMEOUT,
        )
        if r.status_code >= 300:
            log.warning(f"Ollama returned {r.status_code}: {r.text[:200]}")
            return None
        return _parse_model_reply(r.json().get("response", ""))
    except Exception as e:
        log.warning(f"Ollama unavailable: {e}")
        return None


# ── Rules: these decide ───────────────────────────────────────────────────────

def decide_by_rules(metrics: dict, perf: dict) -> tuple:
    """
    Returns (levels, reason). Checked in priority order, most protective first.
    """
    min_lv, max_lv = sc.SUPERVISOR_MIN_LEVELS, sc.SUPERVISOR_MAX_LEVELS

    # 1. Already halted — the grid flattened itself, stay minimal.
    if metrics.get("halted"):
        return min_lv, "grid is halted"

    # 2. Losing meaningfully today. Narrow well before the kill switch fires.
    realised = float(metrics.get("realised_today") or 0)
    threshold = -abs(gc.GRID_MAX_DAILY_LOSS_USDT) * sc.SUPERVISOR_DRAWDOWN_FRACTION
    if realised <= threshold:
        return min_lv, (f"realised {realised:+.2f} past {threshold:.2f} "
                        f"({sc.SUPERVISOR_DRAWDOWN_FRACTION:.0%} of daily limit)")

    # 3. No conviction on direction — a trend-following grid has no edge here.
    if metrics.get("bias") == "neutral":
        return min_lv, "trend bias is neutral"

    # 4. Position already near the per-side cap; more levels cannot fill anyway
    #    and would only sit as unfillable orders.
    cap = gc.GRID_MAX_POSITION_BTC
    if cap > 0 and max(metrics.get("long_size") or 0,
                       metrics.get("short_size") or 0) >= cap - gc.GRID_QTY / 2:
        return min_lv, "position at per-side cap"

    # 5. Let history speak, once there is enough of it to mean something.
    if perf:
        wide = perf.get(max_lv, {})
        narrow = perf.get(min_lv, {})
        if wide.get("trades", 0) >= sc.SUPERVISOR_MIN_TRADES:
            if wide["pnl"] < 0 and narrow.get("pnl", 0) >= 0:
                return min_lv, (f"{max_lv}-level lost {wide['pnl']:+.2f} while "
                                f"{min_lv}-level made {narrow.get('pnl', 0):+.2f}")
            if wide["pnl"] < 0:
                return min_lv, f"{max_lv}-level realised {wide['pnl']:+.2f} over {wide['trades']} trades"
        if narrow.get("trades", 0) >= sc.SUPERVISOR_MIN_TRADES and narrow["pnl"] > 0:
            if wide.get("trades", 0) < sc.SUPERVISOR_MIN_TRADES:
                return max_lv, f"{min_lv}-level profitable ({narrow['pnl']:+.2f}); trying wider"

    # 6. Nothing wrong — run the full grid.
    return max_lv, "conditions normal"


def decide(metrics: dict, perf: dict) -> dict:
    """Combine rules and model into one recorded decision."""
    before = metrics.get("levels_per_side")
    rules_levels, rules_reason = decide_by_rules(metrics, perf)

    suggestion = ask_model(metrics, perf)
    llm_levels = suggestion["levels"] if suggestion else None
    llm_reason = suggestion["reason"] if suggestion else None

    after = rules_levels
    if llm_levels is None:
        source = "rules"
        reason = rules_reason
    elif llm_levels == rules_levels:
        source = "rules"           # agreement; rules still own the call
        reason = f"{rules_reason} (model agrees)"
    elif sc.LLM_ADVISORY_ONLY:
        source = "rules_override_llm"
        reason = f"{rules_reason} (model wanted {llm_levels}, overruled)"
    else:
        source = "llm"
        after = clamp(llm_levels)
        reason = llm_reason or "model decision"

    return {
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "levels_before": before,
        "levels_after": after,
        "changed": after != before,
        "source": source,
        "reason": reason,
        "llm_suggestion": llm_levels,
        "llm_reason": llm_reason,
        "metrics": {**metrics, "performance_by_levels": perf},
    }


# ── Loop ──────────────────────────────────────────────────────────────────────

_running = True


def stop() -> None:
    global _running
    _running = False


def run_cycle(force_decide: bool = False) -> dict:
    """One observation, and a decision when due. Returns what it did."""
    metrics = collect_metrics()

    if memory.available():
        memory.record_observation(metrics)
        n = mirror_closed_trades(metrics)
        if n:
            log.info(f"Recorded {n} newly closed trade(s)")

    result = {"metrics": metrics, "decision": None}
    if not force_decide:
        return result

    perf = memory.performance_by_levels(sc.SUPERVISOR_LOOKBACK_HOURS) if memory.available() else {}
    decision = decide(metrics, perf)
    result["decision"] = decision

    if decision["changed"] and not sc.SUPERVISOR_OBSERVE_ONLY:
        if write_override(decision["levels_after"], decision["reason"]):
            log.info(
                f"Grid width {decision['levels_before']} -> "
                f"{decision['levels_after']} ({decision['source']}): {decision['reason']}"
            )
    elif decision["changed"]:
        log.info(
            f"[observe-only] would set width {decision['levels_before']} -> "
            f"{decision['levels_after']}: {decision['reason']}"
        )
        decision["changed"] = False
        decision["reason"] = f"[observe-only] {decision['reason']}"
    else:
        log.info(f"Grid width stays {decision['levels_after']}: {decision['reason']}")

    if memory.available():
        memory.record_decision(decision)

    return result


def run_supervisor() -> None:
    """Supervisor loop — runs in its own thread."""
    log.info(
        f"Supervisor starting | observe {sc.SUPERVISOR_CYCLE_SECONDS}s "
        f"decide {sc.SUPERVISOR_DECIDE_SECONDS}s | "
        f"levels {sc.SUPERVISOR_MIN_LEVELS}-{sc.SUPERVISOR_MAX_LEVELS} | "
        f"memory={memory.available()} llm={sc.LLM_ENABLED} "
        f"({sc.OLLAMA_MODEL if sc.LLM_ENABLED else 'none'}) | "
        f"observe_only={sc.SUPERVISOR_OBSERVE_ONLY}"
    )

    last_decision = 0.0
    error_streak = 0

    while _running:
        cycle_start = time.time()
        try:
            due = (cycle_start - last_decision) >= sc.SUPERVISOR_DECIDE_SECONDS
            run_cycle(force_decide=due)
            if due:
                last_decision = cycle_start
            error_streak = 0
        except Exception as e:
            error_streak += 1
            log.error(f"Supervisor cycle error (streak={error_streak}): {e}")
            # The supervisor is advisory. If it cannot run, the grid carries on
            # with whatever width it last had, so back off quietly rather than
            # alerting — this must never be the thing that wakes anyone up.
            time.sleep(min(60 * error_streak, 600))
            continue

        time.sleep(max(10, sc.SUPERVISOR_CYCLE_SECONDS - (time.time() - cycle_start)))

    log.info("Supervisor loop exited cleanly")
