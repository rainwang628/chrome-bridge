#!/usr/bin/env python3
"""Chrome 桥接服务。

本地 HTTP API（127.0.0.1:8777）⇄ Chrome 扩展的 WebSocket。
只用标准库，不依赖 node / pip 包。

接口：
  GET  /status          扩展是否已连接
  GET  /ws?token=...    扩展建立 WebSocket（浏览器侧）
  POST /cmd             JSON {"method":..., "params":..., "timeout":20}
                        需带请求头 X-Bridge-Token
"""
import base64
import hashlib
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BRIDGE_PORT", "8777"))
BIND = "127.0.0.1"
TOKEN_FILE = os.path.join(HERE, "token")
EXT_CONFIG = os.path.join(HERE, "extension", "config.js")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_token():
    if os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if tok:
            return tok
    tok = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def write_extension_config(tok):
    """从本地 token 生成扩展配置；该文件含真实凭据，不入库。"""
    body = (
        "// 由 server.py 从本地 token 生成，请勿手改；换 token 删 token 文件重启即可。\n"
        f"export default {{ port: {PORT}, token: {json.dumps(tok)} }};\n"
    )
    with open(EXT_CONFIG, "w", encoding="utf-8") as f:
        f.write(body)


TOKEN = load_token()
write_extension_config(TOKEN)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- #
# WebSocket 帧
# --------------------------------------------------------------------------- #
def encode_frame(payload, opcode=0x1):
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 1 << 16:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def read_frame(rfile):
    head = rfile.read(2)
    if len(head) < 2:
        raise EOFError("socket closed")
    b1, b2 = head[0], head[1]
    fin = b1 & 0x80
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else None
    data = rfile.read(n) if n else b""
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return fin, opcode, data


class ExtensionConn:
    """一条从扩展来的 WebSocket 连接。"""

    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.wlock = threading.Lock()
        self.pending = {}
        self.plock = threading.Lock()
        self.alive = True
        self.since = time.time()
        self.version = None

    def send_json(self, obj):
        self.send_raw(json.dumps(obj, ensure_ascii=False).encode())

    def send_raw(self, payload, opcode=0x1):
        with self.wlock:
            self.wfile.write(encode_frame(payload, opcode))
            self.wfile.flush()

    def request(self, method, params=None, timeout=20.0):
        rid = uuid.uuid4().hex
        q = queue.Queue(maxsize=1)
        with self.plock:
            self.pending[rid] = q
        try:
            self.send_json({"id": rid, "method": method, "params": params or {}})
            try:
                reply = q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"扩展 {timeout:.0f}s 内没有返回")
        finally:
            with self.plock:
                self.pending.pop(rid, None)
        if not reply.get("ok"):
            err = reply.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(msg or "扩展返回失败")
        return reply.get("result")

    def run(self):
        buf = bytearray()
        buf_op = None
        try:
            while True:
                fin, opcode, data = read_frame(self.rfile)
                if opcode == 0x8:  # close
                    try:
                        self.send_raw(b"", 0x8)
                    except OSError:
                        pass
                    break
                if opcode == 0x9:  # ping
                    self.send_raw(data, 0xA)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    buf = bytearray(data)
                    buf_op = opcode
                elif opcode == 0x0:
                    buf += data
                else:
                    continue
                if not fin:
                    continue
                payload, buf = bytes(buf), bytearray()
                if buf_op == 0x1:
                    self.on_text(payload.decode("utf-8", "replace"))
        except (EOFError, OSError, ValueError) as e:
            log(f"扩展连接断开：{e}")
        finally:
            self.alive = False
            with self.plock:
                for q in self.pending.values():
                    q.put({"ok": False, "error": {"message": "扩展连接已断开"}})
            EXT.detach(self)

    def on_text(self, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        rid = msg.get("id")
        if rid:
            with self.plock:
                q = self.pending.get(rid)
            if q:
                q.put(msg)
            return
        ev = msg.get("event")
        if ev == "hello":
            self.version = msg.get("version")
            log(f"扩展已连接 v{self.version}")
        elif ev == "ping":
            self.send_json({"event": "pong", "t": time.time()})


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None

    def attach(self, conn):
        with self.lock:
            old = self.conn
            self.conn = conn
        if old and old.alive:
            try:
                old.send_json({"event": "superseded"})
            except OSError:
                pass

    def detach(self, conn):
        with self.lock:
            if self.conn is conn:
                self.conn = None

    def current(self):
        with self.lock:
            c = self.conn
        return c if (c and c.alive) else None


EXT = Registry()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ChromeBridge/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 工具 ----
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self):
        return self.headers.get("X-Bridge-Token", "") == TOKEN

    # ---- GET ----
    def do_GET(self):
        if self.path.startswith("/ws"):
            return self._ws()
        if self.path == "/status":
            c = EXT.current()
            return self._json(
                200,
                {
                    "connected": bool(c),
                    "version": c.version if c else None,
                    "since": c.since if c else None,
                    "port": PORT,
                },
            )
        self._json(404, {"ok": False, "error": {"message": "not found"}})

    def _ws(self):
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] != TOKEN:
            return self._json(401, {"ok": False, "error": {"message": "bad token"}})
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json(400, {"ok": False, "error": {"message": "no key"}})
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.wfile.flush()
        conn = ExtensionConn(self.rfile, self.wfile)
        EXT.attach(conn)
        log("扩展握手成功")
        conn.run()

    # ---- POST ----
    def do_POST(self):
        if self.path != "/cmd":
            return self._json(404, {"ok": False, "error": {"message": "not found"}})
        if not self._token_ok():
            return self._json(401, {"ok": False, "error": {"message": "缺少或错误的 X-Bridge-Token"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": {"message": "请求体不是合法 JSON"}})
        conn = EXT.current()
        if not conn:
            return self._json(
                409,
                {"ok": False, "error": {"message": "扩展未连接：请在 chrome://extensions 里启用 DSH Chrome Bridge"}},
            )
        method = req.get("method")
        if not method:
            return self._json(400, {"ok": False, "error": {"message": "缺少 method"}})
        timeout = float(req.get("timeout") or 20)
        t0 = time.time()
        try:
            result = conn.request(method, req.get("params"), timeout=timeout)
            log(f"{method} ok {int((time.time()-t0)*1000)}ms")
            self._json(200, {"ok": True, "result": result})
        except TimeoutError as e:
            log(f"{method} timeout")
            self._json(504, {"ok": False, "error": {"message": str(e)}})
        except RuntimeError as e:
            log(f"{method} fail: {e}")
            self._json(200, {"ok": False, "error": {"message": str(e)}})
        except OSError as e:
            self._json(502, {"ok": False, "error": {"message": f"写入扩展连接失败：{e}"}})


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log(f"桥接服务已启动 http://{BIND}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
