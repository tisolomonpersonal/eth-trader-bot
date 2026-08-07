"""Central configuration — all settings come from environment variables."""
import os
from pathlib import Path

# ── Bybit ─────────────────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BYBIT_TESTNET    = os.environ.get("BYBIT_TESTNET",    "false").lower() == "true"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",   "")

# USDT → GHC rate, used only to annotate Telegram alerts with a local-currency
# figure. Display only — never used in sizing or risk maths.
GHC_RATE = float(os.environ.get("GHC_RATE", "12.0"))

# Only notify on profitable exits. Entries, losing exits, startup/shutdown
# notices and hourly summaries are logged but never sent.
TELEGRAM_PROFIT_ONLY = os.environ.get("TELEGRAM_PROFIT_ONLY", "true").lower() == "true"

# Escape hatch for the above: crashes, the daily kill switch and the bot dying
# still reach you. Set false for literally-profit-only, accepting that the bot
# can then fail silently while leveraged positions are open.
TELEGRAM_ALWAYS_CRITICAL = os.environ.get("TELEGRAM_ALWAYS_CRITICAL", "true").lower() == "true"

# ── Market ────────────────────────────────────────────────────────────────────
SYMBOL       = "BTCUSDT"
CATEGORY     = "linear"    # Perpetual futures — required for leverage + shorts
H4_INTERVAL  = "240"       # 4-hour candles (the only timeframe used)
# 250 candles = ~41 days. Enough for MA200 (200) + BB(20) warm-up + a few spare.
H4_LIMIT     = 250

# ── Position sizing ───────────────────────────────────────────────────────────
BTC_QTY  = float(os.environ.get("BTC_QTY",  "0.001"))  # Bybit minimum; ~$2.30 margin at 28x
LEVERAGE = int(os.environ.get("LEVERAGE",   "28"))      # 28× leverage

# ── Strategy parameters — BB Bollinger Short ──────────────────────────────────
# Bollinger Bands
BB_PERIOD  = int(os.environ.get("BB_PERIOD",  "20"))
BB_STD     = float(os.environ.get("BB_STD",   "2.0"))

# Moving averages
MA_SHORT   = int(os.environ.get("MA_SHORT",   "28"))    # MA28 — take-profit target
MA_LONG    = int(os.environ.get("MA_LONG",    "200"))   # MA200 — downtrend filter

# ATR stop cap: SL is the BB-touch candle's high, but never more than
# ATR_CAP_MULT × ATR(ATR_PERIOD) above entry.
ATR_PERIOD   = int(os.environ.get("ATR_PERIOD",   "14"))
ATR_CAP_MULT = float(os.environ.get("ATR_CAP_MULT", "1.5"))

# Small buffer added on top of the BB-touch candle high for the SL order,
# so the stop isn't sitting exactly at the wick tip.
SL_BUFFER_PCT = float(os.environ.get("SL_BUFFER_PCT", "0.05"))

# ── Risk limits ───────────────────────────────────────────────────────────────
MAX_DAILY_LOSS_USDT = float(os.environ.get("MAX_DAILY_LOSS_USDT", "4.0"))
MAX_TRADES_PER_DAY  = int(os.environ.get("MAX_TRADES_PER_DAY",    "5"))

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_MODE   = not bool(BYBIT_API_KEY and BYBIT_API_SECRET)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.environ.get("DATA_DIR",   "/data"))
STATE_FILE    = DATA_DIR / "bot_state.json"
HISTORY_FILE  = DATA_DIR / "trade_history.json"
MAX_HISTORY   = 200
