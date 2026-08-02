"""CB7(#692) apply 闭环 B 段胶水单测：debug 帧 → 实体+目标状态 → commit_ha_service。

关键纪律（来自 WB1 评审 BLOCKER-1）：**桩必须走真实入库路径产帧**，不能手搓 {"payload": dict}。
真机 debug_bridge._ingest 会把 payload 序列化成字符串入库，read(full=True) 返回的 payload
就是字符串。本测试用 DebugBridge.__new__ + _ingest 真实产帧，保证桩形状与真机一致，
结构上不可能再出现「dict payload 推断永不执行」这类缺陷。

覆盖：
  - 单帧 on/off 映射（真·字符串 payload 路径）
  - int 状态映射
  - 显式 entity_id/state 覆盖帧
  - 空帧 / 帧无 entity_id → 不写回 HA（#607）
  - 多帧取最新（BLOCKER-2：read 已倒序，直接正序 = 取最新）
  - 超长帧被截断 → json.loads 失败跳过（并提示截断）
  - unavailable/unknown/未知状态 → fail-closed 拒绝，绝不静默 turn_off（RISK-3）
  - 显式指定实体时只认同实体帧，避免误用别的实体状态（NIT-4）
"""
import sys
import os
import threading

from collections import deque

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.debug_bridge import DebugBridge


@pytest.fixture
def gw():
    g = Gateway.__new__(Gateway)
    g.nr = None
    g.debug_bridge = None
    return g


@pytest.fixture
def stub(gw):
    """截获 commit_ha_service（B 段落点）。"""
    calls = []

    def _commit(domain, service, data, agent_id):
        calls.append({"domain": domain, "service": service,
                      "data": data, "agent_id": agent_id})
        return {"ok": True, "needs_approval": False, "pending_id": None}

    gw.commit_ha_service = _commit
    return calls


def _make_bridge(max_payload_chars: int = 2000):
    """构造一个不连 NR 的纯内存 DebugBridge（真实 _ingest / read 路径）。"""
    db = DebugBridge.__new__(DebugBridge)
    db._events = deque(maxlen=10000)
    db._lock = threading.Lock()
    db.max_total = 10000
    db.max_per_node = 10000
    db.ttl_seconds = 0
    db.max_payload_chars = max_payload_chars
    db.preview_chars = 160
    db._per_node_count = {}
    db.enabled = True
    db._connected = True
    return db


def _seed(db, payloads, node_id="n0", flow_id="f1"):
    """用真实 _ingest 入库（payload 会被 json.dumps 成字符串），并赋递增 received_at
    保证最后 ingest 的帧在 read() 中排最前（= 最新）。"""
    for i, payload in enumerate(payloads):
        db._ingest(node_id, flow_id, f"obs{i}", "test/topic", payload, i, False)
    for i, ev in enumerate(db._events):
        ev["received_at"] = float(i)
    return db


def _wire(gw, db):
    """把 gateway.get_debug_read 接到真实 DebugBridge.read(full=True)。"""
    def _read(flow_id="", node_id="", since=0, limit=50, full=True):
        return db.read(flow_id=flow_id or None, node_id=node_id or None,
                       since=since or None, limit=limit, full=full)
    gw.get_debug_read = _read
    return db


# ───────────── 基础映射（真·字符串 payload 路径）─────────────

def test_debug_frame_string_payload_maps_to_turn_on(gw, stub):
    db = _wire(gw, _seed(_make_bridge(), [{"entity_id": "light.study", "state": "on"}]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    # 关键：真机帧 payload 是字符串，这里必须走 json.loads 才能解析出 entity_id/state
    assert stub == [{"domain": "light", "service": "turn_on",
                     "data": {"entity_id": "light.study"}, "agent_id": "cb"}]


def test_debug_frame_string_payload_maps_to_turn_off(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "switch.desk", "state": "off"}]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert stub == [{"domain": "switch", "service": "turn_off",
                     "data": {"entity_id": "switch.desk"}, "agent_id": "cb"}]


def test_int_state_in_json_maps_to_turn_on(gw, stub):
    # 真机 _ingest 会 json.dumps(1) → "1" → json.loads → 1 → _state_is_on(1)=on
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "light.x", "state": 1}]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert stub and stub[0]["service"] == "turn_on"


