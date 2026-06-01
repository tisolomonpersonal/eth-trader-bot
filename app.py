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

# Paths (shared with bot.py)
STATE_FILE = Path(__file__).parent / "bot_state.json"
LOG_FILE = Path(__file__).parent / "log.txt"
EMOTIONS_DIR = Path(__file__).parent / "static" / "emotions"

# In-memory chat storage
CHAT_SESSIONS = {}
CHAT_MAX_TURNS = int(os.environ.get("CHAT_MAX_TURNS", "8"))
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-opus-4-7")
CHAT_MAX_OUTPUT_TOKENS = int(os.environ.get("CHAT_MAX_OUTPUT_TOKENS", "180"))

# Bot thread state
_bot_thread = None
_bot_lock = threading.Lock()


def _is_bot_running():
    return _bot_thread is not None and _bot_thread.is_alive()


def _start_bot_thread():
    global _bot_thread
    with _bot_lock:
        if _is_bot_running():
            return False
        trader_bot.clear_stop()
        t = threading.Thread(target=trader_bot.run_loop, daemon=True, name="bot-loop")
        t.start()
        _bot_thread = t
        return True


def _stop_bot_thread():
    with _bot_lock:
        trader_bot.request_stop()


# Auto-start bot on app launch
_start_bot_thread()


# ─── helpers ────────────────────────────────────────────────────────────────

@app.route('/static/emotions/<path:filename>')
def serve_emotion(filename):
    return send_from_directory(EMOTIONS_DIR, filename)


def get_random_emotion():
    if not EMOTIONS_DIR.exists():
        return None
    files = [f for f in os.listdir(EMOTIONS_DIR) if f.endswith(('.jpg', '.png', '.jpeg'))]
    return random.choice(files) if files else None


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


def _chat_memory(cid):
    mem = CHAT_SESSIONS.get(cid)
    if not mem:
        mem = []
        CHAT_SESSIONS[cid] = mem
    return mem


def _trim_memory(mem):
    if len(mem) > CHAT_MAX_TURNS:
        del mem[:-CHAT_MAX_TURNS]


def _set_trading_enabled(enabled):
    state = load_state()
    state["trading_enabled"] = bool(enabled)
    save_state(state)
    return state


def _format_money(v):
    try:
        return f"${float(v):.2f}"
    except:
        return "n/a"


def _handle_chat_command(text):
    t = (text or "").strip()
    low = t.lower()
    if low in ("/help", "help"):
        return True, "😼 Commands:\n/balance - Bybit USDT equity\n/status - bot status\n/stop - disable new entries\n/start - enable new entries"
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
        return True, (
            f"📡 Status\n"
            f"Bot thread: {'running' if _is_bot_running() else 'stopped'}\n"
            f"Trading enabled: {st.get('trading_enabled', True)}\n"
            f"Paused: {is_paused}\n"
            f"Equity (cached): {_format_money(equity)}\n"
            f"Position: {side or 'none'}"
        )
    return False, ""


def _call_claude_chat(messages):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "ANTHROPIC_API_KEY is not set."
    if anthropic is None:
        return "Anthropic SDK is not available."
    client = anthropic.Anthropic()
    system = "You are a tsundere personal assistant inside a crypto trading bot chat. Keep replies short (1-4 sentences). Be a little sassy but helpful."
    candidates = [CHAT_MODEL, "claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"]
    for model in candidates:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=CHAT_MAX_OUTPUT_TOKENS,
                system=system,
                messages=messages
            )
            return resp.content[0].text
        except:
            continue
    return "Claude API error."


