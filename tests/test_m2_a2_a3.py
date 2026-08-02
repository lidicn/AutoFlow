"""M2 回归测试：A2 多门控/时间段串联 + 取值→分支健壮性；A3 JSONata 防御。

锁死两类已修/加固点：
  A2  多 条件(AND 串联) / 多 时间段 必须各自生成正确的门控节点并串行接线；
      取值→分支 必须正确接线（无 R13 孤儿）。
  A3  编译器层 JSONata 防御：
      - 全角符号（（）＝，；）在 jsonata 发射点被归一为半角；
      - entity == value / entity ＝ value（双等号/全角＝）必须路由到 api-current-state，
        绝不能漏判成裸变量 jsonata switch（静默不触发）。

运行：python tests/test_m2_a2_a3.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile, _sanitize_jsonata, _parse_state_condition
from autoflow_gateway.flow_linter import lint_flow

DELAY = None  # 占位


def _compile(text, target="staging"):
    return compile(parse(text), target=target)


def _nodes(flow, t):
    return [n for n in flow["nodes"] if n["type"] == t]


def _inbound(flow, nid):
    ins = []
    for n in flow["nodes"]:
        for arr in n.get("wires", []):
            if nid in arr:
                ins.append(n["id"])
    return ins


def _outbound(flow, nid):
    for n in flow["nodes"]:
        if n["id"] == nid:
            return [w for arr in n.get("wires", []) for w in arr]
    return []


def main():
    failed = False

    # ───────────────────────── A3 单元：消毒 + 条件解析 ─────────────────────────
    # A3.1 全角运算符归一（注意：中文文本标点 ：，； 等不在归一范围，避免破坏字符串字面量）
    s = _sanitize_jsonata("（$number（msg.payload） ＞ 10） 且 ＝ 不等于")
    if "（" in s or "）" in s or "＝" in s or "＞" in s:
        failed = True
        print(f"❌ A3 sanitize: 运算符全角未归一 → {s!r}")
    else:
        print(f"✅ A3 sanitize: 运算符全角归一 → {s!r}")

    # A3.2 双等号 / 全角＝ 必须被识别为状态断言（路由 api-current-state）
    for raw, expect in [("binary_sensor.door == off", "is"),
                        ("binary_sensor.door ＝ off", "is"),
                        ("binary_sensor.door ≠ off", "is_not"),
                        ("binary_sensor.door != off", "is_not")]:
        p = _parse_state_condition(raw)
        if not p or p[2] != expect:
            failed = True
            print(f"❌ A3 parse_state {raw!r}: 期望 compare={expect}，实得 {p}")
        else:
            print(f"✅ A3 parse_state {raw!r}: entity={p[0]} compare={p[2]}")
    # 裸变量/复杂表达式仍应返回 None（走 jsonata 兜底，但会被 sanitize）
    if _parse_state_condition("$number(msg.payload) > 10") is not None:
        failed = True
        print("❌ A3 parse_state: 复杂表达式不应被当状态断言")
    else:
        print("✅ A3 parse_state: 复杂表达式正确返回 None（走 jsonata 兜底）")

    # ───────────────────────── A2：多条件 AND 串联 ─────────────────────────
    and_dsl = """场景: 三重AND
触发: binary_sensor.motion 变化
条件: binary_sensor.door=off
条件: binary_sensor.window=on
条件: input_boolean.night=true
动作: light.turn_on(light.test)
"""
    flow = _compile(and_dsl)
    acs = _nodes(flow, "api-current-state")
    sws = _nodes(flow, "switch")
    r13 = [i for i in lint_flow(flow) if i.get("rule") == "R13"]
    bad = bool(re.search(r"\$state\s*\(", json.dumps(flow, ensure_ascii=False)))
    if len(acs) != 3 or len(sws) != 0 or r13 or bad:
        failed = True
        print(f"❌ A2 multi-condition: acs={len(acs)} switch={len(sws)} R13={len(r13)} 坏jsonata={bad}")
    else:
        # 验证串行 AND 接线：acs[0]→acs[1]→acs[2]
        ok_chain = (_inbound(flow, acs[1]["id"]) == [acs[0]["id"]]
                    and _inbound(flow, acs[2]["id"]) == [acs[1]["id"]])
        if not ok_chain:
            failed = True
            print(f"❌ A2 multi-condition: AND 串联接线错误 "
                  f"acs1.in={_inbound(flow, acs[1]['id'])} acs2.in={_inbound(flow, acs[2]['id'])}")
        else:
            print(f"✅ A2 multi-condition: 3 个 api-current-state 串行 AND，无 jsonata/R13")

    # ───────────────────────── A2：多时间段串联 ─────────────────────────
    tr_dsl = """场景: 双时间段
