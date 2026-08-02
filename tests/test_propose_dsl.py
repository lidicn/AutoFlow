#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 MVP 闸门端到端测试：propose_dsl + run_staging_gate（Tier B 场景）。

不依赖真实 NR/HA：staging 闸门把编译产物的 HA 意图重放到内存 vhass 并断言。
运行：python tests/test_propose_dsl.py   （环境无 pytest，内置零依赖运行器）
"""
import os
import sys
import json
import tempfile

# 让 import autoflow_gateway 命中 src 布局
sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
# 种入实体映射（友好名 → entity_id），模拟真实 refresh_catalog 后的共享态
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("客厅主灯", "light.living_room_main")
# 实体身份映射：模拟目录已含这些 entity_id（实体存在性校验依赖 state.resolve）
for _eid in ("light.study_main", "light.living_room_main",
             "binary_sensor.study_door", "device_tracker.me"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


# Tier B 语料：书房入户播报2（贴近生活、agent 易完成）
DSL_STUDY = """场景: 书房入户播报2
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""

DSL_HOME = """场景: 回家开灯播报
触发: device_tracker.me 回家
动作: light.turn_on(客厅主灯, brightness=70)
调用子流程: demo_notify(text=欢迎回家, room=客厅, level=一般)
"""


def test_propose_dsl_gate_pass():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_STUDY, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    assert res["static_validation"] == "passed"
    assert res["gate"]["passed"] is True, res["gate"]
    assert any("light.turn_on(light.study_main)" in r for r in res["gate"]["replayed_services"]), res["gate"]
    assert any("demo_notify" in c for c in res["gate"]["external_calls"]), res["gate"]
    assert res["proposal_id"], "应落提案(raw)"
    # 铁律：编译产物无 Function 节点
    assert "function" not in {n.get("type") for n in res["flow"]["nodes"]}


def test_propose_dsl_gate_fail():
    # 期望灯灭，但 flow 开灯 → 闸门应判 fail
    store = _vhass_with(("light.study_main", "书房主灯", "书房", "off", {}))
    res = GW.propose_dsl(DSL_STUDY, "agent_test",
                         [{"entity_id": "light.study_main", "state": "off"}],
                         vhass_store=store)
    assert res["ok"]
    assert res["gate"]["passed"] is False
    assert res["gate"]["failures"]


def test_propose_dsl_compile_error():
    # 未知子流程 → 编译阶段即失败，不应落提案；返回结构化 compile_error 信封
    bad = "场景: x\n触发: inject\n调用子流程: nope(a=1)\n"
    res = GW.propose_dsl(bad, "agent_test", [], vhass_store=_vhass_with())
    assert res["ok"] is False
    assert res["stage"] == "compile"
    assert "proposal_id" not in res  # 编译失败，未到落提案
    assert res["result_kind"] == "compile_error"
    ce = res["compile_error"]
    assert ce["code"] == "C_SUBFLOW_UNKNOWN", ce
    assert ce["line"] == 3, ce
    assert "建议" in ce["hint"], "hint 应含『怎么改』建议"


def test_propose_dsl_compile_error_missing_trigger():
    # 缺 触发 指令 → C_MISSING_TRIGGER，无行号（line=None）
    bad = "场景: x\n动作: light.turn_on(light.x)\n"
    res = GW.propose_dsl(bad, "agent_test", [], vhass_store=_vhass_with())
    assert res["ok"] is False
    assert res["stage"] == "compile"
    ce = res["compile_error"]
    assert ce["code"] == "C_MISSING_TRIGGER", ce
    assert ce["line"] is None, ce
    assert "建议" in ce["hint"], "hint 应含『怎么改』建议"


def test_propose_dsl_compile_error_action_format():
    # 动作格式错 → C_ACTION_FORMAT，带行号 + hint
    bad = "场景: x\n触发: inject\n动作: light.turn_on light.x\n"
    res = GW.propose_dsl(bad, "agent_test", [], vhass_store=_vhass_with())
    assert res["ok"] is False
    assert res["stage"] == "compile"
    ce = res["compile_error"]
    assert ce["code"] == "C_ACTION_FORMAT", ce
    assert ce["line"] == 3, ce
    assert "建议" in ce["hint"], "hint 应含『怎么改』建议"


