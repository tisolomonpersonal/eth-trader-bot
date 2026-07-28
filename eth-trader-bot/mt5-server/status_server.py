#!/usr/bin/env python3
"""
status_server.py — /status HTTP endpoint for the mt5-server container.

Runs on the Linux host (not inside Wine).
Connects to the mt5linux XML-RPC bridge on localhost:MT5_PORT
and returns a JSON health report on port STATUS_PORT (default 8002).

Accessible via:
  - Zeabur internal: http://mt5-server.zeabur.internal:8002/status
  - Browser (after exposing port 8002 in Zeabur dashboard)
"""
import json
import os
import sys
import xmlrpc.client
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

MT5_RPC_PORT = int(os.environ.get("MT5_PORT", "8001"))
STATUS_PORT  = int(os.environ.get("STATUS_PORT", "8002"))

BOOT_TIME = datetime.now(timezone.utc).isoformat()


def query_mt5() -> dict[str, Any]:
    """Connect to the mt5linux XML-RPC bridge and pull account/terminal info."""
    proxy = xmlrpc.client.ServerProxy(
        f"http://localhost:{MT5_RPC_PORT}",
        allow_none=True,
        use_builtin_types=True,
    )
    result: dict[str, Any] = {
        "rpc_reachable": False,
        "mt5_connected": False,
    }

    try:
        # terminal_info() — general terminal status
        tinfo = proxy.terminal_info()
        if tinfo:
            d = dict(tinfo) if hasattr(tinfo, "_asdict") else tinfo
            result["rpc_reachable"]   = True
            result["mt5_connected"]   = bool(d.get("connected", False))
            result["terminal_build"]  = d.get("build", None)
            result["data_path"]       = d.get("data_path", None)
            result["expert_enabled"]  = bool(d.get("trade_expert", False))

        # account_info() — only meaningful when connected
        if result["mt5_connected"]:
            ainfo = proxy.account_info()
            if ainfo:
                d = dict(ainfo) if hasattr(ainfo, "_asdict") else ainfo
                result["account"] = {
                    "login":    d.get("login"),
                    "name":     d.get("name"),
                    "server":   d.get("server"),
                    "currency": d.get("currency"),
                    "balance":  d.get("balance"),
                    "equity":   d.get("equity"),
                    "margin":   d.get("margin"),
                    "margin_free": d.get("margin_free"),
                    "leverage": d.get("leverage"),
                    "trade_allowed": bool(d.get("trade_allowed", False)),
                    "limit_orders":  d.get("limit_orders"),
                }

        # last_error()
        err = proxy.last_error()
        if err:
            result["last_error"] = list(err) if isinstance(err, (list, tuple)) else err

    except ConnectionRefusedError:
        result["error"] = f"RPC bridge not reachable on localhost:{MT5_RPC_PORT}"
    except Exception as exc:
        result["rpc_reachable"] = True   # reached but something else failed
        result["error"] = str(exc)

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    result["container_boot"] = BOOT_TIME
    result["rpc_port"] = MT5_RPC_PORT
    return result


class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass   # silence access logs

    def do_GET(self):
        if self.path in ("/", "/status", "/healthz"):
            data = query_mt5()
            body = json.dumps(data, indent=2, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')


def main():
    server = HTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler)
    print(
        f"[status_server] Listening on 0.0.0.0:{STATUS_PORT} — "
        f"connecting to mt5linux RPC on localhost:{MT5_RPC_PORT}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[status_server] Stopped.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
