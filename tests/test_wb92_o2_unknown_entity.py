# -*- coding: utf-8 -*-
"""
WB92 · O2（P3-F3 闭环）回归守卫：黑箱 propose 对「未知实体」fail-open 修复。

背景（trust-but-verify，prod 实测数据）：
- run_staging_gate 能检出未知实体（stage=entity_check），但 propose_dsl 对闸门结果
  **fail-open** —— 照常 ok=True 落提案，只有 verify_flow 会拦。于是「NL→DSL→propose、
  不调 verify」的黑箱-only 路径可把编造/失效 entity_id 送进提案。
- 对比 run_e2e_trace 与 deploy_raw：二者对 entity_check 均 **fail-closed**，唯独
  propose_dsl fail-open —— 这个不一致就是 O2。

修复：propose_dsl 对 gate.stage=="entity_check" 且未通过者 fail-closed，绝不落提案。

★ 设计边界（本测试重点守卫）：**仅此一类硬拦**。staging 闸的断言失败 / 保守拦截
（verdict=拦截|未充分验证）仍保持 advisory 落提案供人审 —— 因为 O1/F12 的保守拦
已知会误伤合法流，若一并升级为硬拦将造成可用性回退。

爆炸半径实证（prod proposals 683 条）：gate.passed=False 共 97 条，其中本类 65 条；
127 个去重未知实体里 119 个为测试探针构造，唯一「像真实」的
media_player.xiaomi_cn_1108723976_lx05 经 HA 实况 404 确认不存在 → 真实误伤面 ≈ 0。

全程离线、零运行时副作用。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_wb92_o2_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH

GW = G.Gateway()

KNOWN = "light.wb92_o2_known"
GHOST = "light.wb92_o2_ghost_xyz"   # entity_id 形态、故意不登记进目录/映射
UNTOUCHED = "light.wb92_o2_untouched"

# 只登记「已知」实体；GHOST / UNTOUCHED 故意不登记
GW.state.add_mapping(KNOWN, KNOWN)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


SEED = ((KNOWN, "已知灯", "书房", "off", {}),)

DSL_GHOST = """场景: 幽灵实体
触发: inject
动作: light.turn_on(%s)
""" % GHOST

DSL_KNOWN = """场景: 已知实体
触发: inject
动作: light.turn_on(%s)
""" % KNOWN

AGENT = "test_wb92_o2"


def test_o2_unknown_entity_is_hard_blocked():
    """核心：未知实体 → ok=False、stage=entity_check、绝不落提案。"""
    r = GW.propose_dsl(DSL_GHOST, AGENT, vhass_store=_vhass_with(*SEED))
    assert r.get("ok") is False, r
    assert r.get("stage") == "entity_check", r
    assert GHOST in (r.get("unknown_entities") or []), r
    assert r.get("proposal_id") is None, r      # 绝不落提案（fail-closed 的实质）
    assert "entity_id" in (r.get("message") or ""), r


def test_o2_known_entity_still_passes():
    """不误伤：已知实体照常放行并落提案。"""
    r = GW.propose_dsl(DSL_KNOWN, AGENT, vhass_store=_vhass_with(*SEED))
    assert r.get("ok") is True, r
    assert r.get("proposal_id"), r


def test_o2_staging_assertion_failure_stays_advisory():
    """★关键边界：staging 闸断言失败（非 entity_check）仍 advisory 落提案。

    守卫「仅硬拦 entity_check 一类」这一设计决定 —— 若把断言失败也升级成硬拦，
    O1/F12 的保守拦会误伤大量合法流（可用性回退）。
    """
    r = GW.propose_dsl(
        DSL_KNOWN, AGENT,
        expected_postconditions=[{"entity_id": UNTOUCHED, "state": "on"}],
        vhass_store=_vhass_with(*SEED),
    )
    assert r.get("ok") is True, r                 # 仍落提案供人审
    gate = r.get("gate") or {}
    assert gate.get("passed") is False, gate      # 但闸门如实判未过
    assert gate.get("stage") != "entity_check", gate


def test_o2_whitelist_path_still_hard_blocks():
    """resolved_entities 白名单路径不受影响（先于闸门命中，stage=entity_whitelist）。"""
    r = GW.propose_dsl(
        DSL_KNOWN, AGENT,
        resolved_entities=["light.wb92_something_else"],
        vhass_store=_vhass_with(*SEED),
    )
    assert r.get("ok") is False, r
    assert r.get("stage") == "entity_whitelist", r


def test_o2_failure_reason_is_actionable():
    """失败回执必须可操作：点名未知实体，并指引 agent 用发现工具重取。"""
    r = GW.propose_dsl(DSL_GHOST, AGENT, vhass_store=_vhass_with(*SEED))
    msg = r.get("message") or ""
    assert GHOST in msg
    assert ("resolve_entity" in msg or "list_entities" in msg), msg
