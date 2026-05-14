from flask import Flask, jsonify, request
import json
import os
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)

# Paths
STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE = Path(__file__).parent / "log.txt"

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_last_lines(n=20):
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, 'r') as f:
        lines = f.readlines()
    return lines[-n:]

@app.route('/')
def index():
    state = load_state()
    return jsonify({
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "state": state
    })

@app.route('/api/status')
def api_status():
    state = load_state()
    return jsonify({
        "equity": state.get("equity"),
        "daily_pnl": state.get("daily_pnl"),
        "consecutive_loss": state.get("consecutive_loss"),
        "lifetime_pnl": state.get("lifetime_pnl"),
        "paused": state.get("paused"),
        "pause_reason": state.get("pause_reason"),
        "position": state.get("position"),
        "last_trade": state.get("last_trade"),
        "trades_today": state.get("trades_today"),
        "max_trades_per_day": state.get("max_trades_per_day")
    })

@app.route('/api/pause', methods=['POST'])
def api_pause():
    data = request.get_json() or {}
    minutes = int(data.get("minutes", 30))
    reason = data.get("reason", "User requested pause")
    state = load_state()
    state["paused"] = True
    state["pause_reason"] = reason
    state["pause_until"] = (datetime.now(timezone.utc).timestamp() + minutes * 60)
    save_state(state)
    return jsonify({"status": "paused", "minutes": minutes, "reason": reason})

@app.route('/api/resume', methods=['POST'])
def api_resume():
    state = load_state()
    state["paused"] = False
    state["pause_reason"] = None
    state["pause_until"] = None
    save_state(state)
    return jsonify({"status": "resumed"})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    state = load_state()
    state["trading_enabled"] = False
    save_state(state)
    return jsonify({"status": "trading stopped"})

@app.route('/api/start', methods=['POST'])
def api_start():
    state = load_state()
    state["trading_enabled"] = True
    save_state(state)
    return jsonify({"status": "trading started"})

@app.route('/api/log')
def api_log():
    lines = get_last_lines(50)
    return jsonify({"log": lines})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify({
            "max_daily_loss_usd": os.environ.get("MAX_DAILY_LOSS_USD", "2"),
            "max_consec_loss_usd": os.environ.get("MAX_CONSEC_LOSS_USD", "4"),
            "check_interval": os.environ.get("CHECK_INTERVAL", "3600"),
            "qty": os.environ.get("QTY", "0.04"),
            "leverage": os.environ.get("LEVERAGE", "45")
        })
    else:
        # For simplicity, only allow updating env-like values via POST
        data = request.get_json() or {}
        # In production, you'd want proper validation and env update logic
        return jsonify({"status": "config update not implemented (use Zeabur env vars)"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
