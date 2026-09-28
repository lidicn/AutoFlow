#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mimo 亮点①/② 集成测试：propose_dsl 返回 ref → verify_flow(ref=...) 凭 ref 取回 IR，
免去 agent 重传整份 flow；并验证编译失败返回带 fix 的结构化错误。

零依赖运行：python tests/test_ref_fix.py
"""
import os
import sys
import tempfile

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_ref_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("客厅主灯", "light.living_room_main")
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


DSL_OK = """场景: 书房入户播报2
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""

DSL_BAD = "场景: 语法残缺\n触发: binary_sensor.study_door 有人\n动作: light.turn_on(\n"  # 括号不闭合 → 编译失败


def test_propose_returns_ref():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_OK, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"], res
    assert "ref" in res and res["ref"].startswith("af:"), res
    assert len(res["ref"]) == 11


def test_verify_flow_by_ref():
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_OK, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    ref = res["ref"]
    # 凭 ref 取回暂存 flow，不重传 IR（run_gate=False 避免依赖真实 vhass 环境）
    v = GW.verify_flow(None, ref=ref, run_gate=False)
    assert v["ok"] is True, v
    assert "verdict" in v, v


def test_verify_flow_bad_ref():
    v = GW.verify_flow(None, ref="af:deadbeef")
    assert v["ok"] is False and v["stage"] == "ref", v


def test_compile_error_carries_fix():
    # 括号不闭合 → DSLError → 结构化 compile_error 信封带 fix
    res = GW.propose_dsl(DSL_BAD, "agent_test")
    assert res["ok"] is False, res
    assert res["stage"] == "compile", res
    ce = res["compile_error"]
    assert isinstance(ce["fix"], list) and ce["fix"], ce


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa
            fail += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if fail else 0)
