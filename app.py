from flask import Flask, jsonify, request, render_template_string, make_response
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
import bot as trader_bot
import uuid
import re

try:
    import anthropic
except Exception:
    anthropic = None

app = Flask(__name__)

# Paths
STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE = Path(__file__).parent / "log.txt"

# In-memory chat storage (kept small to reduce token usage + cookie bloat).
# Note: if the process restarts, chat history resets.
CHAT_SESSIONS = {}  # chat_id -> [{"role": "user"|"assistant", "content": "..."}]
CHAT_MAX_TURNS = int(os.environ.get("CHAT_MAX_TURNS", "8"))  # user+assistant messages total

# Default to a generally-available model name; allow override via CHAT_MODEL.
# (If a model name is invalid for your account, the server will fall back to an alternative.)
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-opus-4-7")
CHAT_MAX_OUTPUT_TOKENS = int(os.environ.get("CHAT_MAX_OUTPUT_TOKENS", "180"))
CHAT_TEMPERATURE = float(os.environ.get("CHAT_TEMPERATURE", "0.2"))

# Bot state for API
bot_running = False
bot_error = None

# HTML template
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ETH Trader Bot</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { text-align: center; margin-bottom: 20px; color: #38bdf8; }
        .card { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #334155; }
        .card h2 { font-size: 1.1rem; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
        .stat { background: #0f172a; padding: 12px; border-radius: 8px; text-align: center; }
        .stat-label { font-size: 0.75rem; color: #64748b; margin-bottom: 4px; }
        .stat-value { font-size: 1.5rem; font-weight: bold; }
        .stat-value.positive { color: #4ade80; }
        .stat-value.negative { color: #f87171; }
        .stat-value.neutral { color: #38bdf8; }
        .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; }
        .status-running { background: #10b981; color: white; }
        .status-paused { background: #f59e0b; color: white; }
        .status-error { background: #ef4444; color: white; }
        .controls { display: flex; gap: 10px; flex-wrap: wrap; }
        button { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; transition: all 0.2s; }
        button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
        .btn-primary { background: #38bdf8; color: white; }
        .btn-success { background: #4ade80; color: white; }
        .btn-danger { background: #f87171; color: white; }
        .btn-warning { background: #f59e0b; color: white; }
        .log { background: #0f172a; border-radius: 8px; padding: 12px; max-height: 300px; overflow-y: auto; font-family: 'Consolas', 'Monaco', monospace; font-size: 0.85rem; }
        .log-entry { padding: 4px 0; border-bottom: 1px solid #1e293b; }
        .log-entry:last-child { border-bottom: none; }
        .log-time { color: #64748b; margin-right: 8px; }
        .log-msg { color: #e2e8f0; }
        .log-msg.skip { color: #94a3b8; }
        .log-msg.long { color: #4ade80; }
        .log-msg.short { color: #f87171; }
        .log-msg.error { color: #ef4444; }
        .log-msg.trade { color: #38bdf8; }
        .info { background: #0f172a; padding: 12px; border-radius: 8px; font-size: 0.85rem; color: #94a3b8; }
        .info strong { color: #e2e8f0; }
        .last-update { text-align: center; color: #64748b; font-size: 0.85rem; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 ETH Trader Bot</h1>

        <div class="card">
            <h2>Chat</h2>
            <div class="info">
                <strong>Chat UI:</strong> <a href="/chat" style="color:#38bdf8">Open Messenger-style chat</a>
            </div>
        </div>
        
        <div class="card">
            <h2>Status</h2>
            <div class="grid">
                <div class="stat">
                    <div class="stat-label">Bot Status</div>
                    <div class="stat-value" id="botStatus">Checking...</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Equity</div>
                    <div class="stat-value neutral" id="equity">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Daily PnL</div>
                    <div class="stat-value" id="dailyPnl">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Consecutive Loss</div>
                    <div class="stat-value" id="consecutiveLoss">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Trade Mood</div>
                    <div class="stat-value" id="tradeMood">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Live Trade PnL</div>
                    <div class="stat-value" id="livePnl">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Trade Side</div>
                    <div class="stat-value neutral" id="tradeSide">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Entry / Mark</div>
                    <div class="stat-value neutral" id="entryMark">--</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Performance</h2>
            <div class="grid">
                <div class="stat">
                    <div class="stat-label">Lifetime PnL</div>
                    <div class="stat-value" id="lifetimePnl">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Trades Today</div>
                    <div class="stat-value" id="tradesToday">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Win Rate</div>
                    <div class="stat-value" id="winRate">--</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Avg Win/Loss</div>
                    <div class="stat-value" id="avgWinLoss">--</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Controls</h2>
            <div class="controls">
                <button class="btn-success" onclick="resumeTrading()">▶ Resume</button>
                <button class="btn-warning" onclick="pauseTrading()">⏸ Pause</button>
                <button class="btn-danger" onclick="stopTrading()">⏹ Stop</button>
                <button class="btn-primary" onclick="startTrading()">▶ Start</button>
            </div>
            <div class="info" style="margin-top: 12px;">
                <strong>Max Daily Loss:</strong> $<span id="maxDailyLoss">2</span> | 
                <strong>Max Consecutive Loss:</strong> $<span id="maxConsecLoss">4</span> | 
                <strong>Position:</strong> 0.04 ETH | 
                <strong>Leverage:</strong> 45x
            </div>
        </div>

        <div class="card">
            <h2>Recent Activity</h2>
            <div class="log" id="activityLog">
                <div class="log-entry"><span class="log-time">--:--:--</span><span class="log-msg">Waiting for first cycle...</span></div>
            </div>
        </div>

        <div class="last-update">Last updated: <span id="lastUpdate">--</span></div>
    </div>

    <script>
        function money(value) {
            return typeof value === 'number' && Number.isFinite(value) ? `$${value.toFixed(2)}` : '--';
        }

        function tradeMood(pnl) {
            if (typeof pnl !== 'number' || !Number.isFinite(pnl)) return { icon: '😴', label: 'No trade', tone: 'neutral' };
            if (pnl <= -4) return { icon: '😱', label: 'Very bad', tone: 'negative' };
            if (pnl <= -2) return { icon: '😬', label: 'Bad', tone: 'negative' };
            if (pnl < 0) return { icon: '😐', label: 'Slightly red', tone: 'negative' };
            if (pnl < 1) return { icon: '🙂', label: 'Okay', tone: 'neutral' };
            if (pnl < 3) return { icon: '😎', label: 'Good', tone: 'positive' };
            return { icon: '🚀', label: 'Great', tone: 'positive' };
        }

        async function fetchData() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) throw new Error(`Status ${res.status}`);
                const data = await res.json();
                
                document.getElementById('equity').textContent = money(data.equity);
                document.getElementById('dailyPnl').textContent = money(data.daily_pnl);
                document.getElementById('dailyPnl').className = 'stat-value ' + (data.daily_pnl > 0 ? 'positive' : data.daily_pnl < 0 ? 'negative' : 'neutral');
                document.getElementById('consecutiveLoss').textContent = money(data.consecutive_loss);
                document.getElementById('lifetimePnl').textContent = money(data.lifetime_pnl);
                document.getElementById('tradesToday').textContent = data.trades_today || 0;
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

                const pnl = data.live_pnl;
                const mood = tradeMood(pnl);
                const livePnlEl = document.getElementById('livePnl');
                const moodEl = document.getElementById('tradeMood');
                livePnlEl.textContent = money(pnl);
                livePnlEl.className = `stat-value ${pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral'}`;
                moodEl.textContent = `${mood.icon} ${mood.label}`;
                moodEl.className = `stat-value ${mood.tone}`;
                document.getElementById('tradeSide').textContent = data.position_side || '--';
                document.getElementById('entryMark').textContent = data.entry_price && data.mark_price ? `$${data.entry_price.toFixed(2)} / $${data.mark_price.toFixed(2)}` : '--';
                
                // Win rate
                if (data.win_count || data.loss_count) {
                    const total = data.win_count + data.loss_count;
                    const rate = total > 0 ? ((data.win_count / total) * 100).toFixed(1) : 0;
                    document.getElementById('winRate').textContent = `${rate}%`;
                } else {
                    document.getElementById('winRate').textContent = '--';
                }
                
                // Avg win/loss
                if (data.avg_win || data.avg_loss) {
                    document.getElementById('avgWinLoss').textContent = `W: $${data.avg_win || 0} / L: $${data.avg_loss || 0}`;
                } else {
                    document.getElementById('avgWinLoss').textContent = '--';
                }
                
                // Bot status
                const statusEl = document.getElementById('botStatus');
                if (data.paused) {
                    statusEl.textContent = 'Paused';
                    statusEl.className = 'status-badge status-paused';
                } else if (data.bot_running) {
                    statusEl.textContent = 'Running';
                    statusEl.className = 'status-badge status-running';
                } else {
                    statusEl.textContent = 'Error';
                    statusEl.className = 'status-badge status-error';
                }
                
                // Update controls visibility
                document.querySelector('.btn-success').style.display = data.paused ? 'inline-block' : 'none';
                document.querySelector('.btn-warning').style.display = !data.paused && data.bot_running ? 'inline-block' : 'none';
                document.querySelector('.btn-danger').style.display = data.bot_running ? 'inline-block' : 'none';
                document.querySelector('.btn-primary').style.display = !data.bot_running ? 'inline-block' : 'none';
                fetchLog();
                
            } catch (e) {
                console.error('Fetch error:', e);
            }
        }

        async function fetchLog() {
            try {
                const res = await fetch('/api/log');
                if (!res.ok) throw new Error(`Log ${res.status}`);
                const data = await res.json();
                const log = document.getElementById('activityLog');
                const lines = data.log || [];
                if (!lines.length) return;
                log.innerHTML = lines.slice(-12).reverse().map((line) => {
                    let text = line.trim();
                    let time = '';
                    let cls = 'log-msg';
                    try {
                        const item = JSON.parse(text);
                        time = item.ts ? new Date(item.ts).toLocaleTimeString() : '';
                        cls += ` ${(item.event || '').toLowerCase()}`;
                        text = `${item.event || 'LOG'} ${JSON.stringify(item.payload || item)}`;
                    } catch (e) {
                        cls += text.toLowerCase().includes('error') ? ' error' : '';
                    }
                    return `<div class="log-entry"><span class="log-time">${time}</span><span class="${cls}">${text}</span></div>`;
                }).join('');
            } catch (e) {
                console.error('Log fetch error:', e);
            }
        }
        
        async function control(action) {
            try {
                await fetch(`/api/${action}`, { method: 'POST' });
                fetchData();
            } catch (e) {
                console.error('Control error:', e);
            }
        }
        
        function resumeTrading() { control('resume'); }
        function pauseTrading() { control('pause'); }
        function stopTrading() { control('stop'); }
        function startTrading() { control('start'); }
        
        // Fetch data every 5 seconds
        setInterval(fetchData, 5000);
        fetchData();
    </script>
</body>
</html>'''

CHAT_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ETH Bot Chat</title>
  <style>
    :root{
      --bg:#f4f5ff;
      --panel:#ffffff;
      --border:rgba(0,0,0,.06);
      --text:#111827;
      --muted:#6b7280;
      --me:#4f46e5;
      --me2:#6d28d9;
      --meText:#ffffff;
      --bot:#eef2ff;
      --botText:#111827;
      --input:#f3f4f6;
      --shadow: 0 18px 50px rgba(17,24,39,.18);
    }
    *{box-sizing:border-box;}
    body{
      margin:0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      background: radial-gradient(1000px 600px at 40% -10%, rgba(99,102,241,.35), rgba(0,0,0,0) 60%), var(--bg);
      color:var(--text);
      height:100vh;
      display:flex;
      justify-content:center;
      align-items:center;
      padding:16px;
    }
    .phone{
      width:min(420px, 100%);
      height:min(840px, 100%);
      background:var(--panel);
      border:1px solid var(--border);
      border-radius:28px;
      overflow:hidden;
      box-shadow:var(--shadow);
      display:flex;
      flex-direction:column;
    }
    .topbar{
      padding:16px 16px;
      background: linear-gradient(135deg, var(--me) 0%, var(--me2) 100%);
      display:flex;
      gap:12px;
      align-items:center;
    }
    .avatar{
      width:36px;height:36px;border-radius:50%;
      background: rgba(255,255,255,.18);
      border:1px solid rgba(255,255,255,.35);
      display:flex;align-items:center;justify-content:center;
      font-weight:800;color:white;
    }
    .titlewrap{display:flex;flex-direction:column;line-height:1.1}
    .title{font-weight:750;color:white}
    .subtitle{font-size:12px;color:rgba(255,255,255,.85)}
    .msgs{
      flex:1;
      padding:14px 12px;
      overflow:auto;
      background: linear-gradient(180deg, #ffffff 0%, #fbfbff 100%);
    }
    .row{display:flex; margin:8px 0;}
    .row.me{justify-content:flex-end;}
    .bubble{
      max-width:78%;
      padding:10px 12px;
      border-radius:18px;
      font-size:14px;
      line-height:1.35;
      white-space:pre-wrap;
      word-wrap:break-word;
    }
    .row.me .bubble{
      background: linear-gradient(135deg, var(--me) 0%, var(--me2) 100%);
      color:var(--meText);
      border-bottom-right-radius:6px;
    }
    .row.bot .bubble{
      background:var(--bot);
      border:1px solid rgba(0,0,0,.05);
      color:var(--botText);
      border-bottom-left-radius:6px;
    }
    .chips{
      padding:8px 12px;
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      border-top:1px solid var(--border);
      background:#fff;
    }
    .chip{
      border:1px solid rgba(0,0,0,.08);
      background:#fff;
      color:var(--text);
      padding:7px 10px;
      border-radius:999px;
      font-size:12px;
      cursor:pointer;
    }
    .composer{
      border-top:1px solid var(--border);
      padding:10px 12px;
      background:#fff;
      display:flex;
      gap:10px;
      align-items:center;
    }
    .input{
      flex:1;
      background:var(--input);
      border:1px solid rgba(0,0,0,.08);
      border-radius:999px;
      padding:10px 12px;
      color:var(--text);
      outline:none;
      font-size:14px;
    }
    .send{
      width:40px;height:40px;border-radius:50%;
      border:none;
      background: linear-gradient(135deg, var(--me) 0%, var(--me2) 100%);
      color:white;
      cursor:pointer;
      font-weight:800;
    }
    .hint{
      color:var(--muted);
      font-size:12px;
      padding:10px 12px 0 12px;
    }
  </style>
</head>
<body>
  <div class="phone">
    <div class="topbar">
      <div class="avatar">B</div>
      <div class="titlewrap">
        <div class="title">ETH Bot Assistant</div>
        <div class="subtitle">Online • /balance /start /stop /status</div>
      </div>
    </div>
    <div class="chips">
      <button class="chip" onclick="sendText('/balance')">Balance</button>
      <button class="chip" onclick="sendText('/status')">Status</button>
      <button class="chip" onclick="sendText('/stop')">Stop trading</button>
      <button class="chip" onclick="sendText('/start')">Start trading</button>
    </div>
    <div class="msgs" id="msgs"></div>
    <div class="composer">
      <input class="input" id="text" placeholder="Type a message…" autocomplete="off" />
      <button class="send" id="sendBtn">➤</button>
    </div>
  </div>

  <script>
    const msgs = document.getElementById('msgs');
    const input = document.getElementById('text');
    const btn = document.getElementById('sendBtn');

    function add(role, text){
      const row = document.createElement('div');
      row.className = 'row ' + (role === 'user' ? 'me' : 'bot');
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = text;
      row.appendChild(b);
      msgs.appendChild(row);
      msgs.scrollTop = msgs.scrollHeight;
    }

    async function sendText(text){
      const t = (text ?? input.value ?? '').trim();
      if(!t) return;
      input.value = '';
      add('user', t);
      try{
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({message: t})
        });
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        let data = null;
        if (ct.includes('application/json')) {
          data = await res.json();
          add('assistant', data.reply || 'No reply');
        } else {
          const txt = await res.text();
          const snippet = (txt || '').slice(0, 300);
          add('assistant', `Server returned non-JSON (HTTP ${res.status}).\\n${snippet}`);
        }
      }catch(e){
        add('assistant', 'Error sending message: ' + (e?.message || e));
      }
    }

    btn.addEventListener('click', () => sendText());
    input.addEventListener('keydown', (e) => {
      if(e.key === 'Enter') sendText();
    });

    // welcome
    add('assistant', 'Hi — chat is the only UI. Try /balance or ask a question.');
  </script>
</body>
</html>
'''

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_last_lines(n=20):
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, 'r') as f:
        lines = f.readlines()
    return lines[-n:]

# --- Bot logic (copied from bot.py, simplified for integration) ---
import requests, hmac, hashlib

def get_candles(interval, limit):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol=ETHUSDT&interval={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            send_telegram(f"⚠️ Kline API error: {data.get('retMsg')}")
            return []
        return data.get("result", {}).get("list", [])
    except Exception as e:
        send_telegram(f"⚠️ Kline API error: {str(e)}")
        return []

def calculate_ma(candles, period):
    if not candles or len(candles) < period:
        return None
    try:
        closes = [float(c[4]) for c in candles]
        return sum(closes[-period:]) / period
    except (TypeError, ValueError, IndexError):
        return None

def calculate_rsi(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    try:
        closes = [float(c[4]) for c in candles]
        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        if len(gains) < period:
            return None
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    except (TypeError, ValueError, IndexError):
        return None

def calculate_macd(candles, fast=12, slow=26, signal=9):
    if not candles or len(candles) < slow + signal:
        return None, None, None
    try:
        closes = [float(c[4]) for c in candles]
        ema_fast = [sum(closes[:fast])/fast]
        ema_slow = [sum(closes[:slow])/slow]
        for i in range(fast, len(closes)):
            ema_fast.append((closes[i] * 2/(fast+1)) + (ema_fast[-1] * (1 - 2/(fast+1))))
        for i in range(slow, len(closes)):
            ema_slow.append((closes[i] * 2/(slow+1)) + (ema_slow[-1] * (1 - 2/(slow+1))))
        macd_line = [f - s for f, s in zip(ema_fast[slow-slow:], ema_slow)]
        signal_line = [sum(macd_line[:signal])/signal] * len(macd_line)
        for i in range(signal, len(macd_line)):
            signal_line[i] = (macd_line[i] * 2/(signal+1)) + (signal_line[i-1] * (1 - 2/(signal+1)))
        histogram = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line[-1], signal_line[-1], histogram[-1]
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None, None, None

def calculate_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    try:
        trs = []
        for i in range(1, len(candles)):
            h = float(candles[i][2])
            l = float(candles[i][3])
            c = float(candles[i][4])
            prev_c = float(candles[i-1][4])
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
        return sum(trs[-period:]) / period
    except (TypeError, ValueError, IndexError):
        return None

def get_price():
    url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            send_telegram(f"⚠️ Tickers API error: {data.get('retMsg')}")
            return None
        return float(data["result"]["list"][0]["markPrice"])
    except Exception as e:
        send_telegram(f"⚠️ Tickers API error: {str(e)}")
        return None

def get_equity():
    url = "https://api.bybit.com/v5/account/wallet-balance"
    headers = {
        "X-BA-SIGN": "",
        "X-BA-TS": "",
        "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        return float(r.json()["result"]["list"][0]["totalEquity"])
    except:
        return None

def get_position():
    url = "https://api.bybit.com/v5/position/list"
    headers = {
        "X-BA-SIGN": "",
        "X-BA-TS": "",
        "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        for p in r.json()["result"]["list"]:
            if p["symbol"] == "ETHUSDT":
                return p
        return None
    except:
        return None

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}/sendMessage"
    payload = {"chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""), "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def run_cycle():
    global bot_running, bot_error
    try:
        bot_running = True
        bot_error = None

        # Get data
        candles_15m = get_candles("15", 200)
        candles_1h = get_candles("60", 300)
        price = get_price()
        equity = get_equity()
        position = get_position()

        # Debug logging
        debug_msg = f"📊 Data check:\n"
        debug_msg += f"- candles_15m: {len(candles_15m)} candles\n"
        debug_msg += f"- candles_1h: {len(candles_1h)} candles\n"
        debug_msg += f"- price: {price}\n"
        debug_msg += f"- equity: {equity}\n"
        debug_msg += f"- position: {position is not None}\n"
        send_telegram(debug_msg)

        if not price or not candles_15m or not candles_1h:
            send_telegram("⚠️ Incomplete data, skipping.")
            return

        # Calculate indicators
        ma200_15m = calculate_ma(candles_15m, 200)
        rsi_15m = calculate_rsi(candles_15m, 14)
        atr_15m = calculate_atr(candles_15m, 14)
        macd_line, macd_signal, macd_hist = calculate_macd(candles_15m, 12, 26, 9)

        # Get state
        state = load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("day") != today:
            state = {"day": today, "equity_start": equity, "daily_pnl": 0, "consecutive_loss": 0, "trades_today": 0, "max_trades_per_day": 10, "trading_enabled": True, "paused": False, "pause_until": None, "pause_reason": None, "had_position": False, "entry_equity": None, "lifetime_pnl": state.get("lifetime_pnl", 0), "last_trade": None, "win_count": state.get("win_count", 0), "loss_count": state.get("loss_count", 0), "avg_win": state.get("avg_win", 0), "avg_loss": state.get("avg_loss", 0), "last_5_trades": []}
        state["equity"] = equity
        equity_start = state.get("equity_start")
        if equity is not None and equity_start is not None:
            state["daily_pnl"] = equity - equity_start
        else:
            state["daily_pnl"] = 0
        state["position"] = position
        save_state(state)

        # The dashboard Stop/Start controls toggle this flag.
        if not state.get("trading_enabled", True):
            send_telegram(f"STOPPED - ${price:.2f}\nTrading is disabled from the dashboard.")
            return

        # Check pause
        if state.get("paused") and state.get("pause_until") and time.time() < state.get("pause_until"):
            send_telegram(f"⏸️ <b>PAUSED</b> — ${price:.2f}\n{state.get('pause_reason', 'Paused')}")
            return

        # Check max trades
        if state.get("trades_today", 0) >= state.get("max_trades_per_day", 10):
            send_telegram(f"🚫 <b>MAX TRADES REACHED</b> — ${price:.2f}\n{state.get('trades_today', 0)} trades today")
            return

        # Check daily loss
        if state.get("daily_pnl", 0) <= -float(os.environ.get("MAX_DAILY_LOSS_USD", 2)):
            send_telegram(f"🛑 <b>DAILY LOSS LIMIT HIT</b> — ${price:.2f}\nPnL: ${state.get('daily_pnl', 0):.2f}")
            return

        # Check consecutive loss
        if state.get("consecutive_loss", 0) >= float(os.environ.get("MAX_CONSEC_LOSS_USD", 4)):
            send_telegram(f"🛑 <b>CONSECUTIVE LOSS LIMIT HIT</b> — ${price:.2f}\nLoss: ${state.get('consecutive_loss', 0):.2f}")
            return

        # Check volatility spike (15m ATR%)
        atr_pct = atr_15m / price if atr_15m and price else 0
        if atr_pct > float(os.environ.get("VOL_SPIKE_ATR_PCT", 0.02)):
            send_telegram(f"⚠️ <b>VOLATILITY SPIKE</b> — ${price:.2f}\nATR%: {atr_pct*100:.2f}%")
            state["paused"] = True
            state["pause_reason"] = f"Volatility spike (ATR%: {atr_pct*100:.2f}%)"
            state["pause_until"] = time.time() + float(os.environ.get("VOL_PAUSE_SECONDS", 1800))
            save_state(state)
            return

        # Prepare context for Claude
        last_candle = candles_15m[-1]
        price_now = float(last_candle[4])
        price_open = float(last_candle[1])
        price_high = float(last_candle[2])
        price_low = float(last_candle[3])
        volume = float(last_candle[5])

        # Get performance memory
        perf_text = f"Today PnL: ${state.get('daily_pnl', 0):.2f} | Consecutive loss: ${state.get('consecutive_loss', 0):.2f} | Lifetime PnL: ${state.get('lifetime_pnl', 0):.2f}\n"
        if state.get("last_5_trades"):
            perf_text += "Last 5 trades: " + ", ".join(state.get("last_5_trades", [])) + "\n"

        # Build prompt
        prompt = f"""You are a professional ETHUSDT futures trader. Study the market data below and decide whether to LONG, SHORT, or SKIP. You choose your own strategy — no fixed indicators are imposed. Analyze price action, structure, momentum, volatility, and any patterns you see. Only trade when you see a clear edge.

Current price: ${price_now:.2f} (open: ${price_open:.2f}, high: ${price_high:.2f}, low: ${price_low:.2f})
Volume: {volume}
Time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC

Indicators (15m):
- MA200: ${ma200_15m:.2f}
- RSI(14): {rsi_15m}
- ATR(14): {atr_15m:.2f} ({atr_pct*100:.2f}%)
- MACD: line={macd_line:.2f}, signal={macd_signal:.2f}, hist={macd_hist:.2f}

Performance memory:
{perf_text}

Your constraints:
- Max daily loss: ${os.environ.get("MAX_DAILY_LOSS_USD", 2)}
- Max consecutive loss: ${os.environ.get("MAX_CONSEC_LOSS_USD", 4)}
- Position size: 0.04 ETH
- Leverage: 45x
- Max trades per day: {state.get("max_trades_per_day", 10)}

Return JSON with:
- DECISION: "LONG", "SHORT", or "SKIP"
- REASON: why (1-2 sentences)
- SL: stop loss price (if trading)
- TP: take profit price (if trading)
- QUALITY: "A", "B", or "C" (how strong the setup is)
- PERSONAL_MESSAGE: optional message to user (if SKIP, explain why)

Only trade if QUALITY is A or B and you're confident."""
        state["last_prompt"] = prompt
        save_state(state)

        # Call Claude
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            send_telegram("⚠️ ANTHROPIC_API_KEY not set, skipping Claude analysis")
            return
        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            claude_text = response.content[0].text
        except Exception as e:
            send_telegram(f"⚠️ Claude API error: {str(e)}")
            return

        # Parse response
        try:
            # Try to extract JSON from response
            import re
            json_match = re.search(r'\{[^{}]*\}', claude_text, re.DOTALL)
            if json_match:
                claude_json = json.loads(json_match.group())
            else:
                # Try parsing the whole response as JSON
                claude_json = json.loads(claude_text)
        except:
            claude_json = {"DECISION": "SKIP", "REASON": "Failed to parse Claude response", "PERSONAL_MESSAGE": claude_text}

        decision = claude_json.get("DECISION", "SKIP")
        reason = claude_json.get("REASON", "No reason given")
        personal_msg = claude_json.get("PERSONAL_MESSAGE", "")

        # Log to file
        log_line = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price_now,
            "decision": decision,
            "reason": reason,
            "sl": claude_json.get("SL"),
            "tp": claude_json.get("TP"),
            "quality": claude_json.get("QUALITY"),
            "equity": equity,
            "pnl": state.get("daily_pnl", 0),
            "consecutive_loss": state.get("consecutive_loss", 0),
            "paused": state.get("paused"),
            "pause_reason": state.get("pause_reason")
        })
        with open(LOG_FILE, 'a') as f:
            f.write(log_line + "\n")

        # Send Telegram
        if decision == "SKIP":
            send_telegram(
                f"⏭ <b>SKIP</b> — ${price_now:.2f}\n"
                f"MA200: ${ma200_15m:.2f} | RSI: {rsi_15m} | ATR%: {atr_pct*100:.2f}%\n"
                f"MACD hist: {macd_hist:.2f}\n"
                f"Reason: {reason}"
            )
            if personal_msg:
                send_telegram(f"💬 <i>{personal_msg}</i>")
        else:
            sl = claude_json.get("SL")
            tp = claude_json.get("TP")
            quality = claude_json.get("QUALITY", "B")
            if not sl or not tp:
                send_telegram(f"⚠️ <b>SKIP</b> — SL/TP missing\n{reason}")
                return

            # Check quality
            if quality not in ("A", "B"):
                send_telegram(f"⚠️ <b>SKIP</b> — Quality {quality} below A/B\n{reason}")
                return

            # Check R:R
            sl_dist = abs(price_now - sl)
            tp_dist = abs(tp - price_now)
            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
            if rr < 1.0:
                send_telegram(f"⚠️ <b>SKIP</b> — R:R {rr} < 1.0\n{reason}")
                return

            # Place order
            side = "Buy" if decision == "LONG" else "Sell"
            qty = float(os.environ.get("QTY", "0.04"))
            url = "https://api.bybit.com/v5/order/create"
            headers = {
                "X-BA-SIGN": "",
                "X-BA-TS": "",
                "X-BA-API-KEY": os.environ.get("BYBIT_API_KEY", "")
            }
            payload = {
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": side,
                "orderType": "Market",
                "qty": qty,
                "sl": str(sl),
                "tp": str(tp),
                "leverage": int(os.environ.get("LEVERAGE", "45"))
            }
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=10)
                result = r.json()
                if result.get("retCode") == 0:
                    liq = sl * (1 + (1/45)) if side == "Buy" else sl * (1 - (1/45))
                    send_telegram(
                        f"🎯 <b>{decision}</b> — ${price_now:.2f}\n"
                        f"SL: ${sl} | TP: ${tp}\n"
                        f"R:R: 1:{rr} | Liq: ${liq:.2f}\n"
                        f"Quality: {quality}\n"
                        f"Reason: {reason}"
                    )
                    # Update state
                    state["trades_today"] = state.get("trades_today", 0) + 1
                    state["had_position"] = True
                    state["entry_equity"] = equity
                    save_state(state)
                else:
                    send_telegram(f"❌ Order failed: {result.get('retMsg')}")
            except Exception as e:
                send_telegram(f"❌ Order error: {str(e)}")

    except Exception as e:
        bot_error = str(e)
        send_telegram(f"❌ <b>ERROR</b>\n{str(e)}\n{traceback.format_exc()}")

def bot_thread():
    global bot_running, bot_error
    trader_bot.send_telegram(
        f"📈 <b>ETH Bot Started</b>\n"
        f"{trader_bot.LEVERAGE}x | {trader_bot.QTY} ETH | "
        f"{int(trader_bot.CHECK_INTERVAL/60)}m checks | Claude in charge "
        f"({trader_bot.DECISION_INTERVAL}m MACD/RSI/MA{trader_bot.MA_PERIOD})"
    )
    trader_bot.append_log("START", {
        "symbol": trader_bot.SYMBOL,
        "qty": trader_bot.QTY,
        "leverage": trader_bot.LEVERAGE,
        "check_interval": trader_bot.CHECK_INTERVAL
    })
    while True:
        try:
            bot_running = True
            bot_error = None
            trader_bot.run_cycle()
        except Exception as e:
            bot_error = str(e)
            trader_bot.send_telegram(f"⚠️ Error: {type(e).__name__}: {str(e)}")
        # Sleep for CHECK_INTERVAL (default 1 hour)
        time.sleep(trader_bot.CHECK_INTERVAL)

# Start bot in background thread (after Flask is ready)
import threading
bot_thread_instance = None

def start_bot_thread():
    global bot_thread_instance
    try:
        bot_thread_instance = threading.Thread(target=bot_thread, daemon=True)
        bot_thread_instance.start()
        return True
    except Exception as e:
        print(f"Failed to start bot thread: {e}")
        return False

# Try to start bot thread on import (but don't block if it fails)
try:
    start_bot_thread()
except Exception as e:
    print(f"Bot thread startup error: {e}")

@app.route('/')
def chat_ui():
    resp = make_response(render_template_string(CHAT_TEMPLATE))
    # Ensure the browser has a chat_id cookie to scope memory.
    if not request.cookies.get("chat_id"):
        resp.set_cookie("chat_id", str(uuid.uuid4()), max_age=60*60*24*30, httponly=True, samesite="Lax")
    return resp

@app.route('/chat')
def chat_redirect():
    # Backwards-compatible route; keep chat as the only UI.
    return chat_ui()

def _get_chat_id():
    cid = request.cookies.get("chat_id")
    if not cid:
        cid = str(uuid.uuid4())
    return cid

def _chat_memory(cid: str):
    mem = CHAT_SESSIONS.get(cid)
    if not mem:
        mem = []
        CHAT_SESSIONS[cid] = mem
    return mem

def _trim_memory(mem):
    # Keep only last N messages to reduce token usage.
    if len(mem) > CHAT_MAX_TURNS:
        del mem[:-CHAT_MAX_TURNS]

def _set_trading_enabled(enabled: bool):
    state = load_state()
    state["trading_enabled"] = bool(enabled)
    save_state(state)
    return state

def _format_money(v):
    try:
        return f"${float(v):.2f}"
    except:
        return "n/a"

def _handle_chat_command(text: str):
    """
    Returns (handled: bool, reply: str)
    Commands are handled locally (no Claude call) to save tokens.
    """
    t = (text or "").strip()
    low = t.lower()

    if low in ("/help", "help"):
        return True, (
            "Commands:\n"
            "/balance - show Bybit USDT equity\n"
            "/status - show bot + trading flags\n"
            "/stop - disable new entries\n"
            "/start - enable new entries\n"
            "Tip: these commands do not use Claude tokens."
        )

    if low in ("/stop", "stop", "stop trading", "stop bot"):
        st = _set_trading_enabled(False)
        return True, f"Trading disabled. trading_enabled={st.get('trading_enabled')}"

    if low in ("/start", "start", "start trading", "start bot"):
        st = _set_trading_enabled(True)
        return True, f"Trading enabled. trading_enabled={st.get('trading_enabled')}"

    if low in ("/balance", "balance", "bybit balance", "check balance"):
        try:
            equity = trader_bot.get_wallet_equity_usdt()
            if equity is None:
                return True, "Could not fetch Bybit equity (check BYBIT_API_KEY/BYBIT_API_SECRET + account type)."
            return True, f"Bybit USDT equity: {_format_money(equity)}"
        except Exception as e:
            return True, f"Balance check failed: {type(e).__name__}: {str(e)}"

    if low in ("/status", "status"):
        st = load_state()
        paused_until = int(st.get("paused_until") or st.get("pause_until") or 0)
        is_paused = bool(st.get("paused")) or time.time() < paused_until
        equity = st.get("equity")
        pos = st.get("position") or {}
        size = None
        side = None
        try:
            size = float(pos.get("size") or 0)
            side = pos.get("side")
        except:
            pass
        return True, (
            f"Bot thread: {'running' if bot_running else 'not running'}\n"
            f"Trading enabled: {st.get('trading_enabled', True)}\n"
            f"Paused: {is_paused}\n"
            f"Equity (cached): {_format_money(equity)}\n"
            f"Position: {side or 'none'} size={size or 0}"
        )

    return False, ""

def _call_claude_chat(messages):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "ANTHROPIC_API_KEY is not set. Set it as an environment variable to enable chat."
    if anthropic is None:
        return "Anthropic SDK is not available in this runtime."

    # Let the SDK read ANTHROPIC_API_KEY from env (matches Anthropic docs),
    # but we still check it above to give a friendly error.
    client = anthropic.Anthropic()
    system = (
        "You are a concise assistant for a crypto trading bot dashboard.\n"
        "Keep replies short (1-5 sentences).\n"
        "If the user asks for balance/start/stop/status, suggest the commands /balance /start /stop /status.\n"
        "Do NOT output long explanations unless asked."
    )
    # Some model aliases are not enabled on all accounts. If the chosen model
    # is not found, fall back to a more widely-available option.
    candidates = [CHAT_MODEL, "claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"]
    last_err = None
    for model in candidates:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=CHAT_MAX_OUTPUT_TOKENS,
                temperature=CHAT_TEMPERATURE,
                system=system,
                messages=messages,
            )
            return resp.content[0].text
        except Exception as e:
            last_err = e
            msg = str(e)
            # If it's NOT a model-not-found issue, don't keep retrying other models.
            if "not_found_error" not in msg and "model:" not in msg:
                break
    return f"Claude API error: {type(last_err).__name__}: {str(last_err)}"

@app.route('/api/chat', methods=['POST'])
def api_chat():
    try:
        data = request.get_json(silent=True) or {}
        text = (data.get("message") or "").strip()
        if not text:
            return jsonify({"reply": "Send a message.", "used_claude": False})

        # First: local commands (no Claude call).
        handled, reply = _handle_chat_command(text)
        if handled:
            resp = jsonify({"reply": reply, "used_claude": False})
            # Ensure chat_id cookie exists
            if not request.cookies.get("chat_id"):
                resp.set_cookie("chat_id", str(uuid.uuid4()), max_age=60*60*24*30, httponly=True, samesite="Lax")
            return resp

        # Otherwise: small-memory chat with Claude.
        cid = _get_chat_id()
        mem = _chat_memory(cid)
        mem.append({"role": "user", "content": text})
        _trim_memory(mem)

        # Pass only recent memory; keep it small to reduce token usage.
        reply_text = _call_claude_chat(mem)
        reply_text = (reply_text or "").strip()
        if not reply_text:
            reply_text = "No response."

        mem.append({"role": "assistant", "content": reply_text})
        _trim_memory(mem)

        resp = jsonify({"reply": reply_text, "used_claude": True, "model": CHAT_MODEL})
        if not request.cookies.get("chat_id"):
            resp.set_cookie("chat_id", cid, max_age=60*60*24*30, httponly=True, samesite="Lax")
        return resp
    except Exception as e:
        # Always return JSON (prevents browser JSON parse errors showing HTML).
        return jsonify({"reply": f"Server error: {type(e).__name__}: {str(e)}", "used_claude": False}), 500

@app.route('/api/status')
def api_status():
    state = load_state()
    paused_until = int(state.get("paused_until") or state.get("pause_until") or 0)
    is_paused = bool(state.get("paused")) or time.time() < paused_until
    start_equity = state.get("start_equity") or state.get("equity_start")
    equity = state.get("equity")
    daily_pnl = state.get("daily_pnl")
    if daily_pnl is None and equity is not None and start_equity is not None:
        daily_pnl = float(equity) - float(start_equity)
    perf = trader_bot.performance_summary(state)
    live_position_error = None
    try:
        position = trader_bot.get_position() or {}
        state["position"] = position
        save_state(state)
    except Exception as e:
        live_position_error = f"{type(e).__name__}: {str(e)}"
        position = state.get("position") or {}
    live_pnl = None
    entry_price = None
    mark_price = state.get("price")
    position_side = None
    position_size = None
    try:
        if position and float(position.get("size") or 0) > 0:
            position_size = float(position.get("size") or 0)
            entry_price = float(position.get("avgPrice") or 0) or None
            position_side = position.get("side")
            try:
                mark_price = trader_bot.get_price()
            except Exception:
                mark_price = float(position.get("markPrice") or mark_price or 0) or None
            bybit_pnl = position.get("unrealisedPnl")
            if bybit_pnl not in (None, ""):
                live_pnl = float(bybit_pnl)
            elif entry_price and mark_price:
                direction = 1 if position_side == "Buy" else -1
                live_pnl = (mark_price - entry_price) * position_size * direction
    except (TypeError, ValueError):
        pass
    return jsonify({
        "equity": equity,
        "daily_pnl": daily_pnl,
        "consecutive_loss": state.get("consecutive_loss"),
        "lifetime_pnl": state.get("total_pnl", state.get("lifetime_pnl")),
        "paused": is_paused,
        "pause_reason": state.get("pause_reason", ""),
        "position": state.get("position"),
        "live_pnl": live_pnl,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "position_side": position_side,
        "position_size": position_size,
        "live_position_error": live_position_error,
        "last_trade": state.get("last_trade"),
        "trades_today": state.get("trades_today"),
        "max_trades_per_day": state.get("max_trades_per_day"),
        "trading_enabled": state.get("trading_enabled", True),
        "win_count": perf.get("wins"),
        "loss_count": perf.get("losses"),
        "avg_win": perf.get("avg_win"),
        "avg_loss": perf.get("avg_loss"),
        "bot_running": bot_running,
        "bot_error": bot_error
    })

@app.route('/api/pause', methods=['POST'])
def api_pause():
    data = request.get_json() or {}
    minutes = int(data.get("minutes", 30))
    reason = data.get("reason", "User requested pause")
    state = load_state()
    state["paused"] = True
    state["pause_reason"] = reason
    state["paused_until"] = int(datetime.now(timezone.utc).timestamp() + minutes * 60)
    state["pause_until"] = state["paused_until"]
    save_state(state)
    return jsonify({"status": "paused", "minutes": minutes, "reason": reason})

@app.route('/api/resume', methods=['POST'])
def api_resume():
    state = load_state()
    state["paused"] = False
    state["pause_reason"] = None
    state["paused_until"] = 0
    state["pause_until"] = None
    save_state(state)
    return jsonify({"status": "resumed"})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    state = load_state()
    state["trading_enabled"] = False
    save_state(state)
    return jsonify({"status": "trading stopped"})

@app.route('/api/start', methods=['POST'])
def api_start():
    state = load_state()
    state["trading_enabled"] = True
    save_state(state)
    return jsonify({"status": "trading started"})

@app.route('/api/log')
def api_log():
    lines = get_last_lines(50)
    return jsonify({"log": lines})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify({
            "max_daily_loss_usd": os.environ.get("MAX_DAILY_LOSS_USD", "2"),
            "max_consec_loss_usd": os.environ.get("MAX_CONSEC_LOSS_USD", "4"),
            "check_interval": os.environ.get("CHECK_INTERVAL", "3600"),
            "qty": os.environ.get("QTY", "0.04"),
            "leverage": os.environ.get("LEVERAGE", "45")
        })
    else:
        data = request.get_json() or {}
        return jsonify({"status": "config update not implemented (use Zeabur env vars)"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
