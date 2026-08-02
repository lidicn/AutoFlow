"""A1 回归：黑/白 → 单入口 MCP 合并。

验证「单用户端点 /mcp 承载全部用户工具 + 12 把部署/自检刀，tools/list 按 agent.mode
分层显隐；/mcp-white 为 /mcp 兼容别名；/mcp-admin 独立（仅 admin）」这一收口契约。
纯导入级验证（不启网关进程），与运行实例解耦，可随 CI 跑。

工具数契约（含 B1/B2 新增的 2 个决策取回工具 + 只读/诊断回看工具 autoflow_get_flow / autoflow_debug_read /
autoflow_get_nr_flow / autoflow_trigger_inject）：
  · 用户工具 25（含 autoflow_request_decision / autoflow_get_decision /
    autoflow_list_decisions 决策闭环三件套，autoflow_get_flow / autoflow_debug_read /
    autoflow_get_nr_flow / autoflow_trigger_inject 诊断回看；其中 autoflow_debug_read /
    autoflow_get_nr_flow / autoflow_trigger_inject 仅注册于 /mcp，不进 admin）
  · 单用户端点 /mcp = 25 用户 + 13 刀 = 38（刀含 WB1-F/#694 的 autoflow_apply /
    autoflow_apply_rollback、CB7/#692 的 autoflow_apply_state_from_debug 胶水，及 #701 的
    autoflow_get_trace apply 轨迹读取刀）
  · /mcp-admin = 22 用户 + 13 刀 + 7 运维 = 42（autoflow_debug_read / autoflow_get_nr_flow /
    autoflow_trigger_inject 仅注册于 /mcp，不进 admin；golden/acceptance 评测杠杆已迁 archive，见 C4）
  · black 经 tools/list 过滤后见 25 用户工具（13 把刀已隐藏）
"""
import os
import sys
import json
import unittest

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import mcp_server as ms  # noqa: E402

_KNIVES = {
    "autoflow_deploy_raw", "autoflow_validate_flow", "autoflow_simulate_flow",
    "autoflow_run_e2e_trace", "autoflow_modify_flow", "autoflow_commit_ha_service",
    "autoflow_create_subflow",
    "autoflow_set_tab_state", "autoflow_verify_flow",
    "autoflow_apply", "autoflow_apply_rollback", "autoflow_apply_state_from_debug",
    "autoflow_get_trace",
}


class TestSingleEntryMerge(unittest.TestCase):
    def test_no_mcp_white_variable(self):
        # 合并后不应再残留独立白箱服务器实例
        self.assertFalse(hasattr(ms, "mcp_white"), "mcp_white 实例应已移除")

    def test_user_endpoint_carries_all_tools(self):
        names = {t.name for t in ms.mcp._tool_manager.list_tools()}
        self.assertEqual(len(names), 38, "单用户端点 /mcp 应为 38 工具（25 用户 + 13 刀）")
        self.assertTrue(_KNIVES.issubset(names), "14 把部署/自检刀必须在 /mcp 上注册")

    def test_admin_endpoint_unchanged(self):
        names = {t.name for t in ms.mcp_admin._tool_manager.list_tools()}
        self.assertEqual(len(names), 42, "/mcp-admin 应为 42 工具（22 用户 + 13 刀 + 7 运维）")
        self.assertTrue(_KNIVES.issubset(names))

    def test_filter_strips_knives_for_black(self):
        all_names = sorted(t.name for t in ms.mcp._tool_manager.list_tools())
        fake = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": n} for n in all_names]}}
        out = json.loads(ms._filter_tools_list(json.dumps(fake).encode()))
        visible = {t["name"] for t in out["result"]["tools"]}
        self.assertEqual(visible, set(all_names) - _KNIVES)
        self.assertEqual(len(visible), 25, "black 经 tools/list 过滤后应仅见 25 用户工具")

    def test_filter_passthrough_non_tools_list(self):
        self.assertEqual(ms._filter_tools_list(b""), b"", "空响应原样透传")
        probe = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"foo": "bar"}}).encode()
        self.assertEqual(ms._filter_tools_list(probe), probe, "非 tools/list 响应原样透传")
        bad = b"not-json"
        self.assertEqual(ms._filter_tools_list(bad), bad, "无法解析的响应原样透传")


if __name__ == "__main__":
    unittest.main()
