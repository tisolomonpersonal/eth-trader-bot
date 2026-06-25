#!/usr/bin/env bash
# make-start-ini.sh
# ─────────────────────────────────────────────────────────────────────────────
# Generates the MT5 start.ini config file that makes the terminal
# automatically open and log in to your broker when the container starts.
#
# MT5 reads this file on launch when started with the /portable flag.
# Without it, the terminal opens to a blank login screen and just sits there.
#
# Required env vars:
#   MT5_LOGIN    — broker account number
#   MT5_PASSWORD — broker account password
#   MT5_SERVER   — broker server name (e.g. ICMarkets-Demo)
#
# Optional:
#   MT5_DATA_PATH — path to MT5 data dir (default: C:\Users\root\AppData\Roaming\MetaQuotes\Terminal\...)
# ─────────────────────────────────────────────────────────────────────────────

cat <<INI
[Common]
Login=${MT5_LOGIN:-0}
Password=${MT5_PASSWORD:-}
Server=${MT5_SERVER:-}
; Auto-login on startup — no manual click needed
AutoLogin=true
; Keep the connection alive
KeepAlive=true

[Experts]
; Allow automated trading (needed for bot to place orders via MT5)
AllowLiveTrading=true
AllowDllImports=true

[StartUp]
; Run terminal in portable mode so data stays in the MT5 install dir
; (matches the /portable flag passed to terminal64.exe)
DataPath=

[Network]
; Use the broker server from MT5_SERVER env var
Server=${MT5_SERVER:-}
INI
