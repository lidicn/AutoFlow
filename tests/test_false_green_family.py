#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TEST-TICKET-004 §5 假绿家族总闸守卫（收进仓库 CI，防最高发区回退）。

聚合 6 种假绿模式，本地 harness 跑本仓库真源码（conftest 已把 src 顶到 sys.path[0]），
断言每种都『不伪造绿』（fully_verified 必须为 False —— 要么 fail-closed 拦截，
要么诚实降级未充分验证）。

附：
- V-NEW-3 注册子流程门级验证：子流程输出字段纳入 reliable 后，下游 switch 不再被误判
  「未定义字段」，但声明效果 0 重放仍经 V-NEW-1 诚实降级（绝不假绿）。
- #2 死归因统一守卫：编译器 R31（collect_undefined_field_refs）与闸（_vg_dead_switch_rules）
  必须同源，锁死二者一致，防未来静默分歧。

纪律：不污染 prod、不写回外部目录；证据落临时目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.dont_write_bytecode = True

from autoflow_gateway.gateway import Gateway, _auto_expected_from_nodes, _vg_dead_switch_rules
from autoflow_gateway.flow_linter import collect_undefined_field_refs
from autoflow_gateway import subflows as sf
from autoflow_gateway.subflows import SubflowSpec

TAB = "tab_fgf"
ENTITY = "light.study_main_lamp"  # 目标实体（种子置 on，专门测『断言碰巧过但不得绿』）


def _imp():
    # 仓库内 conftest 已正确加载 autoflow_gateway（src 顶到 sys.path[0]），
    # 无需像 _local_gw 副本那样 purge 重导——否则会令 monkeypatch 绑定的
    # subflows 模块对象与 flow_linter 实际导入的不是同一个，导致 V-NEW-3 验证失效。
    from autoflow_gateway.gateway import Gateway, _auto_expected_from_nodes
    from autoflow_gateway import vhass as vh
    return Gateway, _auto_expected_from_nodes, vh


def _mk_shim(seed_states):
    class _Shim:
        def _gate_node_types(self, flow):
            return None

        def _check_entities_known(self, scene):
            return []

        def _build_vhass_from_staging(self):
            from autoflow_gateway import vhass as vh
            store = vh.VHassStore()
            store.areas = {"area_0": "房间"}
            store.entities = {}
            for eid, st in seed_states.items():
                store.entities[eid] = vh.VHassStore._normalize({
                    "entity_id": eid, "state": st,
                    "attributes": {"friendly_name": eid}, "area": "房间"})
            return store
    return _Shim()


def _run(nodes, seed_states, policy="fail_closed"):
    Gateway, _aefn, _ = _imp()
    os.environ.pop("AUTOFLOW_REPLAY_ZERO_POLICY", None)
    if policy is not None:
        os.environ["AUTOFLOW_REPLAY_ZERO_POLICY"] = policy
    flow = {"id": "tf_fgf", "label": "[T004-FGF]", "nodes": nodes}
    expected, _ = _aefn(flow["nodes"])
    return Gateway.run_staging_gate(_mk_shim(seed_states), dsl="", expected=expected, flow=flow)


def _action(nid, target=ENTITY):
    return {"id": nid, "type": "api-call-service", "z": TAB, "name": "动作",
            "server": "srv", "domain": "light", "service": "turn_on",
            "entityId": [target], "data": "", "wires": [[]]}


def _trigger_inject(out, payload):
    return {"id": "n1", "type": "inject", "z": TAB, "name": "触发",
            "props": [{"p": "payload"}], "repeat": "", "crontab": "", "once": False,
            "payloadType": "json", "payload": payload, "wires": [[out]]}


# ---------- 6 种假绿模式构造 ----------
def _m_conservative():  # V-F1：复杂 JSONata 保守命中
    return [
        _trigger_inject("sw", json.dumps({"x": "hello"})),
        {"id": "sw", "type": "switch", "z": TAB, "property": "payload",
         "propertyType": "msg",
         "rules": [{"t": "jsonata_exp",
                    "v": "$exists(payload.x) and $length(payload.x) > 0",
                    "vt": "jsonata"}], "checkall": "true", "outputs": 1,
         "wires": [["n2"]]},
        _action("n2"),
    ]


def _m_dead():  # G3：switch 读未声明字段 → 恒假分支
    return [
        _trigger_inject("sw", json.dumps({"x": "on"})),
        {"id": "sw", "type": "switch", "z": TAB, "property": "payload.undefined_field",
         "propertyType": "msg", "rules": [{"t": "eq", "v": "on", "vt": "str"}],
         "checkall": "true", "outputs": 1, "wires": [["n2"]]},
        _action("n2"),
    ]


def _m_zero():  # G2：声明效果 0 重放 + 可归因（恒假分支）
    return _m_dead()


def _m_function():  # V-F4 / V-NEW-2：纯 function 黑箱副作用
    return [
        _trigger_inject("fn", json.dumps({"pc": "on"})),
        {"id": "fn", "type": "function", "z": TAB, "name": "处理",
         "func": "msg.payload.x = 1; return msg;", "outputs": 1, "wires": [[]]},
    ]


