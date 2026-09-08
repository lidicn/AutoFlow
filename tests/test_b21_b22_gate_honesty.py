# -*- coding: utf-8 -*-
"""
B21 / B22 守卫（负责人复核发现的竞技场/闸门缺陷）。

B21 判重实体层不对称 → 超集可绕过：
  旧 `_entity_overlap(new, existing) = |∩|/|new|` 只按新题实体数做分母，新题是旧题
  实体的超集时被稀释到 60% 阈值下（实况：2 实体旧题 vs 4 实体新题 → 0.50 漏判）。
  修法：双向覆盖率取大。

B22 外部子流程返回值驱动的分支 → 断言被「未激活分支」跳过 → 假绿放行：
  vhass 不建模 HA 历史，分支读 payload.found 恒缺失 → 走 else → 实际重放 turn_off，
  而期望 on 的断言被标 branch_inactive 跳过 → passed/fully_verified 均为 True。
  修法：flow 含外部调用且本步重放了与期望相反的动作时，不再静默跳过，
  标记 branch_inactive_unverified 并把结论降级为「未充分验证」。

全程离线、零运行时副作用。运行：pytest tests/test_b21_b22_gate_honesty.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_b21b22_")

from autoflow_gateway.arena import _entity_overlap
from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
for _eid in ("light.study_main", "climate.study_ac", "sensor.study_illum"):
    GW.state.add_mapping(_eid, _eid)
# 被测目标是 B22 的断言诚实性，不是「子流程是否已装进目标 NR」；离线环境注册表里
# 没有 af_hist_*（生产 1990 才有），故置空节点闸门，避免与本节断言无关的干扰。
GW._gate_node_types = lambda flow: None


# ── B21：判重实体重叠度双向取大 ───────────────────────────────
def test_b21_superset_no_longer_dilutes_overlap():
    """新题是旧题实体超集时，重叠度应为 1.0（旧实现 0.50 → 绕过 60% 阈值）。"""
    existing = ["sensor.humidity", "climate.ac"]
    new = ["sensor.humidity", "climate.ac", "sensor.temp", "sensor.illum"]
    assert _entity_overlap(new, existing) == 1.0
    # 反向同样成立（对称）
    assert _entity_overlap(existing, new) == 1.0


def test_b21_disjoint_sets_still_zero():
    assert _entity_overlap(["light.a"], ["climate.ac"]) == 0.0


def test_b21_partial_overlap_unchanged():
    """实体面部分重叠的题不受影响（0.5 < 0.6，仍不判重）。"""
    assert _entity_overlap(["a", "b"], ["a", "c"]) == 0.5


# ── B22：外部调用驱动分支 → 不得静默跳过断言 ──────────────────
def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


# 调用历史子流程后按返回值分支：vhass 不建模历史 → found 恒缺失 → 走 else（关灯）
DSL_HIST_BRANCH = """场景: 历史有记录才开灯
触发: inject(payload={"tick":1})
调用子流程: history_state_at(entity=climate.study_ac, at=今天08:00)
提取: found = payload.found
分支 found = true:
    动作: light.turn_on(light.study_main, brightness=80)
否则:
    动作: light.turn_off(light.study_main)
预期:
  light.study_main = on
"""

# 对照组：无外部调用的条件流，分支按可求值条件命中 else → 未激活分支跳过是合法行为
DSL_PLAIN_BRANCH = """场景: 语音开灯分支
触发: inject(payload={"cmd":"关灯"})
分支 payload.cmd = "开灯":
    动作: light.turn_on(light.study_main, brightness=80)
否则:
    动作: light.turn_off(light.study_main)
预期:
  light.study_main = on
"""

SEED = (
    ("light.study_main", "书房主灯", "书房", "on", {}),
    ("climate.study_ac", "书房空调", "书房", "off", {}),
    ("sensor.study_illum", "书房光照", "书房", "500", {}),
)

# 对照组用：光照 500（不满足 <100 → 走 else 关灯）→ 期望 on 的断言来自未激活分支
SEED_BRIGHT = (
    ("light.study_main", "书房主灯", "书房", "off", {}),
    ("sensor.study_illum", "书房光照", "书房", "500", {}),
)


def test_b22_extern_driven_branch_not_silently_skipped():
    """B22：外部子流程返回值驱动分支 + 反置动作被重放 → 降级未充分验证，不再假绿。"""
    store = _vhass_with(*SEED)
    gate = GW.run_staging_gate(DSL_HIST_BRANCH,
                               [{"entity_id": "light.study_main", "state": "on"}],
                               vhass_store=store, branch_aware=True)
    st = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"]
    assert st, gate["assertions"]
    # 关键：不能被静默 skip 成「通过」
    assert st[0].get("branch_inactive_unverified") is True, st[0]
    assert st[0].get("opposite_replayed") is True, st[0]
    assert gate["fully_verified"] is False, gate
    assert gate["verdict"] == "未充分验证", gate
    assert any("分支判定不可求值" in w for w in gate["warnings"]), gate["warnings"]


def test_b22_control_plain_conditional_branch_unaffected():
    """回归护栏：无外部调用的条件流，未激活分支跳过仍是合法行为（不误伤）。"""
    store = _vhass_with(*SEED_BRIGHT)
    gate = GW.run_staging_gate(DSL_PLAIN_BRANCH,
                               [{"entity_id": "light.study_main", "state": "on"}],
                               vhass_store=store, branch_aware=True)
    st = [a for a in gate["assertions"] if a.get("entity_id") == "light.study_main"]
    assert st, gate["assertions"]
    assert st[0].get("branch_inactive") is True, st[0]
    assert not st[0].get("branch_inactive_unverified"), st[0]
