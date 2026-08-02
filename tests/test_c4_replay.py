# -*- coding: utf-8 -*-
"""
C4 · vhass 分支感知重放 + 时间快进 测试。

覆盖：
1) vhass 虚拟时钟 + 重放时间线（单元）：set/advance/now、按序注入事件、序依赖正确。
2) 闸门分支感知：开灯/关灯分支 → 只重放命中分支的 api-call-service（旧实现跳过所有
   switch 后代 → 无法断言任一分支效果；新实现评估 switch 规则只跑命中分支）。
3) 多步 scenario：人体感应 有人→开灯 / 无人→关灯，逐步 world 推进 + 逐步断言。
4) 虚拟时间/时间段：time-range-switch 在窗口内→执行、窗口外→不执行（闸门能抓时间违例）。

全程离线、零运行时副作用。运行：python tests/test_c4_replay.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_c4_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
for _eid in ("light.study_main", "binary_sensor.study_motion",
            "light.philips_cn_249518489_rwread_s_2_light"):
    GW.state.add_mapping(_eid, _eid)


# ── 1) vhass 时钟 / 重放 单元 ──
def test_vhass_clock_set_advance():
    s = VH.VHassStore()
    s.set_clock(1000.0)
    assert abs(s.clock_now() - 1000.0) < 1e-6
    s.advance_clock(60)
    assert abs(s.clock_now() - 1060.0) < 1e-6
    assert s.clock_now_iso().endswith("+00:00") or "T" in s.clock_now_iso()


def test_vhass_replay_order_aware():
    s = VH.VHassStore()
    start = s.clock_now()
    r = s.apply_replay([
        {"at": 0, "entity_id": "light.a", "state": "on"},
        {"at": 120, "entity_id": "light.a", "state": "off"},
        {"at": 300, "entity_id": "light.a", "state": "on"},
    ])
    # 序依赖：最后写入胜出 → on
    assert r["final_states"]["light.a"] == "on"
    assert len(r["timeline"]) == 3
    # at 为相对重放起点(start)的秒偏移 → 偏移量守恒
    assert r["timeline"][0]["at_epoch"] == start
    assert r["timeline"][1]["at_epoch"] == start + 120
    assert r["timeline"][2]["at_epoch"] == start + 300


def test_vhass_replay_http_endpoints():
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen, Request
    import json
    store = VH.VHassStore()
    VH.Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), VH.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # clock
        req = Request(f"http://127.0.0.1:{port}/api/clock",
                      data=json.dumps({"action": "set", "value": 5000}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        c = json.loads(urlopen(req).read())
        assert abs(c["epoch"] - 5000) < 1e-6
        # replay
        req = Request(f"http://127.0.0.1:{port}/api/replay",
                      data=json.dumps({"steps": [
                          {"at": 0, "entity_id": "light.x", "state": "on"},
                          {"at": 10, "entity_id": "light.x", "state": "off"},
                      ]}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        rp = json.loads(urlopen(req).read())
        assert rp["final_states"]["light.x"] == "off"
    finally:
        httpd.shutdown()


# ── 2) 闸门分支感知 ──
def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


DSL_BRANCH_ON = """场景: 语音开灯分支
触发: inject(payload={"cmd":"开灯"})
分支 payload.cmd = "开灯":
    动作: light.turn_on(light.study_main, brightness=80)
分支 payload.cmd = "关灯":
    动作: light.turn_off(light.study_main)
否则:
    动作: light.turn_off(light.study_main)
预期:
  light.study_main = on
"""

DSL_BRANCH_OFF = """场景: 语音关灯分支
触发: inject(payload={"cmd":"关灯"})
分支 payload.cmd = "开灯":
    动作: light.turn_on(light.study_main, brightness=80)
分支 payload.cmd = "关灯":
    动作: light.turn_off(light.study_main)
否则:
    动作: light.turn_off(light.study_main)
预期:
  light.study_main = off
