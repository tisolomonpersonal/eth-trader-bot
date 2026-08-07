"""
Telegram notification layer.
Sends immediate alerts for trades/errors and hourly summaries.
Never sends minute-by-minute updates — HOLD cycles are silent.
"""
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from logger import get_logger

log = get_logger("telegram")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ghc(usdt: float) -> str:
    return f"{usdt * config.GHC_RATE:,.0f} GHC"


def send(text: str) -> bool:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                  config.TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            log.warning(f"Telegram API error: {r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ── Immediate alerts ───────────────────────────────────────────────────────────

def alert_started() -> None:
    mode = "📄 Paper Trading" if config.PAPER_MODE else "💰 Live Trading"
    net  = "(Testnet)" if config.BYBIT_TESTNET else "(Mainnet)"
    send(
        f"🤖 <b>BTC 4H BB Short Bot Started</b>\n"
        f"{'─'*28}\n"
        f"Exchange:   Bybit Perpetuals {net}\n"
        f"Mode:       {mode}\n"
        f"Pair:       {config.SYMBOL}\n"
        f"Size:       {config.BTC_QTY} BTC @ {config.LEVERAGE}×\n"
        f"Setup:      BB({config.BB_PERIOD},{config.BB_STD}) touch, "
        f"MA{config.MA_LONG} downtrend filter\n"
        f"Target:     MA{config.MA_SHORT} (trailing)\n"
        f"Stop:       BB-touch high, capped {config.ATR_CAP_MULT}× ATR({config.ATR_PERIOD})\n"
        f"Daily caps: {config.MAX_TRADES_PER_DAY} trades / "
        f"-${config.MAX_DAILY_LOSS_USDT:.0f} loss\n"
        f"Cycle: 5 min idle, 60 s in position | Summary: hourly"
    )


def alert_buy(price: float, qty: float, usdt: float, confidence: int, reason: str) -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    send(
        f"🟢 <b>BUY EXECUTED{paper}</b>\n"
        f"{'─'*28}\n"
        f"Action:   BUY\n"
        f"Pair:     BNB/USDT Spot\n"
        f"Price:    ${price:,.4f}\n"
        f"Qty:      {qty:.4f} BNB\n"
        f"Amount:   {_ghc(usdt)} (~${usdt:.2f})\n"
        f"AI conf:  {confidence}/100\n"
        f"Time:     {_now()}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )


def alert_sell(price: float, qty: float, usdt: float, pnl: float,
               reason: str, trigger: str = "AI") -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    emoji = "🔴"
    send(
        f"{emoji} <b>SELL EXECUTED{paper}</b>\n"
        f"{'─'*28}\n"
        f"Action:   SELL ({trigger})\n"
        f"Pair:     BNB/USDT Spot\n"
        f"Price:    ${price:,.4f}\n"
        f"Qty:      {qty:.4f} BNB\n"
        f"Value:    {_ghc(usdt)} (~${usdt:.2f})\n"
        f"P&L:      <b>${pnl:+.2f}</b> ({_ghc(pnl)})\n"
        f"Time:     {_now()}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )


def alert_stop_loss(price: float, qty: float, pnl: float, entry: float) -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    send(
        f"🛑 <b>STOP LOSS TRIGGERED{paper}</b>\n"
        f"{'─'*28}\n"
        f"Entry:    ${entry:,.4f}\n"
        f"Exit:     ${price:,.4f}\n"
        f"Qty:      {qty:.4f} BNB\n"
        f"Loss:     <b>${pnl:+.2f}</b> ({_ghc(pnl)})\n"
        f"Time:     {_now()}"
    )


def alert_take_profit(price: float, qty: float, pnl: float, entry: float) -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    send(
        f"✅ <b>TAKE PROFIT REACHED{paper}</b>\n"
        f"{'─'*28}\n"
        f"Entry:    ${entry:,.4f}\n"
        f"Exit:     ${price:,.4f}\n"
        f"Qty:      {qty:.4f} BNB\n"
        f"Profit:   <b>${pnl:+.2f}</b> ({_ghc(pnl)})\n"
        f"Time:     {_now()}"
    )


def alert_api_error(context: str, error: str) -> None:
    send(
        f"⚠️ <b>API ERROR</b>\n"
        f"{'─'*28}\n"
        f"Context: {context}\n"
        f"Error:   {error[:300]}\n"
        f"Time:    {_now()}\n\n"
        f"Bot will retry automatically."
    )


def alert_ai_failure(providers_tried: list[str]) -> None:
    send(
        f"⚠️ <b>AI FAILURE</b>\n"
        f"{'─'*28}\n"
        f"All AI providers failed: {', '.join(providers_tried)}\n"
        f"Falling back to rule-based signals.\n"
        f"Time: {_now()}"
    )


def alert_stopped() -> None:
    send(
        f"🔴 <b>BNB Bot Stopped</b>\n"
        f"{'─'*28}\n"
        f"Service was shut down or redeployed.\n"
        f"Time: {_now()}"
    )


def alert_critical(message: str) -> None:
    send(
        f"🚨 <b>CRITICAL ERROR</b>\n"
        f"{'─'*28}\n"
        f"{message[:400]}\n"
        f"Time: {_now()}"
    )


# ── TradFi alerts ───────────────────────────────────────────────────────────────

def alert_tradfi_started(symbol: str) -> None:
    mode = "📄 Paper Trading" if config.PAPER_MODE else "💰 Live Trading"
    net  = "(Testnet)" if config.BYBIT_TESTNET else "(Mainnet)"
    send(
        f"🤖 <b>TradFi Bot Started</b>\n"
        f"{'─'*28}\n"
        f"Exchange:   Bybit TradFi {net}\n"
        f"Mode:       {mode}\n"
        f"Instrument: {symbol}\n"
        f"Max trade:  ${config.TRADFI_MAX_INVESTMENT_USDT:.2f} USDT\n"
        f"Stop loss:  {config.TRADFI_STOP_LOSS_PCT}%\n"
        f"Take profit:{config.TRADFI_TAKE_PROFIT_PCT}%\n"
        f"Min confidence: {config.TRADFI_MIN_AI_CONFIDENCE}/100\n"
        f"Cycle: every {config.TRADFI_CYCLE_SECONDS}s | Summary: every 1 hour\n\n"
        f"⚠️ TradFi has real market hours (not 24/7) — the bot will hold "
        f"when the market is closed."
    )


def alert_tradfi_entry(symbol: str, side: str, price: float, qty: float, usdt: float,
                        confidence: int, reason: str) -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    emoji = "🟢" if side == "BUY" else "🔻"
    send(
        f"{emoji} <b>TradFi {side} EXECUTED{paper}</b>\n"
        f"{'─'*28}\n"
        f"Instrument: {symbol}\n"
        f"Price:    {price:,.4f}\n"
        f"Qty:      {qty}\n"
        f"Amount:   ${usdt:.2f} USDT\n"
        f"AI conf:  {confidence}/100\n"
        f"Time:     {_now()}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )


def alert_tradfi_exit(symbol: str, trigger: str, price: float, qty: float,
                       pnl: float, reason: str) -> None:
    paper = " [PAPER]" if config.PAPER_MODE else ""
    emoji = {"SL": "🛑", "TP": "✅"}.get(trigger, "🔴")
    label = {"SL": "STOP LOSS", "TP": "TAKE PROFIT", "AI-SELL": "AI EXIT"}.get(trigger, trigger)
    send(
        f"{emoji} <b>TradFi {label}{paper}</b>\n"
        f"{'─'*28}\n"
        f"Instrument: {symbol}\n"
        f"Exit price: {price:,.4f}\n"
        f"Qty:        {qty}\n"
        f"P&L:        <b>${pnl:+.2f}</b> USDT\n"
        f"Time:       {_now()}\n\n"
        f"<b>Reason:</b>\n{reason}"
    )


def alert_tradfi_stopped() -> None:
    send(
        f"🔴 <b>TradFi Bot Stopped</b>\n"
        f"{'─'*28}\n"
        f"Service was shut down or redeployed.\n"
        f"Time: {_now()}"
    )


def send_tradfi_hourly_summary(state: dict, ind: dict, balance: dict, market_open: bool) -> None:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "📄 PAPER | " if config.PAPER_MODE else ""
    symbol = state.get("symbol", config.TRADFI_SYMBOL)

    if state.get("in_position"):
        entry    = float(state.get("entry_price", 0))
        qty      = float(state.get("qty", 0))
        side     = state.get("side", "Buy")
        price    = ind["price"]
        unreal_u = (price - entry) * qty if side == "Buy" else (entry - price) * qty
        unreal_p = (unreal_u / (entry * qty) * 100) if entry > 0 and qty > 0 else 0
        holdings = (f"{side} {qty} {symbol} @ {entry:,.4f}\n"
                    f"SL: {state.get('sl_price',0):,.4f} | TP: {state.get('tp_price',0):,.4f}")
        unrealized = f"${unreal_u:+.2f} ({unreal_p:+.2f}%)"
        acct_val   = balance["usdt"] + unreal_u
    else:
        holdings   = "None"
        unrealized = "—"
        acct_val   = balance["usdt"]

    last_trade = (
        f"{state.get('last_action','NONE')} @ {state.get('entry_price',0):,.4f}"
        if state.get("last_action") not in (None, "NONE", "HOLD")
        else "None"
    )

    send(
        f"📊 <b>{mode}TradFi Bot Report — {symbol}</b>\n"
        f"{'─'*28}\n"
        f"Time:          {now}\n"
        f"Market:        {'🟢 Open' if market_open else '⚪ Closed'}\n"
        f"Current Price: {ind['price']:,.4f}\n"
        f"Trend:         {ind['trend']} (RSI {ind['rsi']})\n"
        f"AI Decision:   {state.get('last_action','HOLD')}\n"
        f"Current Position: {holdings}\n"
        f"Account Value: ${acct_val:.2f} USDT\n"
        f"Unrealized P/L:{unrealized}\n"
        f"Daily P&L:     ${state.get('daily_pnl_usdt',0):+.2f} | "
        f"Total: ${state.get('total_pnl_usdt',0):+.2f}\n"
        f"Last Trade:    {last_trade}\n"
        f"Bot Status:    ✅ Running\n\n"
        f"<b>Reason:</b>\n{state.get('last_reason','Monitoring markets.')}"
    )


# ── Hourly summary ─────────────────────────────────────────────────────────────

def send_hourly_summary(state: dict, ind: dict, balance: dict) -> None:
    """Send the structured hourly report. Format matches the spec."""
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "📄 PAPER | " if config.PAPER_MODE else ""

    # Position details
    if state.get("in_position"):
        entry    = float(state.get("entry_price", 0))
        qty      = float(state.get("qty", 0))
        price    = ind["price"]
        unreal_u = (price - entry) * qty
        unreal_p = (price - entry) / entry * 100 if entry > 0 else 0
        holdings = (f"{qty:.4f} BNB @ ${entry:,.4f}\n"
                    f"SL: ${state.get('sl_price',0):,.4f} | TP: ${state.get('tp_price',0):,.4f}")
        unrealized = f"${unreal_u:+.2f} ({unreal_p:+.2f}%)"
        acct_val   = balance["usdt"] + qty * price
    else:
        holdings   = "None"
        unrealized = "—"
        acct_val   = balance["usdt"]

    last_trade = (
        f"{state.get('last_action','NONE')} @ ${state.get('entry_price',0):,.4f}"
        if state.get("last_action") not in (None, "NONE", "HOLD")
        else "None"
    )

    send(
        f"📊 <b>{mode}BNB Spot Bot Report</b>\n"
        f"{'─'*28}\n"
        f"Time:          {now}\n"
        f"Current Price: ${ind['price']:,.4f}\n"
        f"Trend:         {ind['trend']} (RSI {ind['rsi']})\n"
        f"AI Decision:   {state.get('last_action','HOLD')}\n"
        f"Current Holdings: {holdings}\n"
        f"Account Value: {_ghc(acct_val)} (~${acct_val:.2f})\n"
        f"Unrealized P/L:{unrealized}\n"
        f"Daily P&L:     ${state.get('daily_pnl_usdt',0):+.2f} | "
        f"Total: ${state.get('total_pnl_usdt',0):+.2f}\n"
        f"Last Trade:    {last_trade}\n"
        f"Bot Status:    ✅ Running\n\n"
        f"<b>Reason:</b>\n{state.get('last_reason','Monitoring markets.')}"
    )
