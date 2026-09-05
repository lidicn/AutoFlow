r"""回归：缺陷 A / C6 / C8 的质量闸与 lint 硬拦。

覆盖：
  A  · 保守 fail-closed：
       - vhass fully_verified=False → _build_unified_gate 硬拦（block）
       - verify_flow 遇 lint error 级硬伤 → verdict=block（不再 fail-open）
  C6 · function `func` 含未转义反斜杠正则转义（\d 等）→ R2-ESC error（e2e 二次编码炸）
  C8 · flow 含 effectful 节点却无任何触发源 → R_NO_TRIGGER error（流永远无法启动）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.flow_linter import lint_flow

_CANARY_SKIPPED = {"ok": True, "source": "skipped"}


@pytest.fixture
def gw():
    return Gateway()


# ───────────────────────── A · 保守 fail-closed ─────────────────────────
def test_a_fully_verified_false_blocks():
    """vhass 判过但 fully_verified=False（未证实项）→ 顶层硬拦，不再降级 warn。"""
    g = {"passed": True, "fully_verified": False, "warnings": ["未建模服务 x"]}
    r = Gateway._build_unified_gate(g, None, _CANARY_SKIPPED)
    assert r["verdict"] == "block", r
    assert r["passed"] is False
    assert any("fail-closed" in n or "硬拦" in n for n in r["notes"]), r["notes"]


def test_a_fully_verified_true_passes():
    """fully_verified=True 且无其他问题 → pass。"""
    g = {"passed": True, "fully_verified": True, "warnings": []}
    r = Gateway._build_unified_gate(g, None, _CANARY_SKIPPED)
    assert r["verdict"] == "pass", r


def test_a_verify_flow_lint_error_blocks(gw, monkeypatch):
    """verify_flow 遇 lint error（此处用重复节点 id → R16）必须硬拦，不得 fail-open 放行。"""
    monkeypatch.setattr(gw, "get_nr_subflow_integrity",
                        lambda: {"ok": True, "source": "skipped"})
    monkeypatch.setattr(gw, "run_staging_gate",
                        lambda *a, **k: {"skipped": True, "reason": "test"})
    flow = {
        "id": "f", "label": "f", "nodes": [
            {"id": "n1", "type": "inject", "z": "1", "wires": [["n2"]]},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},
            {"id": "n2", "type": "debug", "z": "1", "wires": []},  # 重复 id → R16 error
        ],
    }
    res = gw.verify_flow(flow)
    assert res["verdict"] == "block", res
    assert res["passed"] is False
    assert res["lint_error_count"] >= 1


# ───────────────────────── C6 · function 反斜杠转义 ─────────────────────────
def test_c6_function_regex_escape_flagged():
    flow = {"nodes": [
        {"id": "t", "type": "inject", "z": "1", "wires": [["f1"]]},
        {"id": "f1", "type": "function", "z": "1",
         "func": "var re = /\\d+/; msg.payload = re.test(msg.payload); return msg;",
         "wires": [[]]},
    ]}
    issues = lint_flow(flow)
    esc = [i for i in issues if i["rule"] == "R2-ESC"]
    assert esc, issues
    assert esc[0]["level"] == "error"
    assert esc[0]["node_id"] == "f1"


def test_c6_function_no_escape_clean():
    flow = {"nodes": [
        {"id": "t", "type": "inject", "z": "1", "wires": [["f1"]]},
        {"id": "f1", "type": "function", "z": "1",
         "func": "msg.payload = msg.payload || {}; return msg;",
         "wires": [[]]},
    ]}
    issues = lint_flow(flow)
    assert not [i for i in issues if i["rule"] == "R2-ESC"], issues


# ───────────────────────── C8 · 无触发源 ─────────────────────────
def test_c8_no_trigger_blocks():
    """含 api-call-service（effectful）却无任何触发源 → R_NO_TRIGGER error。"""
    flow = {"nodes": [
        {"id": "c", "type": "api-call-service", "z": "1", "domain": "light",
         "service": "turn_on", "entityId": "light.x", "wires": [[]]},
        {"id": "d", "type": "debug", "z": "1", "wires": []},
    ]}
    issues = lint_flow(flow)
    assert any(i["rule"] == "R_NO_TRIGGER" for i in issues), issues


def test_c8_with_trigger_ok():
    """有 inject 触发源 → 不报 R_NO_TRIGGER。"""
    flow = {"nodes": [
        {"id": "t", "type": "inject", "z": "1", "wires": [["c"]]},
        {"id": "c", "type": "api-call-service", "z": "1", "domain": "light",
         "service": "turn_on", "entityId": "light.x", "wires": [[]]},
    ]}
    issues = lint_flow(flow)
    assert not any(i["rule"] == "R_NO_TRIGGER" for i in issues), issues


def test_c8_subflow_definition_skipped():
    """顶层无触发源但含 subflow 定义节点 → 触发源在子流程内，不误报。"""
    flow = {"nodes": [
        {"id": "sub", "type": "subflow", "z": "1", "flow": [
            {"id": "a", "type": "inject", "z": "sub", "wires": [["b"]]},
            {"id": "b", "type": "api-call-service", "z": "sub", "domain": "light",
             "service": "turn_on", "entityId": "light.x", "wires": [[]]},
        ]},
        {"id": "d", "type": "debug", "z": "1", "wires": []},
    ]}
    issues = lint_flow(flow)
    assert not any(i["rule"] == "R_NO_TRIGGER" for i in issues), issues
