"""A1 回归：黑/白 → 单入口 MCP 合并。

验证「单用户端点 /mcp 承载全部用户工具 + 部署/自检刀，tools/list 按 agent.mode
分层显隐；/mcp-white 为 /mcp 兼容别名；/mcp-admin 独立（仅 admin）」这一收口契约。
纯导入级验证（不启网关进程），与运行实例解耦，可随 CI 跑。

★ 工具面清单的**唯一登记处**是 `tests/mcp_toolface_contract.py`（single source of truth）。
本文件**不再硬编码任何计数**：期望集合一律从契约模块导入，断言用**集合相等**
（失败时 symdiff 直指「多了谁 / 少了谁」），计数由 `len()` 派生。
改工具面 → 只改契约模块一处；漏登记 → 对账断言红并列出差异工具名。

工具数变更史（事件日志；数字由契约模块派生，不再在此复述）：
  · DEV-acp-integration #4：新增 autoflow_delegate_to_memory_worker（仅用户面 /mcp）。
  · #16：新增 autoflow_snapshot_instance / autoflow_restore_snapshot（双装饰器，列刀）。
  · v2.2.0 收口：新增 autoflow_get_inventory（仅用户面，非刀）
    + autoflow_surgical_edit（#7 手术刀编辑，双装饰器，列刀）。
  · 2026-09-22 v3.0.0 Stage D：新增 autoflow_list_apply_traces（#20 审计索引，双装饰器，列刀）；
    同批把 autoflow_trigger_inject 由「仅用户面」改为「双装饰器」（#19 决策1：收专家档）并列刀。

⚠️ 另有 stage 白名单契约见 `tests/test_contracts_surface.py::_STAGE_WHITELIST`。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)  # 与 compiler_invariants / api_spec_fixture 同法：tests 内共享 helper

os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway import mcp_server as ms  # noqa: E402
import mcp_toolface_contract as C  # noqa: E402


class TestSingleEntryMerge(unittest.TestCase):
    # ── 契约自身的自洽性（不依赖运行时）──────────────────────────────

    def test_contract_self_consistent(self):
        """设计意图内部不许自相矛盾（刀集合互斥、运维刀不得进用户面…）。"""
        problems = C.contract_self_check()
        self.assertFalse(problems, "工具面契约自相矛盾：\n  " + "\n  ".join(problems))

    def test_runtime_knives_match_contract(self):
        """★ 单一真相源的核心对账：运行时 `_DEPLOY_KNIVES` 必须与契约登记**完全一致**。

        本断言替代了此前那份「测试里手抄一份刀名单」的写法 —— 手抄副本会静默漂移，
        而且失败时只会报一个没有信息量的计数不等。
        """
        self.assertEqual(
            set(ms._DEPLOY_KNIVES), set(C.DEPLOY_KNIVES),
            C.symdiff_msg("_DEPLOY_KNIVES", C.DEPLOY_KNIVES, ms._DEPLOY_KNIVES))

    # ── 端点契约 ──────────────────────────────────────────────────

    def test_no_mcp_white_variable(self):
        # 合并后不应再残留独立白箱服务器实例
        self.assertFalse(hasattr(ms, "mcp_white"), "mcp_white 实例应已移除")

    def test_user_endpoint_carries_all_tools(self):
        names = {t.name for t in ms.mcp._tool_manager.list_tools()}
        self.assertEqual(names, set(C.EXPECTED_MCP_TOOLS),
                         C.symdiff_msg("/mcp 工具集", C.EXPECTED_MCP_TOOLS, names))
        self.assertTrue(set(C.DEPLOY_KNIVES).issubset(names), "部署/自检刀必须在 /mcp 上注册")

    def test_admin_endpoint_unchanged(self):
        names = {t.name for t in ms.mcp_admin._tool_manager.list_tools()}
        self.assertEqual(names, set(C.EXPECTED_MCP_ADMIN_TOOLS),
                         C.symdiff_msg("/mcp-admin 工具集", C.EXPECTED_MCP_ADMIN_TOOLS, names))
        self.assertTrue(set(C.DEPLOY_KNIVES).issubset(names))

    def test_acp_delegate_tool_user_face_only(self):
        """DEV-acp-integration #4：ACP 委派工具只挂用户面 /mcp，不得进管理面。

        管理面是运维刀集合，委派是普通 agent 能力；混进 admin 会让运维身份多一条
        对外发起 HTTP 的通道，属无谓的攻击面扩张。
        """
        user = {t.name for t in ms.mcp._tool_manager.list_tools()}
        admin = {t.name for t in ms.mcp_admin._tool_manager.list_tools()}
        self.assertIn("autoflow_delegate_to_memory_worker", user,
                      "delegate 工具必须在用户面 /mcp 上注册")
        self.assertNotIn("autoflow_delegate_to_memory_worker", admin,
                         "delegate 工具不得进 /mcp-admin")
        self.assertNotIn("autoflow_delegate_to_memory_worker", ms._DEPLOY_KNIVES,
                         "delegate 不是部署刀，不应被 black 过滤隐藏")
        # 契约侧同步登记：用户面独有集合应含它
        self.assertIn("autoflow_delegate_to_memory_worker", C.USER_FACE_ONLY,
                      "契约的 USER_FACE_ONLY 应登记 delegate（用户面独有）")

    def test_filter_strips_knives_for_black(self):
        all_names = sorted(t.name for t in ms.mcp._tool_manager.list_tools())
        fake = {"jsonrpc": "2.0", "id": 1,
                "result": {"tools": [{"name": n} for n in all_names]}}
        out = json.loads(ms._filter_tools_list(json.dumps(fake).encode()))
        visible = {t["name"] for t in out["result"]["tools"]}
        self.assertEqual(visible, set(C.EXPECTED_BLACK_VISIBLE),
                         C.symdiff_msg("black 可见集", C.EXPECTED_BLACK_VISIBLE, visible))
        # 等价表述（保留一条：过滤＝全集 − 刀）
        self.assertEqual(visible, set(all_names) - set(C.DEPLOY_KNIVES))

    def test_filter_passthrough_non_tools_list(self):
        self.assertEqual(ms._filter_tools_list(b""), b"", "空响应原样透传")
        probe = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"foo": "bar"}}).encode()
        self.assertEqual(ms._filter_tools_list(probe), probe, "非 tools/list 响应原样透传")
        bad = b"not-json"
        self.assertEqual(ms._filter_tools_list(bad), bad, "无法解析的响应原样透传")


if __name__ == "__main__":
    unittest.main()
