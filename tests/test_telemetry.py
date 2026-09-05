#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""telemetry 模块测试：标签推断 + 日志读写 + 汇总。"""
import os
import sys
import tempfile
import json

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.telemetry import (
    tag_action, recent, summary,
    TAG_OK, TAG_AGENT_PLAN_ERROR, TAG_SIM_MISMATCH,
    TAG_SAFETY_BLOCK, TAG_GATEWAY_ERROR,
)


def _tmplog():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.remove(path)  # 让 tag_action 自己创建
    return path


def test_ok_tag():
    r = {"ok": True, "flow_id": "abc"}
    e = tag_action("deploy_proposal", r, agent_id="deepseek++")
    assert e["tag"] == TAG_OK
    assert e["action"] == "deploy_proposal"
    assert e["agent_id"] == "deepseek++"


def test_compile_error_tags_agent_plan():
    r = {"ok": False, "stage": "compile", "error": "第 3 行: 无法识别"}
    e = tag_action("propose_dsl", r)
    assert e["tag"] == TAG_AGENT_PLAN_ERROR


def test_gate_failure_tags_sim_mismatch():
    r = {"ok": False, "stage": "gate", "error": "断言不通过 expected on/actual off"}
    e = tag_action("deploy_proposal", r)
    assert e["tag"] == TAG_SIM_MISMATCH


def test_defense_error_tags_safety_block():
    r = {"ok": False, "stage": "defense", "error": "defense: protected flow"}
    e = tag_action("deploy_proposal", r)
    assert e["tag"] == TAG_SAFETY_BLOCK


def test_deploy_error_tags_gateway_error():
    r = {"ok": False, "stage": "deploy", "error": "NR 部署失败: 500"}
    e = tag_action("deploy_proposal", r)
    assert e["tag"] == TAG_GATEWAY_ERROR


def test_log_persistence():
    path = _tmplog()
    tag_action("propose_dsl", {"ok": True}, agent_id="a1", log_path=path)
    tag_action("deploy_proposal", {"ok": False, "stage": "compile", "error": "bad"},
               agent_id="a2", log_path=path)
    entries = recent(path, n=10)
    assert len(entries) == 2
    assert entries[0]["tag"] == TAG_AGENT_PLAN_ERROR  # 最新在前
    assert entries[1]["tag"] == TAG_OK


def test_summary():
    path = _tmplog()
    tag_action("propose_dsl", {"ok": True}, log_path=path)
    tag_action("propose_dsl", {"ok": True}, log_path=path)
    tag_action("deploy_proposal", {"ok": False, "stage": "compile", "error": "x"}, log_path=path)
    tag_action("deploy_proposal", {"ok": False, "stage": "gate", "error": "y"}, log_path=path)
    s = summary(path)
    assert s["total"] == 4
    assert s["by_tag"][TAG_OK] == 2
    assert s["by_tag"][TAG_AGENT_PLAN_ERROR] == 1
    assert s["by_tag"][TAG_SIM_MISMATCH] == 1
    assert s["success_rate"] == 50.0
    assert len(s["recent_failures"]) == 2


def test_filter_by_tag():
    path = _tmplog()
    tag_action("a", {"ok": True}, log_path=path)
    tag_action("b", {"ok": False, "stage": "compile", "error": "x"}, log_path=path)
    only_failures = recent(path, tag=TAG_AGENT_PLAN_ERROR)
    assert len(only_failures) == 1
    assert only_failures[0]["action"] == "b"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\ntelemetry: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
