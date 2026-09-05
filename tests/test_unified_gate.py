"""P3 核心聚合闸 _build_unified_gate 单元测试（#686 / WB1-A·A0）。

静态纯函数，直接经 Gateway._build_unified_gate(...) 调用，无需实例化 Gateway。
覆盖：全跳过 / vhass 占位 / vhass 失败 / e2e 通过 / e2e 断点 / e2e 异常 fail-open
/ canary mustache / canary 预存空壳 / 优先级（vhass 失败 > canary warn）。
"""
import pytest

from autoflow_gateway.gateway import Gateway


# ── 输入构造助手 ──
def _vhass_skipped():
    return {"skipped": True, "reason": "无 HA 动作或无预期条件"}


def _vhass_passed():
    return {"passed": True, "verdict": "放行", "reasons": ["ok"], "entity_count": 3}


def _vhass_failed():
    return {"passed": False, "verdict": "拦截",
            "reasons": ["[未过] light.x 期望=on 实测=off"], "failures": ["light.x"]}


def _e2e_passed():
    return {"e2e": True, "verdict": "通过", "reasons": ["E2E 路径：到达 3/3 环节"],
            "report": {"reached_count": 3, "expected_count": 3}}


def _e2e_broken():
    return {"e2e": True, "verdict": "断点", "reasons": ["断点在：n2"],
            "report": {"reached_count": 1, "expected_count": 3, "failed_at": "n2"}}


def _e2e_exception():
    # require_e2e 开启但 run_e2e_trace_raw 抛异常时写入的占位（fail-open 放行）
    return {"e2e": False, "verdict": "拦截", "error": "E2E 验证异常：..."}


def _canary_clean():
    return {"ok": True, "source": "nr_list_flows", "subflows": [
        {"id": "s1", "name": "bark", "internal_node_count": 5, "empty_shell": False,
         "has_mustache_entity": False, "internal_types": ["function"]}
    ], "empty_shells": [], "any_empty_shell": False}


def _canary_mustache():
    return {"ok": True, "source": "nr_list_flows", "subflows": [
        {"id": "s1", "name": "hist", "internal_node_count": 5, "empty_shell": False,
         "has_mustache_entity": True, "internal_types": ["api-get-history"]}
    ], "empty_shells": [], "any_empty_shell": False}


def _canary_historical_empty_shell():
    # 预存空壳（非本次部署）→ 只 warn，不 block（硬拦已由 Step 8.5 早返处理）
    return {"ok": True, "source": "nr_list_flows", "subflows": [
        {"id": "old", "name": "legacy", "internal_node_count": 0, "empty_shell": True,
         "has_mustache_entity": False, "internal_types": []}
    ], "empty_shells": ["old"], "any_empty_shell": True}


# ── 用例 ──
def test_all_skipped_is_pass():
    g = Gateway._build_unified_gate(None, None, None)
    assert g["verdict"] == "pass"
    assert g["passed"] is True
    assert g["layers"]["vhass_staging"]["ran"] is False
    assert g["layers"]["e2e_trace"]["ran"] is False
    assert g["layers"]["structure_canary"]["ran"] is False
    assert any("跳过" in n for n in g["notes"])


def test_vhass_placeholder_is_skipped_not_block():
    g = Gateway._build_unified_gate(_vhass_skipped(), None, None)
    assert g["verdict"] == "pass"
    assert g["layers"]["vhass_staging"]["ran"] is False


def test_vhass_passed_no_block():
    g = Gateway._build_unified_gate(_vhass_passed(), None, None)
    assert g["verdict"] == "pass"
    assert g["passed"] is True


def test_vhass_failed_blocks():
    g = Gateway._build_unified_gate(_vhass_failed(), None, None)
    assert g["verdict"] == "block"
    assert g["passed"] is False
    assert any("staging" in n for n in g["notes"])


def test_e2e_passed_no_block():
    g = Gateway._build_unified_gate(None, _e2e_passed(), None)
    assert g["verdict"] == "pass"
    assert g["layers"]["e2e_trace"]["ran"] is True
    assert g["layers"]["e2e_trace"]["passed"] is True


def test_e2e_broken_blocks():
    g = Gateway._build_unified_gate(None, _e2e_broken(), None)
    assert g["verdict"] == "block"
    assert g["passed"] is False
    assert any("E2E" in n for n in g["notes"])


def test_e2e_exception_failopen_pass():
    # e2e=False（异常/无法验证）→ fail-open 放行，不 block
    g = Gateway._build_unified_gate(None, _e2e_exception(), None)
    assert g["verdict"] == "pass"
    assert g["layers"]["e2e_trace"]["ran"] is False


def test_canary_mustache_is_warn():
    g = Gateway._build_unified_gate(None, None, _canary_mustache())
    assert g["verdict"] == "warn"
    assert g["passed"] is True  # warn 仍放行
    assert g["layers"]["structure_canary"]["mustache_warnings"] == 1
    assert any("mustache" in n for n in g["notes"])


def test_canary_historical_empty_shell_is_warn():
    g = Gateway._build_unified_gate(None, None, _canary_historical_empty_shell())
    assert g["verdict"] == "warn"
    assert g["passed"] is True
    assert g["layers"]["structure_canary"]["any_empty_shell"] is True


def test_canary_clean_is_pass():
    g = Gateway._build_unified_gate(None, None, _canary_clean())
    assert g["verdict"] == "pass"


def test_vhass_fail_priority_over_canary_warn():
    # vhass 失败应优先于 canary warn → block（而非只 warn）
    g = Gateway._build_unified_gate(_vhass_failed(), None, _canary_mustache())
    assert g["verdict"] == "block"
    assert g["passed"] is False


def test_full_pass_with_all_layers():
    g = Gateway._build_unified_gate(_vhass_passed(), _e2e_passed(), _canary_clean())
    assert g["verdict"] == "pass"
    assert g["passed"] is True
    assert g["layers"]["vhass_staging"]["ran"] is True
    assert g["layers"]["e2e_trace"]["ran"] is True
    assert g["layers"]["structure_canary"]["ran"] is True
