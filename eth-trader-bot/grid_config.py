"""
Configuration for the trend-following hedged grid bot (BTC perpetual).

Kept separate from config.py because the grid runs in Bybit **hedge mode**
(positionIdx 1/2) while the BB-short strategy in bybit_client.py runs in
**one-way mode** (positionIdx 0). Position mode is an account+symbol level
setting, so the two cannot share an API key on the same symbol.

Point GRID_API_KEY / GRID_API_SECRET at a separate Bybit sub-account.
"""
import os

from config import DATA_DIR

_true = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")

# ── Master switch ─────────────────────────────────────────────────────────────
GRID_ENABLED = _true(os.environ.get("GRID_ENABLED", "true"))

# Logs every order it would place, sends nothing. Off by default.
GRID_DRY_RUN = _true(os.environ.get("GRID_DRY_RUN", "false"))

# ── Credentials ───────────────────────────────────────────────────────────────
# Falls back to the main keys only if grid-specific ones are absent, so a
# single-account setup still works — but see the module docstring first.
GRID_API_KEY    = os.environ.get("GRID_API_KEY")    or os.environ.get("BYBIT_API_KEY", "")
GRID_API_SECRET = os.environ.get("GRID_API_SECRET") or os.environ.get("BYBIT_API_SECRET", "")

# Testnet flag is grid-specific; defaults to the account-wide BYBIT_TESTNET.
GRID_TESTNET = _true(os.environ.get("GRID_TESTNET", os.environ.get("BYBIT_TESTNET", "false")))

# No keys → paper mode, same convention as config.PAPER_MODE.
GRID_PAPER_MODE = not bool(GRID_API_KEY and GRID_API_SECRET)

# ── Market ────────────────────────────────────────────────────────────────────
GRID_SYMBOL   = os.environ.get("GRID_SYMBOL", "BTCUSDT")
GRID_CATEGORY = "linear"

# Timeframe the EMA trend filter and ATR spacing are computed on.
GRID_INTERVAL = os.environ.get("GRID_INTERVAL", "15")
GRID_KLINE_LIMIT = int(os.environ.get("GRID_KLINE_LIMIT", "400"))  # > EMA slow + warm-up

# ── Sizing ────────────────────────────────────────────────────────────────────
GRID_QTY      = float(os.environ.get("GRID_QTY",      "0.001"))  # BTC per grid level
GRID_LEVERAGE = int(os.environ.get("GRID_LEVERAGE",   "28"))

# ── Grid geometry ─────────────────────────────────────────────────────────────
GRID_LEVELS_ABOVE = int(os.environ.get("GRID_LEVELS_ABOVE", "2"))
GRID_LEVELS_BELOW = int(os.environ.get("GRID_LEVELS_BELOW", "2"))

# Level spacing = GRID_ATR_MULT x ATR(GRID_ATR_PERIOD).
#
# This is set by fees, not by taste. A round trip pays the maker fee twice on
# notional, so at 0.02% and a $65k price roughly $26 per BTC has to be cleared
# before a capture is worth anything. Sizing bigger does not help — fees scale
# with notional exactly as profit does, so the ratio depends only on how far
# price travels between levels. At 0.5x ATR (~$56) fees took 47% of every
# winning trade; at 2.5x (~$278) they take about 9%.
GRID_ATR_PERIOD = int(os.environ.get("GRID_ATR_PERIOD", "14"))
GRID_ATR_MULT   = float(os.environ.get("GRID_ATR_MULT", "2.5"))

# Rebuild the grid when price drifts this many ATRs from the grid centre.
# Must stay beyond the outer level (GRID_ATR_MULT x levels = 5 ATR by default),
# or the grid recentres before its own levels can ever fill.
GRID_RECENTER_ATR = float(os.environ.get("GRID_RECENTER_ATR", "6.0"))

# ── Trend filter ──────────────────────────────────────────────────────────────
GRID_EMA_FAST = int(os.environ.get("GRID_EMA_FAST", "50"))
GRID_EMA_SLOW = int(os.environ.get("GRID_EMA_SLOW", "200"))

# Neutral band: require the EMAs to be separated by at least this fraction of
# price before committing to a bias. Prevents flip-flopping when they're glued
# together in chop. 0 disables the band.
GRID_EMA_MIN_SEP_PCT = float(os.environ.get("GRID_EMA_MIN_SEP_PCT", "0.05"))

# On a bias flip, market-close the position that is now counter-trend.
GRID_CLOSE_COUNTER_ON_FLIP = _true(os.environ.get("GRID_CLOSE_COUNTER_ON_FLIP", "true"))

# ── Risk limits ───────────────────────────────────────────────────────────────
# Hard cap on accumulated size per side, in BTC.
GRID_MAX_POSITION_BTC = float(os.environ.get("GRID_MAX_POSITION_BTC", "0.002"))

# Kill switch: realised loss on the day (UTC) beyond this halts the grid —
# cancels all orders, flattens both sides, waits for the next UTC day.
GRID_MAX_DAILY_LOSS_USDT = float(os.environ.get("GRID_MAX_DAILY_LOSS_USDT", "3.0"))

# Backstop stop-loss on the *net* position, as a multiple of ATR from the
# average entry. 0 disables it. This is separate from the grid's own logic —
# it exists so a one-way run does not accumulate indefinitely.
#
# Must also sit beyond the outer level, otherwise it closes the position before
# the far level has had a chance to fill. Liquidation at 28x is around 20 ATR
# away, so 8 leaves real margin.
GRID_STOP_ATR_MULT = float(os.environ.get("GRID_STOP_ATR_MULT", "8.0"))

# ── Loop ──────────────────────────────────────────────────────────────────────
GRID_CYCLE_SECONDS = int(os.environ.get("GRID_CYCLE_SECONDS", "30"))

# ── Order tagging ─────────────────────────────────────────────────────────────
# Every order this bot places carries this orderLinkId prefix. Reconciliation
# only ever cancels orders it can prove are its own, so a strategy sharing the
# symbol is never disturbed.
GRID_ORDER_PREFIX = os.environ.get("GRID_ORDER_PREFIX", "gr")

# ── Persistence ───────────────────────────────────────────────────────────────
GRID_STATE_FILE   = DATA_DIR / "grid_state.json"
GRID_HISTORY_FILE = DATA_DIR / "grid_history.json"
GRID_MAX_HISTORY  = 500
