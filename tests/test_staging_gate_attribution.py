# -*- coding: utf-8 -*-
"""
P4 准入基准自动化（Phase 2）：staging gate 输出可读的「放行/拦截」归因。

staging gate（run_staging_gate）原本只返回 passed/replayed_services/assertions，
缺「为何放行/拦截」的可读裁决。本文件验证新增的 verdict + reasons：
- 通过后 → verdict=放行，reasons 含逐条 [通过] 标记与重放摘要；
- 预期不符 → verdict=拦截，reasons 含 [未过] 标记指出哪个实体偏差；
- 实体校验失败 → verdict=拦截，reasons 指出未知实体（防假阳性）。

运行：python tests/test_staging_gate_attribution.py   或   python run_tests.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_p4_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
for _eid in ("light.study_main", "binary_sensor.study_motion"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


DSL_OK = """场景: 准入归因-通过
触发: binary_sensor.study_motion 有人
动作: light.turn_on(light.study_main, brightness=60)
预期:
    light.study_main = on
"""

SEED = (
    ("light.study_main", "书房主灯", "书房", "off", {}),
    ("binary_sensor.study_motion", "书房人体感应", "书房", "off", {}),
)


def test_verdict_pass_with_reasons():
    """通过后 verdict=放行，reasons 含逐条 [通过] 标记 + 重放摘要。"""
    store = _vhass_with(*SEED)
    gate = GW.run_staging_gate(
        DSL_OK,
        [{"entity_id": "light.study_main", "state": "on"}],
        vhass_store=store,
    )
    assert gate["passed"] is True
    assert gate["verdict"] == "放行", gate
    reasons = gate["reasons"]
    assert any("light.study_main 期望=on 实测=on" in r and "[通过]" in r
               for r in reasons), reasons
    assert any("重放" in r and "HA 意图" in r for r in reasons), reasons


def test_verdict_block_on_wrong_expected():
    """预期不符 → verdict=拦截，reasons 含 [未过] 指出偏差实体。"""
    store = _vhass_with(*SEED)
    gate = GW.run_staging_gate(
        DSL_OK,
        [{"entity_id": "light.study_main", "state": "off"}],
        vhass_store=store,
    )
    assert gate["passed"] is False
    assert gate["verdict"] == "拦截", gate
    reasons = gate["reasons"]
    assert any("light.study_main 期望=off 实测=on" in r and "[未过]" in r
               for r in reasons), reasons


def test_verdict_block_on_unknown_entity():
    """引用未知实体 → 实体校验阶段即拦截，reasons 指出未知实体。"""
    bad = ("场景: x\n"
            "触发: binary_sensor.not_exist 有人\n"
            "动作: light.turn_on(light.not_exist, brightness=60)\n"
            "预期:\n  light.not_exist = on\n")
    store = _vhass_with()
    gate = GW.run_staging_gate(
        bad,
        [{"entity_id": "light.not_exist", "state": "on"}],
        vhass_store=store,
    )
    assert gate["passed"] is False
    assert gate["stage"] == "entity_check", gate
    assert gate["verdict"] == "拦截", gate
    reasons = gate["reasons"]
    assert any("binary_sensor.not_exist" in r or "light.not_exist" in r
               for r in reasons), reasons


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
