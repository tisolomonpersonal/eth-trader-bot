"""
Multi-market AI scanner — scans crypto, stocks, metals, and forex,
fetches news, calls AI to generate trade signals, then sends
them to Telegram with entry, stop loss, take profit, and full reasoning.

Technical analysis uses real indicators computed from 60 days of OHLC history:
  RSI(14), MACD(12,26,9), Bollinger Bands(20,2), ATR(14), SMA(20/50)
"""

import os
import time
import json
import traceback
from datetime import datetime, timezone

import requests

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

try:
    import feedparser
    FP_AVAILABLE = True
except ImportError:
    FP_AVAILABLE = False

try:
    import anthropic as _anthropic
    ANTH_AVAILABLE = True
except ImportError:
    ANTH_AVAILABLE = False

try:
    from openai import OpenAI as _OpenAI
    OAI_AVAILABLE = True
except ImportError:
    OAI_AVAILABLE = False

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
XAI_API_KEY        = os.environ.get("XAI_API_KEY", "")

# Ollama — free local inference running on your Zeabur server
# Set OLLAMA_HOST=http://ollama.zeabur.internal:11434 and OLLAMA_MODEL=qwen2.5:3b
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST", "").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")


def _detect_ai_provider() -> str:
    # Ollama first — free local inference, no API key needed
    if OLLAMA_HOST and OAI_AVAILABLE:
        return "ollama"
    if XAI_API_KEY and OAI_AVAILABLE:
        return "xai"
    if GROQ_API_KEY and OAI_AVAILABLE:
        return "groq"
    if OPENAI_API_KEY and OAI_AVAILABLE:
        return "openai"
    if ANTHROPIC_API_KEY and ANTH_AVAILABLE:
        return "anthropic"
    return "none"


# ── Asset universe ─────────────────────────────────────────────────────────────
# All assets now use yfinance tickers so we get 60-day OHLC history for
# real RSI / MACD / Bollinger Band calculation on every asset class.

CRYPTO_TICKERS = {
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
    "BNB-USD":  "BNB",
    "SOL-USD":  "Solana",
    "XRP-USD":  "XRP",
    "ADA-USD":  "Cardano",
    "DOGE-USD": "Dogecoin",
    "AVAX-USD": "Avalanche",
    "DOT-USD":  "Polkadot",
    "LINK-USD": "Chainlink",
}

STOCK_TICKERS = {
    "SPY":  "S&P 500 ETF",
    "QQQ":  "NASDAQ ETF",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    "AMD":  "AMD",
}

METALS_TICKERS = {
    "GC=F": "Gold",
    "SI=F": "Silver",
    "PL=F": "Platinum",
}

FOREX_TICKERS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCHF=X": "USD/CHF",
}

NEWS_RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://cryptopanic.com/news/rss/",
]


# ── Technical indicator math (pure Python, no numpy required) ─────────────────

def _sma(closes: list, period: int) -> float:
    if len(closes) < period:
        period = len(closes)
    if not period:
        return 0.0
    return sum(closes[-period:]) / period


def _ema_series(closes: list, period: int) -> list:
    """Return a list of EMA values, seeded from the first SMA."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    emas = [seed]
    for price in closes[period:]:
        emas.append(price * k + emas[-1] * (1.0 - k))
    return emas


def _rsi(closes: list, period: int = 14) -> float:
    """Wilder-smoothed RSI. Returns 50 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _macd(closes: list):
    """
    Returns (macd_line, signal_line, histogram) — all rounded.
    Returns (0, 0, 0) if insufficient data.
    """
    if len(closes) < 35:
        return 0.0, 0.0, 0.0
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    # Align: ema12 is longer (more values), trim so both start at same bar
    offset = len(ema12) - len(ema26)
    macd_line_series = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    if len(macd_line_series) < 9:
        ml = macd_line_series[-1] if macd_line_series else 0.0
        return round(ml, 8), 0.0, round(ml, 8)
    signal_series = _ema_series(macd_line_series, 9)
    if not signal_series:
        return round(macd_line_series[-1], 8), 0.0, round(macd_line_series[-1], 8)
    ml = macd_line_series[-1]
    sl = signal_series[-1]
    hist = ml - sl
    return round(ml, 8), round(sl, 8), round(hist, 8)


