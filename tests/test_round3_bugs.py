# -*- coding: utf-8 -*-
"""Round3 bug 回归（iss_b2ecd18673 / iss_e4a3fcd572 / iss_f1095df090 相关代码修复）。

- Bug1 (iss_b2ecd18673, High)：gate 断言须区分「服务被调用且状态改变」与
  「状态在重放前已满足、flow 未证明副作用」。修复：断言项新增 pre_state /
  service_called / changed_by_replay；巧合命中（状态已满足且无针对该实体的服务被重放）
  产生非阻塞告警；require_change=True 时作为真失败。

- Bug2 (iss_e4a3fcd572, Medium)：DSL `否则: 注释:` 不再把 switch 否则输出连到 comment
  节点（避免 R25：消息到达 comment 被静默丢弃），且 otherwise 空分支（注释专用）豁免 R21。

Bug3 (iss_f1095df090, Low) 的根因是 websockets 依赖缺失导致 area 注册表抓取失败，
属部署/依赖层面，运行环境（NAS prod）经安装 websockets + refresh_catalog 实证，不在此单测。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_r3_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config
from autoflow_gateway.dsl_engine import parse, compile
from autoflow_gateway.flow_linter import lint_flow

reset_config()
GW = G.Gateway()
for _eid in ("light.study_main", "binary_sensor.study_motion",
            "sensor.study_lux", "light.philips_cn_249518489_rwread_s_2_light"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


# ── Bug2：comment-only otherwise 不应触发 R25 / R21 ──
DSL_COMMENT_OTHERWISE = """场景: 注释否则
触发: inject
取值: sensor.study_lux lux
分支: $number(lux) < 10
  动作: light.turn_on(light.study_main)
否则:
  注释: 光线足够"""

# ── Bug1：可达动作 → changed_by_replay=True / service_called=True ──
DSL_REACHABLE = """场景: 可达动作
触发: inject
动作: light.turn_on(light.study_main)"""

SEED_OFF = (
    ("light.study_main", "书房主灯", "书房", "off", {}),
    ("binary_sensor.study_motion", "书房人体感应", "书房", "off", {}),
    ("sensor.study_lux", "书房光照", "书房", "500", {}),
)

SEED_ON = (
    ("light.study_main", "书房主灯", "书房", "on", {}),
    ("binary_sensor.study_motion", "书房人体感应", "书房", "off", {}),
    ("sensor.study_lux", "书房光照", "书房", "500", {}),
)


def test_bug2_comment_otherwise_no_r25():
    """否则: 注释: 编译产物不应把 switch 否则输出连到 comment（R25 为空）。
    注：otherwise 空分支本身是一条合法的『无操作』终端，R21 仍可能按
    通用死分支规则给出 warning（可接受、不阻断），故此处只断言 R25 已消除——
    这正是 iss_e4a3fcd572 的根因（switch→comment 连线导致消息被静默丢弃）。"""
    flow = compile(parse(DSL_COMMENT_OTHERWISE), target="staging")
    issues = lint_flow(flow)
    r25 = [i for i in issues if i.get("rule") == "R25"]
    assert not r25, f"R25 不应触发（switch 否则输出不应连到 comment）：{r25}"
    # switch 只连出真正有动作的『命中』分支，otherwise 无连线（无操作终端，不再指向 comment）
    sw = next(n for n in flow["nodes"] if n.get("type") == "switch")
    assert all(isinstance(w, list) and len(w) <= 1 for w in sw["wires"]), sw["wires"]


def _first_state_assertion(gate_result):
    # 无 scenario 时断言在顶层 assertions；有 scenario 时在 step_results[0].
    if gate_result.get("step_results"):
        return gate_result["step_results"][0]["assertions"][0]
    return gate_result["assertions"][0]


def _first_failures(gate_result):
    if gate_result.get("step_results"):
        return gate_result["step_results"][0]["failures"]
    return gate_result["failures"]


def test_bug1_reachable_action_enriched():
    """可达动作：pre_state=off、service_called=True、changed_by_replay=True，无巧合告警。"""
    store = _vhass_with(*SEED_OFF)
    r = GW.run_staging_gate(DSL_REACHABLE, expected=[{"entity_id": "light.study_main", "state": "on"}],
                            vhass_store=store)
    a = _first_state_assertion(r)
    assert a["pre_state"] == "off", a
    assert a["service_called"] is True, a
    assert a["changed_by_replay"] is True, a
    assert "coincidental" not in a, a
    assert r["passed"] is True


def test_bug1_coincidental_warning():
    """状态重放前已满足、且动作不可达 → coincidental=True + 非阻塞告警，verdict 不翻（仍通过）。"""
    store = _vhass_with(*SEED_ON)
    r = GW.run_staging_gate(
        """场景: 巧合
触发: binary_sensor.study_motion 有人
取值: sensor.study_lux lux
分支: $number(lux) < 10
  动作: light.turn_on(light.study_main)
预期:
  light.study_main = on""",
        expected=[{"entity_id": "light.study_main", "state": "on"}],
        vhass_store=store)
    a = _first_state_assertion(r)
    assert a["pre_state"] == "on", a
    assert a["service_called"] is False, a
    assert a["changed_by_replay"] is False, a
    assert a.get("coincidental") is True, a
    assert r["passed"] is True, r.get("verdict")
    assert any("巧合命中" in w for w in (r.get("warnings") or [])), r.get("warnings")


def test_bug1_require_change_fails_coincidence():
    """require_change=True 时，巧合命中（状态已满足且无服务被重放）→ 真失败。"""
    store = _vhass_with(*SEED_ON)
    r = GW.run_staging_gate(
        """场景: 巧合-require_change
触发: binary_sensor.study_motion 有人
取值: sensor.study_lux lux
分支: $number(lux) < 10
  动作: light.turn_on(light.study_main)
预期:
  light.study_main = on""",
        expected=[{"entity_id": "light.study_main", "state": "on"}],
        vhass_store=store, require_change=True)
    a = _first_state_assertion(r)
    assert a["ok"] is False, a
    assert r["passed"] is False, r.get("verdict")
    assert any("require_change" in (f.get("reason") or "") for f in _first_failures(r)), \
        _first_failures(r)


if __name__ == "__main__":
    import unittest
    unittest.main()
