# -*- coding: utf-8 -*-
"""议题二 · MCP 工具路径 token / 调用量埋点守卫。

背景：既有 `_record_token`（webui.py）只覆盖 WebUI REST Pro 路径（propose-dsl /
deploy-raw），agent 经 MCP 工具（autoflow_*）跑的流量完全没计入 →
① v2.3.0 出口指标「Pro 典型任务 token/轮次下降」无法举证；
② v3.0 工具面收敛缺「各工具真实调用量」数据（DCD 议题二裁定：数据先行，不靠直觉删工具）。

本守卫守住三件事：
  V1 覆盖：mcp / mcp_admin **全部**工具都被包埋点（单一接缝，新增工具自动纳入，不遗漏）；
  V2 落库：真实调用一次只读工具 → token_stats 记入 mode="mcp" 且按工具名分端点，
           且只记 1 次（顺带证明幂等——双层包装会重复计数）；
  V3 无害：包装保留函数签名/元数据（wrapper 的 _af_orig/_af_name 不得出现在签名里，
           否则参数校验与工具契约会被破坏）。

跑法：python -m pytest tests/test_mcp_token_stats.py -q
"""
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_mcpstats_test_")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", _TMP)

import unittest  # noqa: E402

from autoflow_gateway import mcp_server as ms  # noqa: E402
from autoflow_gateway.config import get_config  # noqa: E402

# 只读、无副作用、无需认证的工具（用于端到端落库验证）
_PROBE_TOOL = "autoflow_dsl_help"


def _stats_path() -> str:
    """埋点文件真实落点（get_config 可能被其它用例先导入，故以实际配置为准，不假设 tmp）。"""
    return os.path.join(get_config().data_dir, "token_stats", "token_stats.json")


def _today_bucket(tool_name: str):
    """返回今日该工具与 mcp 模式的计数（不存在则 0）。

    P1-5（审计报告第二批）：token_stats 存储改为 append-only JSONL
    （O(1) 追加、天然并发安全、按天轮转），旧的按天聚合 JSON 已废弃
    （首次 record/get_stats 时自动迁移并归档为 token_stats.json.migrated）。
    故这里改走公开读接口 get_stats()，不再直读内部文件布局——
    直读内部布局等于把「存储格式」钉死在测试里，格式一演进守卫就假红（本例即教训）。
    落库真实性仍被守住：get_stats 读的就是磁盘上的 JSONL。
    """
    from autoflow_gateway.token_stats import TokenStatsStore
    store = TokenStatsStore(os.path.dirname(_stats_path()))
    st = store.get_stats(days=1)
    ep = st.get("by_endpoint", {}).get(tool_name, {}).get("calls", 0)
    mode = st.get("by_mode", {}).get("mcp", {}).get("calls", 0)
    return ep, mode


class TestMcpTokenStats(unittest.TestCase):
    def test_v1_all_tools_instrumented(self):
        """mcp / mcp_admin 全部工具都被包埋点（新增工具自动纳入，不留漏网）。"""
        for srv_name, srv in (("mcp", ms.mcp), ("mcp_admin", ms.mcp_admin)):
            tm = getattr(srv, "_tool_manager", None)
            self.assertIsNotNone(tm, f"{srv_name} 缺少 _tool_manager")
            tools = tm.list_tools()
            self.assertGreater(len(tools), 0, f"{srv_name} 无工具")
            unwrapped = [t.name for t in tools
                         if not getattr(getattr(t, "fn", None),
                                        "_af_token_instrumented", False)]
            self.assertEqual([], unwrapped,
                             f"{srv_name} 有工具未被包埋点（会漏计调用量）：{unwrapped}")

    def test_v2_call_records_mode_mcp_exactly_once(self):
        """真实调用 → 记入 mode=mcp + 按工具名分端点，且只记 1 次（幂等、不重复计数）。"""
        tools = {t.name: t for t in ms.mcp._tool_manager.list_tools()}
        self.assertIn(_PROBE_TOOL, tools, f"探针工具 {_PROBE_TOOL} 不存在")
        tool = tools[_PROBE_TOOL]

        before_ep, before_mode = _today_bucket(_PROBE_TOOL)
        tool.fn()  # 真实调用（只读，无副作用）
        after_ep, after_mode = _today_bucket(_PROBE_TOOL)

        self.assertEqual(after_ep - before_ep, 1,
                         "工具调用应恰好记 1 次（0=没埋上；≥2=被重复包装/重复计数）")
        self.assertEqual(after_mode - before_mode, 1,
                         "应以 mode='mcp' 记入（与 WebUI 的 dsl/raw 路径区分）")

    def test_v3_wrapper_preserves_signature(self):
        """包装不得污染函数签名（wrapper 的内部默认参数绝不能出现在签名中）。"""
        checked = 0
        for srv in (ms.mcp, ms.mcp_admin):
            for t in srv._tool_manager.list_tools():
                params = list(inspect.signature(t.fn).parameters)
                # functools.wraps 使签名跟随原函数；若包装失效，_af_orig/_af_name 会泄漏
                self.assertNotIn("_af_orig", params,
                                 f"工具 {t.name} 签名被包装污染（参数校验/契约会破）")
                self.assertNotIn("_af_name", params,
                                 f"工具 {t.name} 签名被包装污染（参数校验/契约会破）")
                checked += 1
        self.assertGreater(checked, 0, "未检查到任何工具")


    def test_v4_get_stats_exposes_by_endpoint(self):
        """埋了还得读得出：get_stats 必须暴露 by_endpoint（各工具真实调用量）。

        否则 v3.0 工具面收敛拿不到「删哪些工具」的数据依据（议题二白埋）。
        """
        from autoflow_gateway.token_stats import TokenStatsStore

        tools = {t.name: t for t in ms.mcp._tool_manager.list_tools()}
        tool = tools[_PROBE_TOOL]
        store = TokenStatsStore(os.path.join(get_config().data_dir, "token_stats"))

        before = store.get_stats(days=1).get("by_endpoint", {}).get(_PROBE_TOOL, {}).get("calls", 0)
        tool.fn()
        after = store.get_stats(days=1).get("by_endpoint", {}).get(_PROBE_TOOL, {}).get("calls", 0)

        self.assertEqual(after - before, 1,
                         f"get_stats().by_endpoint 应含 {_PROBE_TOOL} 且只增 1 次（实得 {before}→{after}）")


if __name__ == "__main__":
    unittest.main()
