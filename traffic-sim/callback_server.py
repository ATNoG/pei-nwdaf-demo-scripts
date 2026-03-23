"""
Callback server for NWDAF decision notifications.
Receives anomaly/forecasting notifications and applies network mitigations.

Usage:
    CONTROLLER_URL=http://localhost:8000 \
    BRIDGE_INTERFACE=br-69b90f952e97 \
    MITIGATION_DURATION=120 \
    python callback_server.py
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Decision Callback Server")

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8000")
BRIDGE_INTERFACE = os.getenv("BRIDGE_INTERFACE", "br-6b9d61bf076a")
VICTIM_CONTAINER = os.getenv("VICTIM_CONTAINER", "traffic-sim-victim-1")
MITIGATION_DURATION = int(os.getenv("MITIGATION_DURATION", "120"))
PORT = int(os.getenv("PORT", "9999"))

# Singleton tasks per mitigation type
_mitigation_tasks: dict[str, asyncio.Task] = {}

# WebSocket subscribers
_ws_clients: list[WebSocket] = []


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _broadcast(event: dict):
    data = json.dumps(event)
    for ws in list(_ws_clients):
        asyncio.create_task(ws.send_text(data))


def _iptables(args: list[str]) -> bool:
    result = subprocess.run(["iptables"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("[iptables] %s", result.stderr.strip())
    return result.returncode == 0


_CONNLIMIT_ARGS = [
    "-p", "tcp",
    "-m", "connlimit",
    "--connlimit-above", "3",
    "--connlimit-mask", "32",
    "-j", "REJECT",
]


async def _expire_syn_block(duration: int):
    await asyncio.sleep(duration)
    _iptables(["-D", "FORWARD", "-i", BRIDGE_INTERFACE] + _CONNLIMIT_ARGS)
    logger.info("[MITIGATION] Connection limit expired after %ds", duration)
    _mitigation_tasks.pop("limit_connections", None)
    _broadcast({"type": "expired", "action": "limit_connections", "time": _now()})


async def _expire_rate_limit(duration: int):
    await asyncio.sleep(duration)
    _iptables(["-D", "FORWARD", "-i", BRIDGE_INTERFACE,
               "-m", "limit", "--limit", "100/sec", "--limit-burst", "200", "-j", "ACCEPT"])
    _iptables(["-D", "FORWARD", "-i", BRIDGE_INTERFACE, "-j", "DROP"])
    logger.info("[MITIGATION] Rate limit expired after %ds", duration)
    _mitigation_tasks.pop("rate_limit_traffic", None)
    _broadcast({"type": "expired", "action": "rate_limit_traffic", "time": _now()})


def _reset_task(key: str, coro):
    existing = _mitigation_tasks.get(key)
    if existing and not existing.done():
        existing.cancel()
        logger.info("[MITIGATION] Reset timer for %s", key)
    _mitigation_tasks[key] = asyncio.create_task(coro)


async def limit_connections():
    already_active = "limit_connections" in _mitigation_tasks and not _mitigation_tasks["limit_connections"].done()
    if not already_active:
        _iptables(["-I", "FORWARD", "-i", BRIDGE_INTERFACE] + _CONNLIMIT_ARGS)
        logger.info("[MITIGATION] Connection limit applied on %s for %ds", BRIDGE_INTERFACE, MITIGATION_DURATION)
    else:
        logger.info("[MITIGATION] Connection limit already active — resetting timer")
    subprocess.run(["conntrack", "-D", "-p", "tcp", "-s", "172.19.0.0/16"], capture_output=True)
    logger.info("[MITIGATION] Existing connections flushed via conntrack")
    _reset_task("limit_connections", _expire_syn_block(MITIGATION_DURATION))


async def rate_limit_traffic():
    already_active = "rate_limit_traffic" in _mitigation_tasks and not _mitigation_tasks["rate_limit_traffic"].done()
    if not already_active:
        _iptables(["-I", "FORWARD", "-i", BRIDGE_INTERFACE,
                   "-m", "limit", "--limit", "100/sec", "--limit-burst", "200", "-j", "ACCEPT"])
        _iptables(["-A", "FORWARD", "-i", BRIDGE_INTERFACE, "-j", "DROP"])
        logger.info("[MITIGATION] Rate limiting applied on %s for %ds", BRIDGE_INTERFACE, MITIGATION_DURATION)
    else:
        logger.info("[MITIGATION] Rate limit already active — resetting timer")
    _reset_task("rate_limit_traffic", _expire_rate_limit(MITIGATION_DURATION))


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Demo consumer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f6f8fa; color: #24292f; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; height: 100vh; display: flex; flex-direction: column; }
  header { padding: 16px 28px; border-bottom: 1px solid #d0d7de; background: #fff; display: flex; align-items: center; gap: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  header .dot { width: 10px; height: 10px; border-radius: 50%; background: #1a7f37; box-shadow: 0 0 6px #1a7f37; animation: pulse 2s infinite; }
  header h1 { font-size: 15px; font-weight: 600; color: #24292f; letter-spacing: 0.02em; }
  header .subtitle { font-size: 11px; color: #57606a; margin-left: auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .status-bar { display: flex; gap: 10px; padding: 10px 28px; border-bottom: 1px solid #d0d7de; background: #fff; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; border: 1px solid; font-weight: 500; }
  .badge.inactive { color: #57606a; border-color: #d0d7de; background: #f6f8fa; }
  .badge.active { color: #1a7f37; border-color: #1a7f37; background: #dafbe1; animation: pulse 1.5s infinite; }

  #feed { flex: 1; overflow-y: auto; padding: 16px 28px; display: flex; flex-direction: column; gap: 10px; }
  .entry { background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 14px 18px; animation: slideIn .3s ease; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
  .entry.decision { border-left: 3px solid #cf222e; }
  .entry.mitigation { border-left: 3px solid #9a6700; }
  .entry.expired { border-left: 3px solid #57606a; }
  @keyframes slideIn { from{opacity:0;transform:translateY(-8px)} to{opacity:1;transform:translateY(0)} }

  .entry-header { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .entry-type { font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
  .entry.decision .entry-type { color: #cf222e; }
  .entry.mitigation .entry-type { color: #9a6700; }
  .entry.expired .entry-type { color: #57606a; }
  .entry-time { font-size: 11px; color: #57606a; }

  .cell-id { font-size: 13px; color: #0969da; font-weight: 600; margin-bottom: 6px; }
  .reasoning { font-size: 12px; color: #57606a; line-height: 1.5; margin-bottom: 8px; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; }
  .action-tag { font-size: 11px; padding: 2px 8px; border-radius: 4px; background: #fff8c5; border: 1px solid #d4a72c; color: #7d4e00; font-weight: 500; }
  .action-tag.applied { background: #dafbe1; border-color: #1a7f37; color: #1a7f37; }

  .empty { color: #57606a; font-size: 13px; text-align: center; margin-top: 60px; }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>Consumer example</h1>
  <span class="subtitle" id="clock"></span>
</header>
<div class="status-bar">
  <span class="badge inactive" id="badge-limit_connections">limit_connections</span>
  <span class="badge inactive" id="badge-rate_limit_traffic">rate_limit_traffic</span>
</div>
<div id="feed"><p class="empty">Waiting for decisions...</p></div>

<script>
const feed = document.getElementById('feed');
const clock = document.getElementById('clock');

setInterval(() => {
  clock.textContent = new Date().toLocaleTimeString();
}, 1000);

function setBadge(action, active) {
  const el = document.getElementById('badge-' + action);
  if (!el) return;
  el.className = 'badge ' + (active ? 'active' : 'inactive');
}

function addEntry(cls, typeLabel, html) {
  const empty = feed.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = 'entry ' + cls;
  const now = new Date().toLocaleTimeString();
  div.innerHTML = `<div class="entry-header"><span class="entry-type">${typeLabel}</span><span class="entry-time">${now}</span></div>${html}`;
  feed.prepend(div);
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
  const d = JSON.parse(e.data);

  if (d.type === 'decision') {
    const actions = (d.decisions || []).map(a =>
      `<span class="action-tag">${a}</span>`
    ).join('');
    const applied = (d.actions_taken || []).map(a =>
      `<span class="action-tag applied">✓ ${a}</span>`
    ).join('');
    addEntry('decision', 'Anomaly Decision', `
      <div class="reasoning">${d.reasoning || ''}</div>
      <div class="actions">${actions}${applied ? '<span style="color:#8b949e;font-size:11px;margin:0 6px">→ applied:</span>' + applied : ''}</div>
    `);
    (d.actions_taken || []).forEach(a => setBadge(a, true));
  }

  if (d.type === 'mitigation') {
    addEntry('mitigation', 'Mitigation Applied', `
      <div class="actions"><span class="action-tag applied">✓ ${d.action}</span></div>
    `);
    setBadge(d.action, true);
  }

  if (d.type === 'expired') {
    addEntry('expired', 'Mitigation Expired', `
      <div class="actions"><span class="action-tag">${d.action}</span></div>
    `);
    setBadge(d.action, false);
  }
  };
  ws.onclose = () => setTimeout(connect, 2000);
  ws.onerror = () => ws.close();
}
connect();
</script>
</body>
</html>""")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive, ignore incoming
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