def test_propose_dsl_entity_check_rejects_unknown():
    # 引用目录里不存在的实体 → 闸门应在 entity_check 阶段 FAIL（防假阳性）
    bad = ("场景: x\n"
           "触发: binary_sensor.not_exist_motion 有人\n"
           "动作: light.turn_on(light.not_exist, brightness=80)\n"
           "调用子流程: demo_notify(text=hi, room=客厅)\n")
    res = GW.propose_dsl(bad, "agent_test",
                         [{"entity_id": "light.not_exist", "state": "on"}],
                         vhass_store=_vhass_with())
    assert res["ok"]
    assert res["gate"]["passed"] is False
    assert res["gate"]["stage"] == "entity_check", res["gate"]
    assert "binary_sensor.not_exist_motion" in res["gate"]["failures"]
    assert "light.not_exist" in res["gate"]["failures"]


def test_propose_dsl_no_function_ever():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("light.living_room_main", "客厅主灯", "客厅", "off", {}),
        ("device_tracker.me", "我", "全屋", "not_home", {}),
    )
    for dsl in (DSL_STUDY, DSL_HOME):
        res = GW.propose_dsl(dsl, "agent_test",
                             [{"entity_id": "light.living_room_main", "state": "on"}],
                             vhass_store=store)
        assert res["ok"], res
        assert "function" not in {n.get("type") for n in res["flow"]["nodes"]}


# ───────────── A10：propose_dsl / list_pending 富化 ─────────────
def test_propose_dsl_enriched_fields():
    """A10：回执附 dsl 预览 / node_count / lint_summary（只含 error+warning）。"""
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_STUDY, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    # dsl 预览原样回传
    assert res["dsl"] == DSL_STUDY
    # 预计节点数与实际 flow 节点数一致
    assert res["node_count"] == len(res["flow"]["nodes"])
    assert res["node_count"] > 0
    # lint_summary 存在且只含 error/warning（不含 R2 info 噪声）
    assert "lint_summary" in res
    for item in res["lint_summary"]:
        assert item["level"] in ("error", "warning")
        assert "rule" in item and "message" in item
    # 摘要条数 == error+warning 计数之和
    assert len(res["lint_summary"]) == res["lint_error_count"] + res["lint_warning_count"]


def test_enrich_pending_op_flow():
    """A10：update_flow 待确认项被富化出 node_count + preview。"""
    op = {
        "operation": "update_flow",
        "payload": {"flow_id": "abc", "flow": {"label": "测试流", "nodes": [{"id": "a"}, {"id": "b"}]}},
        "summary": "orig",
    }
    e = G._enrich_pending_op(op)
    assert e["node_count"] == 2
    assert "测试流" in e["preview"]
    assert "更新" in e["preview"]
    # 原字段保留
    assert e["operation"] == "update_flow"


def test_enrich_pending_op_ha_call():
    """A10：ha_call 待确认项被富化出 domain.service 预览。"""
    op = {"operation": "ha_call", "payload": {"domain": "light", "service": "turn_on"}, "summary": "s"}
    e = G._enrich_pending_op(op)
    assert "light.turn_on" in e["preview"]


def test_verify_rejects_history_intent_without_subflow():
    """#271：任务池 verify 硬检查——含历史意图(_HIST_PHRASES)却没调用 history_* → 拒。"""
    dsl = "场景: 静默降级陷阱\n触发: inject\n取值: binary_sensor.x 昨晚首次时间\n"
    r = GW.verify_task_dsl(dsl)
    assert r["ok"] is False, r
    assert "语义缺口" in r["error"]
    assert "history_state_at" in r["error"]


def test_verify_accepts_history_subflow_call():
    """#271：正确调用 history_* 子流程 → 通过语义缺口预检（ok=True）。"""
    dsl = ("场景: 查昨晚空调设定\n触发: inject\n"
           "调用子流程: history_state_at(entity=climate.x, at=昨晚23:12)\n")
    r = GW.verify_task_dsl(dsl)
    assert r["ok"] is True, r
    assert r.get("result_kind") in ("compiled", "lint_error", "gate_pass")


def test_verify_rejects_deprecated_history_primitive():
    """#271：旧『历史:』原语在 verify 阶段即被拒（指向 history_*）。"""
    dsl = "场景: x\n触发: inject\n历史: binary_sensor.x 24h\n"
    r = GW.verify_task_dsl(dsl)
    assert r["ok"] is False, r
    assert "history_state_at" in r["error"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
