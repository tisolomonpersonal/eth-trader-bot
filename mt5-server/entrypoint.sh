#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Boot sequence for the MT5-under-Wine Zeabur service
#
# What "small Windows" means here:
#   winetricks installs Visual C++ 2019 runtime + .NET 4.8 — the minimal
#   Windows components MetaTrader5 terminal requires to launch at all.
#   Without them the terminal silently crashes on startup.
#
# Boot flow:
#   1. Xvfb virtual display
#   2. Wine prefix init (first run only)
#   3. winetricks: vcrun2019 + dotnet48 (first run only)
#   4. Python 3.10 for Windows inside Wine (first run only)
#   5. MetaTrader5 + mt5linux Python packages in Wine Python (first run only)
#   6. MT5 terminal install (first run only)
#   7. Write start.ini → MT5 auto-logs in on every boot
#   8. Launch MT5 terminal
#   9. Start mt5linux XML-RPC bridge on port $MT5_PORT
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MT5_PORT="${MT5_PORT:-8001}"
WINEPREFIX="${WINEPREFIX:-/root/.wine}"
WINE_PYTHON="${WINEPREFIX}/drive_c/Python310/python.exe"
MT5_DIR="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5"
MT5_TERMINAL="${MT5_DIR}/terminal64.exe"
SETUP_DONE="${WINEPREFIX}/.mt5_first_run_complete"

export DISPLAY=:99
export WINEPREFIX
export WINEARCH=win64
# Suppress Wine debug noise
export WINEDEBUG=-all

log() { echo "[mt5-server $(date '+%H:%M:%S')] $*"; }

# ── 1. Virtual display ────────────────────────────────────────────────────────
log "Starting Xvfb (${SCREEN_RES:-1024x768x24}) ..."
Xvfb :99 -screen 0 "${SCREEN_RES:-1024x768x24}" -nolisten tcp &
sleep 2

# Optional VNC — set VNC_PASSWORD env var to enable remote desktop
if [ -n "${VNC_PASSWORD:-}" ]; then
    log "Starting VNC on port 5900 ..."
    x11vnc -display :99 -forever -shared -rfbport 5900 \
           -passwd "${VNC_PASSWORD}" -bg -o /var/log/vnc.log 2>/dev/null
fi

# ── 2. First-run: Wine + Windows components + MT5 setup ──────────────────────
if [ ! -f "$SETUP_DONE" ]; then
    log "═══ FIRST-RUN SETUP (takes 5-10 min) ═══"

    # 2a. Initialise 64-bit Wine prefix
    log "[1/6] Initialising Wine64 prefix..."
    wineboot --init 2>/dev/null
    sleep 8

    # Silence Wine autorun popups
    wine reg add 'HKCU\Software\Wine\DllOverrides' \
        /v winemenubuilder.exe /t REG_SZ /d "" /f 2>/dev/null || true
    wine reg add 'HKCU\Software\Wine\WineDbg' \
        /v ShowCrashDialog /t REG_DWORD /d 0 /f 2>/dev/null || true

    # 2b. winetricks — install the "small Windows" components MT5 needs
    #     vcrun2019 = Visual C++ 2019 Redistributable (MT5 hard requirement)
    #     dotnet48  = .NET Framework 4.8 (used by MT5 internal components)
    log "[2/6] Installing Windows components via winetricks (vcrun2019 + dotnet48)..."
    log "      This is the 'small Windows' step — installs MS DLLs MT5 needs."
    WINEDEBUG=-all winetricks -q vcrun2019 2>/dev/null || {
        log "  vcrun2019 failed — trying vcrun2015..."
        WINEDEBUG=-all winetricks -q vcrun2015 2>/dev/null || true
    }
    WINEDEBUG=-all winetricks -q dotnet48 2>/dev/null || {
        log "  dotnet48 failed — trying dotnet461..."
        WINEDEBUG=-all winetricks -q dotnet461 2>/dev/null || true
    }
    log "  winetricks done."

    # 2c. Install Python 3.10 for Windows inside Wine
    log "[3/6] Installing Python 3.10 (Windows x64) in Wine..."
    if [ ! -f "$WINE_PYTHON" ]; then
        wine /opt/mt5-setup/python-win.exe \
            InstallAllUsers=0 \
            TargetDir="C:\\Python310" \
            Include_pip=1 \
            Include_test=0 \
            Include_doc=0 \
            /quiet 2>/dev/null || true
        sleep 15
        if [ -f "$WINE_PYTHON" ]; then
            log "  Python 3.10 installed at C:\\Python310"
        else
            log "  WARNING: Python installer may have failed — check /tmp/python-install.log"
        fi
    else
        log "  Python 3.10 already present."
    fi

    # 2d. Install MetaTrader5 + mt5linux in Wine Python
    if [ -f "$WINE_PYTHON" ]; then
        log "[4/6] Installing MetaTrader5 + mt5linux in Wine Python..."
        wine "$WINE_PYTHON" -m pip install --quiet MetaTrader5 mt5linux 2>/dev/null || \
            log "  pip install had warnings — continuing..."
        sleep 5
    else
        log "[4/6] SKIP — Wine Python not found, pip install skipped."
    fi

    # 2e. Install MT5 terminal
    log "[5/6] Installing MetaTrader5 terminal..."
    if [ ! -f "$MT5_TERMINAL" ]; then
        wine /opt/mt5-setup/mt5setup.exe /auto 2>/dev/null &
        INSTALL_PID=$!
        for i in $(seq 1 120); do
            sleep 1
            [ -f "$MT5_TERMINAL" ] && { log "  MT5 terminal installed (${i}s)"; break; }
        done
        kill "$INSTALL_PID" 2>/dev/null || true
        [ ! -f "$MT5_TERMINAL" ] && log "  WARNING: MT5 terminal not found after install wait"
    else
        log "  MT5 terminal already installed."
    fi

    touch "$SETUP_DONE"
    log "═══ FIRST-RUN COMPLETE ═══"
