"""
Multi-market AI scanner — scans crypto, stocks, metals, and forex,
fetches news, calls Claude AI to generate trade signals, then sends
them to Telegram with entry, stop loss, take profit, and full reasoning.
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

# Auto-detect which AI provider is available (Groq free → OpenAI → Anthropic)
def _detect_ai_provider() -> str:
    if GROQ_API_KEY:
        return "groq"
    if OPENAI_API_KEY and OAI_AVAILABLE:
        return "openai"
    if ANTHROPIC_API_KEY and ANTH_AVAILABLE:
        return "anthropic"
    return "none"

CRYPTO_IDS = [
    "bitcoin", "ethereum", "binancecoin", "solana", "ripple",
    "cardano", "dogecoin", "avalanche-2", "polkadot", "chainlink",
]

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


def _fetch_crypto() -> list:
    try:
        ids = ",".join(CRYPTO_IDS)
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
            f"&include_24hr_change=true&include_24hr_vol=true"
            f"&include_market_cap=true"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = []
        for coin_id in CRYPTO_IDS:
            d = data.get(coin_id, {})
            if not d:
                continue
            results.append({
                "asset":     coin_id.upper().replace("-2", "").replace("-", " "),
                "symbol":    coin_id,
                "category":  "crypto",
                "price":     d.get("usd", 0),
                "change24h": round(float(d.get("usd_24h_change") or 0), 2),
                "volume24h": d.get("usd_24h_vol", 0),
                "market_cap": d.get("usd_market_cap", 0),
                "above_sma5": None,
                "sma5": d.get("usd", 0),
            })
        return results
    except Exception as e:
        print(f"[scanner] crypto fetch error: {e}")
        return []


def _fetch_yfinance(tickers: dict, category: str) -> list:
    if not YF_AVAILABLE:
        return []
    results = []
    for symbol, name in tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="7d", interval="1d")
            if hist.empty:
                continue
            latest = hist.iloc[-1]
            prev   = hist.iloc[-2] if len(hist) > 1 else hist.iloc[-1]
            price  = float(latest["Close"])
            prev_close = float(prev["Close"])
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
            volume     = float(latest.get("Volume", 0) or 0)
            closes     = [float(c) for c in hist["Close"].tolist() if c]
            sma5       = round(sum(closes[-5:]) / min(len(closes), 5), 6) if closes else price
            results.append({
                "asset":      name,
                "symbol":     symbol,
                "category":   category,
                "price":      round(price, 6),
                "prev_close": round(prev_close, 6),
                "change24h":  change_pct,
                "volume":     volume,
                "sma5":       sma5,
                "above_sma5": price > sma5,
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
                    headlines.append(f"[CRYPTO NEWS] {t}")
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


def _calculate_signals(asset: dict) -> dict:
    price      = asset.get("price", 0)
    change     = asset.get("change24h", 0)
    above_sma5 = asset.get("above_sma5")
    category   = asset.get("category", "stock")

    score, reasons = 0, []

    if change > 3:
        score += 2; reasons.append(f"strong bullish momentum (+{change}% 24h)")
    elif change > 1:
        score += 1; reasons.append(f"mild bullish momentum (+{change}% 24h)")
    elif change < -3:
        score -= 2; reasons.append(f"strong bearish pressure ({change}% 24h)")
    elif change < -1:
        score -= 1; reasons.append(f"mild bearish pressure ({change}% 24h)")

    if above_sma5 is True:
        score += 1; reasons.append("price above 5-day SMA")
    elif above_sma5 is False:
        score -= 1; reasons.append("price below 5-day SMA")

    atr_pct = {"crypto": 0.04, "forex": 0.005, "metal": 0.015}.get(category, 0.02)
    atr = price * atr_pct if price else 0

    if score >= 0:
        direction = "BUY"
        sl = round(price - atr * 1.5, 6)
        tp = round(price + atr * 3.0, 6)
    else:
        direction = "SELL"
        sl = round(price + atr * 1.5, 6)
        tp = round(price - atr * 3.0, 6)

    return {
        "direction": direction,
        "entry": round(price, 6),
        "sl": sl,
        "tp": tp,
        "rr": "1:2",
        "score": score,
        "technical_reasons": reasons,
    }


def _build_prompt(market_data: list, news: list) -> str:
    lines = []
    for a in market_data:
        s = a.get("signals", {})
        lines.append(
            f"- {a['asset']} ({a['symbol']}) [{a['category'].upper()}]: "
            f"price=${a['price']}, 24h={a['change24h']}%, "
            f"tech_signal={s.get('direction','?')}, score={s.get('score',0)}, "
            f"entry={s.get('entry')}, sl={s.get('sl')}, tp={s.get('tp')}"
        )
    news_block = "\n".join(f"• {n}" for n in news[:20]) if news else "No news available."
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""You are a professional multi-market trader and quantitative analyst.
Analyze this market data snapshot ({now_str}) plus recent news, then pick the TOP 3 best trade opportunities.

MARKET DATA:
{chr(10).join(lines)}

RECENT NEWS:
{news_block}

Instructions:
- Consider both technical momentum AND news sentiment.
- Pick one each from different asset classes if possible (e.g. one crypto, one stock, one forex/metal).
- Set realistic entry, stop loss, and take profit based on the asset's actual price and volatility.
- Give a detailed, honest reasoning for WHY this is the best trade right now.

Respond with ONLY valid JSON (no markdown fences, no text before or after):
{{
  "signals": [
    {{
      "rank": 1,
      "asset": "BITCOIN",
      "symbol": "bitcoin",
      "category": "crypto",
      "direction": "BUY",
      "entry": 67500.00,
      "stop_loss": 64800.00,
      "take_profit": 72900.00,
      "rr_ratio": "1:2",
      "confidence": "HIGH",
      "reasoning": "Detailed reasoning combining technical and news factors."
    }},
    {{"rank": 2, "asset": "...", "symbol": "...", "category": "...", "direction": "BUY", "entry": 0, "stop_loss": 0, "take_profit": 0, "rr_ratio": "1:2", "confidence": "MEDIUM", "reasoning": "..."}},
    {{"rank": 3, "asset": "...", "symbol": "...", "category": "...", "direction": "BUY", "entry": 0, "stop_loss": 0, "take_profit": 0, "rr_ratio": "1:2", "confidence": "MEDIUM", "reasoning": "..."}}
  ],
  "market_outlook": "2-3 sentence overall market sentiment right now."
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


def _call_groq(prompt: str):
    """Call Groq API (free tier, OpenAI-compatible, llama-3.3-70b)."""
    if not OAI_AVAILABLE:
        raise RuntimeError("openai package not installed")
    client = _OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _call_openai(prompt: str):
    """Call OpenAI API."""
    if not OAI_AVAILABLE:
        raise RuntimeError("openai package not installed")
    client = _OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def _call_anthropic(prompt: str):
    """Call Anthropic Claude API."""
    if not ANTH_AVAILABLE:
        raise RuntimeError("anthropic package not installed")
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
        print("[scanner] No AI provider configured — set GROQ_API_KEY (free), OPENAI_API_KEY, or ANTHROPIC_API_KEY")
        return [], "No AI provider configured."

    prompt = _build_prompt(market_data, news)
    print(f"[scanner] Using AI provider: {provider}")

    try:
        if provider == "groq":
            raw = _call_groq(prompt)
        elif provider == "openai":
            raw = _call_openai(prompt)
        else:
            raw = _call_anthropic(prompt)

        return _parse_ai_response(raw)

    except json.JSONDecodeError as e:
        print(f"[scanner] AI JSON parse error ({provider}): {e}")
        return [], ""
    except Exception as e:
        print(f"[scanner] AI error ({provider}): {e}\n{traceback.format_exc()}")
        return [], ""


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


def _format_summary(signals: list, outlook: str) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    assets  = " | ".join(s.get("asset", "?") for s in signals)
    return (
        f"🤖 <b>AI TRADE SCAN — {now_str}</b>\n"
        f"{'━' * 32}\n"
        f"Markets scanned: Crypto · Stocks · Metals · Forex\n"
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
    conf_str  = "🔥 HIGH" if conf == "HIGH" else "⚡ MEDIUM"

    entry = signal.get("entry", 0)
    sl    = signal.get("stop_loss", 0)
    tp    = signal.get("take_profit", 0)
    rr    = signal.get("rr_ratio", "1:2")

    return (
        f"{cat_emoji} <b>SIGNAL {rank}/{total} — {signal.get('asset', '?')}</b>\n"
        f"{'─' * 32}\n"
        f"Direction:   {dir_str}\n"
        f"Confidence:  {conf_str}\n"
        f"Category:    {category.upper()}\n\n"
        f"💰 <b>Entry:</b>       {_fmt_price(entry, category)}\n"
        f"🛑 <b>Stop Loss:</b>   {_fmt_price(sl, category)}\n"
        f"🎯 <b>Take Profit:</b> {_fmt_price(tp, category)}\n"
        f"⚖️ <b>Risk:Reward:</b> {rr}\n\n"
        f"📋 <b>Why:</b>\n{signal.get('reasoning', 'No reasoning provided.')}\n"
        f"{'─' * 32}\n"
        f"<i>⚠️ This is not financial advice. Always manage your own risk.</i>"
    )


def run_scan() -> dict:
    print("[scanner] ── Starting multi-market AI scan ──")
    t0 = time.time()

    crypto = _fetch_crypto()
    stocks = _fetch_yfinance(STOCK_TICKERS, "stock")
    metals = _fetch_yfinance(METALS_TICKERS, "metal")
    forex  = _fetch_yfinance(FOREX_TICKERS,  "forex")

    all_assets = crypto + stocks + metals + forex
    print(f"[scanner] {len(all_assets)} assets fetched "
          f"({len(crypto)} crypto, {len(stocks)} stocks, "
          f"{len(metals)} metals, {len(forex)} forex)")

    for a in all_assets:
        a["signals"] = _calculate_signals(a)

    news = _fetch_news()
    print(f"[scanner] {len(news)} headlines fetched")

    ai_signals, outlook = _call_ai_analysis(all_assets, news)

    if not ai_signals:
        print("[scanner] AI unavailable — falling back to technical signals")
        sorted_assets = sorted(
            all_assets,
            key=lambda x: abs(x["signals"].get("score", 0)),
            reverse=True,
        )
        ai_signals = []
        for i, a in enumerate(sorted_assets[:3], 1):
            s = a["signals"]
            ai_signals.append({
                "rank":       i,
                "asset":      a["asset"],
                "symbol":     a["symbol"],
                "category":   a["category"],
                "direction":  s["direction"],
                "entry":      s["entry"],
                "stop_loss":  s["sl"],
                "take_profit": s["tp"],
                "rr_ratio":   "1:2",
                "confidence": "MEDIUM",
                "reasoning":  "Technical only: " + ", ".join(s["technical_reasons"]),
            })
        outlook = "AI analysis unavailable — technical signals only."

    if ai_signals:
        _send_telegram(_format_summary(ai_signals, outlook))
        time.sleep(1)
        for i, sig in enumerate(ai_signals, 1):
            sig["rank"] = i
            _send_telegram(_format_signal(sig, i, len(ai_signals)))
            time.sleep(0.6)

    elapsed = round(time.time() - t0, 1)
    print(f"[scanner] Done in {elapsed}s — {len(ai_signals)} signals sent to Telegram")

    return {
        "status":          "ok",
        "signals":         ai_signals,
        "outlook":         outlook,
        "assets_scanned":  len(all_assets),
        "headlines_found": len(news),
        "elapsed_seconds": elapsed,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    result = run_scan()
    print(json.dumps(result, indent=2, default=str))
