"""零依赖 HTTP 服务：静态页面 + 演练 API（Python 标准库）。"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Engine, EngineError

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA = os.path.join(BASE, "data", "state.json")
engine = Engine(snapshot_path=DATA)

_CT = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AddrSplitDemo/1.0"

    def log_message(self, fmt, *args):  # 简洁日志
        print("[http]", fmt % args)

    # ------------------------------------------------------------------ 工具
    def _json(self, payload: dict, status: int = 200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise EngineError(400, "bad_json", "请求体不是合法 JSON")

    def _call(self, fn):
        try:
            result = fn()
            if isinstance(result, tuple):
                payload, status = result
            else:
                payload, status = result, result.get("status", 200)
            # 错误信封拍平：HTTP 4xx/5xx 直接返回 {error,message,extra}
            if status >= 400 and isinstance(payload.get("body"), dict) \
                    and "error" in payload["body"]:
                payload = payload["body"]
            self._json(payload, status)
        except EngineError as e:
            self._json({"error": e.code, "message": str(e), "extra": e.extra}, e.status)

    # ------------------------------------------------------------------ 路由
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            if path == "/api/state":
                return self._call(lambda: engine.snapshot())
            if path == "/api/health":
                return self._json({"status": "ok", "seq": engine.seq})
            return self._json({"error": "not_found", "message": path}, 404)
        return self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/orders": self._create,
            "/api/migrate": self._migrate,
            "/api/write": self._write,
            "/api/read": self._read,
            "/api/manual-resolve": self._manual,
            "/api/mode": self._mode,
            "/api/replica/catchup": lambda: engine.replica_catchup(),
            "/api/replica/format": self._replica_format,
            "/api/replica/online": self._replica_online,
            "/api/reset": lambda: (engine.reset(), engine.snapshot())[1],
        }
        if path not in routes:
            return self._json({"error": "not_found", "message": path}, 404)
        self._call(routes[path])

    def _create(self):
        b = self._body()
        return engine.create_order(b.get("full_address", ""), b.get("request_id"))

    def _migrate(self):
        b = self._body()
        return engine.migrate_order(b["order_id"], b.get("request_id"))

    def _write(self):
        b = self._body()
        return engine.write(
            client=b.get("client", "old"), order_id=b["order_id"],
            value=b.get("value", ""), field=b.get("field"),
            expected_version=int(b.get("expected_version", 0)),
            request_id=b.get("request_id") or f"req-{os.urandom(5).hex()}",
            simulate_lost=bool(b.get("simulate_lost", False)))

    def _read(self):
        b = self._body()
        if b.get("source", "primary") == "replica":
            return engine.read_replica(b["order_id"], b.get("client", "new"))
        return engine.read_primary(b["order_id"], b.get("client", "new"))

    def _manual(self):
        b = self._body()
        return engine.manual_resolve(b["order_id"], b["street"], b["house"],
                                     b.get("request_id"))

    def _mode(self):
        return engine.switch_mode(self._body().get("mode", "v2"))

    def _replica_format(self):
        return engine.replica_switch_format(self._body().get("format", "split"))

    def _replica_online(self):
        return engine.replica_set_online(bool(self._body().get("online", True)))

    # ------------------------------------------------------------------ 静态
    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        target = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not target.startswith(STATIC) or not os.path.isfile(target):
            self.send_error(404)
            return
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"地址拆分演练已启动: http://localhost:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
