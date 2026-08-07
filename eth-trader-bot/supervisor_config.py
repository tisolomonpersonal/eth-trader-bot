"""
Configuration for the supervisor — the bot that watches the grid.

The supervisor never places orders. It widens or narrows the grid by writing an
override file the grid reads each cycle, and it records what happened so the
decision can be reviewed later.
"""
import os

from config import DATA_DIR

_true = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")

# ── Master switch ─────────────────────────────────────────────────────────────
SUPERVISOR_ENABLED = _true(os.environ.get("SUPERVISOR_ENABLED", "false"))

# Analyse and record, but never change the grid. Use this first.
SUPERVISOR_OBSERVE_ONLY = _true(os.environ.get("SUPERVISOR_OBSERVE_ONLY", "false"))

# ── Cadence ───────────────────────────────────────────────────────────────────
# How often to snapshot the grid. Cheap: one Bybit read plus one insert.
SUPERVISOR_CYCLE_SECONDS = int(os.environ.get("SUPERVISOR_CYCLE_SECONDS", "300"))

# How often to actually reconsider the grid width. Deliberately much slower than
# the observation cycle — a grid needs time to be judged, and re-deciding every
# few minutes would chase noise rather than learn from it.
SUPERVISOR_DECIDE_SECONDS = int(os.environ.get("SUPERVISOR_DECIDE_SECONDS", "3600"))

# ── Decision bounds ───────────────────────────────────────────────────────────
# The supervisor may only choose within this range, whatever it or the model
# concludes. 1 = defensive (one level each side), 2 = full grid.
SUPERVISOR_MIN_LEVELS = int(os.environ.get("SUPERVISOR_MIN_LEVELS", "1"))
SUPERVISOR_MAX_LEVELS = int(os.environ.get("SUPERVISOR_MAX_LEVELS", "2"))

# Trades needed before performance is treated as signal rather than noise.
SUPERVISOR_MIN_TRADES = int(os.environ.get("SUPERVISOR_MIN_TRADES", "6"))

# Lookback for the performance window.
SUPERVISOR_LOOKBACK_HOURS = int(os.environ.get("SUPERVISOR_LOOKBACK_HOURS", "24"))

# Narrow to minimum once the day's realised loss reaches this fraction of the
# grid's own daily limit. Acts well before the grid's kill switch fires.
SUPERVISOR_DRAWDOWN_FRACTION = float(os.environ.get("SUPERVISOR_DRAWDOWN_FRACTION", "0.5"))

# ── Supabase (REST; no direct Postgres, so no password and no psycopg2) ───────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# Publishable/anon key works if RLS allows the writes; a service key bypasses
# RLS. Never commit either — set them in the Zeabur dashboard.
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_TIMEOUT = int(os.environ.get("SUPABASE_TIMEOUT", "10"))

MEMORY_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

# ── Ollama (advisory only) ────────────────────────────────────────────────────
# On Zeabur this is the private DNS name of the Ollama service, e.g.
# http://ollama.zeabur.internal:11434
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "30"))

LLM_ENABLED = bool(OLLAMA_HOST)

# The model advises; it cannot act. Its answer is clamped to the bounds above
# and overruled by the deterministic rules whenever they disagree. A 3B model
# guessing at leveraged position sizing is not something to trust unchecked.
LLM_ADVISORY_ONLY = _true(os.environ.get("LLM_ADVISORY_ONLY", "true"))

# ── Local files ───────────────────────────────────────────────────────────────
# The grid reads this every cycle. Written atomically.
GRID_OVERRIDE_FILE = DATA_DIR / "grid_overrides.json"
SUPERVISOR_STATE_FILE = DATA_DIR / "supervisor_state.json"
