# mt5-server/Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Runs MetaTrader5 terminal + mt5linux XML-RPC bridge under Wine on Linux.
# Deploy this as a separate "mt5-server" service on Zeabur.
# The Flask bot connects via Zeabur internal DNS:
#   MT5_HOST=mt5-server.zeabur.internal   MT5_PORT=8001
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

LABEL maintainer="eth-trader-bot"
LABEL description="MetaTrader5 under Wine — mt5linux RPC server for Zeabur"

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99
ENV MT5_PORT=8001
# Wine screen size for the virtual display
ENV SCREEN_RESOLUTION=1024x768x24

# ── 1. System packages ────────────────────────────────────────────────────────
RUN dpkg --add-architecture i386 \
 && apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        ca-certificates gnupg2 wget curl software-properties-common \
        xvfb x11vnc xdotool \
        python3 python3-pip \
        cabextract unzip p7zip-full \
        supervisor procps net-tools \
 && wget -qO /usr/share/keyrings/winehq.gpg \
        "https://dl.winehq.org/wine-builds/winehq.key" \
 && echo "deb [signed-by=/usr/share/keyrings/winehq.gpg] \
        https://dl.winehq.org/wine-builds/ubuntu/ jammy main" \
        > /etc/apt/sources.list.d/winehq.list \
 && apt-get update -qq \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

# ── 2. Host-side Python tools (healthcheck helper, log relay) ─────────────────
RUN pip3 install --quiet requests

# ── 3. Pre-download Windows installers (build-time cache → fast restarts) ────
RUN mkdir -p /opt/mt5-setup

# Python 3.10 for Windows x64 — needed for MetaTrader5 pip package
RUN wget -q \
    "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe" \
    -O /opt/mt5-setup/python-win.exe

# MT5 terminal installer (official MQL5 CDN)
RUN wget -q \
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    -O /opt/mt5-setup/mt5setup.exe

# ── 4. Copy runtime scripts ───────────────────────────────────────────────────
WORKDIR /app
COPY entrypoint.sh       /app/entrypoint.sh
COPY supervisord.conf    /app/supervisord.conf
COPY healthcheck.py      /app/healthcheck.py
RUN chmod +x /app/entrypoint.sh

# ── 5. Ports ──────────────────────────────────────────────────────────────────
# 8001 — mt5linux XML-RPC bridge (internal to Zeabur)
# 5900 — VNC (optional; expose only if you need remote desktop access)
EXPOSE 8001 5900

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD python3 /app/healthcheck.py

ENTRYPOINT ["/app/entrypoint.sh"]
