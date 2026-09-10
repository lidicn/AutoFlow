# -*- coding: utf-8 -*-
"""竞技场 ↔ memory-agent 联动消费点守卫（P1-b，ROADMAP #4，2026-09-10）。

覆盖验收点：
  C1 acp_client 通道配置：URL 推导（acp_url 去 /acp）、未配置三函数 ok=False 不抛。
  C2 读灵感：ACP 工具 get_arena_inspiration 结果解析（含 ```json 围栏兜底）。
  C3 读灵感降级：对端失败/空 → 用确定性 HTTP 快照自建灵感（同构输出）。
  C4 读快照：HTTP GET /api/arena/snapshots/{id} 走 Bearer，异常不抛。
  C5 落战报：8 参数齐备、token_used/used_memory_tools 透传、JSON 围栏可解析。
  C6 网关接线：fetch_memory_inspiration 记录 A/B 遥测；_push_memory_report
     未配置通道时 no-op、配置时把 success(按 fully_verified)/used_memory_tools 上报。
  C7 submit_flow 尾部触发写侧（异步线程），且写侧失败绝不影响验收回执。

全程离线（monkeypatch 网络层），零运行时副作用。
运行：pytest tests/test_arena_memory_channel.py
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import acp_client
from autoflow_gateway.arena import ArenaManager


def _cfg(url="", acp_url="", token="arena_tok"):
    return SimpleNamespace(memory_agent_arena_url=url,
                           memory_worker_acp_url=acp_url,
                           memory_agent_arena_token=token)


_SNAP = {"ok": True, "snapshot": {"version": 2, "snapshot_json": json.dumps({
    "arena_id": "study_room", "room": "书房", "history_days": 30, "total_events": 2000,
    "entities": [
        {"label": "设备14", "original": "binary_sensor.motion_a",
         "count": 354, "busy_hours": [11, 9, 14]},
        {"label": "设备21", "original": "binary_sensor.contact_b",
         "count": 276, "busy_hours": [21, 19, 22]},
    ]}, ensure_ascii=False)}}


# ── C1 通道配置 ──────────────────────────────────────────────
def test_arena_base_url_derivation():
    assert acp_client._arena_ctx(_cfg(url="http://a:1/x/"))[0] == "http://a:1/x"
    # 缺 arena_url → 由 acp_url 推导（同一实例）
    assert acp_client._arena_ctx(_cfg(acp_url="http://h:8086/acp"))[0] == "http://h:8086"
    assert acp_client._arena_ctx(_cfg(acp_url="http://h:8086"))[0] == "http://h:8086"
    assert acp_client.arena_configured(_cfg(acp_url="http://h:8086/acp")) is True
    assert acp_client.arena_configured(_cfg(acp_url="http://h:8086/acp", token="")) is False
    assert acp_client.arena_configured(_cfg(token="arena_tok")) is False  # 无 base


def test_arena_api_unconfigured_is_safe():
    empty = _cfg(url="", acp_url="", token="")
    assert acp_client.arena_fetch_inspiration("r", cfg=empty)["ok"] is False
    assert acp_client.arena_record_result("r", "t", "d", "dsl", True, 0, "a",
                                          cfg=empty)["ok"] is False
    assert acp_client.arena_fetch_snapshot("r", cfg=empty)["ok"] is False


# ── C2 读灵感：结果解析 ──────────────────────────────────────
def _cap_prompt(monkeypatch, payload, name="get_arena_inspiration"):
    seen = {}

    def fake(url, token, messages, **kw):
        seen["url"] = url
        seen["token"] = token
        seen["prompt"] = messages[0]["content"]
        return {"ok": True, "blocks": [{"type": "tool_call", "name": name,
                                        "result": payload}], "text": ""}

    monkeypatch.setattr(acp_client, "prompt_acp", fake)
    return seen


def test_fetch_inspiration_parses_json(monkeypatch):
    seen = _cap_prompt(monkeypatch,
                       json.dumps({"ok": True, "count": 1, "items": [{"id": "ins_001"}]}))
    r = acp_client.arena_fetch_inspiration("study_room", 3, cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is True and r["count"] == 1 and r["items"][0]["id"] == "ins_001"
    assert seen["url"].endswith("/acp") and seen["token"] == "arena_tok"
    assert "get_arena_inspiration" in seen["prompt"] and "study_room" in seen["prompt"]


def test_fetch_inspiration_parses_fenced_json(monkeypatch):
    _cap_prompt(monkeypatch,
                "```json\n" + json.dumps({"ok": True, "count": 2, "items": [{"id": "a"}, {"id": "b"}]})
                + "\n```")
    r = acp_client.arena_fetch_inspiration("study_room", cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is True and r["count"] == 2


def test_fetch_inspiration_empty_is_not_ok(monkeypatch):
    _cap_prompt(monkeypatch, json.dumps({"ok": True, "count": 0, "items": []}))
    r = acp_client.arena_fetch_inspiration("study_room", cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is False and r["items"] == []


# ── C3 读灵感降级：快照自建（同构） ──────────────────────────
def test_inspiration_from_snapshot_isomorphic():
    items = ArenaManager._inspiration_from_snapshot(_SNAP)
    assert len(items) == 2
    a = items[0]
    assert a["entity_hints"] == ["设备14"]                 # 散文用匿名别名
    assert a["entity_ids"] == ["binary_sensor.motion_a"]   # 机器用真实 entity_id
    assert "354" in a["description"] and "11:00" in a["description"]
    assert a["creativity_score"] == 1.0
    assert items[0]["id"] == "ins_001" and items[1]["id"] == "ins_002"
    # 脏输入健壮
    assert ArenaManager._inspiration_from_snapshot({}) == []
    assert ArenaManager._inspiration_from_snapshot(
        {"snapshot": {"snapshot_json": "not-json"}}) == []


def test_fetch_inspiration_falls_back_to_snapshot(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())
    monkeypatch.setattr(acp_client, "arena_fetch_inspiration",
                        lambda *a, **k: {"ok": False, "error": "对端未返回可用灵感", "items": []})
    monkeypatch.setattr(acp_client, "arena_fetch_snapshot", lambda *a, **k: _SNAP)
    r = mgr.fetch_memory_inspiration("study_room", limit=1, agent_id="agent-x")
    assert r["ok"] is True and r["source"] == "snapshot-fallback"
    assert len(r["items"]) == 1 and r["items"][0]["entity_ids"] == ["binary_sensor.motion_a"]
    # 降级路径也算「真实读取」，遥测必须记账
    assert ("study_room", "agent-x") in mgr._memory_inspiration_seen


def test_fetch_inspiration_prefers_acp(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())
    monkeypatch.setattr(acp_client, "arena_fetch_inspiration",
                        lambda *a, **k: {"ok": True, "count": 5,
                                         "items": [{"id": "ins_001", "entity_ids": ["light.x"]}]})
    called = {"snap": False}
    monkeypatch.setattr(acp_client, "arena_fetch_snapshot",
                        lambda *a, **k: called.__setitem__("snap", True) or _SNAP)
    r = mgr.fetch_memory_inspiration("study_room", agent_id="agent-x")
    assert r["source"] == "memory-agent" and r["count"] == 5
    assert called["snap"] is False  # 首选成功则不触发降级


def test_fetch_inspiration_all_fail(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())
    monkeypatch.setattr(acp_client, "arena_fetch_inspiration",
                        lambda *a, **k: {"ok": False, "error": "未配置竞技场联动通道", "items": []})
    monkeypatch.setattr(acp_client, "arena_fetch_snapshot",
                        lambda *a, **k: {"ok": False, "error": "无快照"})
    r = mgr.fetch_memory_inspiration("study_room", agent_id="agent-x")
    assert r["ok"] is False and r["items"] == [] and r["hint"]
    assert ("study_room", "agent-x") not in mgr._memory_inspiration_seen


# ── C4/C5 acp_client 读写细节 ────────────────────────────────
def test_fetch_snapshot_uses_bearer_and_survives_error(monkeypatch):
    seen = {}

    class _Resp:
        headers = {"content-type": "application/json"}

        def read(self):
            return json.dumps(_SNAP).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(acp_client.urllib.request, "urlopen", fake_urlopen)
    r = acp_client.arena_fetch_snapshot("study_room", cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is True
    assert seen["url"] == "http://h:8086/api/arena/snapshots/study_room"
    assert seen["auth"] == "Bearer arena_tok"

    def boom(req, timeout=None):
        raise OSError("conn refused")

    monkeypatch.setattr(acp_client.urllib.request, "urlopen", boom)
    assert acp_client.arena_fetch_snapshot("study_room",
                                           cfg=_cfg(acp_url="http://h:8086/acp"))["ok"] is False


def test_record_result_passes_all_params(monkeypatch):
    seen = {}

    def fake(url, token, messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return {"ok": True, "blocks": [{"type": "tool_call", "name": "record_arena_result",
                                        "result": json.dumps({"ok": True, "recorded": True,
                                                              "insight_id": "arena_x"})}],
                "text": ""}

    monkeypatch.setattr(acp_client, "prompt_acp", fake)
    # 多行 DSL + 中文：原样嵌入 prompt（保真依赖模型照抄，这里验嵌入完整）
    dsl = "当 门磁 状态 变为 关闭\n则 开 学习灯\n并 关 台灯"
    r = acp_client.arena_record_result("study_room", "题目A", "描述B", dsl, True, 4242,
                                       "agent-x", ["get_arena_inspiration"],
                                       cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is True and r["insight_id"] == "arena_x"
    p = seen["prompt"]
    for key in ("arena_id", "task_title", "task_description", "flow_dsl",
                "success", "token_used", "agent_id", "used_memory_tools"):
        assert key in p, key
    assert "当 门磁 状态 变为 关闭" in p and "4242" in p


def test_record_result_unconfirmed_is_not_ok(monkeypatch):
    monkeypatch.setattr(acp_client, "prompt_acp",
                        lambda *a, **k: {"ok": True, "blocks": [
                            {"type": "tool_call", "name": "record_arena_result",
                             "result": json.dumps({"error": "工具执行失败"})}], "text": ""})
    r = acp_client.arena_record_result("r", "t", "d", "dsl", True, 0, "a",
                                       cfg=_cfg(acp_url="http://h:8086/acp"))
    assert r["ok"] is False and "失败" in (r.get("error") or "")


# ── C6 网关写侧 ──────────────────────────────────────────────
def test_push_report_noop_when_unconfigured(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())
    calls = []
    monkeypatch.setattr(acp_client, "arena_configured", lambda cfg=None: False)
    monkeypatch.setattr(acp_client, "arena_record_result",
                        lambda **kw: calls.append(kw) or {"ok": True})
    mgr._push_memory_report("study_room", {"title": "t", "description": "d"},
                            "dsl", {"ok": True, "gate": {"passed": True}}, "a")
    assert calls == []  # 未配置 → 完全 no-op


def test_push_report_success_uses_fully_verified(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())
    calls = []
    monkeypatch.setattr(acp_client, "arena_configured", lambda cfg=None: True)
    monkeypatch.setattr(acp_client, "arena_record_result",
                        lambda **kw: calls.append(kw) or {"ok": True})
    # 取过灵感 → used_memory_tools 应含 get_arena_inspiration
    mgr._memory_inspiration_seen.add(("study_room", "agent-x"))
    # passed 但未充分验证 → success 必须为 False（B20/B22 诚实性）
    mgr._push_memory_report("study_room", {"title": "题一", "description": "描述"},
                            "DSL", {"ok": True, "gate": {"passed": True, "fully_verified": False},
                                    "_telemetry": {"estimated_tokens": 999}}, "agent-x")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["success"] is False
    assert kw["token_used"] == 999
    assert kw["used_memory_tools"] == ["get_arena_inspiration"]
    assert kw["arena_id"] == "study_room" and kw["agent_id"] == "agent-x"
    # 未取过灵感的 agent → 空列表（A/B 对照的自变量）
    calls.clear()
    mgr._push_memory_report("study_room", {"title": "题二", "description": "d"},
                            "DSL", {"ok": True, "gate": {"passed": True, "fully_verified": True}},
                            "agent-y")
    assert calls[0]["used_memory_tools"] == [] and calls[0]["success"] is True


def test_push_report_never_raises(monkeypatch):
    mgr = ArenaManager(tempfile.mkdtemp(prefix="af_chan_"), gateway=object())

    def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(acp_client, "arena_configured", lambda cfg=None: True)
    monkeypatch.setattr(acp_client, "arena_record_result", boom)
    mgr._push_memory_report("study_room", {"title": "t", "description": "d"},
                            "dsl", {"ok": True}, "a")  # 不应抛


# ── C7 submit_flow 尾部触发写侧 ──────────────────────────────
def test_submit_flow_triggers_async_report(monkeypatch):
    d = tempfile.mkdtemp(prefix="af_chan_")
    mgr = ArenaManager(d, gateway=object())
    os.makedirs(mgr.data_dir, exist_ok=True)
    json.dump({"arenas": [{"id": "study_room", "name": "书房", "phase": "free_writing",
                           "locked_task_count": 0, "phase2_threshold": 20, "devices": []}]},
              open(os.path.join(mgr.data_dir, "arenas.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    json.dump({"tasks": [{"id": "t1", "arena_id": "study_room", "status": "available",
                          "title": "题一", "description": "有人开灯", "entity_ids": ["light.x"]}]},
              open(os.path.join(mgr.data_dir, "tasks.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    monkeypatch.setattr(mgr, "_verify_flow",
                        lambda *a, **k: {"ok": True, "gate": {"passed": True, "fully_verified": True}})
    done = threading.Event()
    got = {}

    def rec(arena_id, task, dsl, result, agent_id):
        got.update(arena_id=arena_id, task=task, dsl=dsl, agent_id=agent_id)
        done.set()

    monkeypatch.setattr(mgr, "_push_memory_report", rec)
    r = mgr.submit_flow("study_room", "t1", "当 有人 则 开 灯", "agent-x")
    assert r.get("ok") is True
    assert done.wait(3.0), "写侧应在 submit_flow 尾部异步触发"
    assert got["arena_id"] == "study_room" and got["agent_id"] == "agent-x"
    assert got["dsl"] == "当 有人 则 开 灯"
    assert got["task"]["title"] == "题一"
