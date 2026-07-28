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

# ── AI providers (first available wins) ──────────────────────────────────────
OLLAMA_HOST      = os.environ.get("OLLAMA_HOST",      "http://localhost:11434")
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL",     "qwen2.5:3b")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY",     "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY",   "")
OPENAI_BASE_URL  = os.environ.get("OPENAI_BASE_URL",  "https://api.openai.com/v1")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL",     "gpt-4o-mini")

# ── Market ────────────────────────────────────────────────────────────────────
SYMBOL        = "BTCUSDT"
CATEGORY      = "linear"       # Perpetual futures — required for leverage + shorts
H1_INTERVAL   = "60"           # 1-hour candles for signal detection
M5_INTERVAL   = "5"            # 5-minute candles for entry execution
H1_LIMIT      = 100            # 100 H1 candles (100 hours of history)
M5_LIMIT      = 100            # 100 M5 candles (~8 hours)

# ── Position sizing ───────────────────────────────────────────────────────────
BTC_QTY       = float(os.environ.get("BTC_QTY",  "0.004"))  # Fixed contract size
LEVERAGE      = int(os.environ.get("LEVERAGE",   "25"))      # 25× leverage

# ── Strategy parameters ───────────────────────────────────────────────────────
# Fibonacci retracement zone for M5 entry (61.8% – 70.5% of H1 candle)
FIB_ENTRY_LOW  = 0.618
FIB_ENTRY_HIGH = 0.705

# SL buffer beyond H1 candle extreme (0.05% — tight, above/below the wick)
SL_BUFFER_PCT  = float(os.environ.get("SL_BUFFER_PCT", "0.05"))

# Minimum reward:risk ratio to accept a TP target
MIN_RR         = float(os.environ.get("MIN_RR", "1.5"))

# Max RR multiple used to set hard TP when no swing level is found
DEFAULT_RR     = float(os.environ.get("DEFAULT_RR", "2.0"))

# Hours after which a pending H1 signal expires if no M5 entry triggered
SIGNAL_EXPIRY_HOURS = float(os.environ.get("SIGNAL_EXPIRY_HOURS", "4.0"))

# Structural block filter: if H1 signal extreme is within this % of the
# 50-bar lookback extreme, treat as a major HTF level and skip the setup.
STRUCTURAL_FILTER_PCT = float(os.environ.get("STRUCTURAL_FILTER_PCT", "0.3"))

# Minimum AI confidence to confirm a trade (directional signal still required)
MIN_AI_CONFIDENCE = int(os.environ.get("MIN_AI_CONFIDENCE", "55"))

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