def _bollinger(closes: list, period: int = 20, num_std: float = 2.0):
    """Returns (upper, mid, lower). Falls back to shorter window if needed."""
    if len(closes) < 5:
        p = closes[-1] if closes else 0
        return p, p, p
    w = closes[-min(period, len(closes)):]
    mid = sum(w) / len(w)
    variance = sum((x - mid) ** 2 for x in w) / len(w)
    std = variance ** 0.5
    return round(mid + num_std * std, 8), round(mid, 8), round(mid - num_std * std, 8)


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range from real OHLC data."""
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return closes[-1] * 0.02 if closes else 0.0
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    p = min(period, len(trs))
    return round(sum(trs[-p:]) / p, 8) if trs else 0.0


def _bb_position(price: float, upper: float, lower: float) -> float:
    """0 = at lower band, 1 = at upper band, 0.5 = at midpoint."""
    span = upper - lower
    if span == 0:
        return 0.5
    return max(0.0, min(1.0, (price - lower) / span))


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_yfinance(tickers: dict, category: str) -> list:
    """
    Fetch 60 days of daily OHLC from Yahoo Finance.
    Returns asset dicts with full closes/highs/lows arrays for indicator math.
    """
    if not YF_AVAILABLE:
        return []
    results = []
    for symbol, name in tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="60d", interval="1d")
            if hist.empty or len(hist) < 5:
                continue
            closes = [float(c) for c in hist["Close"].tolist() if c and c == c]
            highs  = [float(h) for h in hist["High"].tolist() if h and h == h]
            lows   = [float(l) for l in hist["Low"].tolist() if l and l == l]
            volumes = [float(v) for v in hist["Volume"].tolist() if v == v]
            if not closes:
                continue
            price      = closes[-1]
            prev_close = closes[-2] if len(closes) > 1 else price
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
            results.append({
                "asset":      name,
                "symbol":     symbol,
                "category":   category,
                "price":      round(price, 6),
                "prev_close": round(prev_close, 6),
                "change24h":  change_pct,
                "volume":     volumes[-1] if volumes else 0,
                "closes":     closes,
                "highs":      highs,
                "lows":       lows,
            })
        except Exception as e:
            print(f"[scanner] yfinance {symbol} error: {e}")
    return results


def _fetch_news() -> list:
    headlines = []
    if FP_AVAILABLE:
        for feed_url in NEWS_RSS_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in (feed.entries or [])[:5]:
                    title = (entry.get("title") or "").strip()
                    if title:
                        headlines.append(title)
            except Exception:
                pass

    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/?auth_token=&public=true&kind=news",
            timeout=10,
        )
        if r.status_code == 200:
            for p in r.json().get("results", [])[:6]:
                t = (p.get("title") or "").strip()
                if t:
                    headlines.append(f"[CRYPTO] {t}")
    except Exception:
        pass

    try:
        r = requests.get(
            "https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
            "&topics=financial_markets&limit=10&apikey=demo",
            timeout=10,
        )
        if r.status_code == 200:
            for article in r.json().get("feed", [])[:5]:
                t = (article.get("title") or "").strip()
                if t:
                    headlines.append(t)
    except Exception:
        pass

    seen, unique = set(), []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique[:25]


# ── Technical signals engine ───────────────────────────────────────────────────

def _calculate_signals(asset: dict) -> dict:
    """
    Compute RSI(14), MACD(12,26,9), Bollinger Bands(20,2), ATR(14), SMA(20/50)
    from real 60-day OHLC history. Scores each indicator and combines them
    into a directional score used for both standalone signals and AI context.
    """
    closes   = asset.get("closes", [])
    highs    = asset.get("highs",  [])
    lows     = asset.get("lows",   [])
    price    = asset.get("price",  closes[-1] if closes else 0)
    change   = asset.get("change24h", 0)
    category = asset.get("category", "stock")

    score, reasons = 0, []

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi = _rsi(closes, 14)
    if rsi < 30:
        score += 3
        reasons.append(f"RSI {rsi} — strongly oversold (reversal likely)")
    elif rsi < 40:
        score += 2
        reasons.append(f"RSI {rsi} — approaching oversold")
    elif rsi < 50:
        score += 1
        reasons.append(f"RSI {rsi} — slightly bearish momentum")
    elif rsi > 70:
        score -= 3
        reasons.append(f"RSI {rsi} — strongly overbought (pullback risk)")
    elif rsi > 60:
        score -= 2
        reasons.append(f"RSI {rsi} — approaching overbought")
    elif rsi > 50:
        score -= 1
        reasons.append(f"RSI {rsi} — slightly bullish momentum")

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_line, signal_line, histogram = _macd(closes)
    if histogram != 0:
        if histogram > 0 and macd_line > 0:
            score += 2
            reasons.append(f"MACD bullish crossover above zero (hist={histogram:+.6f})")
        elif histogram > 0:
            score += 1
            reasons.append(f"MACD histogram positive — bullish momentum building")
        elif histogram < 0 and macd_line < 0:
            score -= 2
            reasons.append(f"MACD bearish crossover below zero (hist={histogram:+.6f})")
        else:
            score -= 1
            reasons.append(f"MACD histogram negative — bearish pressure")

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb_upper, bb_mid, bb_lower = _bollinger(closes, 20, 2.0)
    bb_pos = _bb_position(price, bb_upper, bb_lower)
    if bb_pos < 0.15:
        score += 2
        reasons.append(f"Price near lower Bollinger Band (mean-reversion BUY zone)")
    elif bb_pos < 0.30:
        score += 1
        reasons.append(f"Price in lower Bollinger Band region")
    elif bb_pos > 0.85:
        score -= 2
        reasons.append(f"Price near upper Bollinger Band (mean-reversion SELL zone)")
    elif bb_pos > 0.70:
        score -= 1
        reasons.append(f"Price in upper Bollinger Band region")

    # ── SMA 20 / 50 trend ─────────────────────────────────────────────────────
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    if sma20 and sma50:
        if price > sma20 > sma50:
            score += 1
            reasons.append(f"Price > SMA20 > SMA50 — uptrend confirmed")
        elif price < sma20 < sma50:
            score -= 1
            reasons.append(f"Price < SMA20 < SMA50 — downtrend confirmed")
        elif sma20 > sma50:
            reasons.append(f"SMA20 above SMA50 — bullish structure")
        elif sma20 < sma50:
            reasons.append(f"SMA20 below SMA50 — bearish structure")

    # ── 24h momentum ─────────────────────────────────────────────────────────
    if change > 4:
        score += 2
        reasons.append(f"Strong bullish momentum (+{change}% today)")
    elif change > 1.5:
        score += 1
        reasons.append(f"Moderate bullish momentum (+{change}% today)")
    elif change < -4:
        score -= 2
        reasons.append(f"Strong bearish pressure ({change}% today)")
    elif change < -1.5:
        score -= 1
        reasons.append(f"Moderate bearish pressure ({change}% today)")

    # ── Direction + SL/TP from real ATR ───────────────────────────────────────
    atr = _atr(highs, lows, closes, 14)
    if atr == 0:
        # Fallback: category-based % estimate
        atr_pct = {"crypto": 0.04, "forex": 0.005, "metal": 0.015}.get(category, 0.02)
        atr = price * atr_pct

    direction = "BUY" if score >= 0 else "SELL"

    # Use 1.5× ATR for SL and 3× ATR for TP → 1:2 RR
    if direction == "BUY":
        sl = round(price - atr * 1.5, 6)
        tp = round(price + atr * 3.0, 6)
    else:
        sl = round(price + atr * 1.5, 6)
        tp = round(price - atr * 3.0, 6)

    # Confidence based on indicator agreement strength
    abs_score = abs(score)
    if abs_score >= 7:
        confidence = "HIGH"
    elif abs_score >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "direction":  direction,
        "entry":      round(price, 6),
        "sl":         sl,
        "tp":         tp,
        "rr":         "1:2",
        "score":      score,
        "confidence": confidence,
        "rsi":        rsi,
        "macd_hist":  histogram,
        "bb_pos":     round(bb_pos, 3),
        "atr":        round(atr, 6),
        "sma20":      round(sma20, 6),
        "sma50":      round(sma50, 6),
        "technical_reasons": reasons,
    }


# ── AI analysis ───────────────────────────────────────────────────────────────

def _build_prompt(market_data: list, news: list) -> str:
    lines = []
    for a in market_data:
        s = a.get("signals", {})
        reasons_str = "; ".join(s.get("technical_reasons", [])[:3])
        lines.append(
            f"- {a['asset']} ({a['symbol']}) [{a['category'].upper()}]: "
            f"price=${a['price']}, 24h={a['change24h']}%, "
            f"RSI={s.get('rsi', '?')}, MACD_hist={s.get('macd_hist', '?')}, "
            f"BB_pos={s.get('bb_pos', '?')} (0=lower_band,1=upper), "
            f"tech_signal={s.get('direction','?')} (score={s.get('score',0)}, conf={s.get('confidence','?')}), "
            f"ATR={s.get('atr','?')}, entry={s.get('entry')}, sl={s.get('sl')}, tp={s.get('tp')}, "
            f"indicators=[{reasons_str}]"
        )
    news_block = "\n".join(f"• {n}" for n in news[:20]) if news else "No news available."
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""You are a professional multi-market trader and quantitative analyst.
Analyze this market data snapshot ({now_str}) — which includes real RSI, MACD, and Bollinger Band readings — plus recent news headlines, then select the TOP 3 best trade opportunities.

MARKET DATA (RSI=14, MACD=12/26/9, BB=20 period, ATR=14):
{chr(10).join(lines)}

RECENT NEWS:
{news_block}

Instructions:
- Use the technical indicators (RSI, MACD, BB position) as primary signals.
- Layer in news sentiment as a secondary confirmation or warning.
- Prefer HIGH confidence technical setups (RSI oversold/overbought + MACD confirmation + BB position).
- Diversify across asset classes if possible (e.g. one crypto, one stock, one metal/forex).
- Set SL and TP based on the provided ATR (Stop Loss ~1.5× ATR, Take Profit ~3× ATR for 1:2 RR).
- Explain clearly WHY the technical setup is compelling, referencing the actual indicator values.

CRITICAL: entry, stop_loss, and take_profit MUST be the real numeric price values from the market data above.
NEVER use 0, null, or placeholder values. Use the provided entry/sl/tp values from the ATR-based calculations.

Respond with ONLY valid JSON (no markdown fences, no text before or after):
{{
  "signals": [
    {{
      "rank": 1,
      "asset": "BITCOIN",
      "symbol": "BTC-USD",
      "category": "crypto",
      "direction": "BUY",
      "entry": 67500.00,
      "stop_loss": 64800.00,
      "take_profit": 72900.00,
      "rr_ratio": "1:2",
      "confidence": "HIGH",
      "reasoning": "RSI at 28 (oversold), MACD histogram just turned positive after bearish trend, price touching lower Bollinger Band — classic mean-reversion setup confirmed by [news context]."
    }},
    {{
      "rank": 2,
      "asset": "GOLD",
      "symbol": "GC=F",
      "category": "metal",
      "direction": "BUY",
      "entry": 2350.50,
      "stop_loss": 2315.20,
      "take_profit": 2421.10,
      "rr_ratio": "1:2",
      "confidence": "MEDIUM",
      "reasoning": "Use real entry/sl/tp from the market data — never use 0."
    }},
    {{
      "rank": 3,
      "asset": "EUR/USD",
      "symbol": "EURUSD=X",
      "category": "forex",
      "direction": "SELL",
      "entry": 1.08450,
      "stop_loss": 1.08935,
      "take_profit": 1.07480,
      "rr_ratio": "1:2",
      "confidence": "MEDIUM",
      "reasoning": "Use real entry/sl/tp from the market data — never use 0."
    }}
  ],
  "market_outlook": "2-3 sentence overall market sentiment referencing key indicator readings."
}}"""


