# -*- coding: utf-8 -*-
"""
P3-D 部署后观测闭环（Phase 3，离线）。

把已有的离线就绪 observe_postconditions 接上 vhass 虚拟孪生，形成
「staging 闸门部署 → 状态变更落到 vhass → 部署后观测 tap 断言」的离线闭环。
不碰真实设备、不触发 NR 上的 tts flow。

机制：observe_postconditions 只读 self.ha.get_state；本文件把 gw.ha 换成
一个委托到 VHassStore.get_state 的适配器，使 observe 读到的正是 staging 闸门
重放（部署模拟）改写后的同一份 vhass 状态。

运行：python tests/test_observe_loop.py   或   python run_tests.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_p3d_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config


class VhassHA:
    """把 VHassStore 适配成 observe_postconditions 所需的 HA 层
    （只需 get_state）。observe 读到的就是 staging 闸门重放改写后的孪生状态。"""

    def __init__(self, store: "VH.VHassStore"):
        self.store = store

    def get_state(self, entity_id: str):
        return self.store.get_state(entity_id)


def _make_gw_and_store():
    reset_config()
    gw = G.Gateway()
    for _eid in ("light.study_main", "binary_sensor.study_motion"):
        gw.state.add_mapping(_eid, _eid)
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities((
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_motion", "书房人体感应", "书房", "off", {}),
    ))
    store.areas = seed["areas"]
    store.entities = {e["entity_id"]: VH.VHassStore._normalize(e)
                     for e in seed["entities"]}
    # 把 observe 的 HA 层指向同一份 vhass 孪生
    gw.ha = VhassHA(store)
    return gw, store


DSL = """场景: 观测闭环-开灯
触发: binary_sensor.study_motion 有人
动作: light.turn_on(light.study_main, brightness=60)
预期:
    light.study_main = on
"""

EXPECT_ON = [{"entity_id": "light.study_main", "state": "on"}]
EXPECT_OFF = [{"entity_id": "light.study_main", "state": "off"}]


def test_closed_loop_pass():
    """部署（staging 闸门重放）→ 孪生状态变 on → 部署后观测 tap 同读 on = 通过。"""
    gw, store = _make_gw_and_store()
    gate = gw.run_staging_gate(DSL, EXPECT_ON, vhass_store=store)
    assert gate["passed"] is True, gate
    assert store.get_state("light.study_main")["state"] == "on"

    obs = gw.observe_postconditions(EXPECT_ON)
    assert obs["ok"] is True, obs
    assert obs["source"] == "ha"
    assert obs["assertions"][0]["actual"] == "on"
    assert obs["assertions"][0]["ok"] is True


def test_closed_loop_catches_drift():
    """若部署后实际状态与预期不符（如预期 off 但已开灯），
    部署后观测 tap 独立抓出漂移（actual=on / expected=off）。"""
    gw, store = _make_gw_and_store()
    gate = gw.run_staging_gate(DSL, EXPECT_ON, vhass_store=store)
    assert gate["passed"] is True  # 部署时按「开灯」预期通过

    # 但部署后观测用的是另一份预期（off）——tap 抓出漂移
    obs = gw.observe_postconditions(EXPECT_OFF)
    assert obs["ok"] is False, obs
    assert obs["assertions"][0]["expected"] == "off"
    assert obs["assertions"][0]["actual"] == "on"
    assert obs["failures"][0]["expected"] == "off"


def test_observe_after_deploy_merges_on_vhass():
    """observe_after_deploy 合并 HA 侧（vhass）观测：HA 可用、无 NR debug 时给 note。"""
    gw, store = _make_gw_and_store()
    gate = gw.run_staging_gate(DSL, EXPECT_ON, vhass_store=store)
    assert gate["passed"] is True
    r = gw.observe_after_deploy(EXPECT_ON, flow_id="af_scene_观测闭环_开灯")
    assert r["ok"] is True, r
    assert r["ha"]["ok"] is True
    assert r["nr_debug"] is None
    assert r["nr_note"] is not None  # 标注需 NR 授权（离线）


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
