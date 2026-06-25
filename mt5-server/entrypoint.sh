#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Boot sequence for the MT5-under-Wine Zeabur service
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: intentionally NO set -e / set -euo pipefail.
# Many Wine/winetricks commands return non-zero on warnings; we handle each
# individually with || true so one hiccup doesn't kill the whole container.
# ─────────────────────────────────────────────────────────────────────────────

MT5_PORT="${MT5_PORT:-8001}"
WINEPREFIX="${WINEPREFIX:-/root/.wine}"
WINE_PYTHON="${WINEPREFIX}/drive_c/Python310/python.exe"
MT5_DIR="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5"
MT5_TERMINAL="${MT5_DIR}/terminal64.exe"
SETUP_DONE="${WINEPREFIX}/.mt5_first_run_complete"

export DISPLAY=:99
export WINEPREFIX
export WINEARCH=win64
export WINEDEBUG=-all          # suppress Wine debug noise

log() { echo "[mt5-server $(date '+%H:%M:%S')] $*" >&2; }

# ── 1. Virtual display ────────────────────────────────────────────────────────
log "Starting Xvfb (${SCREEN_RES:-1024x768x24}) ..."
Xvfb :99 -screen 0 "${SCREEN_RES:-1024x768x24}" -nolisten tcp &
XVFB_PID=$!

# Wait until Xvfb is actually answering (up to 15 s)
for i in $(seq 1 15); do
    xdpyinfo -display :99 >/dev/null 2>&1 && break
    sleep 1
done
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    log "WARNING: Xvfb did not respond after 15 s — continuing anyway"
fi
log "Xvfb running (PID $XVFB_PID)"

# Optional VNC
if [ -n "${VNC_PASSWORD:-}" ]; then
    log "Starting VNC on port 5900 ..."
    x11vnc -display :99 -forever -shared -rfbport 5900 \
           -passwd "${VNC_PASSWORD}" -bg -o /var/log/vnc.log 2>/dev/null || true
fi

# ── 2. First-run: Wine + Windows components + MT5 setup ──────────────────────
if [ ! -f "$SETUP_DONE" ]; then
    log "═══ FIRST-RUN SETUP (takes 5-10 min) ═══"

    # 2a. Init Wine prefix
    log "[1/6] Initialising Wine64 prefix..."
    wineboot --init 2>/dev/null || true
    sleep 10

    # Suppress Wine popup dialogs
    wine reg add 'HKCU\Software\Wine\DllOverrides' \
        /v winemenubuilder.exe /t REG_SZ /d "" /f 2>/dev/null || true
    wine reg add 'HKCU\Software\Wine\WineDbg' \
        /v ShowCrashDialog /t REG_DWORD /d 0 /f 2>/dev/null || true

    # 2b. winetricks — the "small Windows" step
    #     vcrun2019 = Visual C++ 2019 runtime (MT5 hard requirement)
    #     dotnet48  = .NET 4.8 (used by MT5 internal components)
    log "[2/6] Installing Windows components via winetricks..."
    log "      vcrun2019 (Visual C++ 2019) ..."
    winetricks -q vcrun2019 2>/dev/null || winetricks -q vcrun2015 2>/dev/null || true
    log "      dotnet48 (.NET 4.8) — this takes a few minutes ..."
    winetricks -q dotnet48 2>/dev/null || winetricks -q dotnet461 2>/dev/null || true
    log "  winetricks done."

    # 2c. Python 3.10 for Windows inside Wine
    log "[3/6] Installing Python 3.10 for Windows in Wine..."
    if [ -f /opt/mt5-setup/python-win.exe ]; then
        wine /opt/mt5-setup/python-win.exe \
            InstallAllUsers=0 \
            TargetDir="C:\\Python310" \
            Include_pip=1 \
            Include_test=0 \
            Include_doc=0 \
            /quiet 2>/dev/null || true
        sleep 15
        [ -f "$WINE_PYTHON" ] \
            && log "  Python 3.10 installed." \
            || log "  WARNING: Python 3.10 installer may have failed."
    else
        log "  WARNING: python-win.exe not found in /opt/mt5-setup"
    fi

    # 2d. MetaTrader5 + mt5linux in Wine Python
    if [ -f "$WINE_PYTHON" ]; then
        log "[4/6] pip install MetaTrader5 mt5linux (in Wine Python)..."
        wine "$WINE_PYTHON" -m pip install --quiet MetaTrader5 mt5linux 2>/dev/null || true
        sleep 5
    else
        log "[4/6] SKIP — Wine Python not found"
    fi

    # 2e. MT5 terminal
    log "[5/6] Installing MetaTrader5 terminal..."
    if [ -f /opt/mt5-setup/mt5setup.exe ] && [ ! -f "$MT5_TERMINAL" ]; then
        wine /opt/mt5-setup/mt5setup.exe /auto 2>/dev/null &
        INSTALL_PID=$!
        for i in $(seq 1 120); do
            sleep 1
            [ -f "$MT5_TERMINAL" ] && { log "  MT5 terminal installed (${i}s)"; break; }
        done
        kill "$INSTALL_PID" 2>/dev/null || true
        [ -f "$MT5_TERMINAL" ] \
            || log "  WARNING: MT5 terminal not found after install wait"
    elif [ -f "$MT5_TERMINAL" ]; then
        log "  MT5 terminal already installed."
    else
        log "  WARNING: mt5setup.exe not found in /opt/mt5-setup"
    fi

    touch "$SETUP_DONE"
    log "═══ FIRST-RUN COMPLETE ═══"
