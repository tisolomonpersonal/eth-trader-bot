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

# ── TradFi (Bybit's stock/forex/commodity CFD perpetuals) ─────────────────────
# TradFi runs on the same V5 API, category="linear" — no separate host/creds
# needed, it reuses BYBIT_API_KEY / BYBIT_API_SECRET above.
TRADFI_CATEGORY   = "linear"
TRADFI_SYMBOL     = os.environ.get("TRADFI_SYMBOL",     "XAUUSD")     # e.g. XAUUSD, EURUSD, US500USD, TSLAUSDT
# "zero_fee"    -> symbols get a ".s" suffix (e.g. XAUUSD.s), STP pricing, no separate commission
# "tight_spread"-> no suffix (e.g. XAUUSD), ECN pricing, requires asset threshold to unlock
TRADFI_MODE       = os.environ.get("TRADFI_MODE",       "zero_fee").lower()
TRADFI_INTERVAL   = os.environ.get("TRADFI_INTERVAL",   "15")         # minutes; TradFi has market hours, coarser interval is friendlier
TRADFI_ACCOUNT_TYPE = "UNIFIED"  # TradFi sits under the Unified Trading Account

# Master switch — the automated TradFi cycle only runs when this is true.
# Defaults to false so enabling TradFi never changes behavior of the existing
# BNB spot bot unless you deliberately opt in.
TRADFI_ENABLED = os.environ.get("TRADFI_ENABLED", "false").lower() == "true"

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

# ── TradFi risk & position sizing ──────────────────────────────────────────────
# Independent risk envelope — separate pot from the crypto spot bot's limits
# above, so a bad TradFi day can't eat into (or be eaten into by) the crypto
# bot's daily loss counters. USDT-native (not GHC) since TradFi instruments
# are USDT-denominated and USDT-settled on Bybit — no currency conversion
# involved in the actual mechanics, unlike the GHC-budgeted BNB bot above.
TRADFI_MAX_INVESTMENT_USDT  = float(os.environ.get("TRADFI_MAX_INVESTMENT_USDT", "65"))
TRADFI_STOP_LOSS_PCT        = float(os.environ.get("TRADFI_STOP_LOSS_PCT",       "1.5"))
TRADFI_TAKE_PROFIT_PCT      = float(os.environ.get("TRADFI_TAKE_PROFIT_PCT",     "3.0"))
TRADFI_MAX_DAILY_LOSS_PCT   = float(os.environ.get("TRADFI_MAX_DAILY_LOSS_PCT",  "5.0"))
TRADFI_RISK_PER_TRADE_PCT   = float(os.environ.get("TRADFI_RISK_PER_TRADE_PCT",  "100"))
TRADFI_ALLOW_AVERAGING_DOWN = os.environ.get("TRADFI_ALLOW_AVERAGING_DOWN", "false").lower() == "true"
TRADFI_MIN_AI_CONFIDENCE    = int(os.environ.get("TRADFI_MIN_AI_CONFIDENCE",     "65"))  # higher bar than crypto by default
TRADFI_CYCLE_SECONDS        = int(os.environ.get("TRADFI_CYCLE_SECONDS",         "300")) # 5 min — matched to coarser TRADFI_INTERVAL

# ── MetaTrader 5 (Bybit TradFi runs on MT5, NOT the V5 crypto API) ─────────────
# TradFi FX/metals/indices/CFDs are only reachable through an MT5 terminal.
# The bot-app connects to the mt5-server service over the mt5linux RPC bridge.
MT5_HOST      = os.environ.get("MT5_HOST", "")            # e.g. mt5-server.zeabur.internal
MT5_PORT      = int(os.environ.get("MT5_PORT", "8001"))
MT5_LOGIN     = os.environ.get("MT5_LOGIN", "")           # Bybit MT5 account number
MT5_PASSWORD  = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER    = os.environ.get("MT5_SERVER", "")          # Bybit MT5 server name
MT5_DEVIATION = int(os.environ.get("MT5_DEVIATION", "20"))    # max slippage, points
MT5_MAGIC     = int(os.environ.get("MT5_MAGIC", "770177"))    # order tag for this bot
# A live tick older than this many hours counts as "market closed". MT5 server
# time can be offset from UTC by a few hours, so 6h absorbs the offset while
# still catching a real weekend/overnight closure.
TRADFI_MARKET_MAX_TICK_AGE_HRS = float(os.environ.get("TRADFI_MARKET_MAX_TICK_AGE_HRS", "6"))

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_MODE = not bool(BYBIT_API_KEY and BYBIT_API_SECRET)
# TradFi trades live only when we can actually reach an MT5 terminal with creds.
TRADFI_PAPER = not bool(MT5_HOST and MT5_LOGIN and MT5_PASSWORD and MT5_SERVER)

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.environ.get("DATA_DIR",   "/data"))
STATE_FILE    = DATA_DIR / "bot_state.json"
HISTORY_FILE  = DATA_DIR / "trade_history.json"
MAX_HISTORY   = 200   # keep last N trades in history file

# ── TradFi persistence (separate files — never shares state with the BNB bot) ─
TRADFI_STATE_FILE   = DATA_DIR / "tradfi_state.json"
TRADFI_HISTORY_FILE = DATA_DIR / "tradfi_trade_history.json"
