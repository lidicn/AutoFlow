# -*- coding: utf-8 -*-
"""
B23 / B24 守卫（竞技场提交与考官诚实性）。

B23：submit_flow 的锁定必须以验收通过（gate.passed）为前提；
     旧实现只看 propose_dsl 流程成功，verdict=拦截 的 flow 也被 locked 并计入 Phase2。
B24：①两级考官——规则考官（无 LLM 可用）抓「实体面文不对题」；
     LLM 考官钩子存在且未配置时自动降级规则（examiner="rules"）。
     ②创意权重创新性优先（novelty 0.5）。

全程离线、零运行时副作用。运行：pytest tests/test_b23_b24_examiner.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TD = tempfile.mkdtemp(prefix="af_b23b24_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TD

from autoflow_gateway.arena import ArenaManager, _rule_logic_review, _creativity_score
from autoflow_gateway.config import reset_config

reset_config()

DEVICES = [
    {"entity_id": "light.study", "friendly_name": "台灯", "state": "off", "domain": "light"},
    {"entity_id": "sensor.temp", "friendly_name": "书房温度", "state": "24.5", "domain": "sensor"},
    {"entity_id": "climate.ac", "friendly_name": "书房空调", "state": "off", "domain": "climate"},
]


def _fresh_mgr():
    d = tempfile.mkdtemp(prefix="af_b23mgr_")
    return ArenaManager(d, gateway=object())  # gateway=object() 非空即可进入 _verify_flow monkeypatch


def _inject_available(mgr, task_id="task_test1"):
    p = os.path.join(mgr.data_dir, "tasks.json")
    tasks = {"tasks": [{
        "id": task_id, "arena_id": "study_room", "title": "测试题",
        "description": "当书房温度超过30度时打开台灯", "entity_ids": ["light.study"],
        "status": "available", "locked_by": None, "locked_at": None,
        "flow_dsl": None, "verification": None, "creativity_score": 0.9,
    }]}
    os.makedirs(mgr.data_dir, exist_ok=True)
    json.dump(tasks, open(p, "w", encoding="utf-8"), ensure_ascii=False)


# ── B23：验收拦截的 flow 不得 locked ─────────────────────────
def test_b23_gate_failed_task_stays_available():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": False, "verdict": "拦截",
                             "fully_verified": False, "reasons": ["[未过] x 期望=on 实测=off"]}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-b23")
    assert r.get("ok") is True  # propose_dsl 流程成功
    tasks = json.load(open(os.path.join(mgr.data_dir, "tasks.json"), encoding="utf-8"))["tasks"]
    t = [x for x in tasks if x["id"] == "task_test1"][0]
    assert t["status"] == "available", t  # ★ 拦截 → 解锁回 available，不再占题
    assert t.get("flow_dsl") is None
    a = json.load(open(os.path.join(mgr.data_dir, "arenas.json"), encoding="utf-8"))
    sr = [x for x in (a.get("arenas") if isinstance(a, dict) else a) if x["id"] == "study_room"][0]
    assert sr["locked_task_count"] == 0, sr  # 不计入 Phase2


def test_b23_gate_passed_still_locks():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": True, "verdict": "放行", "fully_verified": True}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-b23")
    assert r.get("ok") is True
    tasks = json.load(open(os.path.join(mgr.data_dir, "tasks.json"), encoding="utf-8"))["tasks"]
    t = [x for x in tasks if x["id"] == "task_test1"][0]
    assert t["status"] == "locked" and t["flow_dsl"], t


# ── F-R5-01：gate 放行但 fully_verified=false 不得锁题（零断言/前置已满足死锁）──
def test_fr501_not_fully_verified_stays_available():
    """gate.passed=true + fully_verified=false（B20 降级）→ 解锁回 available，可重试。"""
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": True, "verdict": "未充分验证",
                             "fully_verified": False,
                             "warnings": ["【零断言】没有任何后置断言"]}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-fr501")
    assert r.get("ok") is True
    tasks = json.load(open(os.path.join(mgr.data_dir, "tasks.json"), encoding="utf-8"))["tasks"]
    t = [x for x in tasks if x["id"] == "task_test1"][0]
    assert t["status"] == "available", t  # ★ 未充分验证 → 不锁题、不占题
    assert t.get("flow_dsl") is None
    a = json.load(open(os.path.join(mgr.data_dir, "arenas.json"), encoding="utf-8"))
    sr = [x for x in (a.get("arenas") if isinstance(a, dict) else a) if x["id"] == "study_room"][0]
    assert sr["locked_task_count"] == 0, sr  # 不计入 Phase2


def test_fr501_missing_fully_verified_field_still_locks():
    """兼容旧 gate 结构：字段缺失时不改变旧行为（默认 True）。"""
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": True, "verdict": "放行"}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-compat")
    assert r.get("ok") is True
    tasks = json.load(open(os.path.join(mgr.data_dir, "tasks.json"), encoding="utf-8"))["tasks"]
    t = [x for x in tasks if x["id"] == "task_test1"][0]
    assert t["status"] == "locked", t


# ── B24①：规则考官 ───────────────────────────────────────────
def test_b24_rules_catch_entity_face_mismatch():
    """标题开灯、描述关空调（实体面无交集）→ 拦。"""
    r = _rule_logic_review("自动开灯", "当室内温度高于26度时，关闭书房空调",
                           ["light.study"], DEVICES)
    assert r["logic_ok"] is False and r["issues"], r


def test_b24_rules_pass_consistent_task():
    r = _rule_logic_review("书房关门关闭空调", "当门窗传感器检测到门关闭时，关闭书房空调以节省能源",
                           ["climate.ac"], DEVICES)
    assert r["logic_ok"] is True, r


def test_b24_rules_monitor_only_exempt():
    r = _rule_logic_review("书房高温监测", "当温度超过30度时记录并告警预警，不控制任何设备",
                           ["sensor.temp"], DEVICES)
    assert r["logic_ok"] is True, r


# ── B24②：创意权重创新性优先 ─────────────────────────────────
def test_b24_novelty_dominates_score():
    devices = DEVICES
    ents = ["light.study", "climate.ac"]
    dup, bd_dup = _creativity_score("书房空调", "当门窗检测到门关闭时关闭书房空调以节省能源。",
                                    ents, devices,
                                    [{"title": "书房空调", "description": "当门窗检测到门关闭时关闭书房空调以节省能源。"}])
    uniq, bd_uni = _creativity_score("书房空调", "当门窗检测到门关闭时关闭书房空调以节省能源。",
                                     ents, devices, [])
    assert bd_dup["novelty"] == 0.0 and bd_uni["novelty"] == 1.0
    assert uniq - dup >= 0.4  # novelty 权重 0.5：新颖度差异必须显著拉开总分
