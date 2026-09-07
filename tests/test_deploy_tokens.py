# -*- coding: utf-8 -*-
"""DeployTokenStore 冒烟测试（v1.4.x 部署授权码，Trusted Agent Auto-Deploy）。

该模块自引入起零测试覆盖，本文件补齐核心安全路径：
- 创建：明文只返回一次；单 target_tab 兼容转 target_tabs；
- 验证：正确 token + 有权限操作 → ok 且不需人工；
- 无效 / 吊销 / 过期 / 绑定 agent 不符 / NR 实例不符 → 拒绝；
- tab 越界 → 拒绝；URL 格式（#flow/<id>）能正确提取 tab id；
- 无该操作权限 → 拒绝；
- 节点数超阈值 → 放行但 needs_manual_approval=True（不硬拒，转人工）；
- 节点/flow 配额满 → 拒绝；
- 限流窗口：rate_window_start 必须落盘，否则限流永不生效（防回归）；
- record_usage 正确累计统计。
"""
import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.deploy_tokens import DeployTokenStore, _utcnow


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_dtok_")
        self.store = DeployTokenStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, **kw):
        kw.setdefault("name", "t1")
        return self.store.create_token(**kw)


class TestCreate(_Base):
    def test_plaintext_once_and_hash_stored(self):
        r = self._make()
        self.assertIn("token_plaintext", r)
        self.assertNotIn("token_hash", r)
        got = self.store.get_token(r["token_id"])
        self.assertNotIn("token_plaintext", got)

    def test_single_target_tab_compat_to_list(self):
        r = self._make(target_tab="tab_x")
        self.assertEqual(r["target_tabs"], ["tab_x"])


class TestValidateHappyAndNegative(_Base):
    def test_ok_deploy(self):
        r = self._make()
        v = self.store.validate_token(r["token_plaintext"], operation="deploy")
        self.assertTrue(v["ok"])
        self.assertFalse(v["needs_manual_approval"])

    def test_invalid_token(self):
        v = self.store.validate_token("af_dt_" + "0" * 32, operation="deploy")
        self.assertFalse(v["ok"])

    def test_empty_token(self):
        v = self.store.validate_token("", operation="deploy")
        self.assertFalse(v["ok"])

    def test_revoked_rejected(self):
        r = self._make()
        self.store.revoke_token(r["token_id"])
        v = self.store.validate_token(r["token_plaintext"], operation="deploy")
        self.assertFalse(v["ok"])
        self.assertIn("吊销", v["error"])

    def test_expired_rejected(self):
        r = self._make(expires_in_hours=-1)  # 已过期
        v = self.store.validate_token(r["token_plaintext"], operation="deploy")
        self.assertFalse(v["ok"])
        self.assertIn("过期", v["error"])

    def test_bound_agent_mismatch(self):
        r = self._make(bound_agent="agent-a")
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      agent_id="agent-b")
        self.assertFalse(v["ok"])

    def test_bound_agent_match_ok(self):
        r = self._make(bound_agent="agent-a")
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      agent_id="agent-a")
        self.assertTrue(v["ok"])

    def test_bound_nr_instance_mismatch(self):
        r = self._make(bound_nr_instance="http://nr:1880")
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      nr_instance="http://nr:1990")
        self.assertFalse(v["ok"])


class TestTabScope(_Base):
    def test_tab_out_of_scope(self):
        r = self._make(target_tabs=["tab_a"])
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      target_tab="tab_b")
        self.assertFalse(v["ok"])
        self.assertEqual(v["allowed_tabs"], ["tab_a"])

    def test_tab_in_scope(self):
        r = self._make(target_tabs=["tab_a", "tab_b"])
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      target_tab="tab_b")
        self.assertTrue(v["ok"])

    def test_url_format_tab_extracted(self):
        r = self._make(target_tabs=["abc123"])
        v = self.store.validate_token(
            r["token_plaintext"], operation="deploy",
            target_tab="http://192.168.2.200:1880/#flow/abc123")
        self.assertTrue(v["ok"], "URL 中的 #flow/<id> 应被提取后比对")

    def test_no_binding_allows_any_tab(self):
        r = self._make()  # 无 target_tabs
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      target_tab="whatever")
        self.assertTrue(v["ok"])


class TestPermQuotaThreshold(_Base):
    def test_operation_not_permitted(self):
        r = self._make(permissions=["deploy"])
        v = self.store.validate_token(r["token_plaintext"], operation="modify")
        self.assertFalse(v["ok"])

    def test_node_threshold_turns_manual_not_reject(self):
        r = self._make(node_threshold=5)
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      node_count=10)
        self.assertTrue(v["ok"], "超阈值应放行转人工，而非硬拒")
        self.assertTrue(v["needs_manual_approval"])

    def test_node_quota_full_rejected(self):
        r = self._make(max_nodes=10, node_threshold=100)
        self.store.record_usage(r["token_id"], operation="deploy",
                                agent_id="a", node_count=10)
        v = self.store.validate_token(r["token_plaintext"], operation="deploy",
                                      node_count=1)
        self.assertFalse(v["ok"])
        self.assertIn("配额", v["error"])

    def test_flow_quota_full_rejected(self):
        r = self._make(max_flows=1, node_threshold=100)
        self.store.record_usage(r["token_id"], operation="deploy",
                                agent_id="a", flow_id="f1", node_count=1)
        v = self.store.validate_token(r["token_plaintext"], operation="deploy")
        self.assertFalse(v["ok"])
        self.assertIn("配额", v["error"])


class TestRateLimitAndUsage(_Base):
    def test_rate_window_start_persisted(self):
        """首次 validate 后 rate_window_start 必须落盘 —— 否则限流永不生效。"""
        r = self._make()
        self.store.validate_token(r["token_plaintext"], operation="deploy")
        got = self.store.get_token(r["token_id"])
        self.assertIsNotNone(got["stats"]["rate_window_start"],
                             "rate_window_start 不落盘 = 限流失效（防回归）")

    def test_rate_limit_exceeded(self):
        r = self._make(rate_limit_per_min=2)
        # 手动把窗口计数顶到上限
        path = os.path.join(self.tmp, "deploy_tokens.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["tokens"][r["token_id"]]["stats"]["rate_window_start"] = time.time()
        data["tokens"][r["token_id"]]["stats"]["rate_window_count"] = 2
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        v = self.store.validate_token(r["token_plaintext"], operation="deploy")
        self.assertFalse(v["ok"])
        self.assertIn("频率", v["error"])

    def test_record_usage_accumulates(self):
        r = self._make()
        self.store.record_usage(r["token_id"], operation="deploy",
                                agent_id="a", flow_id="f1", node_count=3)
        self.store.record_usage(r["token_id"], operation="modify",
                                agent_id="a", flow_id="f1", node_count=1)
        self.store.record_usage(r["token_id"], operation="deploy",
                                agent_id="a", flow_id="f2", node_count=2,
                                success=False, error="boom")
        got = self.store.get_token(r["token_id"])
        s = got["stats"]
        # 语义：只有成功的 deploy 才计 deploy_count/nodes/flows；
        # modify 只计 modify_count；失败只计 failed_count
        self.assertEqual(s["deploy_count"], 1)
        self.assertEqual(s["modify_count"], 1)
        self.assertEqual(s["nodes_deployed"], 3)
        self.assertEqual(s["flows_deployed"], 1)
        self.assertEqual(s["failed_count"], 1)
        self.assertIsNotNone(s["last_used_at"])
        logs = self.store.get_logs(r["token_id"])
        self.assertGreaterEqual(len(logs), 3)


if __name__ == "__main__":
    unittest.main()