def _parse_ai_response(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:].strip()
    parsed = json.loads(raw)
    return parsed.get("signals", []), parsed.get("market_outlook", "")


def _build_ollama_prompt(market_data: list, news: list) -> str:
    """
    Shorter, simpler prompt optimised for small local models (3B-7B).
    Includes pre-calculated entry/SL/TP so the model never needs to compute prices.
    """
    # Pick top 6 assets by absolute technical score for context
    scored = sorted(market_data, key=lambda a: abs(a.get("signals", {}).get("score", 0)), reverse=True)[:6]
    lines = []
    for a in scored:
        s = a.get("signals", {})
        lines.append(
            f"{a['asset']} ({a['symbol']}, {a['category']}): "
            f"entry={s.get('entry', a['price'])}, sl={s.get('sl','?')}, tp={s.get('tp','?')}, "
            f"direction={s.get('direction','?')}, RSI={s.get('rsi','?')}, "
            f"MACD_hist={s.get('macd_hist','?')}, BB_pos={s.get('bb_pos','?')}"
        )
    top_news = "; ".join(news[:5]) if news else "none"
    # Use the first scored asset's values as a concrete example to anchor the model
    ex = scored[0] if scored else {}
    ex_s = ex.get("signals", {})
    ex_entry = ex_s.get("entry", ex.get("price", 100.0))
    ex_sl    = ex_s.get("sl",    round(ex_entry * 0.97, 6))
    ex_tp    = ex_s.get("tp",    round(ex_entry * 1.06, 6))
    ex_sym   = ex.get("symbol", "BTC-USD")
    ex_name  = ex.get("asset",  "Bitcoin")
    ex_cat   = ex.get("category", "crypto")
    ex_dir   = ex_s.get("direction", "BUY")
    return (
        "You are a trader. Pick the 3 best trades from this data. "
        "IMPORTANT: Use the exact entry/sl/tp numbers provided — do NOT output 0. "
        "Reply ONLY with a JSON object, no other text.\n\n"
        "ASSETS:\n" + "\n".join(lines) + "\n\n"
        "NEWS: " + top_news + "\n\n"
        f'FORMAT (use real numbers from ASSETS above, not 0):\n'
        f'{{"signals":['
        f'{{"rank":1,"asset":"{ex_name}","symbol":"{ex_sym}","category":"{ex_cat}","direction":"{ex_dir}",'
        f'"entry":{ex_entry},"stop_loss":{ex_sl},"take_profit":{ex_tp},'
        f'"rr_ratio":"1:2","confidence":"HIGH","reasoning":"brief reason using RSI/MACD/BB values"}},'
        f'{{"rank":2,"asset":"NAME","symbol":"TICKER","category":"stock","direction":"BUY",'
        f'"entry":150.25,"stop_loss":143.80,"take_profit":163.15,'
        f'"rr_ratio":"1:2","confidence":"MEDIUM","reasoning":"..."}},'
        f'{{"rank":3,"asset":"NAME","symbol":"TICKER","category":"forex","direction":"SELL",'
        f'"entry":1.08500,"stop_loss":1.09225,"take_profit":1.07050,'
        f'"rr_ratio":"1:2","confidence":"LOW","reasoning":"..."}}],'
        f'"market_outlook":"one sentence"}}'
    )