def test_explicit_args_override_frame(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "light.other", "state": "on"}]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", entity_id="light.study",
                              state="off", agent_id="cb")
    assert stub == [{"domain": "light", "service": "turn_off",
                     "data": {"entity_id": "light.study"}, "agent_id": "cb"}]


def test_empty_frames_blocks_writeback(gw, stub):
    db = _make_bridge()
    _wire(gw, db)  # 不 seed，read 返回 events=[]
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert "无 debug 回读帧" in res.get("error", "")
    assert stub == []  # #607：空观测绝不写回 HA


def test_frame_without_entity_id_blocks(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"something": "else"}]))
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert "entity_id" in res.get("error", "")
    assert stub == []


# ───────────── BLOCKER-2：多帧取最新 ─────────────

def test_multi_frame_picks_newest_not_oldest(gw, stub):
    # 旧帧：light.OLD/off；新帧：light.study/on（后 ingest = 最新）
    _wire(gw, _seed(_make_bridge(), [
        {"entity_id": "light.OLD", "state": "off"},
        {"entity_id": "light.study", "state": "on"},
    ]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    # 若误用 reversed() 会取到最旧的 light.OLD/off；正确应取最新的 light.study/on
    assert stub == [{"domain": "light", "service": "turn_on",
                     "data": {"entity_id": "light.study"}, "agent_id": "cb"}]


# ───────────── BLOCKER-1：截断帧跳过 ─────────────

def test_truncated_frame_skipped_but_valid_used(gw, stub):
    # 默认 max_payload_chars=2000；仅超长帧被截断，短帧保持完整
    long_payload = {"entity_id": "light.study", "state": "on", "note": "x" * 2500}
    _wire(gw, _seed(_make_bridge(), [
        long_payload,                                   # 被截断 → json.loads 失败 → 跳过
        {"entity_id": "switch.desk", "state": "off"},   # 有效 → 被采用（最新）
    ]))
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert stub == [{"domain": "switch", "service": "turn_off",
                     "data": {"entity_id": "switch.desk"}, "agent_id": "cb"}]


def test_only_truncated_frame_reports_truncation_hint(gw, stub):
    long_payload = {"entity_id": "light.study", "state": "on", "note": "x" * 2500}
    _wire(gw, _seed(_make_bridge(), [long_payload]))
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert "截断" in res.get("error", "")
    assert stub == []


# ───────────── RISK-3：状态 fail-closed ─────────────

def test_unavailable_state_rejected(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "light.x", "state": "unavailable"}]))
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert "unavailable" in res.get("error", "")
    assert stub == []  # 绝不静默 turn_off


def test_unknown_state_rejected(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "light.x", "state": "unknown"}]))
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert "unknown" in res.get("error", "")
    assert stub == []


def test_unrecognized_state_rejected(gw, stub):
    _wire(gw, _seed(_make_bridge(), [{"entity_id": "media.x", "state": "frobnicate"}]))
    # frobnicate 不在任何白名单 → 拒绝（不再误判为 turn_off）
    res = gw.apply_state_from_debug(flow_id="f1", node_id="n0", agent_id="cb")
    assert res.get("ok") is False
    assert stub == []


# ───────────── NIT-4：显式实体只认同实体帧 ─────────────

def test_explicit_entity_only_matches_same_entity(gw, stub):
    _wire(gw, _seed(_make_bridge(), [
        {"entity_id": "switch.desk", "state": "off"},
        {"entity_id": "light.study", "state": "on"},
    ]))
    # 显式指定 light.study、state 留空 → 只认 light.study 帧的 on，不用 switch.desk 的 off
    gw.apply_state_from_debug(flow_id="f1", node_id="n0", entity_id="light.study", agent_id="cb")
    assert stub == [{"domain": "light", "service": "turn_on",
                     "data": {"entity_id": "light.study"}, "agent_id": "cb"}]
