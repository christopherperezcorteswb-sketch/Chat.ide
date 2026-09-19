#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chat de clase con aspecto de Arduino IDE (el chat es el "Monitor Serie").

Funciona SIN internet: solo hace falta que todos esten en la misma red local
(un router encendido aunque no tenga internet, o el hotspot de un celular).
No hay que instalar nada, solo Python 3.

Uso:
    python chat_arduino.py          (puerto 8000)
    python chat_arduino.py 8080     (otro puerto)

Los estudiantes abren en el navegador la direccion que se imprime al iniciar.
"""
import json
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HISTORY_MAX = 300   # mensajes que se guardan para quien entra tarde
MSG_MAX = 500       # largo maximo de un mensaje
NAME_MAX = 20       # largo maximo de un nombre

lock = threading.RLock()
clients = {}        # cid -> {"name": str, "q": Queue}
history = []


# ---------------------------------------------------------------- estado ----
def now_ms():
    return int(time.time() * 1000)


def sys_event(text):
    return {"type": "sys", "t": now_ms(), "text": text}


def broadcast(event):
    """Enviar a todos los conectados. Llamar con `lock` tomado."""
    for c in clients.values():
        c["q"].put(event)


def post(event):
    """Guardar en el historial y enviar a todos. Llamar con `lock` tomado."""
    history.append(event)
    del history[:-HISTORY_MAX]
    broadcast(event)


def users_event():
    return {"type": "users", "names": [c["name"] for c in clients.values()]}


def clean_name(raw):
    name = "".join(ch for ch in raw if ch.isprintable()).strip()
    return name[:NAME_MAX] or "Anonimo"


def unique_name(name, cid):
    taken = {c["name"].lower() for k, c in clients.items() if k != cid}
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{name} ({n})"
    return candidate


def leave(cid, q=None):
    """Quita al cliente y avisa. Si se indica q, solo lo quita si esa sigue
    siendo su conexion activa (evita avisos falsos al reconectar)."""
    with lock:
        c = clients.get(cid)
        if not c or (q is not None and c["q"] is not q):
            return
        del clients[cid]
        c["q"].put(None)
        post(sys_event(f"{c['name']} salio de la sala"))
        broadcast(users_event())


# ------------------------------------------------------------------ HTTP ----
class Handler(BaseHTTPRequestHandler):
    server_version = "ChatClase"

    def log_message(self, *args):
        pass  # sin ruido en la consola

    def reply(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 4096:
                return None
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else None
        except (ValueError, OSError):
            return None

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/events":
            self.stream(parse_qs(url.query))
        elif url.path == "/favicon.ico":
            self.reply(204)
        else:
            self.send_error(404)

    def do_POST(self):
        data = self.read_json()
        if data is None:
            return self.reply(400)
        cid = str(data.get("cid", ""))[:40]

        if self.path == "/send":
            text = str(data.get("text", "")).replace("\r", " ").replace("\n", " ").strip()[:MSG_MAX]
            with lock:
                c = clients.get(cid)
                if not c:
                    return self.reply(403)
                if text:
                    post({"type": "msg", "t": now_ms(), "name": c["name"], "text": text})
            return self.reply(204)

        if self.path == "/bye":
            leave(cid)
            return self.reply(204)

        self.send_error(404)

    def stream(self, qs):
        cid = qs.get("cid", [""])[0][:40]
        if not cid:
            return self.send_error(400)
        wanted = clean_name(qs.get("name", [""])[0])
        q = queue.Queue()

        with lock:
            old = clients.get(cid)
            if old:                      # misma pestana que se reconecta
                name = old["name"]
                old["q"].put(None)       # cierra la conexion anterior
                clients[cid] = {"name": name, "q": q}
                joined = False
            else:
                name = unique_name(wanted, cid)
                clients[cid] = {"name": name, "q": q}
                joined = True
            q.put({"type": "hello", "name": name})
            q.put({"type": "history", "items": list(history)})
            if joined:
                post(sys_event(f"{name} entro a la sala"))
            broadcast(users_event())

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=5)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # detecta conexiones caidas
                    self.wfile.flush()
                    continue
                if ev is None:
                    break
                payload = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            leave(cid, q)


# ------------------------------------------------------------- arranque ----
def es_privada(ip):
    """True si la IP esta en un rango privado (RFC 1918)."""
    partes = ip.split(".")
    if len(partes) != 4:
        return False
    try:
        a, b = int(partes[0]), int(partes[1])
    except ValueError:
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def es_inservible(ip):
    """Direcciones por las que nunca va a entrar otro equipo.

    127.x.x.x es esta misma maquina. 169.254.x.x es la que el sistema se
    autoasigna cuando no encuentra DHCP: que aparezca significa
    justamente que esa placa NO esta en una red util.
    """
    return ip.startswith("127.") or ip.startswith("169.254.")


def ip_de_salida():
    """La IP de la interfaz por la que este equipo sale a la red.

    Es la unica senal fiable que hay sin usar APIs propias de cada
    sistema operativo: connect() sobre UDP no envia ni un byte, solo
    hace que el sistema elija la interfaz de salida segun su tabla de
    rutas. Leyendo el socket se sabe cual eligio, y esa es la direccion
    que un celular en la misma red puede alcanzar.

    Devuelve None si no hay ninguna red util.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    return None if es_inservible(ip) else ip