def _m_subflow():  # V-NEW-3：未注册 subflow 输出字段不进 reliable（仍过度拦截，安全侧）
    return [
        {"id": "sf", "type": "subflow:abc123", "z": TAB, "name": "子流程",
         "wires": [["sw"]]},
        {"id": "sw", "type": "switch", "z": TAB, "property": "payload.flag",
         "rules": [{"t": "eq", "v": "on", "vt": "str"}], "checkall": "true",
         "outputs": 1, "wires": [["n2"]]},
        _action("n2"),
    ]


def _m_seed():  # H1 / V-NEW-1：声明效果 0 重放 + 种子态满足后置
    return [
        _trigger_inject("sw", json.dumps({"x": "off"})),
        {"id": "sw", "type": "switch", "z": TAB, "property": "payload.x",
         "rules": [{"t": "eq", "v": "on", "vt": "str"}], "checkall": "true",
         "outputs": 1, "wires": [["n2"]]},
        _action("n2"),
    ]


MODES = {
    "conservative_VF1": _m_conservative,
    "dead_G3": _m_dead,
    "zero_G2": _m_zero,
    "function_VF4_VNEW2": _m_function,
    "subflow_VNEW3": _m_subflow,
    "seed_H1_VNEW1": _m_seed,
}


def _eval_mode(name):
    nodes = MODES[name]()
    r = _run(nodes, {ENTITY: "on"})  # 种子置 on：即便断言碰巧过，也不得绿
    fv = r.get("fully_verified")
    verdict = r.get("verdict")
    forged = fv is True
    return {"mode": name, "fully_verified": fv, "verdict": verdict,
            "replayed": r.get("replayed_services"), "forged_green": forged}


# ============================== pytest 用例 ==============================
def test_fgf_conservative_not_false_green():
    r = _eval_mode("conservative_VF1")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_dead_not_false_green():
    r = _eval_mode("dead_G3")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_zero_not_false_green():
    r = _eval_mode("zero_G2")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_function_not_false_green():
    r = _eval_mode("function_VF4_VNEW2")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_subflow_not_false_green():
    r = _eval_mode("subflow_VNEW3")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_seed_not_false_green():
    r = _eval_mode("seed_H1_VNEW1")
    assert r["fully_verified"] is False, r
    assert r["forged_green"] is False


def test_fgf_family_aggregate():
    """聚合 6 种模式，断言全部不伪造绿，落盘总表。"""
    rows = [_eval_mode(n) for n in MODES]
    forged = [r["mode"] for r in rows if r["forged_green"]]
    table = {
        "gate_baseline": "repo-head",
        "total_modes": len(rows),
        "forged_green_count": len(forged),
        "all_honest": len(forged) == 0,
        "rows": rows,
    }
    out = Path(tempfile.gettempdir()) / "false_green_family.json"
    out.write_text(json.dumps(table, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    assert len(forged) == 0, f"存在伪造绿模式: {forged}"
    assert all(r["fully_verified"] is False for r in rows)


# ───────────────── V-NEW-3 注册子流程门级验证 ─────────────────
def test_vnew3_registered_subflow_output_modeled_and_not_false_green(monkeypatch):
    """V-NEW-3 修复：注册子流程声明 outputs 后，下游 switch 读该字段不再被误判
    「未定义字段」→ 不再过度拦截；但声明效果 0 重放仍经 V-NEW-1 诚实降级（不假绿）。"""
    fake = SubflowSpec(name="vnew3_sf", title="t", call={"type": "subflow", "subflow_id": "vnew3_sf"},
                       outputs=["flag"])
    monkeypatch.setitem(sf.SUBFLOWS, "vnew3_sf", fake)

    nodes = [
        {"id": "sf", "type": "subflow:vnew3_sf", "z": TAB, "name": "vnew3_sf", "wires": [["sw"]]},
        {"id": "sw", "type": "switch", "z": TAB, "property": "payload.flag",
         "rules": [{"t": "eq", "v": "on", "vt": "str"}], "checkall": "true",
         "outputs": 1, "wires": [["n2"]]},
        _action("n2"),
    ]
    r = _run(nodes, {ENTITY: "on"})
    # 不假绿：声明效果 0 重放 → V-NEW-1 降级
    assert r["fully_verified"] is False, r
    assert r["verdict"] in ("未充分验证", "拦截"), r
    # V-NEW-3 核心：flag 已进 reliable → 不再是「未定义字段」恒假分支
    assert r.get("dead_branches") == [], r.get("dead_branches")


# ───────────────── #2 死归因统一守卫 ─────────────────
def test_dead_attribution_unified_compiler_vs_gate():
    """编译器 R31 与闸 _vg_dead_switch_rules 必须同源（都委 collect_undefined_field_refs）。"""
    nodes = _m_dead()
    compiler = collect_undefined_field_refs(nodes)
    gate = _vg_dead_switch_rules({"nodes": nodes})
    assert compiler == gate, (compiler, gate)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
