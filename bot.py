import os
import sys
import json
from pybit.unified_trading import HTTP

def test_tradfi_positions():
    print("=== BYBIT TRADFI API CONNECTIVITY TEST ===")
    
    # 1. Pull environment variables from Zeabur
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ ERROR: Missing API credentials in environment variables.")
        print("Please ensure BYBIT_API_KEY and BYBIT_API_SECRET are set in Zeabur.")
        sys.exit(1)
        
    print("✅ Found API credentials. Initializing V5 session...")

    try:
        # Initialize the standard V5 Unified client
        session = HTTP(
            testnet=False,
            api_key=api_key,
            api_secret=api_secret
        )
        
        print("\n--- [TEST 1: Fetching Active TradFi Positions] ---")
        # Bybit processes TradFi (Forex, Commodities, Stocks) inside the 'linear' engine
        position_response = session.get_positions(
            category="linear",
            settleCoin="USDT"  # TradFi balances settle in USDT margin
        )
        
        positions = position_response.get("result", {}).get("list", [])
        
        # Filter for known TradFi suffixes (.s for zero-fee) or symbol types
        # Common pairs: XAUUSD.s, EURUSD.s, GBPUSD.s, US30.s
        tradfi_positions = [p for p in positions if ".s" in p.get("symbol", "") or "USD" in p.get("symbol", "")]

        if not tradfi_positions:
            print("⚠️ No active TradFi positions found on this sub/main account.")
            print("If you have open trades in the UI, they might be isolated to a non-API subaccount.")
            print(f"Total raw linear positions found: {len(positions)}")
        else:
            print(f"🎉 Success! Found {len(tradfi_positions)} TradFi positions:")
            print(json.dumps(tradfi_positions, indent=2))

        print("\n--- [TEST 2: Connectivity Check via Gold Ticker] ---")
        ticker_response = session.get_tickers(
            category="linear",
            symbol="XAUUSD.s"
        )
        gold_price = ticker_response.get("result", {}).get("list", [{}])[0].get("lastPrice")
        print(f"✅ Connection verified. Live Gold (XAUUSD.s) Price: ${gold_price}")

    except Exception as e:
        print("\n❌ API Call Failed!")
        print(f"Exception Type: {type(e).__name__}")
        print(f"Error Details: {str(e)}")
        print("\nPossible root causes:")
        print("1. Your API key lacks 'Contract/Derivatives' read/write permissions.")
        print("2. Your TradFi funds are in a distinct sub-account whose keys you haven't linked.")

if __name__ == "__main__":
    test_tradfi_positions()
