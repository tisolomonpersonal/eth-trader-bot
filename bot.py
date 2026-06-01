from flask import Flask, jsonify, request, render_template_string, make_response, send_from_directory
import json
import os
import threading
import time
import traceback
import random
from datetime import datetime, timezone
from pathlib import Path
import bot as trader_bot
import uuid

try:
    import anthropic
except Exception:
    anthropic = None

app = Flask(__name__)

# Paths
STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE = Path(__file__).parent / "log.txt"
EMOTIONS_DIR = Path(__file__).parent / "static" / "emotions"

# In-memory chat storage
CHAT_SESSIONS = {}  # chat_id -> [{"role": "user"|"assistant", "content": "..."}]
CHAT_MAX_TURNS = int(os.environ.get("CHAT_MAX_TURNS", "8"))

CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5-20251001")
CHAT_MAX_OUTPUT_TOKENS = int(os.environ.get("CHAT_MAX_OUTPUT_TOKENS", "180"))
CHAT_TEMPERATURE = float(os.environ.get("CHAT_TEMPERATURE", "0.7"))

bot_running = False
bot_thread = None
bot_error = None


@app.route('/static/emotions/<path:filename>')
def serve_emotion(filename):
    return send_from_directory(EMOTIONS_DIR, filename)


def get_random_emotion():
    if not EMOTIONS_DIR.exists():
        return None
    files = [f for f in os.listdir(EMOTIONS_DIR) if f.endswith(('.jpg', '.png', '.jpeg'))]
    return random.choice(files) if files else None


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
        .log-msg.error { color: #ef4444; }
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
                    <div class="stat-label">Avg Win / Avg Loss</div>
                    <div class="stat-value" id="avgWinLoss">--</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Controls</h2>
            <div class="controls">
                <button class="btn-success" id="btnResume" onclick="resumeTrading()">▶ Resume</button>
                <button class="btn-warning" id="btnPause" onclick="pauseTrading()">⏸ Pause</button>
                <button class="btn-danger" id="btnStop" onclick="stopTrading()">⏹ Stop</button>
                <button class="btn-primary" id="btnStart" onclick="startTrading()">▶ Start</button>
            </div>
            <div class="info" style="margin-top: 12px;">
                <strong>Max Daily Loss:</strong> $100 | 
                <strong>Max Consecutive Loss:</strong> $100 | 
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
                
                const dpEl = document.getElementById('dailyPnl');
                dpEl.textContent = money(data.daily_pnl);
                dpEl.className = 'stat-value ' + (data.daily_pnl > 0 ? 'positive' : data.daily_pnl < 0 ? 'negative' : 'neutral');

                document.getElementById('consecutiveLoss').textContent = money(data.consecutive_loss);

                const lifetimeEl = document.getElementById('lifetimePnl');
                lifetimeEl.textContent = money(data.total_pnl);
                lifetimeEl.className = 'stat-value ' + (data.total_pnl > 0 ? 'positive' : data.total_pnl < 0 ? 'negative' : 'neutral');

                document.getElementById('tradesToday').textContent = data.trades_today ?? 0;
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
                document.getElementById('entryMark').textContent =
                    data.entry_price && data.mark_price
                        ? `$${data.entry_price.toFixed(2)} / $${data.mark_price.toFixed(2)}`
                        : '--';

                // Win rate
                if (data.win_count != null || data.loss_count != null) {
                    const total = (data.win_count || 0) + (data.loss_count || 0);
                    const rate = total > 0 ? (((data.win_count || 0) / total) * 100).toFixed(1) : 0;
                    document.getElementById('winRate').textContent = total > 0 ? `${rate}%` : '--';
                } else {
                    document.getElementById('winRate').textContent = '--';
                }

                // Avg win/loss
                if (data.avg_win != null || data.avg_loss != null) {
                    document.getElementById('avgWinLoss').textContent =
                        `W: $${(data.avg_win || 0).toFixed(2)} / L: $${(data.avg_loss || 0).toFixed(2)}`;
                } else {
                    document.getElementById('avgWinLoss').textContent = '--';
                }

                // Bot status badge
                const statusEl = document.getElementById('botStatus');
                if (data.paused) {
                    statusEl.textContent = 'Paused';
                    statusEl.className = 'stat-value status-badge status-paused';
                } else if (data.bot_running) {
                    statusEl.textContent = 'Running';
                    statusEl.className = 'stat-value status-badge status-running';
                } else {
                    statusEl.textContent = 'Stopped';
                    statusEl.className = 'stat-value status-badge status-error';
                }

                // Control buttons
                document.getElementById('btnResume').style.display = data.paused ? 'inline-block' : 'none';
                document.getElementById('btnPause').style.display = (!data.paused && data.bot_running) ? 'inline-block' : 'none';
                document.getElementById('btnStop').style.display = data.bot_running ? 'inline-block' : 'none';
                document.getElementById('btnStart').style.display = !data.bot_running ? 'inline-block' : 'none';

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
                        const ev = (item.event || '').toLowerCase();
                        if (ev === 'error') cls += ' error';
                        text = `[${item.event || 'LOG'}] ${item.reason || item.retMsg || item.decision || ''}`;
                    } catch (e) {
                        if (text.toLowerCase().includes('error')) cls += ' error';
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
      padding:16px;
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
    .row{display:flex; margin:8px 0; flex-direction: column;}
    .row.me{align-items:flex-end;}
    .row.bot{align-items:flex-start;}
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
    .emotion-img {
      max-width: 150px;
      border-radius: 12px;
      margin-top: 4px;
      border: 1px solid var(--border);
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

    function add(role, text, emotion){
      const row = document.createElement('div');
      row.className = 'row ' + (role === 'user' ? 'me' : 'bot');
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = text;
      row.appendChild(b);
      if (emotion) {
        const img = document.createElement('img');
        img.src = '/static/emotions/' + emotion;
        img.className = 'emotion-img';
        row.appendChild(img);
      }
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
        const data = await res.json();
        add('assistant', data.reply || 'No reply', data.emotion);
      }catch(e){
        add('assistant', 'Error: ' + (e?.message || e));
      }
    }

    btn.addEventListener('click', () => sendText());
    input.addEventListener('keydown', (e) => {
      if(e.key === 'Enter') sendText();
    });

    add('assistant', 'Hi! Try /balance, /status, /stop, or /start — or just ask me anything.');
  </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/chat')
def chat():
    return render_template_string(CHAT_TEMPLATE)


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


def get_last_lines(n=50):
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, 'r') as f:
        lines = f.readlines()
    return lines[-n:]


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
    if len(mem) > CHAT_MAX_TURNS * 2:
        del mem[:-CHAT_MAX_TURNS * 2]


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
    t = (text or "").strip()
    low = t.lower()
    if low in ("/help", "help"):
        return True, "😼 Commands:\n/balance - show Bybit USDT equity\n/status - show bot + trading flags\n/stop - disable new entries\n/start - enable new entries"
    if low in ("/stop", "stop", "stop trading", "stop bot"):
        st = _set_trading_enabled(False)
        return True, f"🛑 Trading disabled. trading_enabled={st.get('trading_enabled')}"
    if low in ("/start", "start", "start trading", "start bot"):
        st = _set_trading_enabled(True)
        return True, f"✅ Trading enabled. trading_enabled={st.get('trading_enabled')}"
    if low in ("/balance", "balance", "bybit balance", "check balance"):
        try:
            equity = trader_bot.get_wallet_equity_usdt()
            if equity is None:
                return True, "😾 Could not fetch Bybit equity."
            return True, f"🧾 Bybit USDT equity: {_format_money(equity)}"
        except Exception as e:
            return True, f"😾 Balance check failed: {str(e)}"
    if low in ("/status", "status"):
        st = load_state()
        paused_until = int(st.get("paused_until") or 0)
        is_paused = bool(st.get("paused")) or time.time() < paused_until
        equity = st.get("equity")
        pos = st.get("position") or {}
        side = pos.get("side") if isinstance(pos, dict) else None
        size = pos.get("size") if isinstance(pos, dict) else None
        return True, (
            f"📡 Status\n"
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
        return "ANTHROPIC_API_KEY is not set."
    if anthropic is None:
        return "Anthropic SDK is not available."
    client = anthropic.Anthropic(api_key=api_key)
    system = "You are a tsundere personal assistant inside a crypto trading bot chat. Keep replies short (1-4 sentences). Be a little sassy but actually helpful."
    # Try primary model, then fallbacks
    candidates = [CHAT_MODEL, "claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022"]
    for model in candidates:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=CHAT_MAX_OUTPUT_TOKENS,
                system=system,
                messages=messages
            )
            return resp.content[0].text
        except Exception as e:
            if "model" in str(e).lower() or "not found" in str(e).lower():
                continue
            return f"Claude API error: {str(e)}"
    return "Could not reach Claude API."


