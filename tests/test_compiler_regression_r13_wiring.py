"""R13 孤儿节点 回归测试 —— 锁定「分支/否则 体首节点孤儿」编译器连线 bug。

历史：_emit_body 只返回 tail（末节点），而 _emit_switch / _emit_current_state 的否则分支
把上游(switch/current-state)输出连到了 tail，导致分支体第一个 api-call-service 永远收不到
上游连线 → 成为孤儿节点（R13 抓出的 22 例真实提案皆是此因，灯/空调实际永不触发）。

修复：_emit_body 改为返回 (head, tail)，分支/否则 必须把上游输出连到 head。

运行：python tests/test_compiler_regression_r13_wiring.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.flow_linter import lint_flow


def _compile(text, target="staging"):
    return compile(parse(text), target=target)


def _inbound(flow):
    """node_id -> [上游 node_id,...]（含 link out→link in 的 links 感知）。"""
    by_id = {n["id"]: n for n in flow["nodes"]}
    inbound = {nid: [] for nid in by_id}
    for n in flow["nodes"]:
        for arr in (n.get("wires") or []):
            for t in arr:
                if t in inbound:
                    inbound[t].append(n["id"])
        if n.get("type") == "link out":
            for lk in (n.get("links") or []):
                if lk in inbound:
                    inbound[lk].append(n["id"] + "(linkout)")
    return inbound


def _trace_to_action(flow, start_nid, out_idx=0):
    """从 start_nid 的 out_idx 输出出发，顺着 wires 穿过 switch 直到第一个 api-call-service。"""
    seen = set()
    cur = start_nid
    idx = out_idx
    while cur and cur not in seen:
        seen.add(cur)
        nd = _by_id(flow, cur)
        if nd is None:
            return None
        if nd["type"] == "api-call-service":
            return nd
        wires = nd.get("wires") or [[]]
        if idx < len(wires) and wires[idx]:
            nxt = wires[idx][0]
            # 顺着同一输出继续；若下个还是 switch，保持 out_idx=0
            cur, idx = nxt, 0
        else:
            return None
    return None


def _by_id(flow, nid):
    for n in flow["nodes"]:
        if n["id"] == nid:
            return n
    return None


def _by_type(flow, t):
    return [n for n in flow["nodes"] if n["type"] == t]


CASES = []
def _case(name, dsl):
    CASES.append((name, dsl))


# 分支体：首动作 + 次动作 + 子流程（历史失败模式：switch 错连到子流程而非首动作）
_case("branch_2act_subflow", """场景: 分支首动作孤儿
触发: binary_sensor.motion 变化
取值: sensor.lux lux
分支: $number(lux) < 15
  动作: light.turn_on(light.a, brightness_pct=80)
  动作: light.turn_on(light.b)
  调用子流程: demo_notify(text=欢迎)
否则:
  动作: light.turn_on(light.c)
""")

# 分支体：首动作 + 延时 + 次动作（分支+延时 历史失败模式）
_case("branch_act_delay", """场景: 分支首动作+延时
触发: binary_sensor.motion 变化
分支: $number(payload) < 5
  动作: light.turn_on(light.a)
  延时: 60 秒
  动作: light.turn_off(light.a)
否则:
  动作: light.turn_on(light.b)
""")

# 查询→否则：current_state 否则分支首节点孤儿
_case("current_state_else", """场景: 查询否则首动作孤儿
触发: binary_sensor.motion 变化
查询: binary_sensor.door on
分支: $number(payload) < 10
  动作: light.turn_on(light.a)
否则:
  动作: light.turn_on(light.b)
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
        r13 = [i for i in lint_flow(flow) if i.get("rule") == "R13"]
        inbound = _inbound(flow)
        notes = []

        # 1) 全局：任何 api-call-service 都不许孤儿（R13 硬保证）
        if r13:
            failed = True
            notes.append(f"R13={len(r13)}")

        # 2) 分支首动作：分支 switch 输出0 必须直达一个 api-call-service，且该动作有入边
        sw = _by_type(flow, "switch")
        for s in sw:
            head0 = _trace_to_action(flow, s["id"], 0)
            if head0 is None or head0["type"] != "api-call-service":
                failed = True
                notes.append(f"分支 switch 输出0 未直达 api-call-service(实得 {head0['type'] if head0 else None})")
            elif not inbound.get(head0["id"]):
                failed = True
                notes.append("分支首动作仍孤儿(无入边)")

        # 3) 否则首动作：所有 2 输出 gate（switch / api-current-state）的最后一个输出
        #    若连到 api-call-service，该动作必须有入边
        for g in (sw + _by_type(flow, "api-current-state")):
            wires = g.get("wires") or [[]]
            if len(wires) >= 2 and wires[-1]:
                else_head = _trace_to_action(flow, g["id"], len(wires) - 1)
                if else_head is not None and else_head["type"] == "api-call-service":
                    if not inbound.get(else_head["id"]):
                        failed = True
                        notes.append(f"否则首动作孤儿(无入边, gate={g['name']})")

        if notes:
            print(f"❌ {name}: {'; '.join(notes)}")
        else:
            print(f"✅ {name}: 分支首动作/否则首动作 均正确连线，R13=0")
    if failed:
        print("\n存在回归 ❌")
        raise SystemExit(1)
    print("\nR13 分支/否则 首节点连线 回归全绿 🎉")


if __name__ == "__main__":
    main()
