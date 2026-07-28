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



def _engine():
    """The active strategy module — scalp or the original swing path."""
    if config.SCALP_MODE:
        import scalp_strategy
        return scalp_strategy
    import strategy
    return strategy


@app.route("/healthz")
def health():
    return jsonify({"status": "ok",
                    "service": f"{config.SYMBOL.lower()}-{'scalp' if config.SCALP_MODE else 'swing'}-bot",
                    "paper_mode": config.PAPER_MODE})


@app.route("/status")
def status():
    engine  = _engine()
    state   = engine.load_state()
    history = engine.get_history()[-5:]
    return jsonify({
        "status":     "ok",
        "mode":       "scalp" if config.SCALP_MODE else "swing",
        "paper_mode": config.PAPER_MODE,
        "symbol":     config.SYMBOL,
        "state":      state,
        "recent_trades": history,
    })


@app.route("/history")
def history():
    return jsonify(_engine().get_history())


@app.route("/scalp/stats")
def scalp_stats():
    """Live performance summary — the numbers that decide whether to keep going."""
    if not config.SCALP_MODE:
        return jsonify({"status": "disabled", "message": "Set SCALP_MODE=true."})

    import scalp_strategy
    state = scalp_strategy.load_state()
    # Only closed trades carry P&L; entries are the LONG/SHORT records.
    trades = [t for t in scalp_strategy.get_history()
              if t.get("side") == "CLOSE" and t.get("pnl") is not None]

    wins   = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    fees = sum(t.get("fees", 0) for t in trades)

    by_setup = {}
    for t in trades:
        s = by_setup.setdefault(t.get("setup", "?"), {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += 1 if t.get("pnl", 0) > 0 else 0
        s["pnl"] = round(s["pnl"] + t.get("pnl", 0), 4)

    return jsonify({
        "status": "ok",
        "symbol": config.SYMBOL,
        "paper_mode": config.PAPER_MODE,
        "closed_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "net_pnl_usdt": round(state.get("total_pnl_usdt", 0), 4),
        "total_fees_usdt": round(fees, 4),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_usdt": round(state.get("total_pnl_usdt", 0) / len(trades), 4) if trades else None,
        "daily_pnl_usdt": round(state.get("daily_pnl_usdt", 0), 4),
        "trades_today": state.get("trade_count_today", 0),
        "consecutive_losses": state.get("consecutive_losses", 0),
        "in_position": state.get("in_position"),
        "position_side": state.get("side"),
        "current_setup": state.get("setup"),
        "longs": state.get("longs", 0),
        "shorts": state.get("shorts", 0),
        "untracked_position": state.get("untracked_position", False),
        "by_setup": by_setup,
        "market": {
            "category": config.CATEGORY,
            "leverage": config.LEVERAGE,
        },
        "fee_config": {
            "taker_pct": config.TAKER_FEE_PCT,
            "round_trip_pct": config.ROUND_TRIP_FEE_PCT,
            "min_tp_pct": round(config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT, 4),
        },
    })


@app.route("/scalp/signal")
def scalp_signal_debug():
    """
    Live look at what the engine sees right now — indicators, regime, and the
    decision with its reason. The first place to look when it isn't trading.
    """
    if not config.SCALP_MODE:
        return jsonify({"status": "disabled"})

    import perp_client
    import indicators as ind_calc
    import scalp_signal as ss
    import scalp_risk
    import scalp_strategy

    try:
        df = perp_client.get_klines(config.INTERVAL)
        df5 = perp_client.get_klines(config.TREND_INTERVAL, limit=100)
        ind = ind_calc.calculate_scalp(df, df5)
        state = scalp_strategy.load_state()
        sig = ss.get_signal(ind, state)
        balance = perp_client.get_balance()
        allowed, reason = scalp_risk.validate(sig, state, balance, ind)

        # Show what a hypothetical entry would look like in each direction, so
        # the response is useful even when the current signal is HOLD.
        brackets = {
            d: scalp_risk.compute_brackets(ind["price"], ind["atr"], s, None)
            for d, s in (("long", "Buy"), ("short", "Sell"))
        }
        viable, viable_msg = perp_client.check_account_viable(
            balance.get("usdt", 0), ind["price"])

        return jsonify({
            "status": "ok",
            "indicators": ind,
            "signal": {"action": sig.action, "setup": sig.setup,
                       "confidence": sig.confidence, "reason": sig.reason,
                       "target": sig.target},
            "risk": {"allowed": allowed, "reason": reason},
            "account_viable": {"ok": viable, "detail": viable_msg},
            "brackets_if_entered_now": brackets,
            "position_on_exchange": perp_client.get_position(),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route("/tradfi/status")
def tradfi_status():
    if not config.TRADFI_ENABLED:
        return jsonify({"status": "disabled", "message": "Set TRADFI_ENABLED=true to activate."})
    from tradfi_strategy import load_state, get_history
    state   = load_state()
    history = get_history()[-5:]
    return jsonify({
        "status":     "ok",
        "paper_mode": config.TRADFI_PAPER,
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
    """MT5 link diagnostics for a symbol. Use ?symbol=EURUSD to test any instrument."""
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
    mode = "scalp" if config.SCALP_MODE else "swing"
    log.info(f"Starting {config.SYMBOL} {mode} bot "
             f"(paper={config.PAPER_MODE}) on port {port}")

    t = threading.Thread(target=_bot_thread, daemon=True, name=f"{mode}-bot")
    t.start()

    if config.TRADFI_ENABLED:
        log.info(f"TRADFI_ENABLED=true — starting TradFi bot thread (symbol={config.TRADFI_SYMBOL})")
        tt = threading.Thread(target=_tradfi_bot_thread, daemon=True, name="tradfi-bot")
        tt.start()
    else:
        log.info("TRADFI_ENABLED=false — TradFi bot thread not started")

    app.run(host="0.0.0.0", port=port)