def _call_ollama(prompt: str):
    """
    Call a local Ollama instance via its OpenAI-compatible /v1 endpoint.
    Hard 150-second timeout so slow CPU inference falls back gracefully.
    """
    if not OAI_AVAILABLE:
        raise RuntimeError("openai package not installed")
    client = _OpenAI(
        api_key="ollama",            # required field, ignored by Ollama
        base_url=f"{OLLAMA_HOST}/v1",
        timeout=150.0,               # fall back to technical signals if model is too slow
    )
    response = client.chat.completions.create(
        model=OLLAMA_MODEL,
        max_tokens=1200,             # smaller cap — 3B models lose JSON coherence at high token counts
        messages=[
            {
                "role": "system",
                "content": "You are a trading assistant. Always reply with valid JSON only. No markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return (response.choices[0].message.content or "").strip()


def _call_xai(prompt: str):
    client = _OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    response = client.chat.completions.create(
        model="grok-3-mini",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _call_groq(prompt: str):
    client = _OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _call_openai(prompt: str):
    client = _OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _call_anthropic(prompt: str):
    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.content[0].text or "").strip()


def _call_ai_analysis(market_data: list, news: list):
    provider = _detect_ai_provider()
    if provider == "none":
        print("[scanner] No AI provider configured.")
        print("[scanner]   Using Ollama on Zeabur? Set OLLAMA_HOST=http://ollama.zeabur.internal:11434")
        print("[scanner]   Also set OLLAMA_MODEL (e.g. qwen2.5:3b) — or ANTHROPIC_API_KEY/GROQ_API_KEY as fallback.")
        return [], "No AI provider configured."

    print(f"[scanner] Using AI provider: {provider}")

    try:
        if provider == "ollama":
            raw = _call_ollama(_build_ollama_prompt(market_data, news))
        elif provider == "xai":
            raw = _call_xai(_build_prompt(market_data, news))
        elif provider == "groq":
            raw = _call_groq(_build_prompt(market_data, news))
        elif provider == "openai":
            raw = _call_openai(_build_prompt(market_data, news))
        else:
            raw = _call_anthropic(_build_prompt(market_data, news))
        return _parse_ai_response(raw)
    except json.JSONDecodeError as e:
        print(f"[scanner] AI JSON parse error ({provider}): {e}")
        return [], ""
    except Exception as e:
        print(f"[scanner] AI error ({provider}): {e}\n{traceback.format_exc()}")
        return [], ""


# ── Telegram ───────────────────────────────────────────────────────────────────

def _send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[scanner] Telegram not configured — skipping send")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        ok = r.json().get("ok", False)
        if not ok:
            print(f"[scanner] Telegram error: {r.json()}")
        return ok
    except Exception as e:
        print(f"[scanner] Telegram send error: {e}")
        return False


# ── Message formatting ─────────────────────────────────────────────────────────

def _fmt_price(p, category="stock") -> str:
    try:
        p = float(p)
    except Exception:
        return str(p)
    if category == "forex":
        return f"{p:.5f}"
    elif p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:.4f}"
    else:
        return f"${p:.6f}"


def _format_summary(signals: list, outlook: str, is_ai: bool = True) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    assets  = " | ".join(s.get("asset", "?") for s in signals)
    mode    = "🤖 AI + Technical" if is_ai else "📐 Technical Only (RSI/MACD/BB)"
    return (
        f"📡 <b>TRADE SCAN — {now_str}</b>\n"
        f"{'━' * 32}\n"
        f"Analysis: <b>{mode}</b>\n"
        f"Markets: Crypto · Stocks · Metals · Forex\n"
        f"Top picks: <b>{assets}</b>\n\n"
        f"🌍 <b>Market Outlook:</b>\n{outlook}\n"
        f"{'━' * 32}\n"
        f"Sending {len(signals)} signal(s) below ↓"
    )


def _format_signal(signal: dict, rank: int, total: int) -> str:
    direction = signal.get("direction", "BUY")
    category  = signal.get("category", "stock")
    cat_emoji = {"crypto": "₿", "stock": "📈", "metal": "🥇", "forex": "💱"}.get(category, "📊")
    dir_str   = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    conf      = signal.get("confidence", "MEDIUM")
    conf_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "⚠️"}.get(conf, "⚡")
    conf_str  = f"{conf_emoji} {conf}"

    entry = signal.get("entry", 0)
    sl    = signal.get("stop_loss", 0)
    tp    = signal.get("take_profit", 0)
    rr    = signal.get("rr_ratio", "1:2")

    # Include technical indicators if present (from fallback mode)
    tech_line = ""
    rsi = signal.get("rsi")
    macd_h = signal.get("macd_hist")
    bb_pos = signal.get("bb_pos")
    if rsi is not None:
        bb_pct = f"{bb_pos * 100:.0f}%" if bb_pos is not None else "?"
        tech_line = (
            f"\n📊 <b>Indicators:</b>\n"
            f"  RSI(14): {rsi}  |  MACD hist: {macd_h:+.6f}  |  BB pos: {bb_pct}\n"
        )

    return (
        f"{cat_emoji} <b>SIGNAL {rank}/{total} — {signal.get('asset', '?')}</b>\n"
        f"{'─' * 32}\n"
        f"Direction:   {dir_str}\n"
        f"Confidence:  {conf_str}\n"
        f"Category:    {category.upper()}\n"
        f"{tech_line}\n"
        f"💰 <b>Entry:</b>       {_fmt_price(entry, category)}\n"
        f"🛑 <b>Stop Loss:</b>   {_fmt_price(sl, category)}\n"
        f"🎯 <b>Take Profit:</b> {_fmt_price(tp, category)}\n"
        f"⚖️ <b>Risk:Reward:</b> {rr}\n\n"
        f"📋 <b>Why:</b>\n{signal.get('reasoning', 'No reasoning provided.')}\n"
        f"{'─' * 32}\n"
        f"<i>⚠️ Not financial advice. Always manage your own risk.</i>"
    )


# ── Main scan ──────────────────────────────────────────────────────────────────

def run_scan() -> dict:
    print("[scanner] ── Starting multi-market AI scan ──")
    t0 = time.time()

    # All asset classes now use yfinance — 60 days of real OHLC for indicator math
    crypto = _fetch_yfinance(CRYPTO_TICKERS, "crypto")
    stocks = _fetch_yfinance(STOCK_TICKERS, "stock")
    metals = _fetch_yfinance(METALS_TICKERS, "metal")
    forex  = _fetch_yfinance(FOREX_TICKERS,  "forex")

    all_assets = crypto + stocks + metals + forex
    print(f"[scanner] {len(all_assets)} assets fetched "
          f"({len(crypto)} crypto, {len(stocks)} stocks, "
          f"{len(metals)} metals, {len(forex)} forex)")

    # Compute real technical indicators for every asset
    for a in all_assets:
        a["signals"] = _calculate_signals(a)

    news = _fetch_news()
    print(f"[scanner] {len(news)} headlines fetched")

    ai_signals, outlook = _call_ai_analysis(all_assets, news)
    is_ai = bool(ai_signals)

    # ── Back-fill zero/missing prices from technical signals ─────────────────────
    # AI models (especially small Ollama ones) sometimes return entry/SL/TP as 0
    # or omit them entirely. Fill every gap from the pre-calculated ATR-based values.
    if ai_signals:
        sym_lookup  = {a["symbol"].upper(): a for a in all_assets}
        name_lookup = {a["asset"].upper(): a  for a in all_assets}

        def _fuzzy_find_asset(sym_key: str, name_key: str):
            """Exact match first, then substring search on symbol, then on name."""
            asset = sym_lookup.get(sym_key) or name_lookup.get(name_key)
            if asset:
                return asset
            # Partial symbol match (e.g. "BTC" hits "BTC-USD")
            for k, v in sym_lookup.items():
                if sym_key and (sym_key in k or k in sym_key):
                    return v
            # Partial name match (e.g. "BITCOIN" hits "Bitcoin")
            for k, v in name_lookup.items():
                if name_key and (name_key in k or k in name_key):
                    return v
            return None

        for sig in ai_signals:
            e  = float(sig.get("entry",       0) or 0)
            sl = float(sig.get("stop_loss",   0) or 0)
            tp = float(sig.get("take_profit", 0) or 0)
            if e == 0 or sl == 0 or tp == 0:
                sym_key  = sig.get("symbol", "").upper().replace(" ", "")
                name_key = sig.get("asset",  "").upper().replace(" ", "")
                asset = _fuzzy_find_asset(sym_key, name_key)
                if asset:
                    tech = asset["signals"]
                    if e  == 0: sig["entry"]        = tech["entry"]
                    if sl == 0: sig["stop_loss"]    = tech["sl"]
                    if tp == 0: sig["take_profit"]  = tech["tp"]
                    print(f"[scanner] back-filled {sig.get('asset')} entry/SL/TP from technical signals")
                else:
                    print(f"[scanner] WARNING: could not back-fill {sig.get('asset')} ({sig.get('symbol')}) — no match found")
                # Always copy RSI/MACD/BB for display if missing
                if asset:
                    tech = asset["signals"]
                    sig.setdefault("rsi",       tech.get("rsi"))
                    sig.setdefault("macd_hist", tech.get("macd_hist"))
                    sig.setdefault("bb_pos",    tech.get("bb_pos"))
                    sig.setdefault("confidence",tech.get("confidence", "MEDIUM"))

    # ── Technical fallback when AI is unavailable ─────────────────────────────
    if not ai_signals:
        print("[scanner] AI unavailable — generating technical signals (RSI/MACD/BB)")
        sorted_assets = sorted(
            all_assets,
            key=lambda x: abs(x["signals"].get("score", 0)),
            reverse=True,
        )
        seen_cats, tech_signals = set(), []
        for a in sorted_assets:
            cat = a["category"]
            s   = a["signals"]
            if cat not in seen_cats or len(tech_signals) < 3:
                seen_cats.add(cat)
                reasons_text = ". ".join(s.get("technical_reasons", []))
                tech_signals.append({
                    "rank":        len(tech_signals) + 1,
                    "asset":       a["asset"],
                    "symbol":      a["symbol"],
                    "category":    cat,
                    "direction":   s["direction"],
                    "entry":       s["entry"],
                    "stop_loss":   s["sl"],
                    "take_profit": s["tp"],
                    "rr_ratio":    "1:2",
                    "confidence":  s["confidence"],
                    "reasoning":   reasons_text or "No strong technical signal detected.",
                    "rsi":         s.get("rsi"),
                    "macd_hist":   s.get("macd_hist"),
                    "bb_pos":      s.get("bb_pos"),
                })
            if len(tech_signals) == 3:
                break
        ai_signals = tech_signals
        outlook = (
            "Technical-only scan (RSI/MACD/Bollinger Bands). "
            "To enable AI analysis: set OLLAMA_HOST=http://ollama.zeabur.internal:11434 "
            "and OLLAMA_MODEL (e.g. qwen2.5:3b) on the bot-app service in Zeabur."
        )

    # ── No-trade filter ───────────────────────────────────────────────────────
    # If every signal is LOW conviction (score < 3), markets are choppy/neutral.
    # Send an informative "no trade" message instead of weak signals.
    NO_TRADE_THRESHOLD = 3
    best_score = max(
        (abs(a["signals"].get("score", 0)) for a in all_assets),
        default=0,
    )
    if best_score < NO_TRADE_THRESHOLD:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Collect why each top asset doesn't qualify — use actual indicator values
        top5 = sorted(all_assets, key=lambda x: abs(x["signals"].get("score", 0)), reverse=True)[:5]
        reasons = []
        for a in top5:
            s = a["signals"]
            rsi = s.get("rsi", "?")
            hist = s.get("macd_hist", 0)
            bb = s.get("bb_pos", 0.5)
            score_val = s.get("score", 0)
            # Explain concisely why the asset doesn't qualify
            rsi_note = (
                "overbought" if isinstance(rsi, (int, float)) and rsi > 60 else
                "oversold"   if isinstance(rsi, (int, float)) and rsi < 40 else
                "neutral"
            )
            reasons.append(
                f"• <b>{a['asset']}</b>: RSI {rsi} ({rsi_note}), "
                f"MACD hist {hist:+.6f}, "
                f"BB pos {round(bb * 100)}% — score {score_val:+d} (need ±{NO_TRADE_THRESHOLD})"
            )
        no_trade_msg = (
            f"🔍 <b>SCAN — {now_str}</b>\n"
            f"{'━'*32}\n"
            f"⏸ <b>NO TRADE — No conviction setup found</b>\n\n"
            f"Scanned {len(all_assets)} assets across crypto, stocks, metals & forex.\n"
            f"Best signal score: <b>{best_score}</b> (minimum needed: {NO_TRADE_THRESHOLD}).\n"
            f"Indicators are not strongly aligned — waiting for a clearer setup.\n\n"
            f"<b>Top {len(top5)} assets checked:</b>\n"
            + "\n".join(reasons) +
            f"\n\n<i>🕐 Next scan in 30 min. No action needed.</i>"
        )
        _send_telegram(no_trade_msg)
        elapsed = round(time.time() - t0, 1)
        print(f"[scanner] Done in {elapsed}s — no trade (best score={best_score}, threshold={NO_TRADE_THRESHOLD})")
        return {
            "status": "no_trade", "reason": "all signals below threshold",
            "best_score": best_score, "threshold": NO_TRADE_THRESHOLD,
            "assets_scanned": len(all_assets), "elapsed_seconds": elapsed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Send signals to Telegram ──────────────────────────────────────────────
    _send_telegram(_format_summary(ai_signals, outlook, is_ai=is_ai))
    time.sleep(1)
    for i, sig in enumerate(ai_signals, 1):
        sig["rank"] = i
        _send_telegram(_format_signal(sig, i, len(ai_signals)))
        time.sleep(0.6)

    elapsed = round(time.time() - t0, 1)
    print(f"[scanner] Done in {elapsed}s — {len(ai_signals)} signals sent to Telegram")

    return {
        "status":          "ok",
        "ai_used":         is_ai,
        "signals":         ai_signals,
        "outlook":         outlook,
        "assets_scanned":  len(all_assets),
        "headlines_found": len(news),
        "elapsed_seconds": elapsed,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    result = run_scan()
