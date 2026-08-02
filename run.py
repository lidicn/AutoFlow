#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 零摩擦本地启动器（无需 pip install）。

用法：
  python run.py mcp                       # 启动 MCP 服务（Streamable HTTP :8000/mcp）
  python run.py mcp --transport sse       # SSE :8000/sse
  python run.py cli <子命令> ...          # 脚本/JSON 接口（同 python -m autoflow_gateway.cli）
  python run.py --help

前提：依赖已装（pip install -e . 或 pip install mcp pydantic uvicorn starlette python-dotenv）。
"""
import os
import sys

# ── HA 默认地址（本机 HA 在 localhost:8123）─────────────────────────────
# 仅在环境变量未显式设置时才用默认，避免覆盖容器/compose 里配置的 HASS_SERVER
# （如容器内需指向宿主 HA，应在 compose 或 WebUI 里设 http://host.docker.internal:8123）。
os.environ.setdefault("HASS_SERVER", "http://localhost:8123")

# 让包可导入（无需安装）
ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("mcp", "serve", "webui"):
        from autoflow_gateway.mcp_server import main as mcp_main
        if sys.argv[1] == "mcp":
            # 仅 MCP（强制身份，但不同WebUI）
            sys.argv = ["mcp_server", "--no-webui"] + sys.argv[2:]
        else:
            # serve / webui：MCP + WebUI 同端口
            sys.argv = ["mcp_server"] + sys.argv[2:]
        mcp_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "cli":
        from autoflow_gateway.cli import main as cli_main
        sys.argv = sys.argv[1:]
        cli_main()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