@app.route('/api/chat', methods=['POST'])
def api_chat():
    try:
        data = request.get_json(silent=True) or {}
        text = (data.get("message") or "").strip()
        if not text:
            return jsonify({"reply": "Send a message."})
        handled, reply = _handle_chat_command(text)
        emotion = get_random_emotion()
        if handled:
            resp = jsonify({"reply": reply, "emotion": emotion})
            if not request.cookies.get("chat_id"):
                resp.set_cookie("chat_id", str(uuid.uuid4()), max_age=60*60*24*30, httponly=True, samesite="Lax")
            return resp
        cid = _get_chat_id()
        mem = _chat_memory(cid)
        mem.append({"role": "user", "content": text})
        _trim_memory(mem)
        reply_text = _call_claude_chat(mem)
        mem.append({"role": "assistant", "content": reply_text})
        _trim_memory(mem)
        resp = jsonify({"reply": reply_text, "emotion": emotion})
        if not request.cookies.get("chat_id"):
            resp.set_cookie("chat_id", cid, max_age=60*60*24*30, httponly=True, samesite="Lax")
        return resp
    except Exception as e:
        return jsonify({"reply": f"Server error: {str(e)}"}), 500


@app.route('/api/status')
def api_status():
    state = load_state()
    paused_until = int(state.get("paused_until") or 0)
    is_paused = bool(state.get("paused")) or time.time() < paused_until
    equity = state.get("equity")
    daily_pnl = state.get("daily_pnl")
    perf = trader_bot.performance_summary(state)
    position = state.get("position") or {}
    return jsonify({
        "equity": equity,
        "daily_pnl": daily_pnl,
        "consecutive_loss": state.get("consecutive_loss"),
        "total_pnl": state.get("total_pnl"),          # FIX: was "lifetime_pnl" → state key is total_pnl
        "paused": is_paused,
        "position": position,
        "trades_today": state.get("trades_today"),
        "trading_enabled": state.get("trading_enabled", True),
        "win_count": perf.get("wins"),
        "loss_count": perf.get("losses"),
        "avg_win": perf.get("avg_win"),
        "avg_loss": perf.get("avg_loss"),
        "bot_running": bot_running,
        # Live position fields
        "live_pnl": state.get("live_pnl"),
        "position_side": state.get("position_side"),
        "entry_price": state.get("entry_price_live"),
        "mark_price": state.get("mark_price"),
    })


