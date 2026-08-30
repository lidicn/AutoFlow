# -*- coding: utf-8 -*-
"""WB93 · F12 分支正确性断言（期望动作集对称比对）。

_ compare_trace 此前只判「节点可达」，不判「走的分支是否符合意图」：
条件反置流仍判通过（wb93_f12_counterexample.py 坐实）。本测试验证
expected_services / expected_branch_taken 引入后，走错分支会被升级为「断点」。

纯 in-process（直接调 Gateway._compare_trace），不部署、不碰 prod NR。
"""
import os
os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway.gateway import Gateway


def gw():
    # 只用纯函数部分，不初始化连接
    return Gateway.__new__(Gateway)


def mk_inverted_flow():
    """意图：暗(<10)→开灯；实现（反置）：暗→关灯。
    switch 规则 "$number(msg.光照) < 10" → out0=turn_off(错) / out1=turn_on(对)。
    """
    return {
        "nodes": [
            {"id": "n_inj", "type": "inject", "name": "手动触发", "wires": [["n_sw"]]},
            {"id": "n_sw", "type": "switch", "name": "分支",
             "rules": [{"t": "jsonata_exp", "v": "$number(msg.光照) < 10", "vt": "jsonata"},
                       {"t": "else", "v": "true", "vt": "jsonata"}],
             "wires": [["n_off"], ["n_on"]]},
            {"id": "n_off", "type": "api-call-service", "name": "light.turn_off",
             "domain": "light", "service": "turn_off", "entityId": "light.lamp", "wires": []},
            {"id": "n_on", "type": "api-call-service", "name": "light.turn_on",
             "domain": "light", "service": "turn_on", "entityId": "light.lamp", "wires": []},
        ]
    }


def mk_correct_flow():
    """条件修正为 "$number(msg.光照) >= 10" → out0=turn_on(对) / out1=turn_off。"""
    f = mk_inverted_flow()
    for n in f["nodes"]:
        if n.get("id") == "n_sw":
            n["rules"][0]["v"] = "$number(msg.光照) >= 10"
            n["wires"] = [["n_on"], ["n_off"]]
    return f


class TestF12BranchAssert:
    def test_inverted_flow_with_intent_fails(self):
        """F12 反例：条件反置，意图 turn_on，replay 走了 turn_off → 必须判断点。"""
        g = gw()
        flow = mk_inverted_flow()
        trace = [{"node": "n_sw"}, {"node": "n_off"}]  # 走了错分支
        r = g._compare_trace(flow, trace,
                             expected_services=["light.turn_on(light.lamp)"])
        assert r["verdict"] == "断点", r
        assert any(m["service"] == "light.turn_on(light.lamp)" and m["kind"] == "missing"
                   for m in r["service_mismatch"]), r
        assert any(m["service"] == "light.turn_off(light.lamp)" and m["kind"] == "extra"
                   for m in r["service_mismatch"]), r

    def test_correct_flow_with_intent_passes(self):
        """对照：条件正确，意图 turn_on，replay 走对 → 通过。"""
        g = gw()
        flow = mk_correct_flow()
        trace = [{"node": "n_sw"}, {"node": "n_on"}]
        r = g._compare_trace(flow, trace,
                             expected_services=["light.turn_on(light.lamp)"])
        assert r["verdict"] == "通过", r
        assert r["service_mismatch"] == [], r

    def test_backward_compat_no_expected_services(self):
        """向后兼容：不传 expected_services 时仍只判可达性（F12 不改变旧语义）。"""
        g = gw()
        flow = mk_inverted_flow()
        trace = [{"node": "n_sw"}, {"node": "n_off"}]
        r = g._compare_trace(flow, trace)  # 无 expected_services
        assert r["verdict"] == "通过", r  # 旧行为：仅可达性，不判分支
        assert r["service_mismatch"] == [], r

    def test_expected_branch_taken_form(self):
        """expected_branch_taken 结构化形式：声明 switch n_sw 走 branch 1（意图 turn_on），
        但反置流实际走了 branch 0（turn_off）→ 不一致必须判断点。"""
        g = gw()
        flow = mk_inverted_flow()  # out0=turn_off, out1=turn_on
        trace = [{"node": "n_sw"}, {"node": "n_off"}]  # 实际走了 branch 0（错）
        r = g._compare_trace(flow, trace,
                             expected_branch_taken=[{"switch": "n_sw", "branch": 1}])
        assert r["verdict"] == "断点", r
        assert "light.turn_on(light.lamp)" in r["expected_services"], r

    def test_expected_branch_taken_correct(self):
        """expected_branch_taken 与 replay 一致 → 通过。"""
        g = gw()
        flow = mk_correct_flow()
        trace = [{"node": "n_sw"}, {"node": "n_on"}]
        r = g._compare_trace(flow, trace,
                             expected_branch_taken=[{"switch": "n_sw", "branch": 0}])
        assert r["verdict"] == "通过", r
        assert r["service_mismatch"] == [], r
