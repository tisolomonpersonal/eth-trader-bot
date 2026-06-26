"""
Flask health server + bot launcher.
Procfile: web: python app.py
"""
import os
import threading
import json

from flask import Flask, jsonify

from logger import get_logger
import config

log = get_logger("app")
app = Flask(__name__)


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
        "state":      state,
        "recent_trades": history,
    })


@app.route("/history")
def history():
    from strategy import get_history
    return jsonify(get_history())


def _bot_thread():
    from scheduler import run_bot
    run_bot()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting BNB bot (paper={config.PAPER_MODE}) on port {port}")

    t = threading.Thread(target=_bot_thread, daemon=True, name="bnb-bot")
    t.start()

    app.run(host="0.0.0.0", port=port)
