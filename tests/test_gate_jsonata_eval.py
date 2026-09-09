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
    _sanitize_trigger_state,
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
    # F-R5-02：裸数值比较（msg. 前缀、无 $number 包裹）——R5 t2 实况形态
    m, k = _vg_eval_jsonata_expr("msg.温度 > 28", {"温度": 30.5})
    check("裸数值> 真（msg.前缀中文键）", k and m)
    m, k = _vg_eval_jsonata_expr("msg.温度 > 28", {"温度": 26.7})
    check("裸数值> 假", k and not m)
    m, k = _vg_eval_jsonata_expr("温度 <= 26", {"温度": 26})
    check("裸数值<= 边界真", k and m)
    m, k = _vg_eval_jsonata_expr("msg.湿度 < 60 and msg.温度 > 20", {"湿度": 55, "温度": 25})
    check("裸数值 and 组合", k and m)
    m, k = _vg_eval_jsonata_expr("msg.温度 > 28", {"other": 1})
    check("变量缺失 → known=False 兜底不误杀", (not k) and (not m))
    m, k = _vg_eval_jsonata_expr("门状态 == on", {"门状态": "on"})
    check("无引号字符串 == 真", k and m)
    m, k = _vg_eval_jsonata_expr("门状态 == off", {"门状态": "on"})
    check("无引号字符串 == 假", k and not m)
    m, k = _vg_eval_jsonata_expr("state == \"on\"", {"state": "on"})
    check("== 带引号字符串 真", k and m)
    # F-R6：编译器写 payload.<label>、分支读 msg.<label> → 求值器回退 payload 路径
    m, k = _vg_eval_jsonata_expr("msg.温度 > 28", {"payload": {"温度": 30.5}})
    check("payload 回退：msg.标签 数值真", k and m)
    m, k = _vg_eval_jsonata_expr("msg.温度 > 28", {"payload": {"温度": "26.7"}})
    check("payload 回退：字符串数值假向", k and not m)
    m, k = _vg_eval_jsonata_expr("温度 < 24", {"payload": {"温度": 20}})
    check("payload 回退：裸变量", k and m)
    m, k = _vg_eval_jsonata_expr("msg.状态 == on", {"payload": {"状态": "on"}})
    check("payload 回退：无引号字符串", k and m)
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
    test_sanitize_trigger_state()
    test_fires_numeric_condition()
    print(f"\n结果: {n_ok}/{n_total} 通过")
    if n_ok != n_total:
        raise SystemExit(1)


# ── 5) F-R6.5：触发条件原文净化（数值条件 → 满足条件的合成值） ──────
def test_sanitize_trigger_state():
    print("== F-R6.5 触发态净化：数值条件原文 → 合成值 ==")
    check("> 28 → '29'", _sanitize_trigger_state("> 28") == "29")
    check("< 24 → '23'", _sanitize_trigger_state("< 24") == "23")
    check("<= 24 → '24'（边界）", _sanitize_trigger_state("<= 24") == "24")
    check(">= 30 → '30'（边界）", _sanitize_trigger_state(">= 30") == "30")
    check("== on 误形？不——== 数值 → 本值", _sanitize_trigger_state("== 27") == "27")
    check("!= 26 → '27'", _sanitize_trigger_state("!= 26") == "27")
    check("裸数值 26.7 原样", _sanitize_trigger_state("26.7") == "26.7")
    check("字符串 on 原样", _sanitize_trigger_state("on") == "on")
    check("changed 原样", _sanitize_trigger_state("changed") == "changed")
    check("小数合成值 25.5", _sanitize_trigger_state("> 24.5") == "25.5")


def _make_num_flow(if_state="> 28"):
    """受控合成 flow：数值条件触发 → 调服务（无中间取值，直测 _fires 语义）。"""
    svc = {"id": "svc1", "type": "api-call-service", "domain": "light",
           "service": "turn_on", "entityId": ["light.x"], "data": "{}", "wires": [[]]}
    trig = {"id": "trg", "type": "server-state-changed", "ifState": if_state,
            "entities": {"entity": ["sensor.temperature"], "substring": [], "regex": []},
            "wires": [["svc1"]]}
    return {"nodes": [trig, svc]}


def _temp_world(v):
    def _w(eid):
        return v if eid == "sensor.temperature" else None
    return _w


# ── 6) F-R6.5：_fires 数值条件求值（世界态为合成数值时按数值语义判定） ──
def test_fires_numeric_condition():
    print("== F-R6.5 _fires 数值求值：'> 28' 触发条件 ==")
    flow = _make_num_flow("> 28")
    active = _vg_evaluate_active_intents(flow, _temp_world("29"), None)
    check("世界态 29 > 28 → 激活", "svc1" in active)
    active = _vg_evaluate_active_intents(flow, _temp_world("26.7"), None)
    check("世界态 26.7 不满足 → 不激活", "svc1" not in active)
    # 修复前实锤形态：世界态被条件原文污染时必须 fail-closed（不激活、不误判）
    active = _vg_evaluate_active_intents(flow, _temp_world("> 28"), None)
    check("世界态为条件原文 → fail-closed 不激活", "svc1" not in active)
    # 字符串条件不受影响（回归保护）
    flow_on = _make_num_flow("on")
    active = _vg_evaluate_active_intents(flow_on, _temp_world("on"), None)
    check("字符串条件 == 原样比对仍有效", "svc1" in active)


if __name__ == "__main__":
    main()
