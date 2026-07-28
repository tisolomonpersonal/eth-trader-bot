# mt5-server/Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Runs MetaTrader5 terminal under Wine on Linux for Zeabur.
#
# Key pieces:
#   • WineHQ stable (win64)
#   • Xvfb virtual display
#   • winetricks → installs vcrun2019 + dotnet48 (required by MT5 terminal)
#   • Python 3.10 for Windows inside Wine
#   • MetaTrader5 Python package + mt5linux inside Wine Python
#   • MT5 terminal auto-login via start.ini (reads MT5_LOGIN/PASSWORD/SERVER)
#   • mt5linux XML-RPC bridge on port 8001
#
# The Flask bot connects via Zeabur internal DNS:
#   MT5_HOST = mt5-server.zeabur.internal   MT5_PORT = 8001
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

LABEL description="MetaTrader5 under Wine — mt5linux RPC server"

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99
ENV MT5_PORT=8001
ENV SCREEN_RES=1024x768x24

# ── 1. WineHQ stable + all dependencies ──────────────────────────────────────
RUN dpkg --add-architecture i386 \
 && apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        ca-certificates gnupg2 wget curl software-properties-common \
        # virtual display
        xvfb x11vnc \
        # winetricks runtime deps
        cabextract unzip p7zip-full zenity \
        # general utils
        python3 python3-pip \
        procps net-tools supervisor \
 && wget -qO /usr/share/keyrings/winehq.gpg \
        "https://dl.winehq.org/wine-builds/winehq.key" \
 && echo "deb [signed-by=/usr/share/keyrings/winehq.gpg] \
        https://dl.winehq.org/wine-builds/ubuntu/ jammy main" \
        > /etc/apt/sources.list.d/winehq.list \
 && apt-get update -qq \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

# ── 2. winetricks (latest) ────────────────────────────────────────────────────
RUN wget -q "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks" \
        -O /usr/local/bin/winetricks \
 && chmod +x /usr/local/bin/winetricks

# ── 3. Pre-download Windows installers at build time ─────────────────────────
RUN mkdir -p /opt/mt5-setup

# Python 3.10 for Windows x64 (needed by MetaTrader5 pip package)
RUN wget -q \
    "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe" \
    -O /opt/mt5-setup/python-win.exe

# MT5 terminal installer (official MQL5 CDN)
RUN wget -q \
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    -O /opt/mt5-setup/mt5setup.exe

# ── 4. Runtime scripts ────────────────────────────────────────────────────────
WORKDIR /app
COPY entrypoint.sh     /app/entrypoint.sh
COPY healthcheck.py    /app/healthcheck.py
COPY make-start-ini.sh /app/make-start-ini.sh
COPY status_server.py  /app/status_server.py
RUN chmod +x /app/entrypoint.sh /app/make-start-ini.sh

ENV STATUS_PORT=8002

# ── 5. Ports ──────────────────────────────────────────────────────────────────
# 8001 — mt5linux XML-RPC bridge (internal, bot connects here)
# 8002 — /status JSON endpoint (expose in Zeabur dashboard to view in browser)
# 5900 — VNC remote desktop (set VNC_PASSWORD env var to enable)
EXPOSE 8001 8002 5900

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD python3 /app/healthcheck.py

ENTRYPOINT ["/app/entrypoint.sh"]
