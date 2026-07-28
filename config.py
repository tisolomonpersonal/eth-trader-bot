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

# ── Scalp master switch ───────────────────────────────────────────────────────
# Read before the market block because it decides the default symbol.
#
# DEFAULTS TO FALSE ON PURPOSE. This repo auto-deploys to Zeabur on push, so a
# default of true would silently swap the live strategy the moment this merges.
# Switching strategies must be a deliberate act: set SCALP_MODE=true in the
# Zeabur dashboard, only after backtesting and paper-trading, and only after
# any position held by the old bot has been closed (see the orphan check in
# scheduler.run_bot).
SCALP_MODE          = os.environ.get("SCALP_MODE", "false").lower() == "true"

# ── Market ────────────────────────────────────────────────────────────────────
# BTC only. In scalp mode this is the BTCUSDT LINEAR PERPETUAL, not spot.
#
# Why perps rather than spot, in one line: spot costs 0.20% round trip and is
# long-only, which makes a 60%-win-rate scalper mathematically unprofitable.
# Perps cost ~0.11% taker (0.04% maker-only) and allow shorts, which roughly
# doubles the number of valid setups. Same strategy, opposite sign of edge.
#
# The cost is leverage and liquidation risk, which spot does not have. See
# LEVERAGE below — it is deliberately pinned at 1x by default.
SYMBOL        = os.environ.get("SYMBOL",    "BTCUSDT")
BASE_COIN     = os.environ.get("BASE_COIN", "BTC")
# "linear" = USDT-margined perpetual futures. The legacy swing path still runs
# on spot, since it was never written to handle positions or shorts.
CATEGORY      = "linear" if SCALP_MODE else "spot"
INTERVAL      = "1"          # 1-minute candles — the scalping timeframe
TREND_INTERVAL= "5"          # 5-minute candles — higher-TF regime/trend filter
CANDLE_LIMIT  = 250          # 250 candles covers EMA200 warmup

# ── Leverage ──────────────────────────────────────────────────────────────────
# DEFAULTS TO 1x ON PURPOSE. At 1x a perp behaves like spot for risk purposes:
# you get the cheap fees and the ability to short, without adding liquidation
# risk on top of an unproven strategy.
#
# Raising this multiplies BOTH your gains and your losses, and introduces a
# price at which the position is closed for you. On a $10 account at 10x, a
# ~9% adverse BTC move liquidates you — and BTC moves 9% intraday several times
# a year. Do not raise this until the strategy has proven itself at 1x.
LEVERAGE      = float(os.environ.get("LEVERAGE", "1"))
# One-way mode (positionIdx=0) — one position per symbol, long or short.
POSITION_IDX  = 0

# ── Scalping engine ───────────────────────────────────────────────────────────
# SCALP_MODE itself is defined above the market block, since it decides the
# default symbol. Everything else about the scalper is configured here.
SCALP_CYCLE_SECONDS = int(os.environ.get("SCALP_CYCLE_SECONDS", "15"))

# --- Fees. THE most important numbers in this file. -------------------------
# Bybit linear perpetual standard: 0.02% maker / 0.055% taker. A market-in +
# market-out round trip costs 0.11%. Any target below that is a guaranteed loss
# before the strategy has even been consulted, so ROUND_TRIP_FEE_PCT is
# enforced as a hard floor on every take-profit distance in scalp_risk.py.
#
# For reference, spot is 0.10/0.10 = 0.20% round trip. That difference is why
# this bot trades perps: at 0.20% a 60%-win-rate scalper is net negative, and
# at 0.11% the same strategy is positive. Check your actual VIP tier and set
# these to match — guessing low here makes every downstream number a lie.
MAKER_FEE_PCT       = float(os.environ.get("MAKER_FEE_PCT", "0.02"))
TAKER_FEE_PCT       = float(os.environ.get("TAKER_FEE_PCT", "0.055"))
# Both legs are market orders today, so both pay TAKER. Modelling this as
# maker+taker would understate real costs by ~30% and quietly let through
# targets that cannot pay for themselves. If post-only limit entries are added
# later, change this to MAKER_FEE_PCT + TAKER_FEE_PCT.
ROUND_TRIP_FEE_PCT  = TAKER_FEE_PCT * 2
# Gross edge must exceed fees by this multiple or the trade is not worth taking.
MIN_EDGE_FEE_MULT   = float(os.environ.get("MIN_EDGE_FEE_MULT", "2.5"))