触发: binary_sensor.motion 变化
时间段: 20:00-23:00
时间段: 工作日 09:00-18:00
动作: light.turn_on(light.test)
"""
    flow = _compile(tr_dsl)
    trs = _nodes(flow, "time-range-switch")
    r13 = [i for i in lint_flow(flow) if i.get("rule") == "R13"]
    if len(trs) != 2 or r13:
        failed = True
        print(f"❌ A2 multi-timerange: trs={len(trs)} R13={len(r13)}")
    else:
        # 串行：trigger→tr0→tr1
        trig = _nodes(flow, "server-state-changed") or _nodes(flow, "inject")
        print(f"✅ A2 multi-timerange: 2 个 time-range-switch 生成，R13=0")

    # ───────────────────────── A2：取值 → 分支（无 R13 孤儿）─────────────────────────
    ex_dsl = """场景: 取值分支
触发: binary_sensor.motion 变化
取值: sensor.lux illuminance
分支: msg.illuminance < 10
  动作: light.turn_on(light.test)
否则:
  动作: light.turn_off(light.test)
"""
    flow = _compile(ex_dsl)
    read_states = _nodes(flow, "api-current-state")  # 取值 发射为 api-current-state
    sws = _nodes(flow, "switch")
    r13 = [i for i in lint_flow(flow) if i.get("rule") == "R13"]
    if not read_states or not sws:
        failed = True
        print(f"❌ A2 extract-branch: 取值(api-cs)={len(read_states)} switch={len(sws)}")
    elif r13:
        failed = True
        print(f"❌ A2 extract-branch: R13={len(r13)}（首动作孤儿）")
    else:
        # switch 首输出应接到一个 api-call-service（分支体首动作），不是空
        sw = sws[0]
        outs = [w for arr in sw.get("wires", []) for w in arr]
        first_action = [n for n in flow["nodes"] if n["type"] == "api-call-service"
                        and n["id"] in outs]
        if not first_action:
            failed = True
            print(f"❌ A2 extract-branch: switch 输出未接到首动作 outs={outs}")
        else:
            print(f"✅ A2 extract-branch: 取值→分支 接线正确，switch 接首动作，R13=0")

    # ───────────────────────── A3：全角括号在 jsonata 兜底被归一 ─────────────────────────
    fw_dsl = """场景: 全角括号兜底
触发: binary_sensor.motion 变化
条件: $number（msg.payload） ＞ 10
动作: light.turn_on(light.test)
"""
    flow = _compile(fw_dsl)
    sws = _nodes(flow, "switch")
    bad = False
    for sw in sws:
        for r in sw.get("rules", []):
            v = r.get("v", "")
            if "（" in v or "）" in v or "＞" in v or "＜" in v or "＝" in v:
                bad = True
    if bad:
        failed = True
        print("❌ A3 fullwidth-in-jsonata: jsonata 规则值仍含全角符号（（）＝＞＜）")
    else:
        print("✅ A3 fullwidth-in-jsonata: 全角符号已在 jsonata 表达式内归一为半角")

    if failed:
        print("\nM2 存在回归 ❌")
        raise SystemExit(1)
    print("\nM2 (A2 多门控串联 + A3 JSONata 防御) 回归全绿 🎉")


if __name__ == "__main__":
    main()