@app.route('/api/resume', methods=['POST'])
def api_resume():
    state = load_state()
    state["paused"] = False
    state["paused_until"] = 0
    save_state(state)
    return jsonify({"status": "resumed"})


@app.route('/api/pause', methods=['POST'])
def api_pause():
    state = load_state()
    state["paused"] = True
    state["paused_until"] = int(time.time() + 30 * 60)
    save_state(state)
    return jsonify({"status": "paused"})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global bot_running
    state = load_state()
    state["trading_enabled"] = False
    save_state(state)
    bot_running = False
    return jsonify({"status": "stopped"})


@app.route('/api/start', methods=['POST'])
def api_start():
    state = load_state()
    state["trading_enabled"] = True
    save_state(state)
    _ensure_bot_running()
    return jsonify({"status": "started"})


@app.route('/api/log')
def api_log():
    return jsonify({"log": get_last_lines(50)})


def _bot_loop():
    """Background thread that runs the trading bot loop."""
    global bot_running, bot_error
    bot_running = True
    bot_error = None
    try:
        trader_bot.main()
    except Exception as e:
        bot_error = str(e)
        print(f"Bot loop crashed: {e}")
        print(traceback.format_exc())
    finally:
        bot_running = False


def _ensure_bot_running():
    """Start the bot thread if it's not already running."""
    global bot_thread, bot_running
    if bot_thread is None or not bot_thread.is_alive():
        bot_thread = threading.Thread(target=_bot_loop, daemon=True)
        bot_thread.start()
        bot_running = True


if __name__ == '__main__':
    # Auto-start the bot loop when the Flask app launches
    _ensure_bot_running()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