@app.post("/notify")
async def receive_notification(request: Request):
    body = await request.json()
    logger.info("Notification received:\n%s", json.dumps(body, indent=2))

    actions_taken = []
    decisions = [d.get("id", "").lower() for d in body.get("decisions", [])]

    _broadcast({
        "type": "decision",
        "cell_id": body.get("cell_id"),
        "reasoning": body.get("reasoning", ""),
        "decisions": decisions,
        "actions_taken": [],
        "time": _now(),
    })

    for decision_id in decisions:
        if decision_id == "limit_connections":
            try:
                await limit_connections()
                actions_taken.append("limit_connections")
                _broadcast({"type": "mitigation", "action": "limit_connections", "time": _now()})
            except Exception as e:
                logger.error("[MITIGATION] limit_connections failed: %s", e)

        elif decision_id == "rate_limit_traffic":
            try:
                await rate_limit_traffic()
                actions_taken.append("rate_limit_traffic")
                _broadcast({"type": "mitigation", "action": "rate_limit_traffic", "time": _now()})
            except Exception as e:
                logger.error("[MITIGATION] rate_limit_traffic failed: %s", e)

        elif decision_id == "alert_operator":
            logger.warning("[ALERT] Operator notification triggered — manual review required")
            actions_taken.append("alert_operator")

    return {"status": "ok", "actions": actions_taken}


@app.delete("/mitigations")
async def clear_mitigations():
    for task in list(_mitigation_tasks.values()):
        if not task.done():
            task.cancel()
    _mitigation_tasks.clear()
    _iptables(["-D", "FORWARD", "-i", BRIDGE_INTERFACE] + _CONNLIMIT_ARGS)
    logger.info("[MITIGATION] All mitigations cleared")
    _broadcast({"type": "expired", "action": "all", "time": _now()})
    return {"status": "ok", "cleared": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
