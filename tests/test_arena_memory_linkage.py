# -*- coding: utf-8 -*-
"""竞技场记忆联动②③守卫（2026-09-10）。

② agent 战绩画像：get_agent_profile 聚合提交/锁题/错误三源，HTTP 端点透出。
③ 考官校准回路：propose_task 被考官拒绝的题目入经验库（examiner_rejected），
   _examiner_calibration 取最近样本作 few-shot 注入 _llm_logic_review prompt。
   knowledge_feedback 在知识库无同类历史时兜底到 experience.suggest_fix（单出口）。

全程离线、零运行时副作用。运行：pytest tests/test_arena_memory_linkage.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", tempfile.mkdtemp(prefix="af_link_env_"))

from autoflow_gateway.arena import ArenaManager, _llm_logic_review
from autoflow_gateway.error_knowledge import classify_error
from autoflow_gateway.experience import ExperienceLogger
from autoflow_gateway.config import reset_config

reset_config()


def _fresh_mgr():
    d = tempfile.mkdtemp(prefix="af_linkmgr_")
    return ArenaManager(d, gateway=object())


def _inject_arena(mgr):
    p = os.path.join(mgr.data_dir, "arenas.json")
    os.makedirs(mgr.data_dir, exist_ok=True)
    arena = {
        "id": "study_room", "name": "书房竞技场", "phase": "free_writing",
        "locked_task_count": 0, "phase2_threshold": 20,
        "devices": [
            {"entity_id": "light.desk", "friendly_name": "台灯", "state": "off",
             "domain": "light"},
            {"entity_id": "sensor.temp", "friendly_name": "温度传感器", "state": "25",
             "domain": "sensor"},
            {"entity_id": "climate.ac", "friendly_name": "空调", "state": "off",
             "domain": "climate"},
        ],
    }
    json.dump({"arenas": [arena]}, open(p, "w", encoding="utf-8"), ensure_ascii=False)


# ── ② agent 战绩画像 ─────────────────────────────────────────
def test_agent_profile_aggregation():
    mgr = _fresh_mgr()
    # 锁题面：2 题 locked（node_count 5/7）+ 1 题别人的
    p = os.path.join(mgr.data_dir, "tasks.json")
    os.makedirs(mgr.data_dir, exist_ok=True)
    json.dump({"tasks": [
        {"id": "t1", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-x", "creativity_score": 0.8, "node_count": 5,
         "title": "题一"},
        {"id": "t2", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-x", "creativity_score": 0.6, "node_count": 7,
         "title": "题二"},
        {"id": "t3", "arena_id": "study_room", "status": "locked",
         "locked_by": "agent-y", "creativity_score": 0.9, "node_count": 3,
         "title": "别人的题"},
    ]}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    # 提交面：3 次尝试 1 成 2 败
    sp = os.path.join(mgr.data_dir, "submissions.json")
    json.dump({"submissions": [
        {"agent_id": "agent-x", "task_id": "t1", "success": True, "stage": "gate"},
        {"agent_id": "agent-x", "task_id": "t2", "success": False,
         "stage": "compile", "error": "语法错误示例"},
        {"agent_id": "agent-x", "task_id": "t2", "success": False,
         "stage": "gate_rejected", "error": "验收拦截示例"},
        {"agent_id": "agent-y", "task_id": "t3", "success": True, "stage": "gate"},
    ]}, open(sp, "w", encoding="utf-8"), ensure_ascii=False)

    prof = mgr.get_agent_profile("agent-x")
    assert prof["ok"] is True
    s = prof["submissions"]
    assert s["total"] == 3 and s["success"] == 1
    assert s["pass_rate"] == 33.3
    assert s["fail_by_stage"] == {"compile": 1, "gate_rejected": 1}
    assert len(s["recent_failures"]) == 2
    lk = prof["locked"]
    assert lk["count"] == 2
    assert lk["avg_creativity"] == 0.7  # (0.8+0.6)/2
    # eff = (clamp(6/5)=1.0 + clamp(6/7)≈0.857) / 2 ≈ 0.929
    assert lk["avg_efficiency"] == round((1.0 + 6.0 / 7.0) / 2, 3)
    assert "题一" in lk["titles"]
    assert prof["hint"], prof
    # 未知 agent 也能给画像（零历史）
    prof2 = mgr.get_agent_profile("nobody")
    assert prof2["ok"] is True and prof2["submissions"]["total"] == 0
    assert prof2["locked"]["count"] == 0


# ── ③ 考官校准回路 ───────────────────────────────────────────
def test_examiner_rejection_recorded_and_calibrated():
    mgr = _fresh_mgr()
    _inject_arena(mgr)
    # 文不对题必拒：描述不提任何所列设备
    r = mgr.propose_task(
        "study_room", "台灯使用时长记录",
        "当空调温度超过30度的时候自动打开新风系统。",
        ["light.desk", "sensor.temp"], "agent-cal")
    assert r.get("ok") is False, r
    assert "考官判定题目逻辑不成立" in r.get("reason", ""), r
    # 拒绝样本入库（examiner_rejected 精确分类）
    kb = mgr._error_kb.list_errors(error_type="examiner_rejected")
    assert kb["total"] >= 1, kb
    assert "台灯使用时长记录" in kb["errors"][0]["error"]
    # 校准器取到该样本
    cal = mgr._examiner_calibration()
    assert cal and any("台灯使用时长记录" in c["example"] for c in cal), cal


def test_examiner_calibration_injected_into_prompt(monkeypatch):
    captured = {}

    def fake_chat_sync(messages, max_tokens=300, **kw):
        captured["prompt"] = messages[0]["content"]
        return '{"logic_ok": true, "issues": [], "novelty": 0.7}'

    import autoflow_gateway.llm_client as lc
    monkeypatch.setattr(lc, "chat_sync", fake_chat_sync)
    cal = [{"example": "【考官拒绝】高温自动开风扇：文不对题", "reason": "同类逻辑缺陷"}]
    r = _llm_logic_review("有人开灯", "当有人时打开台灯。", ["light.desk"],
                          [{"entity_id": "light.desk", "friendly_name": "台灯"}],
                          calibration=cal)
    assert r["logic_ok"] is True
    assert "历史审题教训" in captured["prompt"]
    assert "高温自动开风扇" in captured["prompt"]
    # 无校准样本 → prompt 不含该段（零开销）
    captured.clear()
    _llm_logic_review("有人开灯", "当有人时打开台灯。", ["light.desk"],
                      [{"entity_id": "light.desk", "friendly_name": "台灯"}])
    assert "历史审题教训" not in captured["prompt"]


def test_classify_examiner_rejected_precise():
    # 精确分类优先于泛化规则
    assert classify_error("【考官拒绝】X：文不对题", "examiner_rejected") == "examiner_rejected"


# ── knowledge_feedback 兜底（单出口合并） ────────────────────
def test_kb_feedback_falls_back_to_experience():
    mgr = _fresh_mgr()  # 知识库为空
    fb = mgr._kb_feedback("完全陌生的错误信息xyz", "compile")
    assert fb, "知识库无历史也必须给出指引（experience 兜底）"
    assert fb["source"] == "experience_fallback", fb
    assert fb["suggestion"], fb


def test_suggest_fix_finds_new_layout():
    d = tempfile.mkdtemp(prefix="af_exp_")
    ek_dir = os.path.join(d, "error_knowledge")
    os.makedirs(ek_dir)
    json.dump({"errors": [{"error": "湿度超标开除湿插座失败", "dsl": "触发: 湿度>80",
                            "error_type": "gate_failed"}]},
              open(os.path.join(ek_dir, "error_knowledge.json"), "w",
                   encoding="utf-8"), ensure_ascii=False)
    logger = ExperienceLogger(d)  # base_dir = d/experience
    sf = logger.suggest_fix("湿度超标开除湿插座失败", "gate_rejected")
    assert sf["ok"] is True
    assert sf["similar_errors"], sf  # 新布局的错误库必须被找到
