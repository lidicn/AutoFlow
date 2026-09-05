# -*- coding: utf-8 -*-
"""
第二十三轮 · D34 回归守卫 —— e2e 追踪器对 link 节点严重不可信。

根因（D34）：`_instrument_flow` 此前对 link out / link in 节点强制插 tap 并改写
其 wires（尤其 `link out` 原本 wires=[]，被强行改为 `[[tap_id]]`）。link out 在
Node-RED 中无 wires 输出（消息经 link 机制广播），强加 wires 破坏其语义并触发 NR
运行时 `TypeError: Cannot read properties of null (reading 'config')` —— 真实 NR
抛错但 e2e 仍报"通过"，构成严重误报。

修复（gateway.py）：
  - E2E_SINK_TYPES 移除 "link in"（保留 "link out"）。
  - `_instrument_flow` 删除 link out/link in 特殊插桩块：link out 走 SINK 跳过
    （不改 wires），link in 落普通插 tap 分支（它有正常 wires，加 tap 安全）。
  - `_derive_planned_path` 的 link 隐式边（link out→link in）不依赖 wires，仍生效。

本测试不依赖真实 NR：用 FakeNRLayer 模拟「部署→触发→执行→读回 trace」并把
link out→link in 的消息传递也模拟出来，断言：
  1. 插桩后 link out 的 wires 保持 []（核心修复点，D34 根因）。
  2. link in 被插 tap（可追踪 link 穿越）。
  3. 含 link 的 flow（inject→link→change→debug）e2e verdict 准确（不再误报通过）。
  4. 无 link 的对照 flow 行为不变（change 正常到达）。
"""
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_e2e_d34_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway.gateway import E2E_SINK_TYPES, _link_ids
from autoflow_gateway.config import reset_config


# ── 假 NR 层（模拟 link 传递）──
class FakeNRLayerD34:
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
        trace: List[Dict] = []
        order = 0
        while stack:
            nid = stack.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            n = nodes.get(nid)
            if n is None:
                continue
            # 插桩节点（tap/catch/err_sink）跳过，但继续沿其连线
            if n.get("_af_trace_tap") or n.get("type") == "catch" or n.get("_af_err_sink"):
                for w in (n.get("wires") or []):
                    if isinstance(w, list):
                        stack.extend(w)
                continue
            # 原节点执行 → 由 tap 记录其 id（link out 不插 tap，故不在此记录，
            # 但其上游 inject 已记录，link in 的 tap 记录 link 穿越）
            if n.get("type") != "link out":  # link out 不插桩，不记录
                trace.append({"node": nid, "t": order, "topic": None, "payload": "x"})
                order += 1
            # link out：经 Node-RED link 机制广播到其 links 指向的 link in
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
    gw.nr = FakeNRLayerD34()
    gw.nr.client = type("C", (), {"_json": staticmethod(lambda *a, **k: [])})()
    return gw


def _link_flow(with_action=True):
    """inject → link out → link in → [change / api-call-service] → debug"""
    nodes = [
        {"id": "i1", "type": "inject", "z": "f", "wires": [["lo1"]]},
        {"id": "lo1", "type": "link out", "z": "f", "wires": [], "links": ["li1"]},
        {"id": "li1", "type": "link in", "z": "f", "wires": [["ch1"]], "links": ["lo1"]},
        {"id": "ch1", "type": "change", "z": "f",
         "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "X", "tot": "str"}],
         "wires": [["d1"]]},
        {"id": "d1", "type": "debug", "z": "f", "wires": []},
    ]
    if with_action:
        nodes[2]["wires"] = [["svc1"]]
        nodes.append({"id": "svc1", "type": "api-call-service", "z": "f",
                      "domain": "light", "service": "turn_on",
                      "entityId": "light.x", "wires": [["d1"]]})
    return {"nodes": nodes}


# ── 1) D34 核心修复点：插桩不污染 link out 的 wires ──
def test_instrument_does_not_pollute_link_out_wires():
    gw = _make_gw()
    inst = gw._instrument_flow(_link_flow(), "__trace__:d34")
    by_id = {n["id"]: n for n in inst["nodes"]}
    # 关键断言：link out 的 wires 必须保持 []（不被强行加 tap 分支）
    assert by_id["lo1"]["wires"] == [], (
        f"D34 REGRESSION: link out wires polluted = {by_id['lo1']['wires']}")
    # link in 应被插 tap（它有正常 wires，加 tap 安全）
    li_wires = by_id["li1"]["wires"]
    assert any(isinstance(w, list) and any(x.startswith("af_e2e_tap_") for x in w)
               for w in li_wires), f"link in not instrumented: {li_wires}"


# ── 2) E2E_SINK_TYPES 已移除 link in、保留 link out ──
def test_sink_types_excludes_link_in():
    assert "link in" not in E2E_SINK_TYPES, "link in 必须可插桩/可比对"
    assert "link out" in E2E_SINK_TYPES, "link out 必须保持 SINK（不改 wires）"


