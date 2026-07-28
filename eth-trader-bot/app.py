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
    import bybit_client

    state   = load_state()
    history = get_history()

    # ── Live price ────────────────────────────────────────────────────────────
    last_price = 0.0
    try:
        df = bybit_client.get_klines_m5()
        if not df.empty:
            last_price = float(df["close"].iloc[-1])
    except Exception as e:
        log.warning(f"Could not fetch live price for /status: {e}")
    state["_last_price"] = round(last_price, 2)

    # ── Live position (always query Bybit directly) ───────────────────────────
    # This ensures the dashboard shows the real position even if the bot thread
    # hasn't reconciled state yet (e.g. fresh deploy, restart mid-trade).
    live_position = None
    try:
        pos = bybit_client.get_position()
        if pos:
            raw_side = pos.get("side", "")
            side = "LONG" if raw_side == "Buy" else "SHORT" if raw_side == "Sell" else None
            if side:
                live_position = {
                    "in_position":    True,
                    "side":           side,
                    "entry_price":    float(pos.get("avgPrice",      0) or 0),
                    "qty":            float(pos.get("size",           0) or 0),
                    "sl_price":       float(pos.get("stopLoss",       0) or 0),
                    "tp_price":       float(pos.get("takeProfit",     0) or 0),
                    "liq_price":      float(pos.get("liqPrice",       0) or 0),
                    "unrealised_pnl": float(pos.get("unrealisedPnl",  0) or 0),
                    "entry_time":     pos.get("createdTime"),
                }
                # Merge into state so the rest of the response is consistent
                if not state.get("in_position"):
                    for k in ("in_position","side","entry_price","qty",
                              "sl_price","tp_price","entry_time"):
                        state[k] = live_position[k]
    except Exception as e:
        log.warning(f"Could not fetch live position for /status: {e}")

    # ── Live balance ──────────────────────────────────────────────────────────
    balance = {"usdt": 0.0, "btc": 0.0}
    try:
        balance = bybit_client.get_balance()
    except Exception as e:
        log.warning(f"Could not fetch balance for /status: {e}")

    return jsonify({
        "status":        "ok",
        "paper_mode":    config.PAPER_MODE,
        "symbol":        config.SYMBOL,
        "category":      config.CATEGORY,
        "leverage":      config.LEVERAGE,
        "btc_qty":       str(config.BTC_QTY),
        "state":         state,
        "live_position": live_position,
        "balance":       balance,
        "recent_trades": history[-20:],
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
