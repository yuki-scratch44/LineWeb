"""
Chat server with:
- SQLite database
- User icons
- WebSocket real-time chat
"""

import asyncio
import json
import logging
from aiohttp import web, WSMsgType
from datetime import datetime
from pathlib import Path
import uuid
import sqlite3

logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
DB_PATH = ROOT / "chat.db"

# Connected clients (WebSocketResponse)
CLIENTS = set()


# -----------------------------
# SQLite 初期化
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            user TEXT,
            icon TEXT,
            text TEXT,
            time TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_message(id, user, icon, text, time):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO messages(id, user, icon, text, time)
        VALUES (?, ?, ?, ?, ?)
    """, (id, user, icon, text, time))
    conn.commit()
    conn.close()


def load_history(limit=100):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, user, icon, text, time
        FROM messages
        ORDER BY time ASC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    msgs = []
    for id, user, icon, text, time in rows:
        msgs.append({
            "id": id,
            "user": user,
            "icon": icon,
            "text": text,
            "time": time
        })
    return msgs


# -----------------------------
# Web Server
# -----------------------------

def now_iso():
    return datetime.utcnow().isoformat() + "Z"


async def index(request):
    return web.FileResponse(ROOT / 'index.html')


async def history(request):
    try:
        limit = int(request.query.get('limit', '200'))
    except:
        limit = 200

    msgs = load_history(limit)
    return web.json_response({"messages": msgs})


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    CLIENTS.add(ws)

    user = None
    icon = None

    logging.info("Client connected. Total: %d", len(CLIENTS))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                typ = data.get("type")

                # -------- join --------
                if typ == "join":
                    user = data.get("user", "anonymous")
                    icon = data.get("icon", "")

                    # recent history
                    await ws.send_json({
                        "type": "history",
                        "messages": load_history(200)
                    })

                # -------- message --------
                elif typ == "message":
                    text = data.get("text", "")
                    user = data.get("user", user)
                    icon = data.get("icon", icon)
                    client_id = data.get("id")

                    msgid = str(uuid.uuid4())
                    time = now_iso()

                    # save to DB
                    save_message(msgid, user, icon, text, time)

                    entry = {
                        "id": msgid,
                        "user": user,
                        "icon": icon,
                        "text": text,
                        "time": time
                    }

                    # broadcast to everyone
                    await broadcast({"type": "message", "message": entry})

                    # ack to sender
                    await ws.send_json({
                        "type": "ack",
                        "client_id": client_id,
                        "server_id": msgid
                    })

    finally:
        CLIENTS.discard(ws)
        logging.info("Client disconnected. Total: %d", len(CLIENTS))

    return ws


async def broadcast(payload, exclude=None):
    txt = json.dumps(payload)
    tasks = []

    for c in list(CLIENTS):
        if c.closed:
            CLIENTS.discard(c)
            continue
        if c is exclude:
            continue
        tasks.append(c.send_str(txt))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app():
    init_db()
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

    web.run_app(create_app(), host=args.host, port=args.port)
