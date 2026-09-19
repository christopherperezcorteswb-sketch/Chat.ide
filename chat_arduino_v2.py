#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHAT.ide LAN v2
===============
Chat multiusuario local inspirado visualmente en Arduino IDE 2.x.

- Funciona SIN Internet.
- Solo necesita Python 3 (sin paquetes externos).
- Usa HTTP POST + Server-Sent Events (SSE).
- Guarda mensajes en SQLite.
- Tiene salas, reconexion, presencia, limite de mensajes y estado LAN.

Uso:
    python chat_arduino_v2.py
    python chat_arduino_v2.py 8080

Los demas dispositivos deben estar en la misma red local y abrir:
    http://IP_DEL_SERVIDOR:8000
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8000
HISTORY_MAX = 250
MSG_MAX = 500
NAME_MAX = 20
ROOM_MAX = 30
BODY_MAX = 8192
QUEUE_MAX = 300
RECONNECT_GRACE_SECONDS = 25
RATE_WINDOW_SECONDS = 4
RATE_MAX_MESSAGES = 12

ROOMS = ["General", "Robot", "Programacion", "Equipo"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CHATIDE_DATA_DIR", os.path.join(BASE_DIR, "chatide_data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "chatide.sqlite3")

STARTED_AT = time.time()
LOCK = threading.RLock()
CLIENTS: dict[str, dict] = {}


def parse_port() -> int:
    if len(sys.argv) < 2:
        return DEFAULT_PORT
    try:
        port = int(sys.argv[1])
    except ValueError:
        print(f"Puerto invalido: {sys.argv[1]!r}. Usando {DEFAULT_PORT}.")
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        print(f"Puerto fuera de rango: {port}. Usando {DEFAULT_PORT}.")
        return DEFAULT_PORT
    return port


PORT = parse_port()


# ---------------------------------------------------------------------------
# BASE DE DATOS SQLITE
# ---------------------------------------------------------------------------
def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db_connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                room TEXT NOT NULL,
                ts INTEGER NOT NULL,
                name TEXT NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_room_ts "
            "ON messages(room, ts DESC)"
        )


def save_message(event: dict) -> None:
    with db_connect() as con:
        con.execute(
            "INSERT INTO messages(id, room, ts, name, text) VALUES(?,?,?,?,?)",
            (event["id"], event["room"], event["t"], event["name"], event["text"]),
        )