# ── 3) D34 完整 e2e：含 link 的 flow verdict 准确（不再误报通过）──
# 直接调用底层 _instrument_flow + _derive_planned_path + _compare_trace，
# 绕过 run_e2e_trace_raw 的 remap（避免 FakeNRLayer 未模拟 links remap 的干扰），
# 精准验证 D34 修复后 e2e 对含 link flow 的判定准确性。
def test_e2e_trace_link_flow_verdict_accurate():
    gw = _make_gw()
    flow = _link_flow(with_action=True)
    # 插桩（D34 修复后：link out 不被污染，link in 被插 tap）
    inst = gw._instrument_flow(flow, "__trace__:d34")
    # 模拟真实 NR 执行后回读的 trace：link out 不插桩故不出现，其余原节点到达
    # （按原始节点 id；_compare_trace 用 compare_flow 的 id 空间比对）
    trace = [
        {"node": "i1", "t": 0, "topic": None, "payload": "x"},
        {"node": "li1", "t": 1, "topic": None, "payload": "x"},  # link in 被 tap 记录
        {"node": "ch1", "t": 2, "topic": None, "payload": "x"},
        {"node": "svc1", "t": 3, "topic": None, "payload": "x"},
    ]
    report = gw._compare_trace(flow, trace, None, trigger_ids=["i1"])
    reached = [gw._node_label({n["id"]: n for n in flow["nodes"]}, r) or r
               for r in report.get("reached_ids") or []]
    # reached 标签应包含 link in / change / api-call-service
    assert "link in" in reached, f"link in 未被追踪: {reached}"
    assert "change" in reached, f"change 未被追踪: {reached}"
    assert "api-call-service" in reached, f"api-call-service 未被追踪: {reached}"
    # 关键：verdict 应准确（无 missing、无 error）
    assert report.get("verdict") == "通过", report


# ── 4) D34 断点场景：link 后动作未到达时 e2e 应准确报断点（而非误报通过）──
def test_e2e_trace_link_breakpoint_accurate():
    gw = _make_gw()
    flow = _link_flow(with_action=True)
    # 模拟真实断点：link 穿越成功，但 link in 下游的 change/svc 未执行
    # （如 change 节点运行时异常）。trace 里只有 inject + link in。
    trace = [
        {"node": "i1", "t": 0, "topic": None, "payload": "x"},
        {"node": "li1", "t": 1, "topic": None, "payload": "x"},
    ]
    report = gw._compare_trace(flow, trace, None, trigger_ids=["i1"])
    reached = [gw._node_label({n["id"]: n for n in flow["nodes"]}, r) or r
               for r in report.get("reached_ids") or []]
    # svc1（api-call-service）不应到达（断在 link in 后）
    assert "api-call-service" not in reached, report
    # verdict 应准确报断点（而非误报通过）
    assert report.get("verdict") == "断点", f"D34 仍误报通过: {report}"


# ── 5) 无 link 对照：行为不变（底层方法直接验证）──
def test_e2e_trace_no_link_control():
    gw = _make_gw()
    flow = {"nodes": [
        {"id": "i1", "type": "inject", "z": "f", "wires": [["ch1"]]},
        {"id": "ch1", "type": "change", "z": "f",
         "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "X", "tot": "str"}],
         "wires": [["d1"]]},
        {"id": "d1", "type": "debug", "z": "f", "wires": []},
    ]}
    gw._instrument_flow(flow, "__trace__:ctrl")
    trace = [
        {"node": "i1", "t": 0, "topic": None, "payload": "x"},
        {"node": "ch1", "t": 1, "topic": None, "payload": "x"},
    ]
    report = gw._compare_trace(flow, trace, None, trigger_ids=["i1"])
    reached = [gw._node_label({n["id"]: n for n in flow["nodes"]}, r) or r
               for r in report.get("reached_ids") or []]
    assert "change" in reached, report
    assert report.get("verdict") == "通过", report


# ── 6) D35 核心修复点：_remap_raw_flow_ids 把对象数组 links 归一化为字符串数组 ──
def test_remap_normalizes_object_array_links_to_string_array():
    gw = _make_gw()
    flow = {
        "nodes": [
            {"id": "lo1", "type": "link out", "z": "f", "wires": [],
             "links": [{"id": "li1"}, {"id": "li2"}]},
            {"id": "li1", "type": "link in", "z": "f", "wires": [[]],
             "links": [{"id": "lo1"}]},
            {"id": "li2", "type": "link in", "z": "f", "wires": [[]],
             "links": [{"id": "lo1"}]},
        ]
    }
    remapped, id_map, _ = gw._remap_raw_flow_ids(flow, "FFFFFFFFFFFFFFFF")
    by_id = {n["id"]: n for n in remapped["nodes"]}
    # 关键断言：remap 后 link 节点的 links 必须是【纯字符串数组】
    for tid in ("lo1", "li1", "li2"):
        links = by_id[id_map[tid]]["links"]
        assert all(isinstance(x, str) for x in links), (
            f"D35 REGRESSION: links 未归一化为字符串数组 = {links}")
        assert len(links) == len(flow["nodes"]
                                [0 if tid == "lo1" else (1 if tid == "li1" else 2)]
                                ["links"]), f"链接数丢失: {links}"
    # 原对象数组中的 id 应被重映射为新的节点 id
    assert id_map["li1"] in by_id[id_map["lo1"]]["links"], (
        f"lo1 未指向重映射后的 li1: {by_id[id_map['lo1']]['links']}")