fi

# ── 3. Write start.ini — MT5 auto-login ──────────────────────────────────────
# This is the config file that makes MT5 automatically open and log in
# to your broker every time the container starts — no manual clicking needed.
if [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ] && [ -n "${MT5_SERVER:-}" ]; then
    log "Writing start.ini for auto-login (login=${MT5_LOGIN}, server=${MT5_SERVER})..."
    mkdir -p "${MT5_DIR}"
    /app/make-start-ini.sh > "${MT5_DIR}/start.ini"
    log "  start.ini written — MT5 will auto-login on startup."
else
    log "WARNING: MT5_LOGIN / MT5_PASSWORD / MT5_SERVER not set."
    log "         MT5 terminal will open but won't auto-login."
    log "         Set these env vars in the Zeabur dashboard."
fi

# ── 4. Launch MT5 terminal ────────────────────────────────────────────────────
if [ -f "$MT5_TERMINAL" ]; then
    log "Launching MetaTrader5 terminal (Wine)..."
    wine "$MT5_TERMINAL" /portable 2>/dev/null &
    MT5_PID=$!
    log "  MT5 terminal starting (PID $MT5_PID) — waiting 20s for init..."
    sleep 20
    log "  MT5 terminal should now be running and logged in."
else
    log "WARNING: MT5 terminal binary not found — RPC bridge will start but"
    log "         trading calls will fail until terminal is installed."
fi

# ── 5. Start mt5linux XML-RPC bridge ─────────────────────────────────────────
log "Starting mt5linux XML-RPC bridge on 0.0.0.0:${MT5_PORT} ..."

start_rpc() {
    if [ ! -f "$WINE_PYTHON" ]; then
        log "FATAL: Wine Python not found — cannot start RPC server."
        return 1
    fi
    wine "$WINE_PYTHON" - <<'PYEOF' &
import sys
sys.path.insert(0, r'C:\Python310\Lib\site-packages')
try:
    from mt5linux.server import run
    print('[mt5linux] RPC server starting...', flush=True)
    run(host='0.0.0.0', port=__import__('os').environ.get('MT5_PORT', '8001'))
except ImportError as e:
    print(f'[mt5linux] mt5linux not installed in Wine Python: {e}', flush=True)
    print('[mt5linux] Falling back to minimal XML-RPC server...', flush=True)
    import MetaTrader5 as mt5
    from xmlrpc.server import SimpleXMLRPCServer
    port = int(__import__('os').environ.get('MT5_PORT', '8001'))
    srv = SimpleXMLRPCServer(('0.0.0.0', port), allow_none=True, logRequests=False)
    srv.register_instance(mt5)
    srv.register_introspection_functions()
    print(f'[mt5linux] Fallback RPC server running on 0.0.0.0:{port}', flush=True)
    srv.serve_forever()
PYEOF
    echo $!
}

RPC_PID=$(start_rpc)
log "  RPC bridge PID: $RPC_PID"
log "Ready. Bot should connect with MT5_HOST=<this-service> MT5_PORT=${MT5_PORT}"
log ""
log "  ┌─────────────────────────────────────────────────┐"
log "  │  mt5linux RPC bridge  →  0.0.0.0:${MT5_PORT}        │"
log "  │  MT5 terminal         →  Wine (auto-login)      │"
log "  │  Virtual display      →  Xvfb :99              │"
log "  └─────────────────────────────────────────────────┘"

# Keep-alive: restart RPC bridge if it exits
while true; do
    if [ -n "$RPC_PID" ] && ! kill -0 "$RPC_PID" 2>/dev/null; then
        log "RPC bridge exited — restarting..."
        RPC_PID=$(start_rpc)
        log "  RPC bridge restarted (PID $RPC_PID)"
    fi
    sleep 30
done