# --- ATR-based exits (replaces the flat 2%/4% swing brackets) ----------------
# Scalp stops must scale with current volatility, not be a fixed percentage.
ATR_PERIOD          = int(os.environ.get("ATR_PERIOD", "14"))
SL_ATR_MULT         = float(os.environ.get("SL_ATR_MULT", "1.2"))
TP_ATR_MULT         = float(os.environ.get("TP_ATR_MULT", "1.8"))
# Absolute guardrails so a volatility spike can't produce an absurd bracket.
MIN_SL_PCT          = float(os.environ.get("MIN_SL_PCT", "0.25"))
MAX_SL_PCT          = float(os.environ.get("MAX_SL_PCT", "1.20"))

# --- Trailing stop & time stop ----------------------------------------------
# Once price has run TRAIL_TRIGGER_ATR in our favour, ratchet the stop to lock
# in gains. Scalps that stall are closed by the time stop — capital sitting in
# a dead trade is capital not available for the next setup.
TRAIL_ENABLED       = os.environ.get("TRAIL_ENABLED", "true").lower() == "true"
TRAIL_TRIGGER_ATR   = float(os.environ.get("TRAIL_TRIGGER_ATR", "1.0"))
TRAIL_DISTANCE_ATR  = float(os.environ.get("TRAIL_DISTANCE_ATR", "0.7"))
MAX_HOLD_MINUTES    = int(os.environ.get("MAX_HOLD_MINUTES", "45"))

# --- Regime detection --------------------------------------------------------
# ADX below RANGE_ADX_MAX  -> ranging  -> mean-reversion fades are armed.
# ADX above TREND_ADX_MIN  -> trending -> only squeeze breakouts / pullbacks.
ADX_PERIOD          = int(os.environ.get("ADX_PERIOD", "14"))
RANGE_ADX_MAX       = float(os.environ.get("RANGE_ADX_MAX", "20"))
TREND_ADX_MIN       = float(os.environ.get("TREND_ADX_MIN", "25"))
BB_PERIOD           = int(os.environ.get("BB_PERIOD", "20"))
BB_STD              = float(os.environ.get("BB_STD", "2.0"))
SQUEEZE_LOOKBACK    = int(os.environ.get("SQUEEZE_LOOKBACK", "50"))
SQUEEZE_PCTILE      = float(os.environ.get("SQUEEZE_PCTILE", "25"))  # bandwidth in bottom X%
RSI_OVERSOLD        = float(os.environ.get("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT      = float(os.environ.get("RSI_OVERBOUGHT", "70"))
VOL_SPIKE_MULT      = float(os.environ.get("VOL_SPIKE_MULT", "1.5"))

# --- Scalp-specific risk limits ---------------------------------------------
# Scalping's real failure mode is overtrading and revenge-trading, not any one
# bad entry. These caps matter more than the entry logic itself.
MAX_TRADES_PER_DAY      = int(os.environ.get("MAX_TRADES_PER_DAY", "30"))
MAX_CONSECUTIVE_LOSSES  = int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "4"))
COOLDOWN_AFTER_LOSS_MIN = int(os.environ.get("COOLDOWN_AFTER_LOSS_MIN", "10"))
# Risk a fixed % of the pot per trade, sized off the stop distance (not a flat
# all-in like the swing bot's RISK_PER_TRADE_PCT=100 default).
SCALP_RISK_PCT          = float(os.environ.get("SCALP_RISK_PCT", "1.0"))

# --- AI veto (off by default for scalping) -----------------------------------
# An LLM round-trip per 15s cycle is latency and noise on a scalp timeframe.
# When enabled the model can only VETO a rule-generated entry, never create one.
AI_VETO_ENABLED     = os.environ.get("AI_VETO_ENABLED", "false").lower() == "true"

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
# MAX_INVESTMENT_USDT is normally derived from the GHC budget, but can be set
# directly — which is what you want for a small USDT-native test float, where
# going via a cedi conversion just obscures the actual dollar size at risk.
MAX_INVESTMENT_USDT   = float(os.environ["MAX_INVESTMENT_USDT"]) \
    if os.environ.get("MAX_INVESTMENT_USDT") else MAX_INVESTMENT_GHC / GHC_RATE
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

# Scalp bot keeps its own files so switching SCALP_MODE never mixes the two
# strategies' P&L, counters or open-position state.
SCALP_STATE_FILE   = DATA_DIR / "scalp_state.json"
SCALP_HISTORY_FILE = DATA_DIR / "scalp_trade_history.json"

# ── TradFi persistence (separate files — never shares state with the BNB bot) ─
TRADFI_STATE_FILE   = DATA_DIR / "tradfi_state.json"
TRADFI_HISTORY_FILE = DATA_DIR / "tradfi_trade_history.json"