# ─── HTML templates ──────────────────────────────────────────────────────────

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
        .log { background: #0f172a; border-radius: 8px; padding: 12px; max-height: 300px; overflow-y: auto; font-family: 'Consolas', monospace; font-size: 0.85rem; }
        .log-entry { padding: 4px 0; border-bottom: 1px solid #1e293b; }
        .log-time { color: #64748b; margin-right: 8px; }
        .log-msg { color: #e2e8f0; }
        .log-msg.error { color: #ef4444; }
        .log-msg.order { color: #38bdf8; }
        .log-msg.close { color: #4ade80; }
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
                    <div class="stat-label">Avg Win / Loss</div>
                    <div class="stat-value" id="avgWinLoss">--</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Controls</h2>
            <div class="controls">
                <button class="btn-success" id="btnResume" onclick="control('resume')">▶ Resume</button>
                <button class="btn-warning" id="btnPause" onclick="control('pause')">⏸ Pause</button>
                <button class="btn-danger" id="btnStop" onclick="control('stop')">⏹ Stop Bot</button>
                <button class="btn-primary" id="btnStart" onclick="control('start_bot')">▶ Start Bot</button>
            </div>
            <div class="info" style="margin-top: 12px;">
                <strong>Max Daily Loss:</strong> $2 |
                <strong>Max Consecutive Loss:</strong> $4 |
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
        function money(v) {
            return typeof v === 'number' && Number.isFinite(v) ? `$${v.toFixed(2)}` : '--';
        }

        function tradeMood(pnl) {
            if (typeof pnl !== 'number' || !Number.isFinite(pnl)) return { icon: '😴', label: 'No trade', tone: 'neutral' };
            if (pnl <= -4) return { icon: '😱', label: 'Very bad', tone: 'negative' };
            if (pnl <= -2) return { icon: '😬', label: 'Bad', tone: 'negative' };
            if (pnl < 0)  return { icon: '😐', label: 'Slightly red', tone: 'negative' };
            if (pnl < 1)  return { icon: '🙂', label: 'Okay', tone: 'neutral' };
            if (pnl < 3)  return { icon: '😎', label: 'Good', tone: 'positive' };
            return { icon: '🚀', label: 'Great', tone: 'positive' };
        }

        async function fetchData() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) throw new Error(`Status ${res.status}`);
                const d = await res.json();

                document.getElementById('equity').textContent = money(d.equity);
                const dpEl = document.getElementById('dailyPnl');
                dpEl.textContent = money(d.daily_pnl);
                dpEl.className = 'stat-value ' + (d.daily_pnl > 0 ? 'positive' : d.daily_pnl < 0 ? 'negative' : 'neutral');

                document.getElementById('consecutiveLoss').textContent = money(d.consecutive_loss);

                const lpEl = document.getElementById('lifetimePnl');
                lpEl.textContent = money(d.lifetime_pnl);
                lpEl.className = 'stat-value ' + (d.lifetime_pnl > 0 ? 'positive' : d.lifetime_pnl < 0 ? 'negative' : 'neutral');

                document.getElementById('tradesToday').textContent = d.trades_today ?? 0;
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

                const pnl = d.live_pnl;
                const mood = tradeMood(pnl);
                const livePnlEl = document.getElementById('livePnl');
                livePnlEl.textContent = money(pnl);
                livePnlEl.className = `stat-value ${pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral'}`;
                const moodEl = document.getElementById('tradeMood');
                moodEl.textContent = `${mood.icon} ${mood.label}`;
                moodEl.className = `stat-value ${mood.tone}`;

                document.getElementById('tradeSide').textContent = d.position_side || '--';
                document.getElementById('entryMark').textContent =
                    d.entry_price && d.mark_price
                        ? `$${d.entry_price.toFixed(2)} / $${d.mark_price.toFixed(2)}`
                        : '--';

                const total = (d.win_count || 0) + (d.loss_count || 0);
                document.getElementById('winRate').textContent =
                    total > 0 ? `${((d.win_count / total) * 100).toFixed(1)}%` : '--';

                document.getElementById('avgWinLoss').textContent =
                    (d.avg_win || d.avg_loss)
                        ? `W: $${(d.avg_win || 0).toFixed(4)} / L: $${(d.avg_loss || 0).toFixed(4)}`
                        : '--';

                // Status badge
                const statusEl = document.getElementById('botStatus');
                if (d.paused) {
                    statusEl.innerHTML = '<span class="status-badge status-paused">Paused</span>';
                } else if (d.bot_running) {
                    statusEl.innerHTML = '<span class="status-badge status-running">Running</span>';
                } else {
                    statusEl.innerHTML = '<span class="status-badge status-error">Stopped</span>';
                }

                // Button visibility
                document.getElementById('btnResume').style.display  = d.paused ? '' : 'none';
                document.getElementById('btnPause').style.display   = (!d.paused && d.bot_running) ? '' : 'none';
                document.getElementById('btnStop').style.display    = d.bot_running ? '' : 'none';
                document.getElementById('btnStart').style.display   = !d.bot_running ? '' : 'none';

                fetchLog();
            } catch (e) {
                console.error('Fetch error:', e);
            }
        }

        async function fetchLog() {
            try {
                const res = await fetch('/api/log');
                if (!res.ok) return;
                const data = await res.json();
                const log = document.getElementById('activityLog');
                const lines = (data.log || []).slice(-12).reverse();
                if (!lines.length) return;
                log.innerHTML = lines.map(line => {
                    let text = line.trim();
                    let timeStr = '';
                    let cls = 'log-msg';
                    try {
                        const item = JSON.parse(text);
                        timeStr = item.ts ? new Date(item.ts).toLocaleTimeString() : '';
                        const ev = (item.event || '').toLowerCase();
                        cls += ` ${ev}`;
                        text = `[${item.event}] ${item.reason || item.decision || item.retMsg || ''}`;
                    } catch {
                        cls += text.toLowerCase().includes('error') ? ' error' : '';
                    }
                    return `<div class="log-entry"><span class="log-time">${timeStr}</span><span class="${cls}">${text}</span></div>`;
                }).join('');
            } catch (e) {
                console.error('Log error:', e);
            }
        }

        async function control(action) {
            try {
                await fetch(`/api/${action}`, { method: 'POST' });
                setTimeout(fetchData, 500);
            } catch (e) {
                console.error('Control error:', e);
            }
        }

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
    :root{--bg:#f4f5ff;--panel:#ffffff;--border:rgba(0,0,0,.06);--text:#111827;--me:#4f46e5;--me2:#6d28d9;--meText:#ffffff;--bot:#eef2ff;--botText:#111827;--input:#f3f4f6;}
    *{box-sizing:border-box;}
    body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:radial-gradient(1000px 600px at 40% -10%,rgba(99,102,241,.35),rgba(0,0,0,0) 60%),var(--bg);color:var(--text);height:100vh;display:flex;justify-content:center;align-items:center;padding:16px;}
    .phone{width:min(420px,100%);height:min(840px,100%);background:var(--panel);border:1px solid var(--border);border-radius:28px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 18px 50px rgba(17,24,39,.18);}
    .topbar{padding:16px;background:linear-gradient(135deg,var(--me) 0%,var(--me2) 100%);display:flex;gap:12px;align-items:center;}
    .avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);display:flex;align-items:center;justify-content:center;font-weight:800;color:white;}
    .titlewrap{display:flex;flex-direction:column;line-height:1.1}
    .title{font-weight:750;color:white}
    .subtitle{font-size:12px;color:rgba(255,255,255,.85)}
    .msgs{flex:1;padding:14px 12px;overflow:auto;background:linear-gradient(180deg,#ffffff 0%,#fbfbff 100%);}
    .row{display:flex;margin:8px 0;flex-direction:column;}
    .row.me{align-items:flex-end;}
    .row.bot{align-items:flex-start;}
    .bubble{max-width:78%;padding:10px 12px;border-radius:18px;font-size:14px;line-height:1.35;white-space:pre-wrap;word-wrap:break-word;}
    .row.me .bubble{background:linear-gradient(135deg,var(--me) 0%,var(--me2) 100%);color:var(--meText);border-bottom-right-radius:6px;}
    .row.bot .bubble{background:var(--bot);border:1px solid rgba(0,0,0,.05);color:var(--botText);border-bottom-left-radius:6px;}
    .emotion-img{max-width:150px;border-radius:12px;margin-top:4px;border:1px solid var(--border);}
    .chips{padding:8px 12px;display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--border);background:#fff;}
    .chip{border:1px solid rgba(0,0,0,.08);background:#fff;color:var(--text);padding:7px 10px;border-radius:999px;font-size:12px;cursor:pointer;}
    .composer{border-top:1px solid var(--border);padding:10px 12px;background:#fff;display:flex;gap:10px;align-items:center;}
    .input{flex:1;background:var(--input);border:1px solid rgba(0,0,0,.08);border-radius:999px;padding:10px 12px;color:var(--text);outline:none;font-size:14px;}
    .send{width:40px;height:40px;border-radius:50%;border:none;background:linear-gradient(135deg,var(--me) 0%,var(--me2) 100%);color:white;cursor:pointer;font-weight:800;}
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
    function add(role, text, emotion){
      const row = document.createElement('div');
      row.className = 'row ' + (role === 'user' ? 'me' : 'bot');
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = text;
      row.appendChild(b);
      if (emotion){
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
        const res = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t})});
        const data = await res.json();
        add('assistant', data.reply || 'No reply', data.emotion);
      }catch(e){
        add('assistant','Error: '+(e?.message||e));
      }
    }
    document.getElementById('sendBtn').addEventListener('click',()=>sendText());
    input.addEventListener('keydown',(e)=>{ if(e.key==='Enter') sendText(); });
    add('assistant','Hi Theophilus! Try /balance or ask me anything 😼');
  </script>
</body>
</html>'''


# ─── routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/chat')
def chat():
    return render_template_string(CHAT_TEMPLATE)


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
    perf = trader_bot.performance_summary(state)
    return jsonify({
        "equity": state.get("equity"),
        "daily_pnl": state.get("daily_pnl"),
        "consecutive_loss": state.get("consecutive_loss"),
        "lifetime_pnl": state.get("lifetime_pnl"),
        "live_pnl": state.get("live_pnl"),
        "position_side": state.get("position_side"),
        "entry_price": state.get("entry_price"),
        "mark_price": state.get("mark_price"),
        "paused": is_paused,
        "trades_today": state.get("trades_today"),
        "trading_enabled": state.get("trading_enabled", True),
        "win_count": perf.get("wins"),
        "loss_count": perf.get("losses"),
        "avg_win": perf.get("avg_win"),
        "avg_loss": perf.get("avg_loss"),
        "bot_running": _is_bot_running(),
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
    """Stop the bot thread."""
    _stop_bot_thread()
    return jsonify({"status": "stopping"})


@app.route('/api/start_bot', methods=['POST'])
def api_start_bot():
    """Start the bot thread."""
    started = _start_bot_thread()
    return jsonify({"status": "started" if started else "already_running"})


@app.route('/api/start', methods=['POST'])
def api_start():
    """Re-enable trading (does not restart thread)."""
    state = load_state()
    state["trading_enabled"] = True
    save_state(state)
    return jsonify({"status": "trading_enabled"})


@app.route('/api/log')
def api_log():
    return jsonify({"log": get_last_lines(50)})


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
