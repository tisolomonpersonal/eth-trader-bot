"""
Flask health server + bot launcher.
Procfile: web: python app.py
"""
import os
import threading
import json

from flask import Flask, jsonify, send_from_directory

from logger import get_logger
import config

log = get_logger("app")
app = Flask(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
@app.route("/dashboard")
def dashboard():
    """Serve the monitoring console (single-file HTML)."""
    return send_from_directory(_HERE, "dashboard.html")



@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "service": "bnb-spot-bot",
                    "paper_mode": config.PAPER_MODE})


@app.route("/status")
def status():
    from strategy import load_state, get_history
    state   = load_state()
    history = get_history()[-5:]  # last 5 trades
    return jsonify({
        "status":     "ok",
        "paper_mode": config.PAPER_MODE,
        "symbol":     config.SYMBOL,
        "state":      state,
        "recent_trades": history,
    })


@app.route("/history")
def history():
    from strategy import get_history
    return jsonify(get_history())


@app.route("/tradfi/status")
def tradfi_status():
    if not config.TRADFI_ENABLED:
        return jsonify({"status": "disabled", "message": "Set TRADFI_ENABLED=true to activate."})
    from tradfi_strategy import load_state, get_history
    state   = load_state()
    history = get_history()[-5:]
    return jsonify({
        "status":     "ok",
        "paper_mode": config.PAPER_MODE,
        "symbol":     config.TRADFI_SYMBOL,
        "state":      state,
        "recent_trades": history,
    })


@app.route("/tradfi/history")
def tradfi_history():
    if not config.TRADFI_ENABLED:
        return jsonify({"status": "disabled"})
    from tradfi_strategy import get_history
    return jsonify(get_history())


@app.route("/tradfi/debug")
def tradfi_debug():
    """Diagnostics: shows exactly what the market-open check sees for a symbol.
    Use ?symbol=EURUSD to test any instrument (defaults to config.TRADFI_SYMBOL)."""
    from datetime import datetime, timezone
    from flask import request
    import tradfi_client as tc

    base = request.args.get("symbol", config.TRADFI_SYMBOL)
    out = {
        "configured_symbol": config.TRADFI_SYMBOL,
        "requested_symbol":  base,
        "tradfi_mode":       config.TRADFI_MODE,
        "interval_min":      config.TRADFI_INTERVAL,
        "paper_mode":        config.PAPER_MODE,
    }
    try:
        resolved = tc.resolve_symbol(base)
        out["resolved_symbol"] = resolved
    except Exception as e:
        out["resolve_error"] = str(e)
        return jsonify(out)

    try:
        df = tc.get_klines(base, limit=3)
        out["candles_returned"] = 0 if df is None else int(len(df))
        if df is not None and not df.empty:
            last = df["ts"].iloc[-1].to_pydatetime()
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            out["newest_candle_utc"] = last.isoformat()
            out["newest_candle_age_min"] = round(age, 1)
            out["last_close"] = float(df["close"].iloc[-1])
    except Exception as e:
        out["klines_error"] = str(e)

    try:
        out["is_market_open"] = tc.is_market_open(base)
    except Exception as e:
        out["market_open_error"] = str(e)

    return jsonify(out)


@app.route("/tradfi/symbols")
def tradfi_symbols():
    """List available TradFi symbols. Use ?search=EUR to filter."""
    from flask import request
    import tradfi_client as tc
    search = request.args.get("search")
    try:
        syms = tc.list_symbols(search)
        return jsonify({"count": len(syms), "search": search, "symbols": syms})
    except Exception as e:
        return jsonify({"error": str(e), "search": search})


def _bot_thread():
    from scheduler import run_bot
    run_bot()


def _tradfi_bot_thread():
    from scheduler import run_tradfi_bot
    run_tradfi_bot()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting BNB bot (paper={config.PAPER_MODE}) on port {port}")

    t = threading.Thread(target=_bot_thread, daemon=True, name="bnb-bot")
    t.start()

    if config.TRADFI_ENABLED:
        log.info(f"TRADFI_ENABLED=true — starting TradFi bot thread (symbol={config.TRADFI_SYMBOL})")
        tt = threading.Thread(target=_tradfi_bot_thread, daemon=True, name="tradfi-bot")
        tt.start()
    else:
        log.info("TRADFI_ENABLED=false — TradFi bot thread not started")

    app.run(host="0.0.0.0", port=port)
