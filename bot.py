import os
import json
import time
from flask import Flask, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# --- 1. Zeabur Required Template Lifecycle Hooks ---

def clear_stop():
    """Called by Zeabur framework to handle graceful shutdowns."""
    print("🔄 Zeabur lifecycle hook: clear_stop called.")
    return True

def run_loop():
    """
    Called by the main bot thread framework. 
    Keeps the background engine running safely without crashing the worker.
    """
    print("🤖 Bot background loop initialized and listening...")
    while True:
        # Prevent CPU pegging while keeping the thread alive
        time.sleep(10) 

def performance_summary():
    """Called by internal status monitors or /api/status dashboards."""
    return {
        "status": "operational",
        "engine": "Bybit V5 TradFi",
        "uptime_state": "healthy"
    }

# --- 2. Standard Flask Application Logic ---

@app.route('/')
@app.route('/api/status')
def check_tradfi_trades():
    print("=== TRADFI POSITION DISCOVERY TRIGGERED ===")
    
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    
    if not api_key or not api_secret:
        return jsonify({"status": "error", "message": "Missing API credentials"}), 400

    try:
        # Initialize unified V5 client
        session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
        
        # Pull positions from the linear engine (Where Bybit settles TradFi)
        position_response = session.get_positions(category="linear", settleCoin="USDT")
        positions = position_response.get("result", {}).get("list", [])
        
        # Filter for TradFi zero-fee suffixes (.s) and asset strings
        tradfi_positions = [p for p in positions if ".s" in p.get("symbol", "") or "USD" in p.get("symbol", "")]
        
        # Verify ticker streaming connectivity via Gold Spot-CFD
        ticker_response = session.get_tickers(category="linear", symbol="XAUUSD.s")
        gold_price = ticker_response.get("result", {}).get("list", [{}])[0].get("lastPrice", "N/A")

        log_data = {
            "status": "connected",
            "live_gold_price": gold_price,
            "total_raw_linear_positions": len(positions),
            "tradfi_positions": tradfi_positions,
            "performance": performance_summary()
        }
        
        print("EXCHANGE DATA:", json.dumps(log_data, indent=2))
        return jsonify(log_data)

    except Exception as e:
        error_msg = f"API verification failed: {str(e)}"
        print(f"❌ {error_msg}")
        return jsonify({"status": "api_error", "details": error_msg}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
