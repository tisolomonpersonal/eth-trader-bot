"""
AI signal engine.
Provider chain: Ollama → Groq → OpenAI → rule-based fallback.
Always returns action (BUY/SELL/HOLD), confidence (0-100), reason, suggested_pct.
The AI is an analyst only — execution decisions are made in strategy.py + risk.py.
"""
import json
from dataclasses import dataclass
from typing import Optional

import requests

import config
from logger import get_logger

log = get_logger("ai")


@dataclass
class AISignal:
    action:          str    # "BUY" | "SELL" | "HOLD"
    confidence:      int    # 0-100
    reason:          str
    suggested_pct:   float  # % of max investment to use (0-100)
    provider:        str    # which provider answered


_SYSTEM = (
    "You are a conservative BNB/USDT spot trading analyst. "
    "You ONLY analyse spot markets. You NEVER recommend futures, margin, leverage, or shorting. "
    "You return structured JSON only."
)

_RESPONSE_FORMAT = """{
  "action": "BUY" or "SELL" or "HOLD",
  "confidence": <integer 0-100>,
  "reason": "<one concise sentence>",
  "suggested_position_pct": <integer 0-100>
}"""


def _build_prompt(ind: dict, holdings: dict, state: dict) -> str:
    pos_note = (
        f"HOLDING {holdings['bnb']:.4f} BNB (entry ${state.get('entry_price',0):,.4f}, "
        f"SL ${state.get('sl_price',0):,.4f}, TP ${state.get('tp_price',0):,.4f}). "
        "Consider SELL if bearish reversal or TP conditions met."
        if state.get("in_position") else
        "NOT in position. Consider BUY only if strong bullish signal."
    )
    last_trade = (
        f"Last trade: {state.get('last_action','NONE')} @ "
        f"${state.get('entry_price',0):,.4f} — {state.get('last_reason','')[:80]}"
        if state.get("last_action") not in (None, "NONE") else "No previous trade."
    )

    return f"""{_SYSTEM}

=== BNB/USDT 1-minute Chart ===
Price:       ${ind['price']:,.4f}
EMA 50:      {ind['ema50']}  |  EMA 200: {ind['ema200']}
Trend:       {ind['trend']}  |  Crossover: {ind['crossover']}
RSI (14):    {ind['rsi']}   {'(overbought)' if ind['rsi']>70 else '(oversold)' if ind['rsi']<30 else '(neutral)'}
MACD hist:   {ind['macd_hist']:+.6f}  {'(bullish momentum)' if ind['macd_hist']>0 else '(bearish momentum)'}
Volume:      {ind['vol_trend']}
ATR (14):    {ind['atr']}

=== Portfolio ===
USDT balance: ${holdings['usdt']:.2f}
{pos_note}

=== Context ===
{last_trade}
Daily P&L: ${state.get('daily_pnl_usdt',0):+.2f} USDT

=== Risk Rules (you must respect these) ===
- Max investment: ${config.MAX_INVESTMENT_USDT:.2f} USDT total
- Stop loss: {config.STOP_LOSS_PCT}% below entry
- Take profit: {config.TAKE_PROFIT_PCT}% above entry
- Only 1 open position allowed
- SPOT only — no futures, margin, leverage, shorts

=== Instructions ===
Analyse all data carefully. Return ONLY this JSON, no other text:
{_RESPONSE_FORMAT}"""


def _parse(raw: dict) -> AISignal:
    action = str(raw.get("action", "HOLD")).upper().strip()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    conf   = max(0, min(100, int(raw.get("confidence", 50) or 50)))
    reason = str(raw.get("reason", "")).strip() or "No reason provided."
    pct    = max(0.0, min(100.0, float(raw.get("suggested_position_pct", 50) or 50)))
    return AISignal(action=action, confidence=conf, reason=reason,
                    suggested_pct=pct, provider="")


def _ollama(prompt: str) -> AISignal:
    r = requests.post(
        f"{config.OLLAMA_HOST}/api/generate",
        json={
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 120},
        },
        timeout=30,
    )
    r.raise_for_status()
    sig = _parse(json.loads(r.json()["response"]))
    sig.provider = "ollama"
    return sig


def _groq(prompt: str) -> AISignal:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 150,
            "response_format": {"type": "json_object"},
        },
        timeout=15,
    )
    r.raise_for_status()
    sig = _parse(json.loads(r.json()["choices"][0]["message"]["content"]))
    sig.provider = "groq"
    return sig


def _openai(prompt: str) -> AISignal:
    r = requests.post(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 150,
            "response_format": {"type": "json_object"},
        },
        timeout=15,
    )
    r.raise_for_status()
    sig = _parse(json.loads(r.json()["choices"][0]["message"]["content"]))
    sig.provider = "openai"
    return sig


