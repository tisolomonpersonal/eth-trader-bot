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

# ── AI providers (first available wins) ──────────────────────────────────────
OLLAMA_HOST      = os.environ.get("OLLAMA_HOST",      "http://localhost:11434")
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL",     "qwen2.5:3b")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY",     "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY",   "")
OPENAI_BASE_URL  = os.environ.get("OPENAI_BASE_URL",  "https://api.openai.com/v1")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL",     "gpt-4o-mini")

# ── Market ────────────────────────────────────────────────────────────────────
SYMBOL       = "BTCUSDT"
CATEGORY     = "linear"    # Perpetual futures — required for leverage + shorts
H4_INTERVAL  = "240"       # 4-hour candles (the only timeframe used)
# 250 candles = ~41 days. Enough for MA200 (200) + BB(20) warm-up + a few spare.
H4_LIMIT     = 250

# ── Position sizing ───────────────────────────────────────────────────────────
BTC_QTY  = float(os.environ.get("BTC_QTY",  "0.004"))  # Fixed contract size
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
MAX_DAILY_LOSS_USDT = float(os.environ.get("MAX_DAILY_LOSS_USDT", "50.0"))
MAX_TRADES_PER_DAY  = int(os.environ.get("MAX_TRADES_PER_DAY",    "5"))

# ── TradFi (Bybit's stock/forex/commodity CFD perpetuals) ─────────────────────
TRADFI_CATEGORY   = "linear"
TRADFI_SYMBOL     = os.environ.get("TRADFI_SYMBOL",     "XAUUSD")
TRADFI_MODE       = os.environ.get("TRADFI_MODE",       "zero_fee").lower()
TRADFI_INTERVAL   = os.environ.get("TRADFI_INTERVAL",   "15")
TRADFI_ACCOUNT_TYPE = "UNIFIED"

TRADFI_ENABLED = os.environ.get("TRADFI_ENABLED", "false").lower() == "true"

TRADFI_MAX_INVESTMENT_USDT  = float(os.environ.get("TRADFI_MAX_INVESTMENT_USDT", "65"))
TRADFI_STOP_LOSS_PCT        = float(os.environ.get("TRADFI_STOP_LOSS_PCT",       "1.5"))
TRADFI_TAKE_PROFIT_PCT      = float(os.environ.get("TRADFI_TAKE_PROFIT_PCT",     "3.0"))
TRADFI_MAX_DAILY_LOSS_PCT   = float(os.environ.get("TRADFI_MAX_DAILY_LOSS_PCT",  "5.0"))
TRADFI_RISK_PER_TRADE_PCT   = float(os.environ.get("TRADFI_RISK_PER_TRADE_PCT",  "100"))
TRADFI_ALLOW_AVERAGING_DOWN = os.environ.get("TRADFI_ALLOW_AVERAGING_DOWN", "false").lower() == "true"
TRADFI_MIN_AI_CONFIDENCE    = int(os.environ.get("TRADFI_MIN_AI_CONFIDENCE",     "65"))
TRADFI_CYCLE_SECONDS        = int(os.environ.get("TRADFI_CYCLE_SECONDS",         "300"))

# ── MetaTrader 5 ──────────────────────────────────────────────────────────────
MT5_HOST      = os.environ.get("MT5_HOST", "")
MT5_PORT      = int(os.environ.get("MT5_PORT", "8001"))
MT5_LOGIN     = os.environ.get("MT5_LOGIN", "")
MT5_PASSWORD  = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER    = os.environ.get("MT5_SERVER", "")
MT5_DEVIATION = int(os.environ.get("MT5_DEVIATION", "20"))
MT5_MAGIC     = int(os.environ.get("MT5_MAGIC", "770177"))
TRADFI_MARKET_MAX_TICK_AGE_HRS = float(os.environ.get("TRADFI_MARKET_MAX_TICK_AGE_HRS", "6"))

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_MODE   = not bool(BYBIT_API_KEY and BYBIT_API_SECRET)
TRADFI_PAPER = not bool(MT5_HOST and MT5_LOGIN and MT5_PASSWORD and MT5_SERVER)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.environ.get("DATA_DIR",   "/data"))
STATE_FILE    = DATA_DIR / "bot_state.json"
HISTORY_FILE  = DATA_DIR / "trade_history.json"
MAX_HISTORY   = 200

TRADFI_STATE_FILE   = DATA_DIR / "tradfi_state.json"
TRADFI_HISTORY_FILE = DATA_DIR / "tradfi_trade_history.json"