def load_history(room: str, limit: int = HISTORY_MAX) -> list[dict]:
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT id, room, ts, name, text
            FROM messages
            WHERE room = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (room, int(limit)),
        ).fetchall()
    rows = list(reversed(rows))
    return [
        {
            "type": "msg",
            "id": row["id"],
            "room": row["room"],
            "t": row["ts"],
            "name": row["name"],
            "text": row["text"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def clean_text(raw, max_len: int) -> str:
    text = "".join(ch for ch in str(raw) if ch.isprintable() or ch in "\t ")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())[:max_len]


def clean_name(raw) -> str:
    name = clean_text(raw, NAME_MAX).strip()
    return name or "Anonimo"


def clean_room(raw) -> str:
    room = clean_text(raw, ROOM_MAX).strip()
    if room in ROOMS:
        return room
    return "General"


def sys_event(text: str, room: str) -> dict:
    return {"type": "sys", "id": uuid.uuid4().hex, "room": room, "t": now_ms(), "text": text}


def unique_name(name: str, room: str, ignore_token: str | None = None) -> str:
    taken = {
        c["name"].lower()
        for token, c in CLIENTS.items()
        if token != ignore_token and c["room"] == room
    }
    candidate = name
    n = 1
    while candidate.lower() in taken:
        n += 1
        suffix = f" ({n})"
        candidate = (name[: max(1, NAME_MAX - len(suffix))] + suffix)[:NAME_MAX]
    return candidate


def safe_enqueue(q: queue.Queue, event: dict | None) -> None:
    try:
        q.put_nowait(event)
        return
    except queue.Full:
        pass

    # Si un cliente es muy lento, descartamos el evento mas viejo antes de
    # bloquear a todo el chat.
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(event)
    except queue.Full:
        pass


def room_clients(room: str):
    return [c for c in CLIENTS.values() if c["room"] == room]


def users_event(room: str) -> dict:
    names = sorted(
        [c["name"] for c in room_clients(room) if c.get("connected")],
        key=str.casefold,
    )
    return {"type": "users", "room": room, "names": names, "count": len(names)}


def broadcast_room(room: str, event: dict) -> None:
    for c in room_clients(room):
        q = c.get("q")
        if c.get("connected") and q is not None:
            safe_enqueue(q, event)


def broadcast_users(room: str) -> None:
    broadcast_room(room, users_event(room))


def disconnect(token: str, q: queue.Queue | None = None) -> None:
    """Marca desconexion pero conserva la sesion unos segundos para reconectar."""
    with LOCK:
        c = CLIENTS.get(token)
        if not c:
            return
        if q is not None and c.get("q") is not q:
            return
        c["connected"] = False
        c["q"] = None
        c["disconnected_at"] = time.time()
        broadcast_users(c["room"])


def leave_now(token: str) -> None:
    with LOCK:
        c = CLIENTS.pop(token, None)
        if not c:
            return
        q = c.get("q")
        if q is not None:
            safe_enqueue(q, None)
        room = c["room"]
        if c.get("announced"):
            broadcast_room(room, sys_event(f"{c['name']} salio de la sala", room))
        broadcast_users(room)


def cleanup_loop() -> None:
    while True:
        time.sleep(4)
        expired = []
        now = time.time()
        with LOCK:
            for token, c in list(CLIENTS.items()):
                if c.get("connected"):
                    continue
                disconnected_at = c.get("disconnected_at")
                if disconnected_at and now - disconnected_at > RECONNECT_GRACE_SECONDS:
                    expired.append(token)
        for token in expired:
            leave_now(token)


def can_send(c: dict) -> bool:
    now = time.monotonic()
    stamps: deque = c["rate"]
    while stamps and now - stamps[0] > RATE_WINDOW_SECONDS:
        stamps.popleft()
    if len(stamps) >= RATE_MAX_MESSAGES:
        return False
    stamps.append(now)
    return True


# ---------------------------------------------------------------------------
# RED LOCAL
# ---------------------------------------------------------------------------
def local_ips() -> list[str]:
    ips: set[str] = set()

    # Ruta preferida del sistema operativo. No envia trafico real importante;
    # se usa para conocer la interfaz que Windows/Linux elegiria.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass

    valid = []
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        if ip.is_private:
            valid.append(raw)

    def priority(raw: str):
        if raw.startswith("192.168."):
            p = 0
        elif raw.startswith("10."):
            p = 1
        elif raw.startswith("172."):
            p = 2
        else:
            p = 3
        return (p, tuple(int(x) for x in raw.split(".")))

    return sorted(set(valid), key=priority)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CHATideLAN/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Solo registra errores importantes; evita llenar la consola con SSE.
        if getattr(self, "_log_this", False):
            super().log_message(fmt, *args)

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )

    def send_bytes(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(code, body, "application/json; charset=utf-8")

    def reply(self, code: int):
        self.send_response(code)
        self.common_headers()
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_json(self):
        try:
            raw_len = self.headers.get("Content-Length", "0")
            n = int(raw_len)
            if n < 0 or n > BODY_MAX:
                return None
            raw = self.rfile.read(n)
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else None
        except (ValueError, OSError, json.JSONDecodeError):
            return None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.encode("utf-8")
            return self.send_bytes(200, body, "text/html; charset=utf-8")

        if url.path == "/health":
            with LOCK:
                connected = sum(1 for c in CLIENTS.values() if c.get("connected"))
            return self.send_json(
                200,
                {
                    "ok": True,
                    "uptime_s": int(time.time() - STARTED_AT),
                    "connected": connected,
                    "rooms": ROOMS,
                },
            )

        if url.path == "/events":
            return self.stream(parse_qs(url.query))

        if url.path == "/favicon.ico":
            return self.reply(204)

        self._log_this = True
        self.send_error(404)

    def do_POST(self):
        data = self.read_json()
        if data is None:
            return self.send_json(400, {"ok": False, "error": "JSON invalido"})

        if self.path == "/join":
            return self.join(data)

        if self.path == "/send":
            return self.send_message(data)

        if self.path == "/bye":
            token = str(data.get("token", ""))[:80]
            leave_now(token)
            return self.reply(204)

        self._log_this = True
        self.send_error(404)

    def join(self, data: dict):
        wanted = clean_name(data.get("name", ""))
        room = clean_room(data.get("room", "General"))
        token = uuid.uuid4().hex

        with LOCK:
            name = unique_name(wanted, room)
            CLIENTS[token] = {
                "name": name,
                "room": room,
                "q": None,
                "connected": False,
                "announced": False,
                "disconnected_at": time.time(),
                "rate": deque(),
            }

        history = load_history(room)
        return self.send_json(
            200,
            {
                "ok": True,
                "token": token,
                "name": name,
                "room": room,
                "history": history,
                "server_time": now_ms(),
            },
        )

    def send_message(self, data: dict):
        token = str(data.get("token", ""))[:80]
        text = clean_text(data.get("text", ""), MSG_MAX).strip()
        if not text:
            return self.reply(204)

        with LOCK:
            c = CLIENTS.get(token)
            if not c:
                return self.send_json(401, {"ok": False, "error": "Sesion vencida"})
            if not can_send(c):
                return self.send_json(429, {"ok": False, "error": "Demasiados mensajes"})
            event = {
                "type": "msg",
                "id": uuid.uuid4().hex,
                "room": c["room"],
                "t": now_ms(),
                "name": c["name"],
                "text": text,
            }

        try:
            save_message(event)
        except sqlite3.Error:
            return self.send_json(500, {"ok": False, "error": "No se pudo guardar el mensaje"})

        with LOCK:
            broadcast_room(event["room"], event)
        return self.send_json(200, {"ok": True, "id": event["id"]})

    def stream(self, qs: dict):
        token = qs.get("token", [""])[0][:80]
        if not token:
            return self.send_error(400)

        q = queue.Queue(maxsize=QUEUE_MAX)
        with LOCK:
            c = CLIENTS.get(token)
            if not c:
                return self.send_error(401)

            old_q = c.get("q")
            if old_q is not None and old_q is not q:
                safe_enqueue(old_q, None)

            c["q"] = q
            c["connected"] = True
            c["disconnected_at"] = None

            safe_enqueue(
                q,
                {
                    "type": "hello",
                    "name": c["name"],
                    "room": c["room"],
                    "t": now_ms(),
                },
            )

            if not c["announced"]:
                c["announced"] = True
                broadcast_room(c["room"], sys_event(f"{c['name']} entro a la sala", c["room"]))

            broadcast_users(c["room"])

        self.send_response(200)
        self.common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        try:
            self.wfile.write(b"retry: 1800\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=5)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue

                if event is None:
                    break

                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            disconnect(token, q)


# ---------------------------------------------------------------------------
# INTERFAZ WEB - inspirada en Arduino IDE 2.x
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CHAT.ide | Arduino LAN</title>
<style>
:root{
  --title:#231b20;
  --menu:#1d2327;
  --toolbar:#162126;
  --toolbar2:#152025;
  --teal:#13a6ad;
  --teal2:#008c95;
  --teal3:#006b72;
  --editor:#162125;
  --editor2:#182428;
  --side:#152024;
  --side2:#11191d;
  --panel:#0d1113;
  --panel2:#101719;
  --border:#2c3a40;
  --text:#d7e1e3;
  --dim:#819298;
  --line:#65747a;
  --orange:#f39a3d;
  --green:#65d17a;
  --red:#e45b64;
  --blue:#69b7ff;
  --mono:"Cascadia Mono","Roboto Mono",Consolas,monospace;
  --ui:"Segoe UI",Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--editor);color:var(--text);font-family:var(--ui);overflow:hidden}
button,input,select{font:inherit}
button{cursor:pointer}

.app{height:100%;display:grid;grid-template-rows:30px 54px minmax(0,1fr) 25px;background:var(--editor)}
.menubar{display:flex;align-items:center;gap:18px;padding:0 10px;background:var(--menu);font-size:13px;color:#f2f2f2;border-bottom:1px solid #20282b;user-select:none}
.menubar span:hover{color:#fff;text-decoration:underline}

.toolbar{display:flex;align-items:center;gap:10px;padding:8px 13px;background:var(--toolbar);border-bottom:1px solid var(--border)}
.toolcircle{width:35px;height:35px;border:0;border-radius:50%;background:var(--teal);color:#061719;display:grid;place-items:center;font-weight:900;font-size:20px}
.toolcircle:hover{filter:brightness(1.12)}
.toolcircle svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.board{margin-left:2px;width:300px;height:35px;border:1px solid #72bac0;background:#1c2a2e;color:#eefcfc;display:flex;align-items:center;gap:11px;padding:0 13px;border-radius:2px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.25)}
.board .usb{font-size:18px}
.board .boardname{min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.board .arrow{color:#c8e4e5}
.toolbar-spacer{flex:1}
.wave{color:#e9f6f7;opacity:.9;font-size:26px}

.workspace{min-height:0;display:grid;grid-template-columns:48px 220px minmax(0,1fr)}
.activity{background:var(--side2);border-right:1px solid var(--border);padding-top:6px;display:flex;flex-direction:column;align-items:stretch}
.activity button{height:50px;border:0;border-left:2px solid transparent;background:transparent;color:#74868d;display:grid;place-items:center;padding:0}
.activity button.active{color:#c7d7db;border-left-color:var(--teal)}
.activity button:hover{color:#e7f4f5}
.activity svg{width:27px;height:27px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.activity .bottom{margin-top:auto;margin-bottom:8px}

.explorer{min-width:0;background:var(--side);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.explorer-title{padding:12px 13px 9px;font-size:11px;color:#9aabb0;text-transform:uppercase;letter-spacing:.4px}
.section-title{padding:7px 10px;background:#18252a;border-top:1px solid #202e33;border-bottom:1px solid #202e33;font-size:11px;font-weight:700;color:#c4d1d4;text-transform:uppercase}
.file{padding:7px 10px 7px 16px;color:#dce7e9;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file.active{background:#213035}
.room-chip{margin:10px;padding:8px 9px;border:1px solid var(--border);background:#111a1e;border-radius:3px;font:12px var(--mono);color:#cde7e8}
.users{overflow:auto;padding:5px 0 10px}
.user{display:flex;align-items:center;gap:8px;padding:5px 12px;font-size:12px;color:#bdcbcf}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px rgba(101,209,122,.35)}

.mainarea{min-width:0;min-height:0;display:grid;grid-template-rows:minmax(210px,58%) 6px minmax(190px,42%);background:var(--editor)}
.editorpane{min-height:0;display:grid;grid-template-rows:40px minmax(0,1fr);background:var(--editor)}
.tabs{display:flex;align-items:end;background:#111a1e;border-bottom:1px solid var(--border)}
.tab{height:39px;display:flex;align-items:center;padding:0 14px;border-top:2px solid var(--teal);background:var(--editor);font:13px var(--ui);color:#fff;min-width:210px}
.editorcode{overflow:auto;padding:8px 0 20px;font:14px/1.55 var(--mono);background:var(--editor)}
.code-line{display:flex;min-width:max-content}
.ln{width:58px;flex:none;text-align:right;padding-right:17px;color:#53656b;user-select:none}
.code{white-space:pre;padding-right:30px}
.comment{color:#788c91}.kw{color:#00c1cc}.type{color:#00a9b5}.num{color:#55c4ff}.str{color:#d8b36d}.fn{color:#e89a3d}

.splitter{background:#223137;border-top:1px solid #2d3d43;border-bottom:1px solid #0d1113;cursor:row-resize}
.output{min-height:0;background:#050708;display:grid;grid-template-rows:38px 44px minmax(0,1fr)}
.output-head{display:flex;align-items:center;padding:0 12px;background:#11191d;border-bottom:1px solid #20292d;color:#e8eeee;font-size:13px}
.output-head strong{font-weight:500}
.output-head .tools{margin-left:auto;display:flex;gap:13px;color:#c4d1d5}
.output-head button{border:0;background:transparent;color:inherit;padding:2px 4px}
.sendrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;padding:7px 10px;background:#0b0f11;border-bottom:1px solid #1c2529}
.msginput{width:100%;height:30px;background:#131a1d;color:#ecf5f6;border:1px solid #34434a;border-radius:2px;padding:0 10px;font:13px var(--mono);outline:none}
.msginput:focus{border-color:var(--teal)}
.sendbtn{min-width:80px;border:1px solid #4eaeb5;background:#0b7f86;color:white;border-radius:2px;padding:0 12px}
.sendbtn:hover{background:#09939b}
.console{min-height:0;overflow:auto;padding:9px 12px 18px;color:#d7e0e1;font:13px/1.55 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.console-line{padding:1px 0}
.console-line.me{color:#fff}
.console-line.sys{color:#7c8c91;font-style:italic}
.ts{color:#66797e}.name{font-weight:700}.me .name{color:var(--orange)!important}

.statusbar{display:flex;align-items:center;gap:15px;padding:0 9px;background:var(--teal3);color:#ecffff;font-size:12px;white-space:nowrap;overflow:hidden}
.statusbar .grow{flex:1;overflow:hidden;text-overflow:ellipsis}
.state-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red);margin-right:6px}
.state-dot.ok{background:var(--green)}

.overlay{position:fixed;inset:0;background:rgba(0,0,0,.72);display:grid;place-items:center;padding:18px;z-index:50}
.overlay[hidden]{display:none}
.dialog{width:min(430px,96vw);background:#182328;border:1px solid #44545a;box-shadow:0 18px 60px rgba(0,0,0,.58);border-radius:4px;overflow:hidden}
.dialog-title{background:#11191d;padding:13px 17px;border-bottom:1px solid #334248;font-weight:600}
.dialog-body{display:grid;gap:10px;padding:17px}
.dialog label{font-size:12px;color:#b9c8cb}
.dialog input,.dialog select{width:100%;height:36px;background:#101719;border:1px solid #38494f;color:#f0f6f7;border-radius:2px;padding:0 9px;outline:none}
.dialog input:focus,.dialog select:focus{border-color:var(--teal)}
.dialog-actions{display:flex;justify-content:flex-end;padding:0 17px 16px}
.dialog-actions button{border:1px solid #4fb5bc;background:var(--teal2);color:white;border-radius:2px;padding:7px 20px}
.hint{font-size:11px;color:#819399;line-height:1.5}
.toast{position:fixed;right:16px;bottom:42px;z-index:80;background:#1b292e;border:1px solid #3b4b51;color:#e8f3f4;padding:9px 12px;border-radius:3px;box-shadow:0 8px 25px rgba(0,0,0,.4);font-size:12px;opacity:0;pointer-events:none;transform:translateY(7px);transition:.18s}
.toast.show{opacity:1;transform:translateY(0)}

@media(max-width:900px){
  .workspace{grid-template-columns:44px 170px minmax(0,1fr)}
  .board{width:230px}
}
@media(max-width:700px){
  .app{grid-template-rows:28px 50px minmax(0,1fr) 25px}
  .menubar{gap:10px;font-size:12px;overflow:hidden}
  .board{width:auto;flex:1}
  .wave{display:none}
  .workspace{grid-template-columns:42px minmax(0,1fr)}
  .explorer{display:none}
  .mainarea{grid-column:2;grid-template-rows:120px 5px minmax(0,1fr)}
  .editorcode{font-size:12px}
  .sendrow{grid-template-columns:minmax(0,1fr) 62px}
}
</style>
</head>
<body>
<div class="app">
  <div class="menubar">
    <span>Archivo</span><span>Editar</span><span>Sketch</span><span>Herramientas</span><span>Ayuda</span>
  </div>

  <div class="toolbar">
    <button class="toolcircle" id="verifyBtn" title="Estado del servidor">✓</button>
    <button class="toolcircle" id="sendTop" title="Enviar mensaje">
      <svg viewBox="0 0 24 24"><path d="M4 12h15M13 5l7 7-7 7"/></svg>
    </button>
    <button class="toolcircle" id="reconnectBtn" title="Reconectar">
      <svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 8.2 5.3M20 3v6h-6"/></svg>
    </button>
    <div class="board">
      <span class="usb">⌁</span>
      <span class="boardname" id="boardName">CHAT.ide LAN</span>
      <span class="arrow">▼</span>
    </div>
    <div class="toolbar-spacer"></div>
    <div class="wave">〰 ◉</div>
  </div>

  <div class="workspace">
    <aside class="activity" aria-label="Barra lateral">
      <button class="active" title="Explorador"><svg viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v11H3z"/></svg></button>
      <button title="Conectados"><svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="3"/><path d="M2.5 20c0-3 2.4-5.5 5.5-5.5s5.5 2.5 5.5 5.5"/><circle cx="17" cy="9" r="2.4"/><path d="M16 14.5c3 0 5 2 5 4.5"/></svg></button>
      <button title="Biblioteca"><svg viewBox="0 0 24 24"><path d="M4 4v16M9 4v16M14 5v15M18 4l3 15"/></svg></button>
      <button title="Red"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M4 12a8 8 0 0 1 16 0M7 12a5 5 0 0 1 10 0"/></svg></button>
      <button title="Buscar"><svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></svg></button>
      <button class="bottom" title="Usuario"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/></svg></button>
    </aside>

    <aside class="explorer">
      <div class="explorer-title">Explorador</div>
      <div class="section-title">CHAT.IDE</div>
      <div class="file active">▾ CHAT_IDE.ino</div>
      <div class="room-chip">Sala: <strong id="roomSide">General</strong></div>
      <div class="section-title">Conectados (<span id="userCountSide">0</span>)</div>
      <div class="users" id="users"></div>
    </aside>

    <main class="mainarea">
      <section class="editorpane">
        <div class="tabs"><div class="tab">CHAT_IDE.ino</div></div>
        <div class="editorcode" id="editorCode">
          <div class="code-line"><span class="ln">1</span><span class="code"><span class="comment">// CHAT.ide LAN - comunicación local sin Internet</span></span></div>
          <div class="code-line"><span class="ln">2</span><span class="code"><span class="type">const char*</span> sala = <span class="str" id="codeRoom">"General"</span>;</span></div>
          <div class="code-line"><span class="ln">3</span><span class="code"><span class="type">const char*</span> usuario = <span class="str" id="codeUser">"sin_conectar"</span>;</span></div>
          <div class="code-line"><span class="ln">4</span><span class="code"><span class="type">bool</span> lanActiva = <span class="kw" id="codeLan">false</span>;</span></div>
          <div class="code-line"><span class="ln">5</span><span class="code"><span class="type">int</span> conectados = <span class="num" id="codeUsers">0</span>;</span></div>
          <div class="code-line"><span class="ln">6</span><span class="code"></span></div>
          <div class="code-line"><span class="ln">7</span><span class="code"><span class="type">void</span> <span class="fn">setup</span>() {</span></div>
          <div class="code-line"><span class="ln">8</span><span class="code">  <span class="fn">Serial.begin</span>(<span class="num">9600</span>);</span></div>
          <div class="code-line"><span class="ln">9</span><span class="code">  <span class="fn">CHAT.begin</span>(sala);</span></div>
          <div class="code-line"><span class="ln">10</span><span class="code">}</span></div>
          <div class="code-line"><span class="ln">11</span><span class="code"></span></div>
          <div class="code-line"><span class="ln">12</span><span class="code"><span class="type">void</span> <span class="fn">loop</span>() {</span></div>
          <div class="code-line"><span class="ln">13</span><span class="code">  <span class="comment">// Los mensajes aparecen en “Salida”, como un Monitor Serie.</span></span></div>
          <div class="code-line"><span class="ln">14</span><span class="code">  <span class="fn">CHAT.update</span>();</span></div>
          <div class="code-line"><span class="ln">15</span><span class="code">}</span></div>
        </div>
      </section>

      <div class="splitter"></div>

      <section class="output">
        <div class="output-head">
          <strong>Salida</strong>
          <div class="tools">
            <button id="clearBtn" title="Limpiar salida">≡×</button>
            <button id="stampBtn" title="Mostrar/ocultar hora">◷</button>
          </div>
        </div>
        <div class="sendrow">
          <input class="msginput" id="msg" maxlength="500" autocomplete="off" placeholder="Escribe un mensaje y pulsa Enter...">
          <button class="sendbtn" id="sendBtn">Enviar</button>
        </div>
        <div class="console" id="console" role="log" aria-live="polite"></div>
      </section>
    </main>
  </div>

  <footer class="statusbar">
    <span><span class="state-dot" id="stateDot"></span><span id="stateText">Sin conexión</span></span>
    <span id="roomStatus">Sala: -</span>
    <span id="userStatus">0 conectados</span>
    <span class="grow" id="hostStatus"></span>
    <span id="identityStatus">-</span>
  </footer>
</div>

<div class="overlay" id="joinOverlay">
  <form class="dialog" id="joinForm" autocomplete="off">
    <div class="dialog-title">Conectar a CHAT.ide LAN</div>
    <div class="dialog-body">
      <div>
        <label for="name">Tu nombre</label>
        <input id="name" maxlength="20" required placeholder="Ej. Chris">
      </div>
      <div>
        <label for="room">Sala</label>
        <select id="room"></select>
      </div>
      <div class="hint">No necesita Internet. Todos deben estar conectados a la misma red Wi-Fi/router que la PC que ejecuta Python.</div>
    </div>
    <div class="dialog-actions"><button type="submit">Entrar</button></div>
  </form>
</div>

<div class="toast" id="toast"></div>

<script>
(() => {
'use strict';
const ROOMS = __ROOMS_JSON__;
const $ = id => document.getElementById(id);
const consoleBox = $('console');
const msg = $('msg');
let token = '';
let myName = '';
let room = 'General';
let es = null;
let connected = false;
let showStamp = true;
let reconnectTimer = null;
let lastUsers = [];

const colors = ['#6bcbd0','#f2a34a','#8fd36b','#d990c4','#8ba7ff','#e8d35f','#ff8b7e','#b89bf0','#66c8a1'];
function colorFor(name){
  let h = 0;
  for(const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return colors[h % colors.length];
}
function escText(v){ return String(v ?? ''); }
function pad(n, l=2){ return String(n).padStart(l,'0'); }
function fmt(t){
  const d = new Date(t);
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '.' + pad(d.getMilliseconds(),3);
}
function toast(text){
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 2500);
}
function setState(ok, text){
  connected = !!ok;
  $('stateDot').classList.toggle('ok', connected);
  $('stateText').textContent = text;
  $('codeLan').textContent = connected ? 'true' : 'false';
  $('codeLan').className = connected ? 'kw' : 'str';
}
function updateIdentity(){
  $('roomSide').textContent = room;
  $('roomStatus').textContent = 'Sala: ' + room;
  $('identityStatus').textContent = myName || '-';
  $('boardName').textContent = (myName ? myName + ' | ' : '') + 'CHAT.ide LAN';
  $('codeRoom').textContent = '"' + room + '"';
  $('codeUser').textContent = '"' + (myName || 'sin_conectar') + '"';
  $('hostStatus').textContent = location.host ? 'Servidor: ' + location.host : '';
}
function updateUsers(names){
  lastUsers = names || [];
  const box = $('users');
  box.textContent = '';
  lastUsers.forEach(n => {
    const row = document.createElement('div');
    row.className = 'user';
    const dot = document.createElement('span'); dot.className = 'dot';
    const name = document.createElement('span'); name.textContent = n + (n === myName ? ' (tú)' : '');
    row.append(dot, name); box.appendChild(row);
  });
  $('userCountSide').textContent = lastUsers.length;
  $('userStatus').textContent = lastUsers.length + (lastUsers.length === 1 ? ' conectado' : ' conectados');
  $('codeUsers').textContent = lastUsers.length;
}
function addLine(ev){
  const row = document.createElement('div');
  row.className = 'console-line';
  if(ev.type === 'sys') row.classList.add('sys');
  if(ev.type === 'msg' && ev.name === myName) row.classList.add('me');

  if(showStamp){
    const ts = document.createElement('span'); ts.className='ts'; ts.textContent = '[' + fmt(ev.t) + '] ';
    row.appendChild(ts);
  }

  if(ev.type === 'sys'){
    const text = document.createElement('span'); text.textContent = escText(ev.text); row.appendChild(text);
  }else{
    const name = document.createElement('span');
    name.className = 'name'; name.style.color = colorFor(ev.name); name.textContent = ev.name + ': ';
    const text = document.createElement('span'); text.textContent = escText(ev.text);
    row.append(name, text);
  }
  consoleBox.appendChild(row);
  while(consoleBox.childElementCount > 600) consoleBox.firstChild.remove();
  consoleBox.scrollTop = consoleBox.scrollHeight;
}
function renderHistory(items){
  consoleBox.textContent = '';
  (items || []).forEach(addLine);
}
function handle(ev){
  if(!ev || typeof ev !== 'object') return;
  if(ev.type === 'hello'){
    myName = ev.name || myName;
    room = ev.room || room;
    updateIdentity();
  } else if(ev.type === 'users'){
    updateUsers(ev.names || []);
  } else if(ev.type === 'msg' || ev.type === 'sys'){
    addLine(ev);
  }
}
async function api(path, body){
  const r = await fetch(path, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})
  });
  let data = null;
  try{ data = await r.json(); }catch{}
  if(!r.ok){ throw new Error(data?.error || ('HTTP ' + r.status)); }
  return data;
}
function openEvents(){
  if(es) es.close();
  if(!token) return;
  setState(false, 'Conectando...');
  es = new EventSource('/events?token=' + encodeURIComponent(token));
  es.onopen = () => {
    setState(true, 'LAN activa');
    clearTimeout(reconnectTimer);
  };
  es.onmessage = e => {
    try{ handle(JSON.parse(e.data)); }catch{}
  };
  es.onerror = () => {
    setState(false, 'Reconectando...');
    // EventSource ya reintenta automaticamente. Este temporizador solo muestra
    // aviso si tarda demasiado.
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => toast('La red local tarda en responder.'), 6000);
  };
}
async function join(){
  const name = $('name').value.trim();
  const chosen = $('room').value;
  if(!name) return;
  const data = await api('/join', {name, room:chosen});
  token = data.token;
  myName = data.name;
  room = data.room;
  try{
    localStorage.setItem('chatide_name', name);
    localStorage.setItem('chatide_room', room);
  }catch{}
  renderHistory(data.history || []);
  updateIdentity();
  $('joinOverlay').hidden = true;
  openEvents();
  msg.focus();
}
async function send(){
  const text = msg.value.trim();
  if(!text || !token) return;
  try{
    await api('/send', {token, text});
    msg.value = '';
  }catch(err){
    toast(err.message || 'No se pudo enviar');
    if(String(err.message).includes('Sesion')){
      setState(false,'Sesión vencida');
      $('joinOverlay').hidden = false;
    }
  }
  msg.focus();
}
async function health(){
  try{
    const r = await fetch('/health', {cache:'no-store'});
    if(!r.ok) throw new Error();
    const d = await r.json();
    if(!token) setState(true, 'Servidor local listo');
    $('verifyBtn').textContent = '✓';
    $('verifyBtn').title = 'Servidor activo · ' + d.connected + ' conectados';
  }catch{
    if(!token) setState(false, 'Servidor no disponible');
    $('verifyBtn').textContent = '!';
  }
}

ROOMS.forEach(r => {
  const o = document.createElement('option'); o.value = r; o.textContent = r; $('room').appendChild(o);
});
try{
  $('name').value = localStorage.getItem('chatide_name') || '';
  const savedRoom = localStorage.getItem('chatide_room');
  if(savedRoom && ROOMS.includes(savedRoom)) $('room').value = savedRoom;
}catch{}

$('joinForm').addEventListener('submit', async e => {
  e.preventDefault();
  try{ await join(); }catch(err){ toast(err.message || 'No se pudo entrar'); }
});
$('sendBtn').addEventListener('click', send);
$('sendTop').addEventListener('click', send);
$('reconnectBtn').addEventListener('click', () => { if(token){ openEvents(); toast('Reconectando...'); } });
$('verifyBtn').addEventListener('click', health);
$('clearBtn').addEventListener('click', () => { consoleBox.textContent=''; toast('Salida limpiada solo en este dispositivo'); });
$('stampBtn').addEventListener('click', () => { showStamp = !showStamp; toast(showStamp ? 'Hora visible' : 'Hora oculta'); });
msg.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); }
});
window.addEventListener('pagehide', () => {
  if(!token) return;
  try{
    navigator.sendBeacon('/bye', new Blob([JSON.stringify({token})], {type:'application/json'}));
  }catch{}
});

updateIdentity();
health();
setInterval(health, 15000);
$('name').focus();
})();
</script>
</body>
</html>
'''

PAGE = PAGE_TEMPLATE.replace("__ROOMS_JSON__", json.dumps(ROOMS, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ARRANQUE
# ---------------------------------------------------------------------------
def main() -> None:
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True, name="chatide-cleanup").start()

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        server.daemon_threads = True
        server.allow_reuse_address = True
    except OSError as e:
        print(f"\nNo se pudo abrir el puerto {PORT}: {e}")
        print(f"Prueba con otro puerto: python {os.path.basename(__file__)} 8080")
        return

    print("\n" + "=" * 62)
    print(" CHAT.ide LAN v2 - servidor local")
    print("=" * 62)
    print(f"Base de datos: {DB_PATH}")
    print(f"Puerto: {PORT}")

    ips = local_ips()
    if ips:
        print("\nDirecciones para los demas dispositivos de la misma red:")
        for i, ip in enumerate(ips):
            marker = "  RECOMENDADA ->" if i == 0 else "              "
            print(f"{marker} http://{ip}:{PORT}")
    else:
        print("\nNo se detecto una IPv4 privada util.")
        print("Conecta la PC a un router/Wi-Fi y revisa el firewall de Windows.")

    print(f"\nEn esta PC: http://localhost:{PORT}")
    print("Ctrl+C para cerrar.\n")

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nCerrando CHAT.ide...")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
