# -*- coding: utf-8 -*-
"""
第十七轮 · D30 / D25-link 回归守卫。

D30：e2e trace 含 link out / link in 节点时崩溃
     `unhashable type: 'dict'` —— 根因是 `_derive_planned_path` 等位点对
     link 节点的 `links` 字段做 `set(...)`，而 Node-RED 部分导出 / 构造形态
     下 `links` 是对象数组 `[{"id": ...}]`，直接 `set([dict])` 抛异常。
     修复：统一用 `_link_ids()` 把 links 剥成 id 字符串集合。

D25(深化)/round17：即便 links 是字符串数组，旧 link 边解析条件写反
     （`set(link_in.links) & set(link_out.links)` 永远为空），BFS 在 link out
     处中断、下游节点永远走不到 → 误报断点。修复后按「link out 的 links 即其
     指向的 link in 目标 id 集合」正确建边。

不依赖真实 NR：用 FakeNRLayer 模拟「部署→触发→执行→读回 trace」，
并把 link out→link in 的消息传递也模拟出来（见 _run 的 link 边处理）。
"""
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_e2e_link_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway.gateway import _link_ids
from autoflow_gateway.config import reset_config


# ── 假 NR 层：在 FakeNRLayer 基础上模拟 link out→link in 消息传递 ──
class FakeNRLayerLink:
    def __init__(self):
        self.flows: Dict[str, Dict] = {}
        self._trace: List[Dict] = []
        self.server_id = "srv_fake"
        self.deleted: List[str] = []

    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        self.flows[fid] = flow_data
        return {"id": fid, "created": True}

    def get_default_server_id(self):
        return self.server_id

    def trigger_inject(self, node_id):
        self._run(node_id)
        return 200

    def get_context(self, store, key):
        return list(self._trace)

    def set_context(self, store, key, value):
        if value in ([], None, {}):
            self._trace = []
        return None

    def delete_context(self, store, key):
        self._trace = []
        return {}

    def delete_flow(self, fid, force=False, allow_prod=False):
        self.flows.pop(fid, None)
        self.deleted.append(fid)
        return {"deleted": True}

    def _run(self, start_id):
        flow = None
        for f in self.flows.values():
            if any(n.get("id") == start_id for n in f.get("nodes", [])):
                flow = f
                break
        if flow is None:
            return
        nodes = {n["id"]: n for n in flow.get("nodes", [])}
        seen = set()
        stack = [start_id]
        order = 0
        trace: List[Dict] = []
        while stack:
            nid = stack.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            n = nodes.get(nid)
            if n is None:
                continue
            # 插桩节点（tap / catch / 错误记录）跳过，但继续沿其连线
            if n.get("_af_trace_tap") or n.get("type") == "catch" or n.get("_af_err_sink"):
                for w in (n.get("wires") or []):
                    if isinstance(w, list):
                        stack.extend(w)
                continue
            # 原节点执行 → 由 tap 记录其 id
            trace.append({"node": nid, "t": order, "topic": None, "payload": "x"})
            order += 1
            # link out：经 Node-RED link 机制把消息广播到其 links 指向的 link in
            if n.get("type") == "link out":
                for tgt in _link_ids(n.get("links")):
                    if tgt not in seen:
                        stack.append(tgt)
            # 普通 wires 下游
            for w in (n.get("wires") or []):
                if isinstance(w, list):
                    stack.extend(w)
        self._trace = trace


def _make_gw():
    reset_config()
    gw = G.Gateway()
    gw._e2e_settle = 0
    gw.nr = FakeNRLayerLink()
    # 给一个最小 client，使 _gate_node_types / _validate_link_out_targets 不 fail-open
    # 跳过（_json 返回空 → valid_ids 仅取本流节点 id，足以判定 link 目标合法性）。
    gw.nr.client = type("C", (), {"_json": staticmethod(lambda *a, **k: [])})()
    return gw


def _link_flow(links_fmt, with_action=False):
    """inject → link out → link in → [api-call-service →] debug"""
    nodes = [
        {"id": "i1", "type": "inject", "z": "f", "wires": [["lo1"]]},
        {"id": "lo1", "type": "link out", "z": "f", "wires": [], "links": links_fmt["lo"]},
        {"id": "li1", "type": "link in", "z": "f", "wires": [["d1"]], "links": links_fmt["li"]},
        {"id": "d1", "type": "debug", "z": "f", "wires": []},
    ]
    if with_action:
        nodes[2]["wires"] = [["svc1"]]
        nodes.append({"id": "svc1", "type": "api-call-service", "z": "f",
                      "domain": "light", "service": "turn_on",
                      "entityId": "light.x", "wires": [["d1"]]})
    return {"nodes": nodes}


# ── 1) _link_ids 四种形态 ──
def test_link_ids_all_forms():
    assert _link_ids("a") == {"a"}
    assert _link_ids({"id": "a"}) == {"a"}
    assert _link_ids(["a", "b"]) == {"a", "b"}
    assert _link_ids([{"id": "a"}, {"id": "b"}]) == {"a", "b"}
    assert _link_ids(["a", {"id": "b"}]) == {"a", "b"}
    assert _link_ids(None) == set()
    assert _link_ids([]) == set()


