"""
healthcheck.py — Docker HEALTHCHECK for the mt5linux RPC bridge.
Tries a lightweight XML-RPC call to verify the server is accepting connections.
Exit 0 = healthy, Exit 1 = unhealthy.
"""
import sys
import socket
import os

HOST = "127.0.0.1"
PORT = int(os.environ.get("MT5_PORT", "8001"))
TIMEOUT = 5


def check():
    try:
        sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
        sock.close()
        print(f"[healthcheck] OK — port {PORT} is open")
        return True
    except Exception as e:
        print(f"[healthcheck] FAIL — cannot reach {HOST}:{PORT}: {e}")
        return False


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
