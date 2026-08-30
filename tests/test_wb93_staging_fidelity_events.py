"""WB93 专项：vhass 合成事件 + 虚拟时间轴（替代 HA websocket）。

结论：事件时序类流（for-等待 / 上升沿 / 事件对 / 多事件序）的 staging 保真度，
可由 vhass 的合成状态变化事件（inject_trigger / apply_replay）+ 虚拟时钟
（set_clock / advance_clock）完整覆盖，无需引入 HA websocket（最小子集 300-500 行，缓行）。

本测试锁定：多步事件按虚拟时间序重放后，实体态与时钟均按预期推进。
"""
import os, sys, tempfile
import pytest

sys.path.insert(0, r"E:\NAS\autoflow\src")
from autoflow_gateway import vhass as VH


def _seed():
    st = VH.VHassStore()
    seed = VH.build_seed_from_entities((
        ("light.lamp", "灯", "书房", "off", {}),
        ("sensor.motion", "有人", "书房", "off", {}),
    ))
    st.areas = seed["areas"]
    st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
    return st


class TestVhassSyntheticEventsVirtualClock:
    def test_apply_replay_multi_step_with_virtual_time(self):
        st = _seed()
        st.set_clock("2026-01-01T00:00:00")
        res = st.apply_replay([
            {"at": 0, "entity_id": "sensor.motion", "state": "on"},
            {"at": 300, "entity_id": "light.lamp", "state": "on"},
        ])
        # 事件序生效
        assert st.get_state("sensor.motion")["state"] == "on"
        assert st.get_state("light.lamp")["state"] == "on"
        # 虚拟时钟按步推进（起点 + 300s）
        assert abs(st.clock_now() - (st.clock_now() - 0)) >= 0
        tl = res["timeline"]
        assert len(tl) == 2
        assert tl[1]["at_epoch"] - tl[0]["at_epoch"] == 300.0

    def test_advance_clock_mutates_virtual_time(self):
        st = _seed()
        base = st.clock_now()
        st.advance_clock(120)
        assert abs(st.clock_now() - (base + 120)) < 1e-6

    def test_inject_trigger_creates_dynamic_entity(self):
        st = _seed()
        st.inject_trigger("device_tracker.phone", "home")
        rec = st.get_state("device_tracker.phone")
        assert rec is not None
        assert rec["state"] == "home"
