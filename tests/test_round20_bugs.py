# -*- coding: utf-8 -*-
"""Round20 bug 回归（D32 modify_flow allow_prod schema 缺口）。

D32（iss_129c4354e4, Medium）：autoflow_modify_flow 的 MCP schema 未暴露 allow_prod
参数，导致 PROD 环境下无法直接通过 modify_flow 直改已部署 flow（只有 apply 内部能设
allow_prod）。修复：给 autoflow_modify_flow 加 allow_prod: bool = False 形参并透传
到 Gateway.modify_flow（与 apply/verify_flow 的 allow_prod 语义一致）。

此处固化：
1. 签名含 allow_prod 形参（静态断言，防 MCP 工具面回退）；
2. 调用时 allow_prod 透传到 Gateway.modify_flow（用 stub Gateway 验证透传，不依赖真实 NR）。
"""
import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import mcp_server


def test_d32_modify_flow_exposes_allow_prod():
    """D32 修复：autoflow_modify_flow 必须暴露 allow_prod 形参。"""
    sig = inspect.signature(mcp_server.autoflow_modify_flow)
    assert "allow_prod" in sig.parameters, "autoflow_modify_flow 缺 allow_prod 形参（D32 回退）"
    p = sig.parameters["allow_prod"]
    # 默认 False（PROD 锁保持生效，需显式 True 放行）
    assert p.default is False, f"allow_prod 默认值应 False，实际 {p.default}"


def test_d32_allow_prod_passthrough_to_gateway(monkeypatch):
    """D32 修复：autoflow_modify_flow 必须把 allow_prod 透传给 Gateway.modify_flow。"""
    captured = {}

    class _StubGw:
        def modify_flow(self, flow_id, dsl=None, node_patches=None, allow_prod=False):
            captured["allow_prod"] = allow_prod
            captured["flow_id"] = flow_id
            return {"ok": True, "flow_id": flow_id, "changed_nodes": 0, "mode": "node_patches"}

    class _StubAgent:
        mode = "expert"

    monkeypatch.setattr(mcp_server, "_gw", lambda: _StubGw())
    monkeypatch.setattr(mcp_server, "get_current_agent", lambda: _StubAgent())

    # 默认不传 -> allow_prod=False 透传
    mcp_server.autoflow_modify_flow(flow_id="abc", node_patches="[]")
    assert captured.get("allow_prod") is False, captured

    # 显式 True -> 透传 True
    mcp_server.autoflow_modify_flow(flow_id="abc", node_patches="[]", allow_prod=True)
    assert captured.get("allow_prod") is True, captured
