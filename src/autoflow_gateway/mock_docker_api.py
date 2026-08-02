#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_docker_api — AutoFlow staging 的「非实体类能力」模拟服务

为什么需要：除了 HA 设备，staging 里还会有 Docker 提供的 HTTP API（或业务 API）。
真实调用有副作用/需鉴权，不适合 agent 练手。这里用纯标准库实现：
- 从注册表（catalog 中 type=api 的能力，或本服务的 registry.json）暴露 HTTP 端点；
- 每个端点返回 canned 数据，零副作用；
- 提供 GET /api/registry 供 agent 发现可用 API。

注册表示例（mock_api_registry.json）：
{
  "endpoints": [
    {"name": "docker_ps", "method": "GET", "path": "/docker/ps",
     "response": [{"id":"abc","image":"nginx","status":"running"}]},
    {"name": "my_biz_status", "method": "GET", "path": "/biz/status",
     "response": {"ok": true, "uptime": 1234}}
  ]
}

与网关集成：网关在 staging 把这类能力登记进 device_catalog 的 "api_capabilities"
字段（type=api），agent 经 autoflow_discover(domain='api') 发现，调用时走网关
confirm 闸 + 本服务的 mock 端点（仅 staging，prod 指向真实服务）。

运行：
    python -m autoflow_gateway.mock_docker_api --port 9100 --registry mock_api_registry.json
"""
import json
import os
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_PORT = int(os.environ.get("MOCKAPI_PORT", "9100"))
DEFAULT_REGISTRY = os.environ.get("MOCKAPI_REGISTRY", "mock_api_registry.json")


# ── 默认注册表（示例：Docker + 一个业务 API）──
DEFAULT_REGISTRY = {
    "endpoints": [
        {"name": "docker_ps", "method": "GET", "path": "/docker/ps",
         "response": [{"id": "a1b2", "image": "nginx:latest", "status": "running", "name": "web"}]},
        {"name": "docker_images", "method": "GET", "path": "/docker/images",
         "response": [{"id": "img1", "repo": "nginx", "tag": "latest"}]},
        {"name": "biz_status", "method": "GET", "path": "/biz/status",
         "response": {"ok": True, "uptime_seconds": 86400}},
    ]
}


def load_registry(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_REGISTRY


class Handler(BaseHTTPRequestHandler):
    registry = None

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/") or "/"
        if p in ("/", "/health"):
            return self._send(200, {"name": "mock_docker_api", "endpoints": len(self.registry.get("endpoints", []))})
        if p == "/api/registry":
            return self._send(200, self.registry)
        for ep in self.registry.get("endpoints", []):
            if ep["method"] == "GET" and ep["path"] == p:
                return self._send(200, ep.get("response", {}))
        return self._send(404, {"error": "not found", "path": p})

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        for ep in self.registry.get("endpoints", []):
            if ep["method"] == "POST" and ep["path"] == p:
                return self._send(200, ep.get("response", {"ok": True}))
        return self._send(404, {"error": "not found", "path": p})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mock_docker_api", description="AutoFlow staging 非实体能力模拟")
    ap.add_argument("--host", default=os.environ.get("MOCKAPI_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    args = ap.parse_args(argv)

    reg = load_registry(args.registry)
    Handler.registry = reg
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[mock_docker_api] 已启动：http://{args.host}:{args.port}  "
          f"({len(reg.get('endpoints', []))} 个 API)")
    for ep in reg.get("endpoints", []):
        print(f"    {ep['method']:4} {ep['path']:20} -> {ep['name']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock_docker_api] 停止。")


if __name__ == "__main__":
    main()
