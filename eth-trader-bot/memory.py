"""
Supabase-backed memory for the supervisor.

Talks to PostgREST over HTTPS rather than opening a Postgres connection. That
avoids shipping psycopg2 (a compiled dependency) in the image and avoids needing
the database password — the API key is enough.

Every call is best-effort. Memory is for learning, not for trading decisions in
the moment: if Supabase is unreachable the supervisor still runs on live Bybit
data, it just cannot consult history. Nothing here raises into the caller.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import supervisor_config as sc
from logger import get_logger

log = get_logger("memory")

# Tables, created by supabase_schema.sql.
T_OBSERVATIONS = "grid_observations"
T_TRADES = "grid_trades"
T_DECISIONS = "supervisor_decisions"

_warned = set()


def _warn_once(key: str, msg: str) -> None:
    """Log a recurring failure once rather than every cycle."""
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _headers() -> dict:
    return {
        "apikey": sc.SUPABASE_KEY,
        "Authorization": f"Bearer {sc.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def available() -> bool:
    return sc.MEMORY_ENABLED


def _insert(table: str, row: dict) -> bool:
    if not sc.MEMORY_ENABLED:
        return False
    try:
        r = requests.post(
            f"{sc.SUPABASE_URL}/rest/v1/{table}",
            headers=_headers(),
            data=json.dumps(row, default=str),
            timeout=sc.SUPABASE_TIMEOUT,
        )
        if r.status_code >= 300:
            _warn_once(f"insert:{table}:{r.status_code}",
                       f"Supabase insert into {table} failed "
                       f"({r.status_code}): {r.text[:200]}")
            return False
        return True
    except Exception as e:
        _warn_once(f"insert:{table}:exc", f"Supabase insert into {table} failed: {e}")
        return False


def _select(table: str, params: dict) -> list:
    if not sc.MEMORY_ENABLED:
        return []
    try:
        headers = _headers()
        headers.pop("Prefer", None)          # we do want the rows back
        r = requests.get(
            f"{sc.SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=params,
            timeout=sc.SUPABASE_TIMEOUT,
        )
        if r.status_code >= 300:
            _warn_once(f"select:{table}:{r.status_code}",
                       f"Supabase select from {table} failed "
                       f"({r.status_code}): {r.text[:200]}")
            return []
        return r.json()
    except Exception as e:
        _warn_once(f"select:{table}:exc", f"Supabase select from {table} failed: {e}")
        return []


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ── Writes ────────────────────────────────────────────────────────────────────

def record_observation(obs: dict) -> bool:
    """Snapshot of the grid at one moment."""
    return _insert(T_OBSERVATIONS, obs)


def record_trade(trade: dict) -> bool:
    """One closed trade, as reported by Bybit."""
    return _insert(T_TRADES, trade)


def record_decision(decision: dict) -> bool:
    """A width change, or a considered decision to leave it alone."""
    return _insert(T_DECISIONS, decision)


def known_trade_ids(hours: int = 48) -> set:
    """Bybit order ids already stored, so re-polling does not duplicate rows."""
    rows = _select(T_TRADES, {
        "select": "order_id",
        "closed_at": f"gte.{_since(hours)}",
        "limit": "500",
    })
    return {r["order_id"] for r in rows if r.get("order_id")}


# ── Reads ─────────────────────────────────────────────────────────────────────

def recent_trades(hours: int) -> list:
    return _select(T_TRADES, {
        "select": "*",
        "closed_at": f"gte.{_since(hours)}",
        "order": "closed_at.desc",
        "limit": "200",
    })


def recent_decisions(limit: int = 20) -> list:
    return _select(T_DECISIONS, {
        "select": "*",
        "order": "decided_at.desc",
        "limit": str(limit),
    })


def performance_by_levels(hours: int) -> dict:
    """
    Realised PnL grouped by how wide the grid was at the time.

    This is the whole point of keeping memory: it answers "did 2 levels or 1
    level actually do better lately", which no amount of live data can tell you.
    """
    trades = recent_trades(hours)
    out = {}
    for t in trades:
        key = t.get("levels_per_side")
        if key is None:
            continue
        b = out.setdefault(int(key), {"trades": 0, "pnl": 0.0, "wins": 0})
        b["trades"] += 1
        pnl = float(t.get("pnl") or 0)
        b["pnl"] += pnl
        if pnl > 0:
            b["wins"] += 1
    for b in out.values():
        b["pnl"] = round(b["pnl"], 4)
        b["win_rate"] = round(b["wins"] / b["trades"], 3) if b["trades"] else 0.0
    return out


def health() -> dict:
    """Cheap connectivity probe for /supervisor/status."""
    if not sc.MEMORY_ENABLED:
        return {"enabled": False, "reason": "SUPABASE_URL / SUPABASE_KEY not set"}
    rows = _select(T_OBSERVATIONS, {"select": "id", "limit": "1"})
    return {"enabled": True, "reachable": isinstance(rows, list)}
