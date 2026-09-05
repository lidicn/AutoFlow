"""A1 条件自然写法 + delay 字段 回归测试。

锁死两类已修 bug，防止编译器静默回退：
  A1  条件自然写法（entity=value / entity!=value / 数值）必须编译为 api-current-state
      read-state 节点（不门控，state_location="data"）+ 下游 switch 按 payload 路由；
      绝不能生成含裸实体名 / $state() 的坏 JSONata switch（会静默不触发）。
  A4  delay 节点必须带齐 NR 标准默认字段（rate/nbRateUnits/rateUnits/randomFirst/
      randomLast/randomUnits/drop/allowrate），否则 NR 编辑器打红三角。

运行：python tests/test_compiler_regression_a1.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.flow_linter import lint_flow

DELAY_REQUIRED = {
    "rate", "nbRateUnits", "rateUnits", "randomFirst",
    "randomLast", "randomUnits", "drop", "allowrate",
}


def _compile(text, target="staging"):
    scene = parse(text)
    return compile(scene, target=target)


def _acs_nodes(flow):
    return [n for n in flow["nodes"] if n["type"] == "api-current-state"]


def _switches(flow):
    return [n for n in flow["nodes"] if n["type"] == "switch"]


CASES = []


def _case(name, dsl):
    CASES.append((name, dsl))


_case("natural_eq", """场景: 自然等式
触发: binary_sensor.motion 变化
条件: binary_sensor.door=off
动作: light.turn_on(light.test)
""")

_case("negation", """场景: 否定
触发: binary_sensor.motion 变化
条件: binary_sensor.door!=off
动作: light.turn_on(light.test)
""")

_case("numeric", """场景: 数值
触发: binary_sensor.motion 变化
条件: input_number.foo=5
动作: light.turn_on(light.test)
""")

_case("and_chain", """场景: 多重AND
触发: binary_sensor.motion 变化
条件: binary_sensor.door=off
条件: binary_sensor.window=on
时间段: 20:00-23:00
动作: light.turn_on(light.test)
""")

_case("delay_fields", """场景: 延时字段
触发: binary_sensor.motion 变化
动作: light.turn_on(light.test)
延时: 120 秒
动作: light.turn_off(light.test)
""")


def main():
    failed = False
    for name, dsl in CASES:
        try:
            flow = _compile(dsl)
        except Exception as e:
            print(f"❌ {name}: 编译抛异常 {type(e).__name__}: {e}")
            failed = True
            continue
        raw = json.dumps(flow, ensure_ascii=False)
        bad_jsonata = bool(re.search(r"\$state\s*\(", raw))
        r13 = [i for i in lint_flow(flow) if i.get("rule") == "R13"]

        if name in ("natural_eq", "negation", "numeric", "and_chain"):
            acs = _acs_nodes(flow)
            sws = _switches(flow)
            # 新设计(#634/FEEDBACK #8): 条件 → read-state api-current-state(不门控) + 下游路由 switch。
            # 关键不变量: 绝不出现 $state() 坏 jsonata switch(R13=0, 无 "$state(")。
            ok = (len(acs) >= 1) and (len(sws) >= 1) and (not bad_jsonata) and (len(r13) == 0)
            if ok:
                print(f"✅ {name}: 条件→api-current-state ({len(acs)}个) 无坏JSONata R13=0")
            else:
                failed = True
                print(f"❌ {name}: acs={len(acs)} switch={len(sws)} "
                      f"坏jsonata={bad_jsonata} R13={len(r13)}")
                for n in acs:
                    print(f"     ACS {n['name']!r} halt_if={n.get('halt_if')!r} "
                          f"type={n.get('halt_if_type')} compare={n.get('halt_if_compare')}")
            # 额外断言具体语义（新设计: read-state 而非门控）
            if name == "natural_eq" and acs:
                n = acs[0]
                # 条件 → read-state 节点(不门控，由下游 switch 路由)
                if not (n.get("outputs") == 1 and n.get("halt_if") == ""
                        and n.get("state_location") == "data"
                        and n.get("halt_if_compare") == "is"):
                    failed = True
                    print(f"❌ natural_eq: 期望 read-state(无门控) 节点 compare=is，实得 "
                          f"outputs={n.get('outputs')} halt_if={n.get('halt_if')!r} "
                          f"state_location={n.get('state_location')!r} compare={n.get('halt_if_compare')!r}")
            if name == "negation" and acs:
                n = acs[0]
                if n.get("halt_if_compare") != "is_not":
                    failed = True
                    print(f"❌ negation: 期望 compare=is_not，实得 {n.get('halt_if_compare')!r}")
            if name == "numeric" and acs:
                n = acs[0]
                if n.get("halt_if_type") != "num":
                    failed = True
                    print(f"❌ numeric: 期望 halt_if_type=num，实得 {n.get('halt_if_type')!r}")
            if name == "and_chain":
                if len(acs) != 2:
                    failed = True
                    print(f"❌ and_chain: 期望 2 个 read-state 条件节点，实得 {len(acs)}")

        elif name == "delay_fields":
            delays = [n for n in flow["nodes"] if n["type"] == "delay"]
            if not delays:
                failed = True
                print("❌ delay_fields: 未生成 delay 节点")
                continue
            missing = {k: (k not in delays[0]) for k in DELAY_REQUIRED}
            miss = [k for k, v in missing.items() if v]
            if not miss and len(r13) == 0:
                print(f"✅ delay_fields: 8 个标准字段齐全 R13=0")
            else:
                failed = True
                print(f"❌ delay_fields: 缺字段 {miss} R13={len(r13)}")

    if failed:
        print("\n存在回归 ❌")
        raise SystemExit(1)
    print("\nA1 条件编译 + delay 字段 回归全绿 🎉")


if __name__ == "__main__":
    main()
