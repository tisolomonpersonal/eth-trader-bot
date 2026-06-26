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
SYMBOL        = "BNBUSDT"
CATEGORY      = "spot"
INTERVAL      = "1"          # 1-minute candles
CANDLE_LIMIT  = 250          # 250 candles covers EMA200 warmup

# ── Risk & position sizing ────────────────────────────────────────────────────
GHC_RATE              = float(os.environ.get("GHC_RATE",              "15.5"))
MAX_INVESTMENT_GHC    = float(os.environ.get("MAX_INVESTMENT_GHC",    "1000"))
MAX_INVESTMENT_USDT   = MAX_INVESTMENT_GHC / GHC_RATE   # ~64.5 USDT default
STOP_LOSS_PCT         = float(os.environ.get("STOP_LOSS_PCT",         "2.0"))
TAKE_PROFIT_PCT       = float(os.environ.get("TAKE_PROFIT_PCT",       "4.0"))
MAX_DAILY_LOSS_PCT    = float(os.environ.get("MAX_DAILY_LOSS_PCT",    "5.0"))
RISK_PER_TRADE_PCT    = float(os.environ.get("RISK_PER_TRADE_PCT",    "100"))  # % of max investment
ALLOW_AVERAGING_DOWN  = os.environ.get("ALLOW_AVERAGING_DOWN", "false").lower() == "true"
MIN_AI_CONFIDENCE     = int(os.environ.get("MIN_AI_CONFIDENCE",       "60"))   # 0-100

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_MODE = not bool(BYBIT_API_KEY and BYBIT_API_SECRET)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.environ.get("DATA_DIR",   "/data"))
STATE_FILE    = DATA_DIR / "bot_state.json"
HISTORY_FILE  = DATA_DIR / "trade_history.json"
MAX_HISTORY   = 200   # keep last N trades in history file