def otras_ips(principal=None):
    """Las demas IPv4 del equipo. Devuelve [(ip, es_sospechosa)].

    `es_sospechosa` marca las que casi seguro pertenecen a un adaptador
    virtual: WSL, Hyper-V, Docker, VirtualBox. Esas placas suelen tomar
    el .1 de su propia subred privada, porque hacen de puerta de enlace
    de una red que solo existe dentro de esta maquina. Un alumno que
    copie una de esas no se conecta nunca.

    Es una heuristica, no una certeza, asi que se muestran igual pero
    avisando. Ocultarlas del todo seria peor: en un equipo con dos
    placas de red buenas, la segunda podria ser la que sirve.
    """
    encontradas = set()
    try:
        encontradas.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass

    resultado = []
    for ip in sorted(encontradas):
        if es_inservible(ip) or ip == principal:
            continue
        resultado.append((ip, es_privada(ip) and ip.endswith(".1")))
    return resultado


def local_ips():
    """La lista plana de siempre, pero con la buena primero.

    Se conserva para no romper a quien ya la use. Lo nuevo deberia
    llamar a ip_de_salida() y otras_ips(), que distinguen cual sirve.
    """
    principal = ip_de_salida()
    resto = [ip for ip, _ in otras_ips(principal)]
    return [principal] + resto if principal else resto


def main():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"No se pudo abrir el puerto {PORT}: {e}")
        print("Prueba con otro:  python chat_arduino.py 8080")
        return
    print("Chat de clase en marcha. Ctrl+C para cerrar.\n")

    principal = ip_de_salida()
    otras = otras_ips(principal)

    if principal:
        print("Que los estudiantes abran en su navegador:\n")
        print(f"    http://{principal}:{PORT}\n")
    else:
        print("No veo ninguna red util.")
        print("Conecta este equipo al Wi-Fi o al router de la clase.\n")

    if otras:
        # Se muestran, pero separadas y avisando: antes iban mezcladas
        # con la buena y no habia forma de saber cual copiar.
        print("Otras direcciones de este equipo. Casi seguro NO sirven:")
        for ip, sospechosa in otras:
            nota = "   <- adaptador virtual (WSL/Hyper-V/Docker)" if sospechosa else ""
            print(f"    http://{ip}:{PORT}{nota}")
        print()

    print(f"(En este mismo equipo: http://localhost:{PORT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChat cerrado.")
    finally:
        server.server_close()


# ------------------------------------------------------------ interfaz ----
PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chat_clase | Arduino IDE</title>
<style>
:root{
  --toolbar:#00595d; --btn:#00979d; --status:#00777b;
  --editor:#1e2427; --panel:#242b2f; --side:#1a2023; --line:#37424a;
  --text:#d8e1e1; --dim:#7f9092; --teal:#00b5bc; --orange:#e4842a;
  --ok:#7bd88f; --warn:#f0b04a;
  --ui:"Open Sans","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
  --mono:"Roboto Mono","Cascadia Mono",Consolas,Menlo,"DejaVu Sans Mono",monospace;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:grid;grid-template-rows:auto minmax(0,1fr) auto;background:var(--editor);color:var(--text);font:13px/1.45 var(--ui);color-scheme:dark}
