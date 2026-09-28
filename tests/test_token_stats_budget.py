"""Test TokenStatsStore 预算字段（议题七 D · token 预算条）。

验证 get_stats 向后兼容地新增预算可视化字段：budget / budget_used_pct /
today_estimated_tokens / today_rounds（轮次 ≈ 当天调用次数）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from autoflow_gateway.token_stats import TokenStatsStore, DEFAULT_DAILY_TOKEN_BUDGET


def _store():
    return TokenStatsStore(tempfile.mkdtemp())


def test_budget_fields_present_and_backward_compatible():
    s = _store()
    s.record("propose-dsl", "agent_A", 4000, 1000, "dsl")   # 估算 1250 tok
    s.record("deploy-raw", "agent_A", 8000, 500, "raw")     # 估算 2125 tok
    stats = s.get_stats(days=7)
    # 既有字段不被破坏
    assert "estimated_tokens" in stats
    assert "by_agent" in stats
    assert stats["by_agent"]["agent_A"]["calls"] == 2
    # 新增预算字段
    assert stats["budget"] == DEFAULT_DAILY_TOKEN_BUDGET
    assert stats["today_estimated_tokens"] == 3375   # (12000+1500)//4
    assert stats["today_rounds"] == 2                # 当天调用次数 ≈ 轮次
    # 占用比例 = 今天估算 / 预算
    assert stats["budget_used_pct"] == round(
        100.0 * 3375 / DEFAULT_DAILY_TOKEN_BUDGET, 1)


def test_budget_pct_zero_when_no_data():
    s = _store()
    stats = s.get_stats(days=1)
    assert stats["today_estimated_tokens"] == 0
    assert stats["today_rounds"] == 0
    assert stats["budget_used_pct"] == 0.0
