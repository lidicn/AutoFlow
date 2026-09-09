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


# ── F-R6-A-01：目的从句不得污染期望推导 ─────────────────────
def test_fr601_purpose_clause_not_flip_postcondition():
    """task A 实况回归：描述含「避免…被误关、回来还要重新启动空调和电脑」，
    目的从句中的反向动词（启动）不得把期望翻成 on。"""
    mgr = _fresh_mgr()
    task = {
        "id": "task_a", "title": "人离开书房延时20分钟再关空调电脑",
        "description": "当人在传感器显示书房无人时，先等待 20 分钟确认不是短暂离开，"
                       "再自动关闭书房空调和电脑。延时缓冲是为避免有人短暂出门（如取快递）"
                       "导致空调电脑被误关、回来还要重新启动空调和电脑。",
        "entity_ids": ["climate.ac", "switch.pc"],
    }
    dsl = "触发: x off\n延时: 20 分钟\n动作: climate.turn_off(climate.ac)\n动作: switch.turn_off(switch.pc)"
    expected = mgr._infer_postconditions(task, dsl)
    by_id = {e["entity_id"]: e["state"] for e in expected}
    assert by_id.get("climate.ac") == "off", expected
    assert by_id.get("switch.pc") == "off", expected


def test_fr601_clean_description_still_infer():
    """无目的从句的干净描述（task C 形态）推导方向不变。"""
    mgr = _fresh_mgr()
    task = {"id": "task_c", "title": "湿度回落退出除湿",
            "description": "当湿度回落到 60% 以下时，自动关闭书房空调、退出除湿强档，"
                           "避免持续过度除湿损伤设备与家具。",
            "entity_ids": ["climate.ac"]}
    expected = mgr._infer_postconditions(task, "触发: x\n动作: climate.turn_off(climate.ac)")
    assert expected and expected[0]["state"] == "off", expected


# ── F-R6-T9-01：经验库精确分类 ───────────────────────────────
def test_error_kb_precise_classification():
    from autoflow_gateway.error_knowledge import classify_error
    assert classify_error("未充分验证: 【零断言】没有任何后置断言") == "zero_assertion"
    assert classify_error("未充分验证: 【前置已满足】后置条件 switch=on 已满足") == "pre_satisfied"
    assert classify_error("switch 规则含无法本地求值的 JSONata「msg.温度 > 28」，保守视为命中") == "jsonata_conservative"
    # 泛化兜底文案（已中性化）不再被误归 JSONata 类
    assert classify_error("验证存在未覆盖层（存在未经完整证实的执行路径，结论未充分验证）") != "jsonata_conservative"
# ── 经验库写入链路：失败样本必须进 error_knowledge ──────────
def _read_errkb(mgr):
    p = os.path.join(mgr.data_dir, "..", "error_knowledge", "error_knowledge.json")
    return json.load(open(p, encoding="utf-8"))


def test_error_kb_records_not_fully_verified():
    """未充分验证（B20 降级类）的提交要喂给错误知识库，供 get_suggestion 反哺。"""
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": True, "verdict": "未充分验证",
                             "fully_verified": False,
                             "warnings": ["【零断言】没有任何后置断言"]}}
    mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-exp")
    data = _read_errkb(mgr)
    assert data["stats"]["_total"] >= 1, data
    e = data["errors"][-1]
    assert e["stage"] == "not_fully_verified" and "零断言" in e["error"], e
    assert e["agent_id"] == "agent-exp" and "task_test1" in e["error"], e


def test_error_kb_records_flow_failure():
    """流程失败（编译/实体/异常类）也要入库。"""
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": False, "error": "R_unknown_entity: sensor.foo", "stage": "entity_check"}
    mgr.submit_flow("study_room", "task_test1", "触发: sensor.foo\n动作: y", "agent-exp2")
    data = _read_errkb(mgr)
    e = data["errors"][-1]
    assert e["stage"] == "entity_check" and "R_unknown_entity" in e["error"], e


def test_error_kb_records_gate_rejected():
    """验收拦截（verdict=拦截）入库，带拦截原因。"""
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": False, "verdict": "拦截",
                             "fully_verified": False,
                             "reasons": ["[未过] climate.ac 期望=on 实测=off"]}}
    mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-exp3")
    data = _read_errkb(mgr)
    e = data["errors"][-1]
    assert e["stage"] == "gate_rejected" and "期望=on" in e["error"], e


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