# ── 2) D25 深化：link 边解析（两种 links 形态都要走通）──
def test_derive_planned_path_traverses_link_edge():
    for fmt in ({"lo": ["li1"], "li": ["lo1"]},
                {"lo": [{"id": "li1"}], "li": [{"id": "lo1"}]}):
        order = G.Gateway._derive_planned_path(None, _link_flow(fmt), start_ids=["i1"])
        # 关键断言：BFS 必须越过 link out 走到 link in 与后续 debug
        assert "li1" in order, order
        assert "d1" in order, order


def test_derive_planned_path_link_then_action():
    for fmt in ({"lo": ["li1"], "li": ["lo1"]},
                {"lo": [{"id": "li1"}], "li": [{"id": "lo1"}]}):
        order = G.Gateway._derive_planned_path(
            None, _link_flow(fmt, with_action=True), start_ids=["i1"])
        assert "li1" in order, order
        assert "svc1" in order, order   # 动作终点必须被纳入计划路径


# ── 3) D35：remap 后 dict 形态 links 必须【归一化为字符串数组】(NR 只认字符串数组)──
# 旧断言 [{"id": new_li}] 是 D30 的保留对象数组行为，但实测 Node-RED 运行时
# 不认对象数组 links → link 不建立 → 下游全断（D35 / round24）。故 D35 改为
# 统一输出字符串数组 ["new_li"]。
def test_remap_rewrites_dict_links():
    gw = _make_gw()
    new_flow, id_map, _ = gw._remap_raw_flow_ids(
        _link_flow({"lo": [{"id": "li1"}], "li": [{"id": "lo1"}]}), "TARGET")
    lo_node = [n for n in new_flow["nodes"] if n.get("type") == "link out"][0]
    li_node = [n for n in new_flow["nodes"] if n.get("type") == "link in"][0]
    # 必须被改写（不再是原始对象数组）
    assert lo_node["links"] != [{"id": "li1"}], lo_node["links"]
    assert li_node["links"] != [{"id": "lo1"}], li_node["links"]
    # D35 关键：remap 后 links 必须是【纯字符串数组】，且 id 指向 remap 后的节点
    assert isinstance(lo_node["links"], list) and all(
        isinstance(x, str) for x in lo_node["links"]), lo_node["links"]
    assert isinstance(li_node["links"], list) and all(
        isinstance(x, str) for x in li_node["links"]), li_node["links"]
    new_li = [n["id"] for n in new_flow["nodes"] if n.get("type") == "link in"][0]
    assert lo_node["links"] == [new_li], lo_node["links"]


# ── 4) D30：validate_link_out_targets 解析 dict 形态，不错杀、不漏杀 ──
def test_validate_dict_links():
    gw = _make_gw()
    # 合法：dict 形态 links 指向同流 link in
    errs = gw._validate_link_out_targets(_link_flow({"lo": [{"id": "li1"}], "li": [{"id": "lo1"}]}))
    assert errs == [], errs
    # 非法：指向不存在的目标仍应被抓出
    bad = {"nodes": [
        {"id": "lo1", "type": "link out", "z": "f", "wires": [], "links": [{"id": "ghost"}]},
        {"id": "li1", "type": "link in", "z": "f", "wires": [[]], "links": ["lo1"]}]}
    errs = gw._validate_link_out_targets(bad)
    assert any(e.get("rule") == "R_LINKIN" for e in errs), errs


# ── 5) D30 end-to-end：run_e2e_trace_raw 含 dict 形态 link 不再崩溃 ──
def test_e2e_trace_raw_link_dict_no_crash():
    gw = _make_gw()
    # 含动作终点，使 e2e 有可验证路径；links 用 dict 形态（D30 触发器）
    flow = _link_flow({"lo": [{"id": "li1"}], "li": [{"id": "lo1"}]}, with_action=True)
    res = gw.run_e2e_trace_raw(flow, allow_prod=False)
    # 不得出现 unhashable type: 'dict'
    assert "unhashable" not in str(res), res
    assert isinstance(res, dict)
    assert "verdict" in res, res
    report = res.get("report", {})
    # FakeNRLayer 已模拟 link 传递，api-call-service 应被真实到达
    assert "api-call-service" in (report.get("reached") or []), report
    # 清理：临时部署的 flow 应被回滚
    assert res.get("flow_id") in gw.nr.deleted, "e2e 临时 flow 未清理"


if __name__ == "__main__":
    test_link_ids_all_forms()
    test_derive_planned_path_traverses_link_edge()
    test_derive_planned_path_link_then_action()
    test_remap_rewrites_dict_links()
    test_validate_dict_links()
    test_e2e_trace_raw_link_dict_no_crash()
    print("ALL D30/D25-link REGRESSION TESTS PASSED")
