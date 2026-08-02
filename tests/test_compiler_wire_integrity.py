"""编译器连线完整性（wire-integrity）回归测试。

锁定两类历史/当前连线缺陷：
  - 悬空端口：节点 wires 外层长度 != outputs（典型如 server-state-changed 写死
    outputs=2 但只用了 port0，port1 永远空 → NR 编辑器显示悬空线头，视觉像「线没连到」）。
  - 时间门被旁路：触发节点与时间门平行直连同一终端动作，导致时间段约束失效。

运行：python tests/test_compiler_wire_integrity.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.flow_linter import lint_flow


def _compile(text, target="prod"):
    return compile(parse(text), target=target)


def _by_type(flow, t):
    return [n for n in flow["nodes"] if n.get("type") == t]


def _inbound(flow):
    """node_id -> [上游 node_id,...]。"""
    by_id = {n["id"]: n for n in flow["nodes"]}
    inbound = {nid: [] for nid in by_id}
    for n in flow["nodes"]:
        for arr in (n.get("wires") or []):
            for t in arr:
                if t in inbound:
                    inbound[t].append(n["id"])
    return inbound


def _entry_types():
    return {"inject", "server-state-changed", "link in", "comment"}


def _bfs_reachable(flow):
    """从所有 entry 节点出发 BFS，返回可达节点 id 集合。"""
    entries = [n["id"] for n in flow["nodes"] if n.get("type") in _entry_types() and n.get("type") != "comment"]
    if not entries:
        # 退路：若一个 entry 都没有（极异常），用第一个节点
        entries = [flow["nodes"][0]["id"]] if flow["nodes"] else []
    reachable = set(entries)
    frontier = list(entries)
    while frontier:
        cur = frontier.pop()
        nd = next((n for n in flow["nodes"] if n["id"] == cur), None)
        if nd is None:
            continue
        for arr in (nd.get("wires") or []):
            for t in arr:
                if t not in reachable:
                    reachable.add(t)
                    frontier.append(t)
    return reachable


CASES = []
def _case(name, dsl, target="prod"):
    CASES.append((name, dsl, target))


# 1) 普通触发+动作：SSC 必须 outputs=1、wires 维度正确
_case("trigger_action", """场景: 有人开灯
触发: binary_sensor.motion on
动作: light.turn_on(light.study_desk)
""")

# 2) 触发+时间段+动作：SSC 单输出，且 SSC 不可平行直连动作（时间门不可旁路）
_case("trigger_timer_action", """场景: 工作日晚8到11点有人开吊灯
触发: binary_sensor.motion on
时间段: 每天 20:00-23:00
动作: light.turn_on(light.chandelier)
""")

# 3) 嵌套 DSL（触发含时间段作为嵌套 body）
_case("trigger_nested_timer", """场景: 书房夜间有人开台灯
触发: binary_sensor.motion on
  时间段: 每天 23:00-06:00
  动作: light.turn_on(light.desk_lamp)
动作: light.turn_on(light.ambient)
""")

# 4) 分支结构（复用 R13 场景，确保多分支不引入悬空/孤儿）
_case("branch_two_actions", """场景: 分支首动作孤儿
触发: binary_sensor.motion 变化
取值: sensor.lux lux
分支: $number(lux) < 15
  动作: light.turn_on(light.a, brightness_pct=80)
  动作: light.turn_on(light.b)
否则:
  动作: light.turn_on(light.c)
""")

# 5) 多分支 fan-out（并行块）
_case("parallel_fanout", """场景: 有人并行开多设备
触发: binary_sensor.motion on
并行:
  动作: light.turn_on(light.a)
  动作: light.turn_on(light.b)
  动作: switch.turn_on(switch.c)
""")

# 6) staging target：状态触发编译为 server-state-changed（含 for），同样要维度正确
_case("staging_inject", """场景: staging定时开灯
触发: binary_sensor.motion on
动作: light.turn_on(light.study_desk)
""", target="staging")


def main():
    failed = False
    for name, dsl, target in CASES:
        try:
            flow = _compile(dsl, target=target)
        except Exception as e:
            print(f"❌ {name}: 编译抛异常 {type(e).__name__}: {e}")
            failed = True
            continue

        notes = []
        nodes = flow["nodes"]

        # WI-1：wires 维度 == outputs（通用守卫，抓悬空端口）
        for n in nodes:
            w = n.get("wires")
            if w is None:
                continue
            exp = n.get("outputs", 1)
            if len(w) != exp:
                failed = True
                notes.append(f"维度不一致 {n.get('type')}({n['id']}): wires={len(w)} != outputs={exp}")

        # WI-2：server-state-changed 必须 outputs==1（本次 bug 回归锁）
        for n in _by_type(flow, "server-state-changed"):
            if n.get("outputs") != 1:
                failed = True
                notes.append(f"SSC {n['id']} outputs={n.get('outputs')} != 1（悬空端口回归）")

        # WI-3：无孤儿（所有非 comment 节点须从触发可达）
        reachable = _bfs_reachable(flow)
        inbound = _inbound(flow)
        for n in nodes:
            if n.get("type") == "comment":
                continue
            if n["id"] not in reachable and not inbound.get(n["id"]):
                failed = True
                notes.append(f"孤儿节点 {n.get('type')}({n['id']}) 无入边且不可达")

        # WI-4：单触发 + 单时间门 场景，触发 out0 只能连时间门（不可旁路直连动作）
        ssc_or_inject = _by_type(flow, "server-state-changed") + _by_type(flow, "inject")
        timers = _by_type(flow, "time-range-switch")
        if len(ssc_or_inject) == 1 and len(timers) == 1:
            src = ssc_or_inject[0]
            t = timers[0]
            src_wires = (src.get("wires") or [[]])[0]
            if t["id"] not in src_wires:
                failed = True
                notes.append(f"触发 {src['id']} 未连接时间门 {t['id']}（链路断裂）")
            elif len(src_wires) != 1:
                failed = True
                notes.append(f"触发 {src['id']} 平行直连 {len(src_wires)} 个目标（时间门被旁路）：{src_wires}")

        if notes:
            print(f"❌ {name}: {'; '.join(notes)}")
        else:
            print(f"✅ {name}: wires 维度一致 / SSC 单输出 / 无孤儿 / 时间门未旁路")
    if failed:
        print("\n存在连线完整性回归 ❌")
        raise SystemExit(1)
    print("\n编译器连线完整性（wire-integrity）全绿 🎉")


if __name__ == "__main__":
    main()
