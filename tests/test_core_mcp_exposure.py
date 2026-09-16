# -*- coding: utf-8 -*-
"""#7/#9 · Core 档暴露到 MCP 管理面回归。

验证：
- NRLayer.get_inventory 委托到底层 client.get_inventory（protected_flow_ids 透传）。
- NRLayer.modify_node_field 委托到底层 client.modify_node_field（dry_run 透传）。
- autoflow_get_inventory（@mcp.tool 只读）三面板可调，返回 ok。
- autoflow_surgical_edit（@mcp_admin.tool 写刀）dry_run 默认 True 预览；
  普通身份(mode=normal) 不可调（_DEPLOY_KNIVES 隐藏 + 运行时闸）。

全程离线：用内存假 backend 注入 NRLayer；MCP 工具经 monkeypatch _gw / get_current_agent 驱动。
运行：pytest tests/test_core_mcp_exposure.py
"""
import os
import sys
import json
import types
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_cme_")

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.config import reset_config

reset_config()

import autoflow_gateway.mcp_server as M


class _InvClient:
    def __init__(self, captured):
        self._captured = captured

    def get_inventory(self, protected_flow_ids=None):
        self._captured["protected"] = protected_flow_ids
        return {"tab_count": 2, "node_count": 7, "tabs": [
            {"id": "t1", "label": "af_mine", "owned_by_af": True, "node_count": 3,
             "risks": [], "nodes": []},
            {"id": "t2", "label": "user_flow", "owned_by_af": False, "node_count": 4,
             "risks": [], "nodes": []},
        ]}


class _SurgicalClient:
    def get_flow(self, fid):
        return {"id": fid, "label": "l", "nodes": [
            {"id": "n1", "type": "inject", "name": "old", "wires": [[]]}]}

    def update_flow(self, fid, f, **k):
        return {"ok": True}

    def modify_node_field(self, flow_id, node_id, fields, *, dry_run=False,
                          allow_structural=False):
        return {"success": not dry_run, "dry_run": dry_run, "node_id": node_id,
                "node_count": 1,
                "diff": {k: {"old": "old", "new": fields[k]} for k in fields}}


def _gw_with(client):
    gw = Gateway()
    gw.nr = NRLayer(gw.cfg, backend=client)
    gw.state = types.SimpleNamespace(get_flow_catalog=lambda: {"flows": {"af_x": {}}})
    return gw


def test_layer_get_inventory_delegates():
    cap = {}
    gw = _gw_with(_InvClient(cap))
    inv = gw.nr.get_inventory(protected_flow_ids={"af_x"})
    assert cap["protected"] == {"af_x"}
    assert inv["tab_count"] == 2
    assert inv["tabs"][0]["owned_by_af"] is True


def test_layer_surgical_edit_dry_run_diff():
    gw = _gw_with(_SurgicalClient())
    r = gw.nr.modify_node_field("f", "n1", {"name": "new"}, dry_run=True)
    assert r["dry_run"] is True
    assert r["diff"]["name"]["old"] == "old"
    assert r["diff"]["name"]["new"] == "new"


def test_get_inventory_tool_ok(monkeypatch):
    cap = {}
    gw = _gw_with(_InvClient(cap))
    monkeypatch.setattr(M, "_gw", lambda: gw)
    out = M.autoflow_get_inventory(area="")
    obj = json.loads(out)
    assert obj["ok"] is True
    assert obj["tab_count"] == 2


def test_surgical_edit_tool_dry_run(monkeypatch):
    gw = _gw_with(_SurgicalClient())
    monkeypatch.setattr(M, "_gw", lambda: gw)
    monkeypatch.setattr(M, "get_current_agent",
                        lambda: types.SimpleNamespace(mode="developer"))
    out = M.autoflow_surgical_edit("f", "n1", '{"name":"new"}', dry_run=True)
    obj = json.loads(out)
    assert obj["ok"] is True
    assert obj["diff"]["name"]["new"] == "new"


def test_surgical_edit_blocked_for_normal(monkeypatch):
    gw = _gw_with(_SurgicalClient())
    monkeypatch.setattr(M, "_gw", lambda: gw)
    monkeypatch.setattr(M, "get_current_agent",
                        lambda: types.SimpleNamespace(mode="normal"))
    out = M.autoflow_surgical_edit("f", "n1", "{}")
    obj = json.loads(out)
    assert obj["ok"] is False                       # ★ 普通身份不可调


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
