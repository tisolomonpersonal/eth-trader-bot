# mt5-server — MetaTrader5 on Zeabur (Wine)

Runs MetaTrader5 terminal under Wine on Linux as a Zeabur Docker service.
The Flask trading bot connects to it via Zeabur's private internal network.

## How it works

```
Container boot sequence
─────────────────────────────────────────────────────────
1. Xvfb          — virtual display so Wine/MT5 has a screen
2. wineboot      — initialise the Wine Windows environment
3. winetricks    — install vcrun2019 (Visual C++ 2019) + dotnet48
                   ← this is the "small Windows" step:
                      these are the Windows DLLs MT5 needs to run
4. Wine Python   — install Python 3.10 for Windows inside Wine
5. pip (Wine)    — MetaTrader5 + mt5linux packages
6. mt5setup.exe  — install MT5 terminal inside Wine
7. start.ini     — auto-login config (reads MT5_LOGIN/PASSWORD/SERVER)
8. terminal64.exe— MT5 terminal launches and auto-logs in to broker
9. mt5linux RPC  — XML-RPC bridge on port 8001 → Flask bot connects here
```

> **First cold start takes 5–10 minutes** because steps 2–6 only run once.
> After that Wine prefix is persisted on a Zeabur volume — restarts take < 30 s.

## Deploy on Zeabur

### Step 1 — Add the mt5-server service

1. Zeabur dashboard → **Add Service** → **Git** → select your repo
2. Set **Root Directory** to `mt5-server`
3. Zeabur detects the `Dockerfile` → click **Deploy**

### Step 2 — Add a persistent volume

Zeabur dashboard → mt5-server → **Storage** → Add Volume
- Mount path: `/root/.wine`
- Name: `wine-prefix`

This keeps the Wine prefix (and MT5 terminal installation) across redeploys.

### Step 3 — Set environment variables on mt5-server

| Variable | Value | Required |
|---|---|---|
| `MT5_PORT` | `8001` | Yes |
| `MT5_LOGIN` | your broker account number | Yes (for auto-login) |
| `MT5_PASSWORD` | your broker account password | Yes (for auto-login) |
| `MT5_SERVER` | broker server name e.g. `ICMarkets-Demo` | Yes (for auto-login) |
| `VNC_PASSWORD` | any string | No — enables VNC remote desktop |

### Step 4 — Set environment variables on bot-app

| Variable | Value |
|---|---|
| `MT5_HOST` | `mt5-server.zeabur.internal` |
| `MT5_PORT` | `8001` |

`mt5-server.zeabur.internal` is Zeabur's private DNS — only works between services in the same project. No public exposure needed.

## What winetricks installs (the "small Windows")

| Component | Why MT5 needs it |
|---|---|
| `vcrun2019` | Visual C++ 2019 runtime — MT5 terminal is compiled with MSVC 2019; without this DLL it crashes silently on launch |
| `dotnet48` | .NET Framework 4.8 — used by MT5's internal update and scripting components |

These are the minimal Windows DLLs MT5 requires. `winetricks` downloads and installs them automatically on first boot.

## Auto-login (start.ini)

When `MT5_LOGIN`, `MT5_PASSWORD`, and `MT5_SERVER` are set, the container writes a `start.ini` file inside the MT5 directory before launching the terminal. MT5 reads this on startup and automatically connects to your broker — no manual clicking required.

## VNC remote desktop (optional)

Set `VNC_PASSWORD=anything` on the mt5-server service in Zeabur, then expose port **5900**.
Connect with any VNC viewer (e.g. RealVNC, TigerVNC) to see the live MT5 terminal desktop.

## Architecture

```
Zeabur Project
│
├── bot-app (Flask + gunicorn)            ← public URL (eth-bot.zeabur.app)
│     bot.py → mt5linux client
│          └── connects to port 8001 ──────────────────┐
│                                                       │
└── mt5-server (Docker)                                 │
      ├── Xvfb :99 (virtual display)                   │
      ├── Wine64 prefix (/root/.wine persistent vol)   │
      │     ├── vcrun2019 + dotnet48 (winetricks)      │
      │     ├── Python 3.10 for Windows                │
      │     │     └── MetaTrader5 + mt5linux pip pkgs  │
      │     └── MetaTrader5 terminal (auto-login)      │
      └── mt5linux RPC bridge 0.0.0.0:8001 ←──────────┘
```
