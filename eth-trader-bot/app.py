"""
Flask health server + bot launcher — BTC Directional Candle Bot.
Procfile: web: gunicorn app:app --config gunicorn.conf.py
"""
import os
import threading

from flask import Flask, jsonify, send_from_directory

from logger import get_logger
import config

log = get_logger("app")
app = Flask(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
@app.route("/dashboard")
def dashboard():
    """Serve the monitoring console."""
    return send_from_directory(_HERE, "dashboard.html")


@app.route("/healthz")
def health():
    return jsonify({
        "status":     "ok",
        "service":    "btc-directional-candle-bot",
        "symbol":     config.SYMBOL,
        "category":   config.CATEGORY,
        "leverage":   config.LEVERAGE,
        "btc_qty":    config.BTC_QTY,
        "paper_mode": config.PAPER_MODE,
    })


@app.route("/status")
def status():
    from strategy import load_state, get_history
    state   = load_state()
    history = get_history()

    # Fetch live price for the dashboard's unrealised PnL display
    last_price = 0.0
    try:
        import bybit_client
        df = bybit_client.get_klines_m5()
        if not df.empty:
            last_price = float(df["close"].iloc[-1])
    except Exception as e:
        log.warning(f"Could not fetch live price for /status: {e}")

    # Inject last price into state so the frontend can use it without a
    # separate API call (underscore prefix signals it's a transient field)
    state["_last_price"] = round(last_price, 2)

    return jsonify({
        "status":        "ok",
        "paper_mode":    config.PAPER_MODE,
        "symbol":        config.SYMBOL,
        "category":      config.CATEGORY,
        "leverage":      config.LEVERAGE,
        "btc_qty":       str(config.BTC_QTY),
        "state":         state,
        "recent_trades": history[-20:],   # last 20 for the history table
    })


@app.route("/history")
def history():
    from strategy import get_history
    return jsonify(get_history())


# ── TradFi routes (kept intact — only runs when TRADFI_ENABLED=true) ──────────

@app.route("/tradfi/status")
def tradfi_status():
    if not config.TRADFI_ENABLED:
        return jsonify({"status": "disabled",
                        "message": "Set TRADFI_ENABLED=true to activate."})
    from tradfi_strategy import load_state, get_history
    state   = load_state()
    history = get_history()[-5:]
    return jsonify({
        "status":        "ok",
        "paper_mode":    config.TRADFI_PAPER,
        "symbol":        config.TRADFI_SYMBOL,
        "state":         state,
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
    from flask import request
    import tradfi_client as tc
    base = request.args.get("symbol", config.TRADFI_SYMBOL)
    out = {
        "configured_symbol": config.TRADFI_SYMBOL,
        "requested_symbol":  base,
        "tradfi_mode":       config.TRADFI_MODE,
        "interval_min":      config.TRADFI_INTERVAL,
    }
    try:
        out.update(tc.diagnose(base))
    except Exception as e:
        out["diagnose_error"] = str(e)
    return jsonify(out)


@app.route("/tradfi/symbols")
def tradfi_symbols():
    from flask import request
    import tradfi_client as tc
    search = request.args.get("search")
    try:
        syms = tc.list_symbols(search)
        return jsonify({"count": len(syms), "search": search, "symbols": syms})
    except Exception as e:
        return jsonify({"error": str(e), "search": search})


# ── Bot threads ────────────────────────────────────────────────────────────────

def _bot_thread():
    from scheduler import run_bot
    run_bot()


def _tradfi_bot_thread():
    from scheduler import run_tradfi_bot
    run_tradfi_bot()


def _start_bot_threads():
    """Start bot threads. Safe to call multiple times — guarded by env var."""
    if os.environ.get("_BTC_BOT_STARTED") == "1":
        return
    os.environ["_BTC_BOT_STARTED"] = "1"

    log.info(
        f"Starting BTC Directional Candle Bot | "
        f"symbol={config.SYMBOL} leverage={config.LEVERAGE}× "
        f"qty={config.BTC_QTY} BTC | paper={config.PAPER_MODE}"
    )
    threading.Thread(target=_bot_thread, daemon=True, name="btc-bot").start()

    if config.TRADFI_ENABLED:
        log.info(f"TRADFI_ENABLED=true — starting TradFi thread (symbol={config.TRADFI_SYMBOL})")
        threading.Thread(target=_tradfi_bot_thread, daemon=True, name="tradfi-bot").start()
    else:
        log.info("TRADFI_ENABLED=false — TradFi thread not started")


# Start threads when the module is imported by gunicorn (no --config needed).
# The env-var guard prevents a double-start if __main__ also calls this.
_start_bot_threads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