fi

# ── 3. Write start.ini — MT5 auto-login ──────────────────────────────────────
if [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ] && [ -n "${MT5_SERVER:-}" ]; then
    log "Writing start.ini for auto-login (login=${MT5_LOGIN}, server=${MT5_SERVER})..."
    mkdir -p "${MT5_DIR}"
    /app/make-start-ini.sh > "${MT5_DIR}/start.ini"
    log "  start.ini written."
else
    log "WARNING: MT5_LOGIN / MT5_PASSWORD / MT5_SERVER not set — MT5 won't auto-login."
fi

# ── 4. Launch MT5 terminal ────────────────────────────────────────────────────
if [ -f "$MT5_TERMINAL" ]; then
    log "Launching MT5 terminal (Wine, /portable)..."
    wine "$MT5_TERMINAL" /portable 2>/dev/null &
    sleep 20
    log "MT5 terminal launched."
else
    log "WARNING: MT5 terminal binary not found — skipping terminal launch."
fi

# ── 5. mt5linux XML-RPC bridge ────────────────────────────────────────────────
start_rpc_bridge() {
    if [ ! -f "$WINE_PYTHON" ]; then
        log "FATAL: Wine Python not found — RPC bridge cannot start."
        return 1
    fi
    # Write the server script to a temp file (avoids heredoc PID capture issues)
    cat > /tmp/mt5_rpc_server.py <<'PYEOF'
import os, sys
sys.path.insert(0, r'C:\Python310\Lib\site-packages')
port = int(os.environ.get('MT5_PORT', '8001'))
try:
    from mt5linux.server import run
    print(f'[mt5linux] Starting mt5linux RPC on 0.0.0.0:{port}', flush=True)
    run(host='0.0.0.0', port=port)
except ImportError as e:
    print(f'[mt5linux] mt5linux not installed ({e}) — using fallback XML-RPC', flush=True)
    import MetaTrader5 as mt5
    from xmlrpc.server import SimpleXMLRPCServer
    srv = SimpleXMLRPCServer(('0.0.0.0', port), allow_none=True, logRequests=False)
    srv.register_instance(mt5)
    srv.register_introspection_functions()
    print(f'[mt5linux] Fallback RPC running on 0.0.0.0:{port}', flush=True)
    srv.serve_forever()
PYEOF
    wine "$WINE_PYTHON" /tmp/mt5_rpc_server.py &
    echo $!   # only line on stdout — captured cleanly by $()
}

log "Starting mt5linux XML-RPC bridge on 0.0.0.0:${MT5_PORT} ..."
RPC_PID=$(start_rpc_bridge 2>/dev/null)
log "RPC bridge PID: ${RPC_PID:-UNKNOWN}"
log ""
log "  ┌─────────────────────────────────────────────────┐"
log "  │  mt5linux RPC  →  0.0.0.0:${MT5_PORT}               │"
log "  │  MT5 terminal  →  Wine (auto-login via ini)     │"
log "  │  Virtual disp  →  Xvfb :99                     │"
log "  └─────────────────────────────────────────────────┘"

# ── 6. Keep-alive watchdog ────────────────────────────────────────────────────
while true; do
    sleep 30

    # Check RPC bridge
    if [ -n "${RPC_PID:-}" ] && [ "$RPC_PID" -eq "$RPC_PID" ] 2>/dev/null; then
        if ! kill -0 "$RPC_PID" 2>/dev/null; then
            log "RPC bridge exited — restarting..."
            RPC_PID=$(start_rpc_bridge 2>/dev/null)
            log "  RPC bridge restarted (PID: ${RPC_PID:-UNKNOWN})"
        fi
    fi

    # Check Xvfb
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        log "Xvfb crashed — restarting..."
        Xvfb :99 -screen 0 "${SCREEN_RES:-1024x768x24}" -nolisten tcp &
        XVFB_PID=$!
    fi
done
