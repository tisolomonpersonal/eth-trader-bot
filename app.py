"""Flask health server + BNB bot launcher."""
import os, sys, signal, threading, logging
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)

@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "service": "bnb-usdt-bot"})

@app.route("/status")
def status():
    try:
        from bot import get_state
        state = get_state()
        return jsonify({"status": "ok", "bot": state})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

def _stop_handler(sig, frame):
    """Notify Telegram then exit cleanly on SIGTERM/SIGINT."""
    log.info("Received shutdown signal — notifying Telegram.")
    try:
        from bot import send_telegram
        send_telegram("🔴 <b>BNB Bot Stopped</b>\nService shut down or redeployed.")
    except Exception:
        pass
    sys.exit(0)

def _run_bot():
    from bot import run_bot
    run_bot()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT,  _stop_handler)

    t = threading.Thread(target=_run_bot, daemon=True, name="bnb-bot")
    t.start()
    log.info(f"Flask starting on port {port}")
    app.run(host="0.0.0.0", port=port)