"""

SEED_LIGHT = (("light.study_main", "书房主灯", "书房", "off", {}),)


def test_gate_branch_on_replays_only_taken():
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(DSL_BRANCH_ON,
                                [{"entity_id": "light.study_main", "state": "on"}],
                                vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate
    # 分支感知：只重放命中分支(开灯)的 api-call-service，关灯分支绝不执行
    assert any("turn_on" in r for r in gate["replayed_services"]), gate
    assert not any("turn_off" in r for r in gate["replayed_services"]), \
        f"关灯分支被误重放：{gate['replayed_services']}"


def test_gate_branch_off_replays_only_taken():
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(DSL_BRANCH_OFF,
                                [{"entity_id": "light.study_main", "state": "off"}],
                                vhass_store=store, branch_aware=True)
    assert gate["passed"] is True, gate
    assert any("turn_off" in r for r in gate["replayed_services"]), gate
    assert not any("turn_on" in r for r in gate["replayed_services"]), \
        f"开灯分支被误重放：{gate['replayed_services']}"


# ── 3) 多步 scenario ──
DSL_MOTION = """场景: 人体感应开关灯
触发: binary_sensor.study_motion 有人
动作: light.turn_on(light.study_main, brightness=80)
触发: binary_sensor.study_motion 无人
动作: light.turn_off(light.study_main)
预期:
  light.study_main = on
"""

SEED_MOTION = (
    ("light.study_main", "书房主灯", "书房", "off", {}),
    ("binary_sensor.study_motion", "书房人体感应", "书房", "off", {}),
)


def test_gate_multistep_scenario():
    store = _vhass_with(*SEED_MOTION)
    scenario = [
        {"world": {"binary_sensor.study_motion": "on"},
         "expected": [{"entity_id": "light.study_main", "state": "on"}]},
        {"world": {"binary_sensor.study_motion": "off"},
         "expected": [{"entity_id": "light.study_main", "state": "off"}]},
    ]
    gate = GW.run_staging_gate(DSL_MOTION,
                                [{"entity_id": "light.study_main", "state": "on"}],
                                vhass_store=store, scenario=scenario, target="prod",
                                branch_aware=True)
    assert gate["passed"] is True, gate
    assert gate["step_count"] == 2
    # 第1步只开了灯，第2步只关了灯（每步重放意图应单一）
    assert any("turn_on" in r for r in gate["steps"][0]["replayed_services"])
    assert any("turn_off" in r for r in gate["steps"][1]["replayed_services"])
    # 终态：关灯
    assert store.get_state("light.study_main")["state"] == "off"


# ── 4) 虚拟时间 / 时间段门控 ──
DSL_TIMERANGE = """场景: 时间段开灯
触发: inject(payload={"tick":1})
时间段: 07:00-09:00
动作: light.turn_on(light.study_main, brightness=80)
预期:
  light.study_main = on
"""


def test_gate_timerange_inside_window_executes():
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(DSL_TIMERANGE,
                                [{"entity_id": "light.study_main", "state": "on"}],
                                vhass_store=store, virtual_time="2026-07-16T08:00:00",
                                branch_aware=True)
    assert gate["passed"] is True, gate
    assert any("turn_on" in r for r in gate["replayed_services"]), gate


def test_gate_timerange_outside_window_blocks():
    store = _vhass_with(*SEED_LIGHT)
    gate = GW.run_staging_gate(DSL_TIMERANGE,
                                [{"entity_id": "light.study_main", "state": "on"}],
                                vhass_store=store, virtual_time="2026-07-16T10:00:00",
                                branch_aware=True)
    # 窗口外 time-range-switch 走 out1(空) → 开灯意图不执行 → light 仍为 off → 断言失败
    assert gate["passed"] is False, gate
    assert not any("turn_on" in r for r in gate["replayed_services"]), \
        f"窗口外仍重放了开灯：{gate['replayed_services']}"


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
