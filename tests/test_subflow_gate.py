"""子流程部署闸门单测（node_gate 不再误杀合法 subflow:<id> 引用）。

根因：NR 的 GET /flows 是**扁平**节点数组（tab/subflow/普通节点/
subflow 实例都是顶层带 type 的节点）；而旧代码按嵌套 {flows:[{nodes:[...]}]}
解析 → 收不到任何 subflow:* 类型。再叠加 _gate_node_types 只把子流程 def 的
裸 id 合并进白名单（实例节点 type 是 "subflow:<id>"，带前缀，对不上）→ 合法
子流程被硬拦「节点类型未注册」。

修复后：
  - get_installed_node_types 兼容扁平 /flows（含 subflow:<id> 实例类型）；
  - _gate_node_types 合并子流程 def 时同时加 "subflow:<id>" 前缀。

全部不依赖 live NR —— 用 FakeNR / 子类 stub _json / _request 覆盖。
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway import gateway as G
from autoflow_gateway.lib import nr_client as NC
from autoflow_gateway.lib.nr_client import _Resp


# ── 1) get_installed_node_types：扁平 /flows ───────────────
class _FlatFlowsClient(NC.NodeRedClient):
    def _json(self, method, endpoint, **kwargs):
        if endpoint == "/nodes":
            return []  # 模拟 /nodes 不含 subflow 内部类型
        if endpoint == "/flows":
            # NR 5.x 扁平结构：顶层即节点配置
            return [
                {"id": "tab1", "type": "tab", "label": "t"},
                {"id": "b0bbc86abb2172a5", "type": "subflow", "name": "bark"},
                {"id": "inst1", "type": "subflow:b0bbc86abb2172a5", "z": "tab1"},
                {"id": "inject1", "type": "inject", "z": "tab1"},
            ]
        raise AssertionError(endpoint)


def test_get_installed_node_types_flat_flows():
    c = _FlatFlowsClient("http://test.local")
    types = c.get_installed_node_types()
    # 关键：扁平 /flows 里的 subflow 实例类型必须被收进已装集合
    assert "subflow:b0bbc86abb2172a5" in types, types
    assert "inject" in types


# ── 2) get_installed_node_types：嵌套 /flows（回归，旧版结构）──
class _NestedFlowsClient(NC.NodeRedClient):
    def _json(self, method, endpoint, **kwargs):
        if endpoint == "/nodes":
            return []
        if endpoint == "/flows":
            return {"flows": [{"id": "tab1", "label": "t", "nodes": [
                {"id": "s1", "type": "subflow:b0bbc86abb2172a5", "z": "tab1"},
                {"id": "i1", "type": "inject", "z": "tab1"},
            ]}]}
        raise AssertionError(endpoint)


def test_get_installed_node_types_nested_flows():
    c = _NestedFlowsClient("http://test.local")
    types = c.get_installed_node_types()
    assert "subflow:b0bbc86abb2172a5" in types, types
    assert "inject" in types


# ── 3) _gate_node_types：已知 subflow 放行 ────────────────
class _GateKnownClient:
    def get_installed_node_types(self):
        return {"inject", "subflow:b0bbc86abb2172a5"}

    def _request(self, method, endpoint):
        # 无 subflow def，靠 get_installed_node_types 已含实例类型
        return _Resp(200, json.dumps([{"id": "tab1", "type": "tab"}]))


class _GateKnownNR:
    def __init__(self):
        self.client = _GateKnownClient()


def test_gate_allows_known_subflow():
    gw = G.Gateway()
    gw.nr = _GateKnownNR()
    flow = {"nodes": [
        {"id": "s1", "type": "subflow:b0bbc86abb2172a5"},
        {"id": "i1", "type": "inject"},
    ]}
    gw._gate_node_types(flow)  # 不应抛 RuntimeError


def test_gate_blocks_unknown_subflow():
    gw = G.Gateway()
    gw.nr = _GateKnownNR()
    flow = {"nodes": [{"id": "x", "type": "time-range"}]}
    try:
        gw._gate_node_types(flow)
    except RuntimeError as e:
        assert "未注册" in str(e)
    else:
        raise AssertionError("未知子流程类型应被闸门拦截")


# ── 4) _gate_node_types：仅靠 merge 补 "subflow:<id>" 前缀（Bug 2）──
class _MergeClient:
    def get_installed_node_types(self):
        # 故意不含 subflow:b0bbc86abb2172a5 —— 全靠 merge 从 /flows def 补
        return {"inject"}

    def _request(self, method, endpoint):
        if endpoint == "/flows":
            # 目标 NR 上仅有子流程定义（type=="subflow", id 裸），无实例
            return _Resp(200, json.dumps([
                {"id": "b0bbc86abb2172a5", "type": "subflow", "name": "bark"},
            ]))
        return _Resp(200, "[]")


class _MergeNR:
    def __init__(self):
        self.client = _MergeClient()


def test_gate_merge_adds_subflow_prefix():
    gw = G.Gateway()
    gw.nr = _MergeNR()
    flow = {"nodes": [{"id": "s1", "type": "subflow:b0bbc86abb2172a5"}]}
    # 修复前：merge 只加裸 id，subflow:b0bbc86abb2172a5 不在白名单 → 抛错；
    # 修复后：merge 加 "subflow:<id>" 前缀 → 放行。
    gw._gate_node_types(flow)  # 不应抛 RuntimeError


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    raise SystemExit(1 if failed else 0)
