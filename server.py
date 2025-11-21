"""
Render-ready chat app using FastAPI + WebSocket + SQLite (file DB).

Features:
- User register/login with JWT
- Image upload (for profile icons and message images) saved under ./static/uploads
- Messages persisted in SQLite
- Read receipts, message edit
- WebSocket endpoint /ws for real-time events
- Single-room demo (can be extended)
Notes:
- For production, set SECRET_KEY env var, enable HTTPS/WSS, use bcrypt/passlib,
  use cloud storage for files, add rate-limits and CSRF protections where needed.
"""
import os
import asyncio
import json
import sqlite3
import hashlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict
from contextlib import asynccontextmanager

import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from jwt import ExpiredSignatureError, InvalidTokenError

# Config
ROOT = Path(__file__).parent
DB_PATH = ROOT / "chat.db"
UPLOAD_DIR = ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
JWT_ALGO = "HS256"
TOKEN_EXPIRE_MIN = 60 * 24 * 7  # 7 days

# ─────────────────────────────────
# Lifespan (on_startup replacement)
# ─────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(lifespan=lifespan)

# Serve static files (client)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# ----------------------------
# Database helpers (sqlite3)
# ----------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        icon_path TEXT
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT,
        image_path TEXT,
        time TEXT,
        edit_time TEXT
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS read_states (
        message_id INTEGER,
        user_id INTEGER,
        read_time TEXT,
        PRIMARY KEY (message_id, user_id)
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS tokens (
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        expire_at TEXT
    );
    """)
    conn.commit()
    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def create_token(user_id: int) -> str:
    payload = {"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MIN)}
    token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGO)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO tokens(token, user_id, expire_at) VALUES (?, ?, ?)",
              (token, user_id, (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MIN)).isoformat()))
    conn.commit()
    conn.close()
    return token

def verify_token(token: str):
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGO])
        sub = data.get("sub")
        try:
            return int(sub)
        except Exception:
            print("verify_token: invalid 'sub' in token:", sub)
            return None
    except ExpiredSignatureError:
        print("verify_token: token expired")
        return None
    except InvalidTokenError as e:
        print("verify_token: invalid token:", e)
        return None
    except Exception as e:
        print("verify_token: unexpected error:", e)
        return None

# ----------------------------
# Simple WebSocket manager
# ----------------------------
class ConnectionManager:
    def __init__(self):
        self.active: Dict[WebSocket, int] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        async with self.lock:
            self.active[websocket] = user_id

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.active:
                del self.active[websocket]

    async def broadcast(self, payload: dict, exclude_ws: WebSocket = None):
        txt = json.dumps(payload)
        to_remove = []
        async with self.lock:
            websockets = list(self.active.keys())
        for ws in websockets:
            if ws is exclude_ws:
                continue
            try:
                await ws.send_text(txt)
            except Exception:
                to_remove.append(ws)
        if to_remove:
            async with self.lock:
                for ws in to_remove:
                    self.active.pop(ws, None)

    def get_user_id(self, websocket: WebSocket):
        return self.active.get(websocket)

manager = ConnectionManager()

# ----------------------------
# Utility: load history
# ----------------------------
def load_history(limit=200):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    SELECT messages.id, messages.user_id, users.username, users.icon_path,
           messages.text, messages.image_path, messages.time, messages.edit_time
    FROM messages
    LEFT JOIN users ON users.id = messages.user_id
    ORDER BY messages.id ASC
    LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    msgs = []
    for r in rows:
        msgs.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "icon": r["icon_path"],
            "text": r["text"],
            "image": r["image_path"],
            "time": r["time"],
            "edit_time": r["edit_time"]
        })
    return msgs

# ----------------------------
# REST endpoints
# ----------------------------
@app.get("/")
async def index():
    path = ROOT / "static" / "index.html"
    return FileResponse(path)

@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users(username, password_hash) VALUES (?, ?)", (username, hash_password(password)))
        conn.commit()
        user_id = c.lastrowid
        token = create_token(user_id)
        return {"user_id": user_id, "token": token}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="username_taken")
    finally:
        conn.close()

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
    r = c.fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=400, detail="invalid_credentials")
    if hash_password(password) != r["password_hash"]:
        raise HTTPException(status_code=400, detail="invalid_credentials")
    token = create_token(r["id"])
    return {"user_id": r["id"], "token": token}

@app.post("/upload")
async def upload(file: UploadFile = File(...), token: str = Form(None), type: str = Form("image")):
    user_id = None
    if token:
        user_id = verify_token(token)
    ext = Path(file.filename).suffix or ".bin"
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / fname
    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)
    url_path = f"/static/uploads/{fname}"
    if type == "icon" and user_id:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE users SET icon_path = ? WHERE id = ?", (url_path, user_id))
        conn.commit()
        conn.close()
    return {"url": url_path}

@app.get("/history")
async def history(limit: int = 200):
    msgs = load_history(limit)
    return {"messages": msgs}

# ----------------------------
# WebSocket endpoint
# ----------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return
    user_id = verify_token(token)
    if not user_id:
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, user_id)
    await websocket.send_text(json.dumps({"type": "history", "messages": load_history(200)}))
    try:
        while True:
            data_txt = await websocket.receive_text()
            try:
                data = json.loads(data_txt)
            except Exception:
                await websocket.send_text(json.dumps({"type": "error", "message": "invalid_json"}))
                continue
            typ = data.get("type")
            if typ == "message":
                client_id = data.get("id")
                text = data.get("text")
                image = data.get("image")
                msg_time = datetime.utcnow().isoformat() + "Z"
                conn = get_conn()
                c = conn.cursor()
                c.execute("INSERT INTO messages(user_id, text, image_path, time) VALUES (?, ?, ?, ?)",
                          (user_id, text, image, msg_time))
                conn.commit()
                server_id = c.lastrowid
                conn.close()
                entry = {
                    "id": server_id,
                    "user_id": user_id,
                    "username": get_username(user_id),
                    "icon": get_user_icon(user_id),
                    "text": text,
                    "image": image,
                    "time": msg_time
                }
                await manager.broadcast({"type": "message", "message": entry}, exclude_ws=websocket)
                await websocket.send_text(json.dumps({"type": "ack", "client_id": client_id, "server_id": server_id}))

            elif typ == "edit":
                message_id = int(data.get("message_id"))
                new_text = data.get("new_text")
                edit_time = datetime.utcnow().isoformat() + "Z"
                conn = get_conn()
                c = conn.cursor()
                c.execute("SELECT user_id FROM messages WHERE id = ?", (message_id,))
                r = c.fetchone()
                if not r or r["user_id"] != user_id:
                    await websocket.send_text(json.dumps({"type": "error", "message": "not_allowed"}))
                    conn.close()
                    continue
                c.execute("UPDATE messages SET text = ?, edit_time = ? WHERE id = ?", (new_text, edit_time, message_id))
                conn.commit()
                conn.close()
                payload = {"type": "edit", "message_id": message_id, "new_text": new_text, "edit_time": edit_time}
                await manager.broadcast(payload)

            elif typ == "read":
                message_id = int(data.get("message_id"))
                read_time = datetime.utcnow().isoformat() + "Z"
                conn = get_conn()
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO read_states(message_id, user_id, read_time) VALUES (?, ?, ?)",
                          (message_id, user_id, read_time))
                conn.commit()
                conn.close()
                payload = {"type": "read", "message_id": message_id, "user_id": user_id, "read_time": read_time}
                await manager.broadcast(payload)

            elif typ == "typing":
                state = bool(data.get("state", False))
                payload = {"type": "typing", "user_id": user_id, "state": state}
                await manager.broadcast(payload, exclude_ws=websocket)

            else:
                await websocket.send_text(json.dumps({"type": "error", "message": "unknown_type"}))

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)

# Helpers
def get_username(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r["username"] if r else "unknown"

def get_user_icon(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT icon_path FROM users WHERE id = ?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r["icon_path"] if r else None