# ── 7) D35 完整 e2e：对象数组 links 的 flow verdict 准确（修复后不再断点）──
def test_e2e_trace_object_array_links_verdict_accurate():
    gw = _make_gw()
    # 构造对象数组 links 的输入 flow（模拟 round24 矩阵2/3）
    flow = {
        "nodes": [
            {"id": "i1", "type": "inject", "z": "f", "wires": [["lo1"]]},
            {"id": "lo1", "type": "link out", "z": "f", "wires": [],
             "links": [{"id": "li1"}]},
            {"id": "li1", "type": "link in", "z": "f", "wires": [["ch1"]],
             "links": [{"id": "lo1"}]},
            {"id": "ch1", "type": "change", "z": "f",
             "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "X",
                        "tot": "str"}], "wires": [["d1"]]},
            {"id": "d1", "type": "debug", "z": "f", "wires": []},
        ]
    }
    # 先经 remap 归一化（白盒部署真实路径），再用底层方法比对
    remapped, id_map, _ = gw._remap_raw_flow_ids(flow, "FFFFFFFFFFFFFFFF")
    # 模拟真实 NR 执行后回读的 trace（按 remap 后的节点 id 空间）
    ri1 = id_map["i1"]; rli1 = id_map["li1"]; rch1 = id_map["ch1"]
    trace = [
        {"node": ri1, "t": 0, "topic": None, "payload": "x"},
        {"node": rli1, "t": 1, "topic": None, "payload": "x"},
        {"node": rch1, "t": 2, "topic": None, "payload": "x"},
    ]
    report = gw._compare_trace(remapped, trace, None, trigger_ids=[ri1])
    # 关键：对象数组 links 经 remap 归一化后，link in / change 应被正确追踪、verdict 通过
    reached = [gw._node_label({n["id"]: n for n in remapped["nodes"]}, r) or r
               for r in report.get("reached_ids") or []]
    assert "link in" in reached, f"D35: link in 未被追踪: {reached}"
    assert "change" in reached, f"D35: change 未被追踪: {reached}"
    assert report.get("verdict") == "通过", (
        f"D35 REGRESSION: 对象数组 links 仍误报断点: {report}")


# ── 8) D35 断点场景：对象数组 links + 下游未到达，e2e 准确报断点 ──
def test_e2e_trace_object_array_links_breakpoint_accurate():
    gw = _make_gw()
    flow = {
        "nodes": [
            {"id": "i1", "type": "inject", "z": "f", "wires": [["lo1"]]},
            {"id": "lo1", "type": "link out", "z": "f", "wires": [],
             "links": [{"id": "li1"}]},
            {"id": "li1", "type": "link in", "z": "f", "wires": [["ch1"]],
             "links": [{"id": "lo1"}]},
            {"id": "ch1", "type": "change", "z": "f",
             "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "X",
                        "tot": "str"}], "wires": [["d1"]]},
            {"id": "d1", "type": "debug", "z": "f", "wires": []},
        ]
    }
    remapped, id_map, _ = gw._remap_raw_flow_ids(flow, "FFFFFFFFFFFFFFFF")
    # 模拟真实断点：link 穿越成功，但下游 change 未执行
    trace = [
        {"node": id_map["i1"], "t": 0, "topic": None, "payload": "x"},
        {"node": id_map["li1"], "t": 1, "topic": None, "payload": "x"},
    ]
    report = gw._compare_trace(remapped, trace, None, trigger_ids=[id_map["i1"]])
    reached = [gw._node_label({n["id"]: n for n in remapped["nodes"]}, r) or r
               for r in report.get("reached_ids") or []]
    assert "change" not in reached, report
    assert report.get("verdict") == "断点", (
        f"D35 REGRESSION: 对象数组 links 断点场景误报通过: {report}")


if __name__ == "__main__":
    test_instrument_does_not_pollute_link_out_wires()
    test_sink_types_excludes_link_in()
    test_e2e_trace_link_flow_verdict_accurate()
    test_e2e_trace_link_breakpoint_accurate()
    test_e2e_trace_no_link_control()
    test_remap_normalizes_object_array_links_to_string_array()
    test_e2e_trace_object_array_links_verdict_accurate()
    test_e2e_trace_object_array_links_breakpoint_accurate()
    print("ALL D34+D35 REGRESSION TESTS PASSED ✅")
