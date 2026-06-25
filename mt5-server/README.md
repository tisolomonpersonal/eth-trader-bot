# mt5-server — MetaTrader5 on Zeabur (Wine)

Runs MetaTrader5 terminal + the `mt5linux` XML-RPC bridge inside a Docker container on Zeabur.  
The Flask trading bot (`bot-app`) connects to this service via Zeabur's internal private network.

## Architecture

```
Zeabur Project
├── bot-app (Flask + gunicorn)          ← public URL
│     └── connects to mt5-server:8001
└── mt5-server (Docker / Wine)          ← internal only
      ├── Xvfb (virtual display)
      ├── MetaTrader5 terminal (Wine)
      └── mt5linux XML-RPC bridge :8001
```

## Deploy on Zeabur (step by step)

### 1. Add the mt5-server service

1. Open your Zeabur project → **Add Service** → **Git** → select your repo
2. Set **Root Directory** to `mt5-server`
3. Zeabur auto-detects the `Dockerfile` — click **Deploy**

### 2. Set environment variables on mt5-server

| Variable | Value |
|---|---|
| `MT5_PORT` | `8001` |
| `VNC_PASSWORD` | *(optional — enables VNC remote desktop on port 5900)* |

### 3. Set environment variables on bot-app

| Variable | Value |
|---|---|
| `MT5_HOST` | `mt5-server.zeabur.internal` |
| `MT5_PORT` | `8001` |
| `MT5_LOGIN` | Your broker account number |
| `MT5_PASSWORD` | Your broker password |
| `MT5_SERVER` | Your broker server name (e.g. `ICMarkets-Demo`) |

> `mt5-server.zeabur.internal` is Zeabur's private DNS — it only works between services in the same project. No public port exposure needed.

### 4. First-run time

The first cold start takes **3–5 minutes** because:
- Wine prefix is initialised
- Python 3.10 for Windows is installed inside Wine
- MetaTrader5 Python package is installed in Wine Python
- MT5 terminal (`mt5setup.exe`) is installed

Subsequent restarts are **under 30 seconds** because the Wine prefix is persisted on a Zeabur volume (`/root/.wine`).

## Persistent volume

Mount `/root/.wine` as a persistent volume in Zeabur so the Wine prefix (and MT5 terminal installation) survive redeploys.

Zeabur dashboard → mt5-server → **Storage** → Add Volume → path `/root/.wine`

## VNC remote desktop (optional)

Set `VNC_PASSWORD=yourpassword` on the mt5-server service, then expose port **5900** in Zeabur.  
Connect with any VNC client to see the MT5 terminal desktop.

## Healthcheck

The container runs `healthcheck.py` every 30 s — it checks that port 8001 is accepting TCP connections. Zeabur shows the service as healthy once the RPC bridge is up.

## Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `MT5_PORT` | `8001` | XML-RPC port the bridge listens on |
| `SCREEN_RESOLUTION` | `1024x768x24` | Xvfb display resolution |
| `VNC_PASSWORD` | *(unset)* | Set to enable VNC on port 5900 |
| `WINEPREFIX` | `/root/.wine` | Wine prefix directory (should be a volume) |
