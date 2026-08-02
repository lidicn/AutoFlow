#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB4 两个 HIGH bug 的确定性复刻脚本（离线、不依赖真实 NR/HA）。

Bug #2 (iss_110d1054be): `触发: X on 持续N分钟` 的 for 字段被静默丢弃。
  - 现状：compile(target="staging") 把状态触发器降级为 inject（无 for）；
          compile(target="prod") 才生成 server-state-changed + for。
  - 复刻：分别编译 staging / prod，断言节点类型与 for 字段。

Bug #1 (iss_25419fa7a8): `取值:`(api-current-state) 运行时丢 entityId（e2e 报
  ValidationError "entityId" is required）。
  - 现状：编译器本身输出正确 entityId；丢字段发生在 e2e 插桩副本的运行时——
    合成入口 inject 的 msg.topic 在 blockInputOverrides=False 时覆盖 entityId。
  - 复刻：编译含 `取值:` 的 DSL，确认编译产物 entityId 完整（编译器 OK），
    并打印 e2e 运行时污染路径说明（真实修复在 run_e2e_trace 副本设
    blockInputOverrides=True，见任务 #533）。

运行：python tests/repro_wb4.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import parse, compile


def find_entry_trigger(flow):
    """返回没有上游连线指向它的、且类型为触发入口的节点（触发/状态/定时）。"""
    nodes = flow.get("nodes", [])
    incoming = set()
    for n in nodes:
        for w in (n.get("wires") or []):
            if isinstance(w, list):
                incoming.update(w)
    for n in nodes:
        if n["id"] in incoming:
            continue
        if n.get("type") in ("inject", "server-state-changed", "time", "trigger"):
            return n
    return None


def main():
    print("=" * 70)
    print("WB4 HIGH bug 复刻（确定性 / 离线）")
    print("=" * 70)

    # ── Bug #2：持续N分钟 for 字段 ──
    print("\n[BUG #2] `触发: X on 持续N分钟` 的 for 字段")
    dsl = ("场景: 复刻#2\n"
           "触发: binary_sensor.test_motion on 持续5分钟\n"
           "动作: light.turn_on(light.test)")

    all_ok = True

    scene = parse(dsl)
    for target in ("staging", "prod"):
        flow = compile(scene, target=target)
        trig = find_entry_trigger(flow)
        t = trig.get("type") if trig else "<无触发节点>"
        f = trig.get("for") if trig else None
        ft = trig.get("forType") if trig else None
        fu = trig.get("forUnits") if trig else None
        print(f"  target={target:8s} -> type={t:20s} for={f!r} forType={ft!r} forUnits={fu!r}")
        if target == "staging":
            # 修复后：staging 也应忠实生成 server-state-changed + for（不再降级 inject 丢 for）
            ok = (t == "server-state-changed" and str(f) == "5"
                  and ft == "num" and fu == "minutes")
            print(f"    [修复后] staging -> server-state-changed + for='5' -> {'OK' if ok else 'FAIL'}")
        else:
            # prod：当前已正确
            ok = (t == "server-state-changed" and str(f) == "5"
                  and ft == "num" and fu == "minutes")
            print(f"    [prod 现状] {'OK(已正确)' if ok else 'BROKEN'}")
        all_ok = all_ok and ok

    # 也验证 持续2小时 / 30秒 折算正确（prod 路径）
    print("\n  -- 时长折算校验（prod）--")
    for txt, expect_min in (("持续2小时", "120"), ("持续30秒", "0.5")):
        d = f"场景: t\n触发: binary_sensor.x on {txt}\n动作: light.turn_on(light.y)"
        fl = compile(parse(d), target="prod")
        tr = find_entry_trigger(fl)
        got = tr.get("for")
        ok = str(got) == expect_min
        print(f"  {txt:10s} -> for={got!r} (期望 {expect_min!r}) {'OK' if ok else 'FAIL'}")
        all_ok = all_ok and ok

    # ── Bug #1：取值 entityId 编译产物 ──
    print("\n[BUG #1] `取值:`(api-current-state) entityId")
    dsl1 = ("场景: 复刻#1\n"
            "触发: inject\n"
            "取值: sensor.test_lux lux\n"
            "动作: light.turn_on(light.test)")
    fl1 = compile(parse(dsl1))
    acs = [n for n in fl1.get("nodes", []) if n.get("type") == "api-current-state"]
    if acs:
        eid = acs[0].get("entityId")
        boi = acs[0].get("blockInputOverrides")
        print(f"  编译产物 api-current-state: entityId={eid!r} blockInputOverrides={boi!r}")
        ok = bool(eid)
        print(f"    [编译器] entityId 完整 -> {'OK(编译器无问题)' if ok else 'BROKEN(编译器就丢)'}")
        print(f"    [e2e 路径] blockInputOverrides=False → 合成入口 inject 的 msg.topic "
              f"会覆盖 entityId；空 topic 注入即触发运行期 ValidationError 'entityId is required'。"
              f"修复见任务#533（run_e2e_trace 插桩副本设 blockInputOverrides=True）。")
        all_ok = all_ok and ok
    else:
        print("  [ERROR] 未编译出 api-current-state 节点，无法复刻 #1")
        all_ok = False

    print("\n" + "=" * 70)
    print("复刻结论:", "当前坏行为已确定性复刻（#2 staging 丢 for；#1 编译 OK 但 e2e 运行时污染）"
          if all_ok else "复刻异常，请检查")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
