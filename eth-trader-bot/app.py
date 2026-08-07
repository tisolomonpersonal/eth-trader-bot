"""
Flask health server + bot launcher — BTC 4H BB Short Bot.
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
        "service":    "btc-4h-bb-short-bot",
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
        df = bybit_client.get_klines_h4()
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

    # ── Leverage: what we asked for vs what Bybit actually has ────────────────
    # These can disagree. set_leverage returns 110043 both when the value is
    # already correct and when the margin mode refuses a per-symbol change, so
    # the startup warning alone cannot tell you which one is live.
    state["_leverage_configured"] = config.LEVERAGE
    state["_leverage_effective"] = bybit_client.get_effective_leverage()

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


# ── Grid routes (only active when GRID_ENABLED=true) ─────────────────────────

@app.route("/grid/status")
def grid_status():
    import grid_config as gc
    if not gc.GRID_ENABLED:
        return jsonify({"status": "disabled",
                        "message": "Set GRID_ENABLED=true to activate."})

    import grid_strategy
    state = grid_strategy.load_state()

    positions = {"long": None, "short": None}
    try:
        import grid_client
        positions = grid_client.get_positions()
    except Exception as e:
        # Status must stay reachable even when Bybit is unhappy.
        positions = {"error": str(e)[:200]}

    return jsonify({
        "status":      "halted" if state.get("halted") else "ok",
        "paper_mode":  gc.GRID_PAPER_MODE,
        "dry_run":     gc.GRID_DRY_RUN,
        "symbol":      gc.GRID_SYMBOL,
        "qty":         gc.GRID_QTY,
        "leverage":    gc.GRID_LEVERAGE,
        "max_per_side": gc.GRID_MAX_POSITION_BTC,
        "positions":   positions,
        "state":       state,
    })


# ── Bot threads ────────────────────────────────────────────────────────────────

def _bot_thread():
    from scheduler import run_bot
    run_bot()


def _grid_bot_thread():
    from scheduler import run_grid_bot
    run_grid_bot()


def _start_bot_threads():
    """Start bot threads. Safe to call multiple times — guarded by env var."""
    if os.environ.get("_BTC_BOT_STARTED") == "1":
        return
    os.environ["_BTC_BOT_STARTED"] = "1"

    if config.BB_ENABLED:
        log.info(
            f"BB_ENABLED=true — starting BTC 4H BB Short Bot | "
            f"symbol={config.SYMBOL} leverage={config.LEVERAGE}× "
            f"qty={config.BTC_QTY} BTC | paper={config.PAPER_MODE}"
        )
        threading.Thread(target=_bot_thread, daemon=True, name="btc-bot").start()
    else:
        log.info("BB_ENABLED=false — BB short thread not started (grid owns the symbol)")

    import grid_config as gc
    if gc.GRID_ENABLED:
        log.info(
            f"GRID_ENABLED=true — starting grid thread "
            f"(symbol={gc.GRID_SYMBOL} qty={gc.GRID_QTY} x{gc.GRID_LEVERAGE} "
            f"paper={gc.GRID_PAPER_MODE} dry_run={gc.GRID_DRY_RUN})"
        )
        threading.Thread(target=_grid_bot_thread, daemon=True, name="grid-bot").start()
    else:
        log.info("GRID_ENABLED=false — grid thread not started")


# Start threads when the module is imported by gunicorn (no --config needed).
# The env-var guard prevents a double-start if __main__ also calls this.
_start_bot_threads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
