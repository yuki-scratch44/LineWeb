"""
Simple chat server that serves an HTML client and a WebSocket endpoint.
Usage:
  1. pip install aiohttp
  2. python /mnt/data/server.py
Then open http://localhost:8080/ in your browser.

Notes:
- This is a minimal single-room chat for demonstration.
- For production, use persistent storage, authentication, TLS, and process managers.
"""
import asyncio
import json
import logging
from aiohttp import web, WSMsgType
from datetime import datetime
from pathlib import Path
import uuid

logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent

# In-memory message store (list of dicts)
MESSAGES = []
MAX_MESSAGES = 1000

# Connected websockets (set of aiohttp.web.WebSocketResponse)
CLIENTS = set()

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

async def index(request):
    return web.FileResponse(ROOT / 'index.html')

async def history(request):
    try:
        limit = int(request.query.get('limit', '50'))
    except:
        limit = 50
    # return last `limit` messages (ascending)
    msgs = MESSAGES[-limit:]
    return web.json_response({"messages": msgs})

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    CLIENTS.add(ws)
    user = None
    logging.info("Client connected. Total: %d", len(CLIENTS))

    # Optionally send a ready/system message or history
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception as e:
                    await ws.send_json({"type": "error", "message": "invalid json"})
                    continue

                typ = data.get("type")
                if typ == "join":
                    # data: {type: "join", user: "Alice"}
                    user = data.get("user", "anonymous")
                    # send recent history
                    await ws.send_json({"type": "history", "messages": MESSAGES[-50:]})
                    # broadcast presence
                    payload = {"type": "system", "message": f"{user} joined", "time": now_iso()}
                    await broadcast(payload)
                elif typ == "message":
                    # data: {type: "message", id: client_temp_id, text: "...", user: "Alice"}
                    text = data.get("text", "")
                    user = data.get("user", user or "anonymous")
                    msgid = str(uuid.uuid4())
                    entry = {
                        "id": msgid,
                        "user": user,
                        "text": text,
                        "time": now_iso()
                    }
                    MESSAGES.append(entry)
                    # cap memory
                    if len(MESSAGES) > MAX_MESSAGES:
                        del MESSAGES[0: len(MESSAGES) - MAX_MESSAGES]
                    # broadcast to all clients
                    await broadcast({"type": "message", "message": entry})
                    # send ack back to sender mapping client id to server id
                    await ws.send_json({"type": "ack", "client_id": data.get("id"), "server_id": msgid})
                elif typ == "typing":
                    # broadcast typing notification to others
                    await broadcast({"type": "typing", "user": data.get("user"), "state": data.get("state", False)}, exclude=ws)
                else:
                    await ws.send_json({"type": "error", "message": "unknown message type"})
            elif msg.type == WSMsgType.ERROR:
                logging.error('ws connection closed with exception %s', ws.exception())
    finally:
        CLIENTS.discard(ws)
        if user:
            await broadcast({"type": "system", "message": f"{user} left", "time": now_iso()})
        logging.info("Client disconnected. Total: %d", len(CLIENTS))

    return ws

async def broadcast(payload, exclude=None):
    # send payload (dict) to all connected clients except `exclude`
    txt = json.dumps(payload)
    coros = []
    for c in list(CLIENTS):
        if c.closed:
            CLIENTS.discard(c)
            continue
        if c is exclude:
            continue
        coros.append(c.send_str(txt))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)

def create_app():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/history', history)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_static('/static/', path=ROOT, show_index=False)
    return app

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    app = create_app()
    web.run_app(app, host=args.host, port=args.port)
