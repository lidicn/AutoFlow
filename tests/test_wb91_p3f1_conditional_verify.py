# -*- coding: utf-8 -*-
"""
WB91 · P3-F1 / P3-F2 回归守卫。

P3-F1（dry-run 后置条件假阳性）：verify_flow 对「含反向切换 else 的合法条件流」
硬拦——因 old 断言逻辑把两个分支的后置条件(on/off)都拿去比对同一设备态，
设备不可能同时 on 又 off → 必有一项失败 → 整流硬拦。
P3-F2（双闸方向矛盾）：同一份流 propose 放行、verify 拦截。

修复（gateway.run_staging_gate）：分支感知后置条件断言——来自「未激活分支」的
(entity_id, state) 收为 inactive_effects，断言时跳过（不计入失败）。device 处于
某一态时，未执行分支的后置条件本就不可达，跳过才是正确结论；已命中分支的后置
条件仍严格断言（不降级安全性）。

覆盖：
1) 条件流（暗→开灯 / 亮→关灯，含反向 else）→ verify 路径(passed=True)，不再 block。
2) P3-F2 方向一致：propose 路径(dsl=) 与 verify 路径(flow=, 自动提取两分支期望)
   对同份条件流给出一致 verdict（都 pass），不再相互矛盾。
3) 安全不降级：无条件流 + 调用方显式错期望 → 仍硬拦（branch_inactive 不弱化真失败）。
4) 真实 bug 仍被抓：后置条件指向「未被任何节点触碰的实体」仍判失败（非分支可解释）。

全程离线、零运行时副作用。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_wb91_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.gateway import _auto_expected_from_nodes

GW = G.Gateway()
for _eid in ("light.study_main", "light.wrong", "binary_sensor.study_lux",
            "binary_sensor.study_pc"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


SEED_LIGHT = (("light.study_main", "书房主灯", "书房", "off", {}),)
SEED_LIGHT_ON = (("light.study_main", "书房主灯", "书房", "on", {}),)

# S5：暗→开灯 / 亮(否则)→关灯，含反向切换 else。WB91 报告核心用例。
# 用 inject payload 直接驱动分支（与 test_c4_replay DSL_BRANCH_ON 同构，replay 追踪可命中），
# 避免取值节点在追踪里的接线时序问题干扰本测试（本测试只验证「两分支期望不再误杀」）。
S5 = """场景: 书房条件灯
触发: inject(payload={"lux":"暗"})
分支 payload.lux = "暗":
    动作: light.turn_on(light.study_main, brightness=80)
否则:
    动作: light.turn_off(light.study_main)
"""

# 无条件流（安全不降级基准，显式传错期望）
S_UNCOND = """场景: 无条件开灯
触发: inject
动作: light.turn_on(light.study_main, brightness=80)
"""

# 开灯（真实 bug 仍须抓：显式传 orphan 期望）
S_ORPHAN = """场景: 开灯
触发: inject
动作: light.turn_on(light.study_main, brightness=80)
"""


def _compile(dsl):
    return compile(parse(dsl), target="staging")


def test_p3f1_conditional_flow_verify_passes():
    """条件流(含反向 else)经 verify 内部路径(auto 提取两分支期望) → passed=True，不再 block。"""
    flow = _compile(S5)
    store = _vhass_with(*SEED_LIGHT)
    expected_auto, _ = _auto_expected_from_nodes(flow["nodes"])
    # 自动提取应含两个分支后置条件(on/off)
    states = {c["state"] for c in expected_auto}
    assert states == {"on", "off"}, expected_auto
    gate = GW.run_staging_gate(dsl="", expected=expected_auto, flow=flow,
                               vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate
    assert gate["verdict"] == "放行", gate
    # 未激活分支的后置条件应被标 [跳过]（branch_inactive），而非 [未过]
    skipped = [a for a in gate["assertions"] if a.get("branch_inactive")]
    assert skipped, f"应有未激活分支跳过标记：{gate['assertions']}"
    # 已命中分支(on)仍被断言且通过
    hit = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"
           and a.get("expected") == "on" and not a.get("branch_inactive")]
    assert hit and hit[0]["ok"] is True, gate["assertions"]


def test_p3f1_conditional_flow_verify_passes_when_device_on():
    """设备当前 on + lux 亮 → 否则关灯分支命中，开灯分支未激活 → 仍 passed（对称验证）。"""
    s5_on = S5.replace('payload={"lux":"暗"}', 'payload={"lux":"亮"}')
    flow = _compile(s5_on)
    store = _vhass_with(*SEED_LIGHT_ON)
    expected_auto, _ = _auto_expected_from_nodes(flow["nodes"])
    gate = GW.run_staging_gate(dsl="", expected=expected_auto, flow=flow,
                               vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate
    assert gate["verdict"] == "放行", gate
    skipped = [a for a in gate["assertions"] if a.get("branch_inactive")]
    assert skipped, f"应有未激活分支跳过标记：{gate['assertions']}"


def test_p3f2_propose_verify_direction_consistent():
    """P3-F2：propose 路径(dsl=, 调用方期望) 与 verify 路径(flow=, auto 两分支期望)
    对同份条件流给出一致 verdict（都 pass），不再相互矛盾。"""
    flow = _compile(S5)
    store = _vhass_with(*SEED_LIGHT)
    expected_auto, _ = _auto_expected_from_nodes(flow["nodes"])
    # propose 路径：调用方只声明命中分支期望
    g_propose = GW.run_staging_gate(S5, [{"entity_id": "light.study_main", "state": "on"}],
                                    vhass_store=store, branch_aware=True)
    # verify 路径：自动提取两分支期望
    g_verify = GW.run_staging_gate(dsl="", expected=expected_auto, flow=flow,
                                   vhass_store=store, branch_aware=True)
    assert g_propose["passed"] == g_verify["passed"], \
        f"双闸方向矛盾：propose={g_propose['passed']} verify={g_verify['passed']}"
    assert g_propose["verdict"] == g_verify["verdict"], \
        f"双闸 verdict 不一致：propose={g_propose['verdict']} verify={g_verify['verdict']}"


def test_p3f1_safety_unconditional_wrong_still_blocked():
    """安全不降级：无条件流 + 调用方显式传错期望(off) → 仍硬拦（branch_inactive 不弱化真失败）。"""
    flow = _compile(S_UNCOND)
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(dsl="", expected=[{"entity_id": "light.study_main", "state": "off"}],
                               flow=flow, vhass_store=store, branch_aware=True)
    assert gate["passed"] is False, gate
    assert gate["verdict"] == "拦截", gate
    # 失败项不应被误标 branch_inactive（它不是未激活分支产物）
    assert not any(a.get("branch_inactive") for a in gate["assertions"]), gate["assertions"]


def test_p3f1_orphan_expected_still_fails():
    """真实 bug 仍被抓：后置条件指向「未被任何节点触碰的实体」(light.wrong) →
    该 (eid,state) 既非 active 也非 inactive 分支产物 → 仍断言并失败。"""
    flow = _compile(S_ORPHAN)
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(
        dsl="", expected=[{"entity_id": "light.study_main", "state": "on"},
                          {"entity_id": "light.wrong", "state": "on"}],
        flow=flow, vhass_store=store, branch_aware=True)
    assert gate["passed"] is False, gate
    orphan = [a for a in gate["assertions"]
              if a.get("entity_id") == "light.wrong" and not a.get("branch_inactive")]
    assert orphan and orphan[0]["ok"] is False, gate["assertions"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
