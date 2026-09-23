#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledge_evo 单测（8 条必测 + 2 条闭环钩子补充 + 1 条契约 guard）。

全部使用 tmp_path 临时目录，不依赖 HA / NR / 网络（C5）。
"""
import os
import sys
from datetime import datetime

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:  # 优先按包导入（正常 CI 形态）
    from autoflow_gateway import knowledge_evo
    from autoflow_gateway.error_knowledge import ErrorKnowledgeStore
except Exception:  # pragma: no cover - 回退：把包目录当顶层模块路径
    _PKG = os.path.join(_SRC, "autoflow_gateway")
    if _PKG not in sys.path:
        sys.path.insert(0, _PKG)
    import knowledge_evo
    from error_knowledge import ErrorKnowledgeStore

ke = knowledge_evo

UNKNOWN_ERROR = "unknown entity light.x not found"
SYNTAX_ERROR = "syntax error at line 3"


def _store(tmp_path, **kw):
    return ke.EvoErrorKnowledgeStore(str(tmp_path), **kw)


# ── §4 必测 8 条 ────────────────────────────────────────────────────────────
def test_attach_prior_art_injects_keys(tmp_path):
    store = _store(tmp_path)
    store.record(dsl="flow: service light.turn_on target light.x",
                 error_msg=UNKNOWN_ERROR, stage="compile",
                 agent_id="agent_1", proposal_id="p1")
    result = {"ok": False, "stage": "compile", "error": UNKNOWN_ERROR,
              "result_kind": "compile_error"}
    out = ke.attach_prior_art(result, store, agent_id="agent_1",
                              dsl="flow: service light.turn_on target light.x")
    assert out is result                       # 原地注入 + 返回同一对象
    pa = out["prior_art"]
    assert pa["error_type"] == "unknown_entity"
    assert pa["suggestion"]
    assert isinstance(pa["similar_cases"], list) and pa["similar_cases"]
    assert isinstance(datetime.fromisoformat(pa["readback_attached_at"]), datetime)
    assert pa["total_same_type"] >= 1
    assert pa["recurring"] is False and pa["recurring_hint"] == ""
    # 脱敏：每条只含 5 个键，无 agent 凭证
    for c in pa["similar_cases"]:
        assert set(c) == {"id", "error_type", "timestamp", "dsl", "error"}
        assert len(c["dsl"]) <= 120 and len(c["error"]) <= 120
    # 既有字段未被改动（C2）
    assert out["stage"] == "compile" and out["result_kind"] == "compile_error" \
        and out["error"] == UNKNOWN_ERROR


def test_attach_prior_art_demotes_resolved(tmp_path):
    store = _store(tmp_path)
    ids = []
    for i in range(5):
        r = store.record(dsl=f"flow variant {i} uses light.x", error_msg=UNKNOWN_ERROR,
                         stage="compile", agent_id="agent_2")
        ids.append(r["id"])
    for rid in ids[:3]:                        # 失效前 3 条
        assert store.mark_resolved(rid, "fixed", by="tester")["ok"]

    sug = store.get_suggestion(UNKNOWN_ERROR, "compile")
    cases = sug["similar_cases"]
    assert len(cases) == 5                     # 一条不删（C1）
    assert sug["stale_count"] == 3
    assert [c["stale"] for c in cases] == [False, False, True, True, True]
    assert [c["id"] for c in cases] == [ids[3], ids[4], ids[0], ids[1], ids[2]]

    # attach_prior_art 的脱敏视图同样「失效下沉」
    result = {"ok": False, "stage": "compile", "error": UNKNOWN_ERROR}
    pa = ke.attach_prior_art(result, store, agent_id="agent_2", dsl="")["prior_art"]
    assert [c["id"] for c in pa["similar_cases"]] == [ids[3], ids[4], ids[0], ids[1], ids[2]]


def test_recurring_flag_after_threshold(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.record(dsl=f"flow {i} light.x", error_msg=UNKNOWN_ERROR,
                     stage="compile", agent_id="agent_3")
    result = {"ok": False, "stage": "compile", "error": "unknown entity light.y not found"}
    pa = ke.attach_prior_art(result, store, agent_id="agent_3",
                             dsl="flow light.y")["prior_art"]
    assert pa["recurring"] is True
    assert pa["recurring_hint"] and "3" in pa["recurring_hint"]

    store2 = _store(tmp_path / "b")
    for i in range(2):
        store2.record(dsl=f"flow {i} light.x", error_msg=UNKNOWN_ERROR,
                      stage="compile", agent_id="agent_3")
    result2 = {"ok": False, "stage": "compile", "error": "unknown entity light.y not found"}
    pa2 = ke.attach_prior_art(result2, store2, agent_id="agent_3",
                              dsl="flow light.y")["prior_art"]
    assert pa2["recurring"] is False and pa2["recurring_hint"] == ""


def test_effect_funnel_recovery_rate(tmp_path):
    store = _store(tmp_path)
    ids = []
    for i in range(3):
        ids.append(store.record(dsl=f"flow {i} light.x", error_msg=UNKNOWN_ERROR,
                                stage="compile", agent_id="agent_4")["id"])
    ids.append(store.record(dsl="flow bad syntax", error_msg=SYNTAX_ERROR,
                            stage="compile", agent_id="agent_4")["id"])
    assert store.record_outcome(ids[1], recovered=True, followup_stage="e2e")["ok"]
    assert store.record_outcome(ids[3], recovered=True)["ok"]

    f = store.get_effect_funnel(agent_id="agent_4", days=7)
    assert f["total_errors"] == 4
    assert f["recovered"] == 2
    assert f["recovery_rate"] == 0.5
    by = f["by_error_type"]
    assert by["unknown_entity"] == {"errors": 3, "repeats": 2, "recovered": 1, "resolved": 0}
    assert by["syntax_error"] == {"errors": 1, "repeats": 0, "recovered": 1, "resolved": 0}
    assert f["repeated_same_type_within_24h"] == by["unknown_entity"]["repeats"] \
        + by["syntax_error"]["repeats"]
    assert f["resolved_count"] == 0 and f["active_count"] == 4


def test_mark_resolved_no_deletion(tmp_path):
    store = _store(tmp_path)
    rid = store.record(dsl="flow light.x", error_msg=UNKNOWN_ERROR,
                       stage="compile", agent_id="agent_5")["id"]
    before = store._load()["errors"]            # 落盘前计数
    res = store.mark_resolved(rid, "fixed", by="tester")
    assert res["ok"] and res["resolved_version"] == ke.VERSION
    after = store._load()["errors"]             # 重新读文件（验证落盘）
    assert len(after) == len(before) == 1       # 总数不减（C1）
    entry = [e for e in after if e["id"] == rid][0]
    assert entry["resolution"] == "fixed"
    assert entry["resolved_by"] == "tester"
    assert entry["resolved_at"] and entry["resolved_version"] == ke.VERSION
    assert entry["dsl"] == "flow light.x" and entry["error"] == UNKNOWN_ERROR  # 原文未动
    # 找不到 id -> ok:False，且不新增/删除任何条目
    assert store.mark_resolved("err_nope", "fixed")["ok"] is False
    assert len(store._load()["errors"]) == 1


def test_lazy_expire_obsolete(tmp_path, monkeypatch):
    monkeypatch.setattr(ke, "VERSION", "0.9.0")
    store = _store(tmp_path)
    rid = store.record(dsl="flow light.x", error_msg=UNKNOWN_ERROR,
                       stage="compile", agent_id="agent_6")["id"]
    store.mark_resolved(rid, "fixed")
    monkeypatch.setattr(ke, "VERSION", "1.0.0")
    res = store.lazy_expire()
    assert res["ok"] and res["expired_count"] == 1
    entry = [e for e in store._load()["errors"] if e["id"] == rid][0]
    assert entry["resolution"] == "obsolete"   # 版本漂移自动失效
    assert len(store._load()["errors"]) == 1    # 不删


def test_attach_prior_art_fail_safe(tmp_path):
    class BoomStore:
        def get_suggestion(self, *a, **k):
            raise RuntimeError("boom")

    result = {"ok": False, "stage": "compile",
              "error": "unknown entity light.x not found", "result_kind": "compile_error"}
    out = ke.attach_prior_art(result, BoomStore(), agent_id="a", dsl="x")
    assert out is result
    assert "prior_art" not in out                # 异常时原样返回，不缺键、不抛


def test_integration_propose_dsl_attaches(tmp_path):
    """§3.4 缝的最小复刻：失败返回前调用 attach_prior_art（用基类 store 证明缝通用）。"""

    class _MiniGateway:
        def __init__(self, data_dir):
            self.error_store = ErrorKnowledgeStore(data_dir)  # 生产 gateway 用基类

        def propose_dsl(self, dsl, agent_id=""):
            if not dsl:
                result = {"ok": False, "stage": "empty_dsl",
                          "result_kind": "validation_error", "error": "DSL 不能为空"}
                return ke.attach_prior_art(result, self.error_store,
                                           agent_id=agent_id, dsl=dsl)
            result = {"ok": False, "stage": "compile",
                      "error": UNKNOWN_ERROR, "compile_error": {"kind": "unknown_entity"},
                      "result_kind": "compile_error"}
            self.error_store.record(dsl, result["error"], result["stage"], agent_id)
            return ke.attach_prior_art(result, self.error_store,
                                       agent_id=agent_id, dsl=dsl)  # §3.4 缝

    gw = _MiniGateway(str(tmp_path))
    out = gw.propose_dsl("flow: service light.turn_on target light.x", agent_id="agent_8")
    assert out["ok"] is False and out["stage"] == "compile"
    assert "prior_art" in out
    assert out["prior_art"]["error_type"] == "unknown_entity"
    assert out["prior_art"]["suggestion"]
    assert set(out) >= {"ok", "stage", "error", "compile_error",
                        "result_kind", "prior_art"}
    out2 = gw.propose_dsl("", agent_id="agent_8")     # 空 DSL 同样带回 prior_art
    assert out2["prior_art"]["error_type"] == "empty_dsl"


# ── 补充 · 成功闭环钩子（§3.4「实现并在单测覆盖」） ─────────────────────────
def test_close_loop_success_marks_recovered(tmp_path):
    store = _store(tmp_path)
    ids = [store.record(dsl=f"flow {i} light.x", error_msg=UNKNOWN_ERROR,
                        stage="compile", agent_id="agent_9")["id"] for i in range(3)]

    res = store.close_loop_success("agent_9", "unknown_entity")
    assert res["ok"] and res["id"] == ids[2]             # 取最近一条未失效
    res2 = store.close_loop_success("agent_9", "unknown_entity")
    assert res2["ok"] and res2["id"] == ids[1]           # 已 recovered 的跳过

    f = store.get_effect_funnel(agent_id="agent_9")
    assert f["recovered"] == 2 and f["recovery_rate"] > 0
    assert store.close_loop_success("agent_nobody")["ok"] is False


def test_try_close_loop_success_safe_with_base_store(tmp_path):
    base = ErrorKnowledgeStore(str(tmp_path))            # 生产 gateway 用的是基类
    assert ke.try_close_loop_success(base, "agent_x")["ok"] is False   # 静默跳过

    class Boom:
        def close_loop_success(self, agent_id, error_type_hint=""):
            raise RuntimeError("boom")

    assert ke.try_close_loop_success(Boom(), "agent_x")["ok"] is False       # 不抛（C3）


# ── 契约 guard：C1 永不删除（grep 级断言，防止回归引入 del/.pop） ─────────────
def test_no_deletion_primitives_in_module():
    import inspect
    src = inspect.getsource(ke)
    assert "\n    del " not in src and "\n    .pop(" not in src   # 模块体不得含删除原语
    assert "def mark_resolved" in src and "def lazy_expire" in src
