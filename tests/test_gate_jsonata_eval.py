"""submit_run_gate 的 JSONata 分支求值单测（防回归）。

覆盖：数值比较真假两向、字符串等于/不等、and/or 组合、无法解析时的保守兜底。
这些用例锁定「带分支的 DSL 不再被闸门误杀，且条件方向可真实验证」。

设计要点：闸门 run_staging_gate 的 entity_check 依赖实时 device_catalog.json，
而运行中的网关会后台刷新该文件 → 瞬时竞态使集成测试不稳定。
故集成部分直接测 _vg_evaluate_active_intents（闸门分支求值核心），
用受控合成 flow + 受控 world 字典，完全确定性、不依赖 catalog。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.gateway import (
    _vg_eval_jsonata_expr,
    _vg_split_outer,
    _vg_evaluate_active_intents,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
n_ok = 0
n_total = 0


def check(name, cond):
    global n_ok, n_total
    n_total += 1
    if cond:
        n_ok += 1
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}")


# ── 1) 子集求值器单元 ──────────────────────────────────────────────
def test_evaluator_unit():
    print("== 子集求值器单元 ==")
    m, k = _vg_eval_jsonata_expr("$number(lux) < 30", {"lux": 26})
    check("数值< 真", k and m)
    m, k = _vg_eval_jsonata_expr("$number(lux) < 10", {"lux": 26})
    check("数值< 假", k and not m)
    m, k = _vg_eval_jsonata_expr("$number(lux) >= 20", {"lux": 26})
    check("数值>= 真", k and m)
    m, k = _vg_eval_jsonata_expr("state = \"on\"", {"state": "on"})
    check("字符串= 真", k and m)
    m, k = _vg_eval_jsonata_expr("state != \"off\"", {"state": "on"})
    check("字符串!= 真", k and m)
    m, k = _vg_eval_jsonata_expr(
        "$number(lux) < 30 and $number(temp) > 10", {"lux": 26, "temp": 22})
    check("and 全真", k and m)
    m, k = _vg_eval_jsonata_expr(
        "$number(lux) < 30 and $number(temp) > 50", {"lux": 26, "temp": 22})
    check("and 含假 → 假", k and not m)
    m, k = _vg_eval_jsonata_expr(
        "$number(lux) < 5 or $number(temp) > 10", {"lux": 26, "temp": 22})
    check("or 一真 → 真", k and m)
    m, k = _vg_eval_jsonata_expr("occupied", {"occupied": True})
    check("裸变量真", k and m)
    m, k = _vg_eval_jsonata_expr("$exists(lux) and $custom(foo)", {"lux": 26})
    check("无法解析 → known=False", (not k) and (not m))
    parts = _vg_split_outer("$number(a) < 10 and $number(b) > 5", " and ")
    check("_vg_split_outer 顶层拆分", parts == ["$number(a) < 10", "$number(b) > 5"])


def _make_flow(condition="$number(lux) < 30", with_else=True):
    """受控合成 flow：触发→读 lux→switch(条件)→[THEN:调服务] / [ELSE:注释]。

    world 仅对 'sensor.lux' 返回受控值，其余为 None。
    """
    svc = {"id": "svc1", "type": "api-call-service", "domain": "light",
           "service": "turn_on", "entityId": ["light.x"], "data": "{}", "wires": [[]]}
    cmt = {"id": "cmt", "type": "comment", "wires": [[]]}
    rules = [{"t": "jsonata", "v": condition, "vt": "jsonata"}]
    if with_else:
        rules.append({"t": "else", "v": "true", "vt": "jsonata"})
    switch = {"id": "sw", "type": "switch", "property": "payload",
              "propertyType": "msg", "checkall": "true", "rules": rules,
              "outputs": len(rules),
              "wires": [["svc1"], []] if with_else else [["svc1"]]}
    read = {"id": "rd", "type": "api-current-state", "entity_id": "sensor.lux",
            "halt_if": "", "halt_if_type": "str", "halt_if_compare": "is",
            "outputs": 1,
            "outputProperties": [{"property": "lux", "propertyType": "msg",
                                  "value": "", "valueType": "entityState"}],
            "wires": [["sw"]]}
    trig = {"id": "trg", "type": "server-state-changed",
            "entities": {"entity": ["binary.x"], "substring": [], "regex": []},
            "wires": [["rd"]]}
    return {"nodes": [trig, read, switch, svc, cmt]}


def _world_factory(lux_value):
    def _w(eid):
        return lux_value if eid == "sensor.lux" else None
    return _w


# ── 2) 闸门分支求值：条件真 → 激活 THEN 服务 ──────────────────────
def test_active_intents_branch_true():
    print("== 分支求值：lux=10 <30 成立 → 激活 api-call-service ==")
    flow = _make_flow()
    active = _vg_evaluate_active_intents(flow, _world_factory("10"), None)
    check("svc1 被激活", "svc1" in active)
    check("comment 不被激活", "cmt" not in active)


# ── 3) 闸门分支求值：条件假 → 走 ELSE，不激活 THEN ────────────────
def test_active_intents_branch_false():
    print("== 分支求值：lux=100 <30 不成立 → 不激活 api-call-service ==")
    flow = _make_flow()
    active = _vg_evaluate_active_intents(flow, _world_factory("100"), None)
    check("svc1 未被激活（灯不开）", "svc1" not in active)
    # else 输出口 wires=[]（空），comment 无可达连线 → 无任何节点激活，正是期望：
    # 条件不满足时既不开灯、也不产生误激活。
    check("else 为空 → 无节点激活", "cmt" not in active and "svc1" not in active)


# ── 4) 无法解析的复杂 jsonata → 保守视为命中（不误杀） ────────────
def test_active_intents_fallback():
    print("== 分支求值：复杂 jsonata 兜底（保守命中，不误杀）==")
    flow = _make_flow(condition="$exists(lux) and $number(lux) < 30")
    warnings = []
    active = _vg_evaluate_active_intents(flow, _world_factory("10"), None, warnings)
    check("兜底仍激活 svc1（不误杀）", "svc1" in active)
    check("记录了兜底 warning", len(warnings) == 1)


def main():
    test_evaluator_unit()
    test_active_intents_branch_true()
    test_active_intents_branch_false()
    test_active_intents_fallback()
    print(f"\n结果: {n_ok}/{n_total} 通过")
    if n_ok != n_total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
