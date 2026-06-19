import os
import json
from flask import Flask, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# --- Zeabur Lifecycle Compatibility Patches ---
def clear_stop():
    """Exposed for Zeabur startup thread management to prevent attribute errors."""
    print("🔄 Zeabur lifecycle hook: clear_stop called.")
    return True

# If the runner tries to access 'bot.clear_stop' as a variable or property
class ClearStopMock:
    def __call__(self): return True
    def __str__(self): return "mock"

# Ensuring it exists as both a function and an attribute if needed
clear_stop = clear_stop

# ---------------------------------------------

@app.route('/')
def check_tradfi_trades():
    print("=== TRADFI POSITION LOG TRIGGERED ===")
    
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    
    if not api_key or not api_secret:
        return jsonify({"status": "error", "message": "Missing API credentials in Zeabur env"}), 400

    try:
        # Initialize standard V5 client
        session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
        
        # Pull positions from the linear engine (where TradFi lives)
        position_response = session.get_positions(category="linear", settleCoin="USDT")
        positions = position_response.get("result", {}).get("list", [])
        
        # Filter for TradFi asset formats
        tradfi_positions = [p for p in positions if ".s" in p.get("symbol", "") or "USD" in p.get("symbol", "")]
        
        # Try to pull a live asset ticker to verify the connection works
        ticker_response = session.get_tickers(category="linear", symbol="XAUUSD.s")
        gold_price = ticker_response.get("result", {}).get("list", [{}])[0].get("lastPrice", "N/A")

        log_data = {
            "status": "connected",
            "live_gold_price": gold_price,
            "total_raw_linear_positions": len(positions),
            "tradfi_positions": tradfi_positions
        }
        
        print("RESULT TO LOG:", json.dumps(log_data, indent=2))
        return jsonify(log_data)

    except Exception as e:
        error_msg = f"API call failed: {str(e)}"
        print(f"❌ {error_msg}")
        return jsonify({"status": "api_error", "details": error_msg}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
