# -*- coding: utf-8 -*-
"""竞技场三个回路闭环守卫（2026-09-09 优化）。

① 经验库回读：submit_flow 失败回执必须附 knowledge_feedback
   （历史同类错误前车之鉴），成功不得附。
② 精确类别建议：pre_satisfied / jsonata_conservative 等 B20 降级族
   必须有专门建议文案，不得落入 other。
③ 种子态健康检查：种子==断言期望（B20 死区）与断言目标不可用必须被检出。
④ 效率激励：排行榜 node_count ≤6 满分（系数 1.1），≥12 触底（系数 1.0），
   无 node_count 历史题保持中性。

全程离线、零运行时副作用。运行：pytest tests/test_arena_feedback_loops.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", tempfile.mkdtemp(prefix="af_loops_env_"))

from autoflow_gateway.arena import ArenaManager
from autoflow_gateway.error_knowledge import ErrorKnowledgeStore, classify_error
from autoflow_gateway.config import reset_config

reset_config()


def _fresh_mgr():
    d = tempfile.mkdtemp(prefix="af_loopsmgr_")
    return ArenaManager(d, gateway=object())


def _inject_available(mgr, task_id="task_test1", title="测试题",
                      description="当书房温度超过30度时打开台灯"):
    p = os.path.join(mgr.data_dir, "tasks.json")
    tasks = {"tasks": [{
        "id": task_id, "arena_id": "study_room", "title": title,
        "description": description, "entity_ids": ["light.study"],
        "status": "available", "locked_by": None, "locked_at": None,
        "flow_dsl": None, "verification": None, "creativity_score": 0.9,
    }]}
    os.makedirs(mgr.data_dir, exist_ok=True)
    json.dump(tasks, open(p, "w", encoding="utf-8"), ensure_ascii=False)


# ── ① 经验库回读闭环 ─────────────────────────────────────────
def test_kb_feedback_attached_on_gate_rejected():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    # 预置一条历史同类失败（前车之鉴的数据源）
    mgr._record_error_kb(None, "触发: 温度>28\n动作: 开灯",
                         "验收拦截: [未过] 台灯 期望=on 实测=off",
                         "gate_rejected", "agent-old")
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": False, "verdict": "拦截",
                             "fully_verified": False, "reasons": ["[未过] 台灯 期望=on 实测=off"]}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-new")
    fb = r.get("knowledge_feedback")
    assert fb, f"失败回执必须附 knowledge_feedback: {r}"
    assert fb["error_type"] == "gate_failed", fb
    assert fb["suggestion"], fb
    assert fb["total_same_type"] >= 2, fb  # 预置 1 条 + 本次 1 条
    assert fb["historical_cases"], fb
    for c in fb["historical_cases"]:
        assert "dsl" not in c  # 回执瘦身：不带原始 DSL
    # 且本次失败也入了库
    assert mgr._error_kb.get_stats()["by_type"].get("gate_failed", 0) >= 2


def test_kb_feedback_attached_on_not_fully_verified():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "gate": {"passed": True, "verdict": "放行", "fully_verified": False,
                             "warnings": ["【前置已满足】light.study 种子态 on == 期望 on"]}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-nfv")
    fb = r.get("knowledge_feedback")
    assert fb, r
    # 精确特征优先于泛化 gate_failed（F-R6-T9-01 顺序纪律）
    assert fb["error_type"] == "pre_satisfied", fb
    assert "反态" in fb["suggestion"], fb


def test_kb_feedback_absent_on_success():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "node_count": 5,
        "gate": {"passed": True, "verdict": "放行", "fully_verified": True}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-ok")
    assert r.get("ok") is True
    assert "knowledge_feedback" not in r, r


# ── ② 精确类别建议文案 ───────────────────────────────────────
def test_kb_suggestions_precise_categories():
    store = ErrorKnowledgeStore(tempfile.mkdtemp(prefix="af_ek_"))
    cases = [
        ("【前置已满足】light.study 种子态 on == 期望 on", "gate_rejected",
         "pre_satisfied", "反态"),
        ("【零断言】该 flow 无可断言状态变化", "gate_rejected",
         "zero_assertion", "状态"),
        ("【巧合命中】断言在重放前已满足", "gate_rejected",
         "coincidental_hit", "状态"),
        ("规则含无法本地求值的 JSONata: msg.温度 > 28", "gate_rejected",
         "jsonata_conservative", "msg."),
    ]
    for msg, stage, want_type, want_kw in cases:
        sug = store.get_suggestion(msg, stage)
        assert sug["error_type"] == want_type, (msg, sug)
        assert want_kw in sug["suggestion"], (msg, sug)


def test_classify_error_pre_satisfied_beats_gate_failed():
    # 顺序纪律：精确特征必须先于 gate_failed 泛化匹配
    assert classify_error("验收拦截: 【前置已满足】x", "gate_rejected") == "pre_satisfied"
    assert classify_error("未充分验证: 规则含无法本地求值的 JSONata", "not_fully_verified") == "jsonata_conservative"


# ── ③ 种子态健康检查 ─────────────────────────────────────────
class _FakeStore:
    def __init__(self, states):
        self._s = states

    def get_state(self, eid):
        return self._s.get(eid)


def test_seed_health_pre_satisfied_and_unavailable():
    mgr = _fresh_mgr()
    store = _FakeStore({
        "light.study": {"state": "off"},       # == 期望 off → B20 死区
        "climate.ac": {"state": "unavailable"},  # 断言目标离线
        "light.desk": {"state": "on"},
    })
    expected = [
        {"entity_id": "light.study", "state": "off"},
        {"entity_id": "climate.ac", "state": "off"},
        {"entity_id": "light.desk", "state": "off"},  # 健康：种子 on ≠ 期望 off
    ]
    issues = mgr._seed_health_check(store, expected)
    kinds = {i["entity_id"]: i["issue"] for i in issues}
    assert kinds.get("light.study") == "pre_satisfied_seed", issues
    assert kinds.get("climate.ac") == "assertion_target_unavailable", issues
    assert "light.desk" not in kinds, issues


def test_seed_health_empty_when_healthy():
    mgr = _fresh_mgr()
    store = _FakeStore({"light.study": {"state": "on"}})
    assert mgr._seed_health_check(store, [{"entity_id": "light.study", "state": "off"}]) == []
    assert mgr._seed_health_check(store, []) == []


def test_verify_flow_attaches_seed_health():
    mgr = _fresh_mgr()
    mgr._reset_vhass = lambda *a, **k: None
    mgr._get_vhass = lambda *a, **k: _FakeStore({"light.study": {"state": "off"}})
    mgr.gateway = SimpleNamespace(propose_dsl=lambda **k: {
        "ok": True, "node_count": 6,
        "gate": {"passed": True, "verdict": "放行", "fully_verified": True}})
    task = {"id": "t", "title": "关闭台灯", "description": "关闭台灯",
            "entity_ids": ["light.study"]}
    r = mgr._verify_flow("study_room", task, "触发: x\n动作: 关灯", "agent-seed")
    assert r.get("ok") is True
    sh = r.get("seed_health")
    assert sh and sh["issues"], r
    assert sh["issues"][0]["issue"] == "pre_satisfied_seed", sh
    assert "B20" in sh["hint"], sh


# ── ④ 排行榜效率激励 ─────────────────────────────────────────
def _inject_locked_tasks(mgr, tasks):
    p = os.path.join(mgr.data_dir, "tasks.json")
    os.makedirs(mgr.data_dir, exist_ok=True)
    json.dump({"tasks": tasks}, open(p, "w", encoding="utf-8"), ensure_ascii=False)


def test_leaderboard_efficiency_factor():
    mgr = _fresh_mgr()
    _inject_locked_tasks(mgr, [
        # agent-lean：2 题，各 5 节点 → eff 1.0 → factor 1.1
        {"id": "t1", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-lean", "creativity_score": 0.8, "node_count": 5},
        {"id": "t2", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-lean", "creativity_score": 0.8, "node_count": 5},
        # agent-bloat：2 题，各 12 节点 → eff 0.5 → factor 1.0（臃肿不罚）
        {"id": "t3", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-bloat", "creativity_score": 0.8, "node_count": 12},
        {"id": "t4", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-bloat", "creativity_score": 0.8, "node_count": 24},
        # agent-legacy：1 题无 node_count（历史题）→ factor 1.0 中性
        {"id": "t5", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-legacy", "creativity_score": 0.8},
    ])
    lb = {x["agent_id"]: x for x in mgr.get_leaderboard()}
    assert lb["agent-lean"]["efficiency_factor"] == 1.1, lb["agent-lean"]
    assert lb["agent-lean"]["avg_efficiency"] == 1.0
    assert lb["agent-lean"]["score"] == round(2 * 0.8 * 1.1, 3)
    assert lb["agent-bloat"]["efficiency_factor"] == 1.0, lb["agent-bloat"]
    assert lb["agent-bloat"]["avg_efficiency"] == 0.5
    assert lb["agent-bloat"]["score"] == 1.6
    assert lb["agent-legacy"]["efficiency_factor"] == 1.0
    assert lb["agent-legacy"]["avg_efficiency"] is None
    assert lb["agent-legacy"]["score"] == 0.8
    # 精简者排在臃肿者之前（同创造力下）
    board = mgr.get_leaderboard()
    ids = [x["agent_id"] for x in board]
    assert ids.index("agent-lean") < ids.index("agent-bloat"), ids


def test_lock_records_node_count():
    mgr = _fresh_mgr()
    _inject_available(mgr)
    mgr._verify_flow = lambda *a, **k: {
        "ok": True, "node_count": 7,
        "gate": {"passed": True, "verdict": "放行", "fully_verified": True}}
    r = mgr.submit_flow("study_room", "task_test1", "触发: x\n动作: y", "agent-nc")
    assert r.get("ok") is True
    tasks = json.load(open(os.path.join(mgr.data_dir, "tasks.json"), encoding="utf-8"))["tasks"]
    t = [x for x in tasks if x["id"] == "task_test1"][0]
    assert t["status"] == "locked"
    assert t.get("node_count") == 7, t