:focus-visible{outline:2px solid var(--teal);outline-offset:1px}

.toolbar{display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--toolbar)}
.round{flex:none;width:34px;height:34px;border-radius:50%;border:0;background:var(--btn);color:#fff;display:grid;place-items:center;cursor:pointer}
.round:hover{background:#00b0b7}
.round svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.board{display:flex;align-items:center;gap:8px;min-width:0;padding:6px 12px;background:rgba(0,0,0,.28);border-radius:3px;color:#dff3f3}
.board span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip{flex:none;width:9px;height:9px;border-radius:50%;background:var(--warn)}
.chip.ok{background:var(--ok)}

.main{display:grid;grid-template-columns:48px 190px minmax(0,1fr);min-height:0}
.bar{background:var(--side);border-right:1px solid var(--line);padding-top:6px}
.bar span{display:grid;place-items:center;width:48px;height:44px;color:var(--dim);opacity:.45}
.bar span.on{opacity:1;color:#fff;box-shadow:inset 2px 0 var(--teal)}
.bar svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.side{background:var(--side);border-right:1px solid var(--line);padding:12px 0;overflow:auto;min-height:0}
.side h2{margin:0 14px 8px;font-size:12px;font-weight:600;color:var(--dim)}
.side ul{margin:0;padding:0;list-style:none}
.side li{display:flex;align-items:center;gap:8px;padding:4px 14px;min-width:0}
.side li span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{flex:none;width:8px;height:8px;border-radius:50%}

.work{display:grid;grid-template-rows:auto minmax(0,1fr) minmax(0,2.2fr);min-width:0;min-height:0}
.tabs{display:flex;background:var(--side);border-bottom:1px solid var(--line)}
.tab{padding:7px 16px;background:var(--editor);border-top:2px solid var(--teal);color:#fff}
.editor{overflow:auto;padding:10px 0;background:var(--editor);font:13px/1.55 var(--mono)}
.ln{display:flex;white-space:pre}
.ln b{flex:none;width:44px;padding-right:14px;text-align:right;font-weight:400;color:#5b6b6e;user-select:none}
.k{color:var(--orange)} .f{color:var(--teal)} .c{color:var(--dim)}

.monitor{display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;min-height:0;background:var(--panel);border-top:1px solid var(--line)}
.mtabs{display:flex;background:var(--side);border-bottom:1px solid var(--line)}
.mtab{padding:6px 16px;color:#fff;border-bottom:2px solid var(--teal)}
.msgbox{padding:8px 10px}
.msgbox input{width:100%;padding:7px 10px;background:var(--editor);border:1px solid var(--line);border-radius:3px;color:var(--text);font:13px var(--mono)}
.msgbox input:focus{outline:none;border-color:var(--teal);box-shadow:0 0 0 1px var(--teal)}
.out{overflow:auto;padding:2px 12px 8px;font:13px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.line{padding-left:8px;border-left:2px solid transparent}
.line.me{border-left-color:var(--orange)}
.line.sys{color:var(--dim)}
.ts{color:#6a7e81}
.nm{font-weight:600}
.nots .ts{display:none}
.mfoot{display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:flex-end;align-items:center;padding:6px 12px;border-top:1px solid var(--line);color:var(--dim);font-size:12px}
.mfoot label{display:flex;align-items:center;gap:6px;cursor:pointer;accent-color:var(--teal)}
.mfoot button{background:none;border:0;padding:0;color:var(--dim);font:inherit;cursor:pointer}
.mfoot button:hover{color:#fff}

.status{display:flex;justify-content:space-between;gap:12px;padding:3px 12px;background:var(--status);color:#e8fafa;font-size:12px}

.overlay{position:fixed;inset:0;z-index:10;display:grid;place-items:center;padding:16px;background:rgba(10,14,16,.72)}
.overlay[hidden]{display:none}
.dialog{width:min(380px,100%);background:#2b3338;border:1px solid var(--line);border-radius:4px;box-shadow:0 12px 40px rgba(0,0,0,.5)}
.dialog h1{margin:0;padding:14px 18px;font-size:15px;font-weight:600;border-bottom:1px solid var(--line)}
.dialog .body{display:grid;gap:8px;padding:16px 18px}
.dialog input{padding:8px 10px;background:var(--editor);border:1px solid var(--line);border-radius:3px;color:var(--text);font:14px var(--ui)}
.dialog input:focus{outline:none;border-color:var(--teal);box-shadow:0 0 0 1px var(--teal)}
.dialog .actions{display:flex;justify-content:flex-end;padding:0 18px 16px}
.dialog button{padding:7px 24px;border:0;border-radius:3px;background:var(--btn);color:#fff;font:600 13px var(--ui);cursor:pointer}
.dialog button:hover{background:#00b0b7}

@media (max-width:760px){
  .main{grid-template-columns:minmax(0,1fr)}
  .bar,.side,.tabs,.editor{display:none}
  .work{grid-template-rows:minmax(0,1fr)}
}
</style>
</head>
<body>

<div class="toolbar">
  <button class="round" id="sendBtn" title="Enviar mensaje" aria-label="Enviar mensaje">
    <svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
  </button>
  <div class="board"><span class="chip" id="chip"></span><span id="boardLabel">Sin conexión</span></div>
</div>

<div class="main">
  <nav class="bar" aria-hidden="true">
    <span class="on"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.4"/><path d="M17 14c2.6 0 4.5 2 4.5 5"/></svg></span>
    <span><svg viewBox="0 0 24 24"><path d="M3 6h6l2 2h10v11H3z"/></svg></span>
    <span><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg></span>
    <span><svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 6 6"/></svg></span>
  </nav>

  <aside class="side">
    <h2>Conectados (<span id="count">0</span>)</h2>
    <ul id="users"></ul>
  </aside>

  <section class="work">
    <div class="tabs"><div class="tab">chat_clase.ino</div></div>
    <div class="editor">
      <div class="ln"><b>1</b><span class="c">// chat_clase.ino</span></div>
      <div class="ln"><b>2</b><span class="c">// Escribe en el Monitor Serie y pulsa Enter.</span></div>
      <div class="ln"><b>3</b></div>
      <div class="ln"><b>4</b><span><span class="k">void</span> <span class="k">setup</span>() {</span></div>
      <div class="ln"><b>5</b><span>  <span class="f">Serial.begin</span>(<span class="f">9600</span>);</span></div>
      <div class="ln"><b>6</b><span>}</span></div>
      <div class="ln"><b>7</b></div>
      <div class="ln"><b>8</b><span><span class="k">void</span> <span class="k">loop</span>() {</span></div>
      <div class="ln"><b>9</b><span class="c">  // conectados: <span id="codeCount">0</span></span></div>
      <div class="ln"><b>10</b><span>}</span></div>
    </div>

    <section class="monitor">
      <div class="mtabs"><div class="mtab">Monitor Serie</div></div>
      <div class="msgbox">
        <input id="msg" type="text" maxlength="500" autocomplete="off" placeholder="Mensaje (Enter para enviar a la sala)" aria-label="Mensaje">
      </div>
      <div class="out" id="out" role="log" aria-live="polite"></div>
      <div class="mfoot">
        <label><input type="checkbox" id="auto" checked> Autoscroll</label>
        <label><input type="checkbox" id="stamp" checked> Mostrar marca de tiempo</label>
        <button type="button" id="clear">Borrar salida</button>
      </div>
    </section>
  </section>
</div>

<footer class="status"><span id="stat">Sin conexión</span><span id="statUsers">0 conectados</span></footer>

<div class="overlay" id="join">
  <form class="dialog" id="joinForm" autocomplete="off">
    <h1>Conectar a la sala</h1>
    <div class="body">
      <label for="name">Tu nombre</label>
      <input id="name" type="text" maxlength="20" required>
    </div>
    <div class="actions"><button type="submit">Entrar</button></div>
  </form>
</div>

<script>
(function(){
'use strict';
const $ = id => document.getElementById(id);
const out = $('out'), msg = $('msg'), auto = $('auto'), stamp = $('stamp');
const COLORS = ['#6cc6cb','#f0a24b','#a7d066','#d98fbf','#8fa8ff','#e6d36a','#ff8f80','#b79cf2'];
const cid = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let myName = '', es = null, connected = false, flashTimer = null;

function colorFor(name){
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

function fmt(t){
  const d = new Date(t), p = (n, l = 2) => String(n).padStart(l, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) + '.' + p(d.getMilliseconds(), 3);
}

function baseStatus(){
  return connected ? 'Conectado a ' + location.host : 'Sin conexión. Reintentando...';
}
function paintBoard(){
  $('chip').classList.toggle('ok', connected);
  $('boardLabel').textContent = connected && myName ? myName + ' en ' + location.host : 'Sin conexión';
  $('stat').textContent = baseStatus();
}
function flashStatus(text){
  $('stat').textContent = text;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { $('stat').textContent = baseStatus(); }, 3500);
}

function addLine(ev){
  const row = document.createElement('div');
  row.className = 'line';
  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = fmt(ev.t) + ' -> ';
  row.appendChild(ts);
  if (ev.type === 'sys'){
    row.classList.add('sys');
    const s = document.createElement('span');
    s.textContent = ev.text;
    row.appendChild(s);
  } else {
    if (ev.name === myName) row.classList.add('me');
    const n = document.createElement('span');
    n.className = 'nm';
    n.style.color = colorFor(ev.name);
    n.textContent = ev.name + ': ';
    const s = document.createElement('span');
    s.textContent = ev.text;
    row.append(n, s);
  }
  out.appendChild(row);
  while (out.childElementCount > 500) out.firstChild.remove();
  if (auto.checked) out.scrollTop = out.scrollHeight;
}

function renderUsers(names){
  const ul = $('users');
  ul.textContent = '';
  names.forEach(n => {
    const li = document.createElement('li');
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.style.background = colorFor(n);
    const t = document.createElement('span');
    t.textContent = n + (n === myName ? ' (tú)' : '');
    li.append(dot, t);
    ul.appendChild(li);
  });
  $('count').textContent = names.length;
  $('codeCount').textContent = names.length;
  $('statUsers').textContent = names.length + (names.length === 1 ? ' conectado' : ' conectados');
}

function handle(ev){
  switch (ev.type){
    case 'hello':   myName = ev.name; paintBoard(); break;
    case 'history': out.textContent = ''; ev.items.forEach(addLine); break;
    case 'msg':
    case 'sys':     addLine(ev); break;
    case 'users':   renderUsers(ev.names); break;
  }
}

function connect(name){
  if (es) es.close();
  es = new EventSource('/events?cid=' + encodeURIComponent(cid) + '&name=' + encodeURIComponent(name));
  es.onopen = () => { connected = true; paintBoard(); };
  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handle(ev);
  };
  es.onerror = () => {
    connected = false;
    paintBoard();
    if (es.readyState === EventSource.CLOSED) setTimeout(() => connect(myName || name), 3000);
  };
}

async function send(){
  const text = msg.value.trim();
  if (!text || !myName) return;
  try {
    const r = await fetch('/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cid, text})
    });
    if (!r.ok) throw new Error(r.status);
    msg.value = '';
  } catch {
    flashStatus('No se pudo enviar el mensaje. Revisa la conexión.');
  }
  msg.focus();
}

msg.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing){ e.preventDefault(); send(); }
});
$('sendBtn').addEventListener('click', send);
stamp.addEventListener('change', () => out.classList.toggle('nots', !stamp.checked));
$('clear').addEventListener('click', () => { out.textContent = ''; });

$('joinForm').addEventListener('submit', e => {
  e.preventDefault();
  const name = $('name').value.trim();
  if (!name){ $('name').focus(); return; }
  try { localStorage.setItem('chat_name', name); } catch {}
  $('join').hidden = true;
  connect(name);
  msg.focus();
});

window.addEventListener('pagehide', () => {
  if (!myName) return;
  try { navigator.sendBeacon('/bye', new Blob([JSON.stringify({cid})], {type: 'application/json'})); } catch {}
});
window.addEventListener('pageshow', e => { if (e.persisted && myName) connect(myName); });

try { $('name').value = localStorage.getItem('chat_name') || ''; } catch {}
$('name').focus();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
