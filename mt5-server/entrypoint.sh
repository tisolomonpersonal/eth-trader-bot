#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Boot sequence for the MT5-under-Wine Zeabur service
#
# Flow:
#   1. Start Xvfb virtual display
#   2. Init Wine prefix (first run only)
#   3. Install Python for Windows inside Wine (first run only)
#   4. Install MetaTrader5 + mt5linux packages in Wine Python (first run only)
#   5. Install & launch MT5 terminal (first run only)
#   6. Start the mt5linux XML-RPC bridge on port $MT5_PORT
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MT5_PORT="${MT5_PORT:-8001}"
WINEPREFIX="${WINEPREFIX:-/root/.wine}"
WINE_PYTHON="${WINEPREFIX}/drive_c/Python310/python.exe"
MT5_TERMINAL="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"
SETUP_DONE_FLAG="${WINEPREFIX}/.mt5_setup_complete"

log() { echo "[mt5-server $(date '+%H:%M:%S')] $*"; }

# ── Step 1: Virtual display ───────────────────────────────────────────────────
log "Starting Xvfb on DISPLAY=:99 ..."
Xvfb :99 -screen 0 "${SCREEN_RESOLUTION:-1024x768x24}" -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:99
sleep 3

# Optional VNC for remote viewing (disabled by default; set VNC_PASSWORD to enable)
if [ -n "${VNC_PASSWORD:-}" ]; then
    log "Starting x11vnc on port 5900 ..."
    x11vnc -display :99 -forever -nopw -shared -rfbport 5900 \
           -passwd "${VNC_PASSWORD}" -bg -o /var/log/vnc.log
fi

# ── Step 2: One-time Wine + MT5 setup ────────────────────────────────────────
if [ ! -f "$SETUP_DONE_FLAG" ]; then
    log "First-run setup — this takes 3-5 minutes..."

    # 2a. Init Wine prefix
    log "Initialising Wine prefix (win64)..."
    WINEARCH=win64 WINEPREFIX="$WINEPREFIX" wineboot --init 2>/dev/null
    sleep 8

    # 2b. Silence Wine popups
    WINEARCH=win64 WINEPREFIX="$WINEPREFIX" \
        wine reg add 'HKCU\Software\Wine\DllOverrides' \
        /v winemenubuilder.exe /t REG_SZ /d "" /f 2>/dev/null || true
    WINEARCH=win64 WINEPREFIX="$WINEPREFIX" \
        wine reg add 'HKCU\Software\Wine\WineDbg' \
        /v ShowCrashDialog /t REG_DWORD /d 0 /f 2>/dev/null || true

    # 2c. Install Python 3.10 for Windows
    if [ ! -f "$WINE_PYTHON" ]; then
        log "Installing Python 3.10 (Windows) in Wine..."
        wine /opt/mt5-setup/python-win.exe \
            InstallAllUsers=0 TargetDir="C:\\Python310" \
            Include_pip=1 Include_test=0 \
            /quiet 2>/dev/null || true
        sleep 15
    fi

    # 2d. Install MetaTrader5 and mt5linux in Wine Python
    if [ -f "$WINE_PYTHON" ]; then
        log "Installing MetaTrader5 Python package in Wine..."
        wine "$WINE_PYTHON" -m pip install --quiet MetaTrader5 mt5linux 2>/dev/null || true
        sleep 5
    else
        log "WARNING: Wine Python not found at $WINE_PYTHON — MT5 package install skipped"
    fi

    # 2e. Install MT5 terminal
    if [ ! -f "$MT5_TERMINAL" ]; then
        log "Installing MetaTrader5 terminal..."
        wine /opt/mt5-setup/mt5setup.exe /auto 2>/dev/null &
        MT5_INSTALL_PID=$!
        # Wait up to 90 seconds for MT5 to install
        for i in $(seq 1 90); do
            sleep 1
            if [ -f "$MT5_TERMINAL" ]; then
                log "MT5 terminal installed successfully (${i}s)"
                break
            fi
        done
        kill $MT5_INSTALL_PID 2>/dev/null || true
    fi

    touch "$SETUP_DONE_FLAG"
    log "First-run setup complete."
fi

# ── Step 3: Launch MT5 terminal ───────────────────────────────────────────────
if [ -f "$MT5_TERMINAL" ]; then
    log "Launching MetaTrader5 terminal..."
    wine "$MT5_TERMINAL" &
    MT5_PID=$!
    sleep 20  # allow terminal to fully initialise and auto-login
    log "MT5 terminal running (PID ${MT5_PID})"
else
    log "WARNING: MT5 terminal not found — broker connection will fail until terminal is installed"
fi

# ── Step 4: Start mt5linux XML-RPC bridge ─────────────────────────────────────
log "Starting mt5linux RPC server on 0.0.0.0:${MT5_PORT} ..."

if [ ! -f "$WINE_PYTHON" ]; then
    log "FATAL: Wine Python not found — cannot start RPC server"
    exit 1
fi

# The server runs inside Wine Python with the MetaTrader5 package available
wine "$WINE_PYTHON" -c "
import sys, socket
sys.path.insert(0, r'C:\\Python310\\Lib\\site-packages')
try:
    from mt5linux.server import run
    print('[mt5linux] RPC server starting on 0.0.0.0:${MT5_PORT}', flush=True)
    run(host='0.0.0.0', port=${MT5_PORT})
except ImportError as e:
    print(f'[mt5linux] Import error: {e}', flush=True)
    # Fallback: inline minimal XML-RPC server if mt5linux package missing
    import MetaTrader5 as mt5
    from xmlrpc.server import SimpleXMLRPCServer
    server = SimpleXMLRPCServer(('0.0.0.0', ${MT5_PORT}), allow_none=True, logRequests=False)
    server.register_instance(mt5)
    server.register_introspection_functions()
    print('[mt5linux] Fallback RPC server running', flush=True)
    server.serve_forever()
" &
RPC_PID=$!

log "RPC bridge PID: $RPC_PID"
log "MT5 server ready. Bot should set MT5_HOST=<this-service-host> MT5_PORT=${MT5_PORT}"

# Keep container alive; restart RPC if it dies
while true; do
    if ! kill -0 $RPC_PID 2>/dev/null; then
        log "RPC server died — restarting..."
        wine "$WINE_PYTHON" -c "
import sys
sys.path.insert(0, r'C:\\Python310\\Lib\\site-packages')
from mt5linux.server import run
run(host='0.0.0.0', port=${MT5_PORT})
" &
        RPC_PID=$!
        log "RPC server restarted (PID $RPC_PID)"
    fi
    sleep 30
done