def _rules(ind: dict, in_position: bool) -> AISignal:
    """Deterministic rule-based fallback — no external calls needed."""
    rsi_val  = ind["rsi"]
    hist     = ind["macd_hist"]
    trend    = ind["trend"]
    crossover= ind["crossover"]

    if not in_position:
        if crossover == "Golden Cross":
            return AISignal("BUY",  78, "Golden Cross: EMA50 crossed above EMA200.", 80, "rules")
        if rsi_val < 35 and hist > 0 and trend == "Bullish":
            return AISignal("BUY",  72, f"RSI oversold ({rsi_val}) with bullish MACD momentum.", 70, "rules")
        if rsi_val < 48 and hist > 0 and trend == "Bullish":
            return AISignal("BUY",  61, f"RSI {rsi_val} with positive MACD and bullish trend.", 60, "rules")
    else:
        if crossover == "Death Cross":
            return AISignal("SELL", 80, "Death Cross: EMA50 crossed below EMA200.", 100, "rules")
        if rsi_val > 72 and hist < 0:
            return AISignal("SELL", 75, f"RSI overbought ({rsi_val}) with bearish MACD.", 100, "rules")
        if trend == "Bearish" and hist < 0 and rsi_val > 55:
            return AISignal("SELL", 65, "Trend turned bearish with negative MACD momentum.", 100, "rules")

    return AISignal("HOLD", 55, f"No strong signal. RSI={rsi_val}, trend={trend}, MACD hist={hist:+.6f}.", 0, "rules")


_TRADFI_SYSTEM = (
    "You are a conservative TradFi CFD/perpetual analyst trading {symbol} on Bybit. "
    "You NEVER recommend position sizes beyond the stated risk limits. "
    "You are aware this instrument has real market hours, unlike crypto — "
    "if data looks stale or thin, prefer HOLD. "
    "You return structured JSON only."
)


def _build_tradfi_prompt(ind: dict, symbol: str, holdings: dict, state: dict) -> str:
    pos_note = (
        f"HOLDING a {state.get('side','')} position, qty {state.get('qty',0)} "
        f"(entry {state.get('entry_price',0):,.4f}, "
        f"SL {state.get('sl_price',0):,.4f}, TP {state.get('tp_price',0):,.4f}). "
        "Consider closing if reversal or TP/SL conditions are near."
        if state.get("in_position") else
        "NOT in position. Consider entry only if strong signal and market is open."
    )
    last_trade = (
        f"Last trade: {state.get('last_action','NONE')} @ "
        f"{state.get('entry_price',0):,.4f} — {state.get('last_reason','')[:80]}"
        if state.get("last_action") not in (None, "NONE") else "No previous trade."
    )

    return f"""{_TRADFI_SYSTEM.format(symbol=symbol)}

=== {symbol} Chart ({config.TRADFI_INTERVAL}-min candles) ===
Price:       {ind['price']:,.4f}
EMA 50:      {ind['ema50']}  |  EMA 200: {ind['ema200']}
Trend:       {ind['trend']}  |  Crossover: {ind['crossover']}
RSI (14):    {ind['rsi']}   {'(overbought)' if ind['rsi']>70 else '(oversold)' if ind['rsi']<30 else '(neutral)'}
MACD hist:   {ind['macd_hist']:+.6f}  {'(bullish momentum)' if ind['macd_hist']>0 else '(bearish momentum)'}
Volume:      {ind['vol_trend']}
ATR (14):    {ind['atr']}

=== Account ===
USDT balance: {holdings.get('usdt',0):.2f}
{pos_note}

=== Context ===
{last_trade}
Daily P&L: {state.get('daily_pnl_usdt',0):+.2f} USDT

=== Risk Rules (you must respect these) ===
- Max investment: {config.TRADFI_MAX_INVESTMENT_USDT:.2f} USDT total
- Stop loss: {config.TRADFI_STOP_LOSS_PCT}% from entry
- Take profit: {config.TRADFI_TAKE_PROFIT_PCT}% from entry
- Only 1 open position allowed on this instrument
- This is a real-money-adjacent CFD/perpetual — be conservative, prefer HOLD when unsure

=== Instructions ===
Analyse all data carefully. Return ONLY this JSON, no other text:
{_RESPONSE_FORMAT}"""


def get_tradfi_signal(ind: dict, symbol: str, holdings: dict, state: dict) -> AISignal:
    """Get a trading signal for a TradFi instrument from the best available AI provider."""
    prompt = _build_tradfi_prompt(ind, symbol, holdings, state)

    providers = []
    if config.OLLAMA_HOST:     providers.append(("ollama", _ollama))
    if config.GROQ_API_KEY:    providers.append(("groq",   _groq))
    if config.OPENAI_API_KEY:  providers.append(("openai", _openai))

    for name, fn in providers:
        try:
            sig = fn(prompt)
            log.info(f"[tradfi/{name}] {sig.action} conf={sig.confidence} — {sig.reason[:80]}")
            return sig
        except Exception as e:
            log.warning(f"[tradfi/{name}] failed: {e}")

    log.info("All AI providers failed for TradFi — using rule-based signal")
    sig = _rules(ind, state.get("in_position", False))
    log.info(f"[tradfi/rules] {sig.action} conf={sig.confidence} — {sig.reason[:80]}")
    return sig


def get_signal(ind: dict, holdings: dict, state: dict) -> AISignal:
    """Get trading signal from the best available AI provider."""
    prompt     = _build_prompt(ind, holdings, state)
    in_position= state.get("in_position", False)

    providers = []
    if config.OLLAMA_HOST:     providers.append(("ollama", _ollama))
    if config.GROQ_API_KEY:    providers.append(("groq",   _groq))
    if config.OPENAI_API_KEY:  providers.append(("openai", _openai))

    for name, fn in providers:
        try:
            sig = fn(prompt)
            log.info(f"[{name}] {sig.action} conf={sig.confidence} — {sig.reason[:80]}")
            return sig
        except Exception as e:
            log.warning(f"[{name}] failed: {e}")

    log.info("All AI providers failed — using rule-based signal")
    sig = _rules(ind, in_position)
    log.info(f"[rules] {sig.action} conf={sig.confidence} — {sig.reason[:80]}")
    return sig
