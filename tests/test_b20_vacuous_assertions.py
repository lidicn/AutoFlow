# -*- coding: utf-8 -*-
"""
B20 / FFL-R2-01 · 验收 oracle 弱验证守卫（vacuous assertion）。

背景（FFL R2 FINDINGS F-R2-01，docs/04_test/findings-ledger.md B20）：
1) 缺口①：目标实体在重放前已处于期望状态时（pre_state==want），即使服务被重放，
   状态「转变」也从未被证明——旧实现静默放行且 fully_verified=True（T5 case1 实录：
   pre_state=on / service_called=true / changed_by_replay=false 仍判通过）。
2) 缺口②：DSL 声明了效果且动作被重放，但 expected 为空/自指 → 0 断言，
   all_pass 平凡为真 → 放行 + fully_verified=True。

修法（沿 A22/V-NEW-1 诚实降级模式，不硬拦）：
- 缺口①：断言项标 pre_satisfied=True + 告警；全部 state 断言均前置已满足时
  fully_verified 降级 False、verdict=未充分验证；require_change=True 时硬失败。
- 缺口②：零断言 → 告警 + fully_verified 降级 False、verdict=未充分验证。

全程离线、零运行时副作用。运行：pytest tests/test_b20_vacuous_assertions.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_b20_")

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
GW.state.add_mapping("light.study_main", "light.study_main")


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


DSL_TURN_ON = """场景: 开灯
触发: inject(payload={"cmd":"开灯"})
动作: light.turn_on(light.study_main, brightness=80)
预期:
  light.study_main = on
"""

# 零断言用：无「预期」节，expected 传 []
DSL_TURN_ON_NO_EXPECT = """场景: 开灯无预期
触发: inject(payload={"cmd":"开灯"})
动作: light.turn_on(light.study_main, brightness=80)
"""

SEED_ON = (("light.study_main", "书房主灯", "书房", "on", {}),)
SEED_OFF = (("light.study_main", "书房主灯", "书房", "off", {}),)


def test_b20_pre_satisfied_warns_and_degrades():
    """缺口①：种子态已 on → turn_on 重放成功但转变未证明 → 降级未充分验证。"""
    store = _vhass_with(*SEED_ON)
    gate = GW.run_staging_gate(DSL_TURN_ON,
                               [{"entity_id": "light.study_main", "state": "on"}],
                               vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate  # 不硬拦（沿 A22 模式）
    assert gate["fully_verified"] is False, gate
    assert gate["verdict"] == "未充分验证", gate
    # 断言项带 pre_satisfied 标记
    st = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"]
    assert st and st[0].get("pre_satisfied") is True, gate["assertions"]
    assert any("前置已满足" in w for w in gate["warnings"]), gate["warnings"]


def test_b20_pre_satisfied_require_change_fails():
    """缺口①：require_change=True 时前置已满足 = 硬失败（无法证明转变）。"""
    store = _vhass_with(*SEED_ON)
    gate = GW.run_staging_gate(DSL_TURN_ON,
                               [{"entity_id": "light.study_main", "state": "on"}],
                               vhass_store=store, branch_aware=True,
                               require_change=True)
    assert gate["passed"] is False, gate
    assert gate["verdict"] == "拦截", gate
    st = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"]
    assert st and st[0]["ok"] is False and st[0].get("pre_satisfied") is True, gate["assertions"]


def test_b20_zero_assertion_degrades():
    """缺口②：声明效果 + 动作被重放，但 expected 为空 → 降级未充分验证。"""
    store = _vhass_with(*SEED_OFF)
    gate = GW.run_staging_gate(DSL_TURN_ON_NO_EXPECT, [],
                               vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate  # 不硬拦（沿 A22 模式）
    assert gate["fully_verified"] is False, gate
    assert gate["verdict"] == "未充分验证", gate
    assert any("零断言" in w for w in gate["warnings"]), gate["warnings"]


def test_b20_real_transition_still_fully_verified():
    """回归护栏：种子 off → 期望 on，真转变路径不受影响，仍 fully_verified=True。"""
    store = _vhass_with(*SEED_OFF)
    gate = GW.run_staging_gate(DSL_TURN_ON,
                               [{"entity_id": "light.study_main", "state": "on"}],
                               vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate
    assert gate["fully_verified"] is True, gate
    assert gate["verdict"] == "放行", gate
    st = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"]
    assert st and st[0].get("changed_by_replay") is True, gate["assertions"]
