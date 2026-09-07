# -*- coding: utf-8 -*-
"""APIKeyStore 冒烟测试（v1.5.x Pro API Key 体系）。

该模块自引入起零测试覆盖，本文件补齐核心安全路径：
- 创建：明文只返回一次，list 不泄露明文/hash；
- 验证：正确 key → ok 且 use_count 递增；错误 key → 401；
- 吊销：revoked 后验证 → 403；
- 过期：aware 过期时间 → 403；
- ★ P0 回归守卫（e63ae3d 修复）：expires_at 为 naive datetime（缺时区）或
  非法格式时**必须拒绝**（fail-closed）。旧实现 except-pass 静默放行，
  导致 API Key 永不过期；
- 权限不足 → 403；tab 越界 → 403 且回显 authorized_tabs；
- authorized_tabs 为空 = 全部 tab 放行。
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.api_keys import APIKeyStore, _hash_key, _utcnow


def _aware_iso(dt: datetime) -> str:
    """生成带时区的 ISO 字符串（合法 expires_at）。"""
    return dt.astimezone(timezone.utc).isoformat()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_apikeys_")
        self.store = APIKeyStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCreateAndValidate(_Base):
    def test_create_returns_plaintext_once_and_list_hides_secrets(self):
        r = self.store.create_key(name="bot", agent_id="agent-1")
        self.assertTrue(r["ok"])
        self.assertTrue(r["key"].startswith("af_pro_"))
        # list 不得含明文和 hash
        keys = self.store.list_keys()
        self.assertEqual(len(keys), 1)
        self.assertNotIn("key", keys[0])
        self.assertNotIn("key_hash", keys[0])
        self.assertNotIn("key_plaintext", keys[0])

    def test_validate_ok_and_use_count_increments(self):
        r = self.store.create_key(name="bot", agent_id="agent-1")
        v1 = self.store.validate_key(r["key"])
        self.assertTrue(v1["ok"])
        self.assertEqual(v1["agent_id"], "agent-1")
        v2 = self.store.validate_key(r["key"])
        self.assertTrue(v2["ok"])
        keys = self.store.list_keys()
        self.assertEqual(keys[0]["use_count"], 2)
        self.assertIsNotNone(keys[0]["last_used_at"])

    def test_validate_wrong_key_401(self):
        self.store.create_key(name="bot", agent_id="agent-1")
        v = self.store.validate_key("af_pro_" + "0" * 32)
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 401)

    def test_validate_empty_key_401(self):
        v = self.store.validate_key("")
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 401)


class TestRevokeAndExpiry(_Base):
    def test_revoked_key_rejected_403(self):
        r = self.store.create_key(name="bot", agent_id="agent-1")
        self.store.revoke_key(r["key_id"])
        v = self.store.validate_key(r["key"])
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 403)
        self.assertIn("吊销", v["error"])

    def test_expired_key_rejected(self):
        r = self.store.create_key(
            name="bot", agent_id="agent-1",
            expires_at=_aware_iso(_utcnow() - timedelta(hours=1)),
        )
        v = self.store.validate_key(r["key"])
        self.assertFalse(v["ok"])
        self.assertIn("过期", v["error"])

    def test_future_expiry_passes(self):
        r = self.store.create_key(
            name="bot", agent_id="agent-1",
            expires_at=_aware_iso(_utcnow() + timedelta(hours=1)),
        )
        v = self.store.validate_key(r["key"])
        self.assertTrue(v["ok"])


class TestP0ExpiryFailClosed(_Base):
    """★ P0 回归守卫（e63ae3d）：naive / 非法 expires_at 必须拒绝，不得静默放行。"""

    def _plant_key_with_expiry(self, expires_at_raw: str) -> str:
        """直接写入一条带指定 expires_at 原文的 key，返回明文 key。"""
        r = self.store.create_key(name="bot", agent_id="agent-1")
        path = os.path.join(self.tmp, "api_keys.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["keys"][0]["expires_at"] = expires_at_raw
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return r["key"]

    def test_naive_datetime_expiry_rejected(self):
        # naive（无时区）ISO 格式 —— 旧 bug 里这种值会让 except 静默放行
        key = self._plant_key_with_expiry("2099-01-01T00:00:00")
        v = self.store.validate_key(key)
        self.assertFalse(v["ok"], "naive datetime 必须 fail-closed 拒绝")
        self.assertIn("过期", v["error"])

    def test_garbage_expiry_rejected(self):
        key = self._plant_key_with_expiry("not-a-date")
        v = self.store.validate_key(key)
        self.assertFalse(v["ok"], "非法 expires_at 必须 fail-closed 拒绝")

    def test_garbage_expiry_far_future_also_rejected(self):
        # 即便含义上是"未来"，格式不合法也一律拒绝 —— 不存在"猜对了就放行"
        key = self._plant_key_with_expiry("9999-99-99")
        v = self.store.validate_key(key)
        self.assertFalse(v["ok"])


class TestPermAndTabScope(_Base):
    def test_perm_insufficient_403(self):
        r = self.store.create_key(name="bot", agent_id="agent-1",
                                  permissions=["propose_dsl"])
        v = self.store.validate_key(r["key"], required_perm="deploy")
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 403)

    def test_tab_out_of_scope_403_with_echo(self):
        r = self.store.create_key(name="bot", agent_id="agent-1",
                                  authorized_tabs=["tab_a"])
        v = self.store.validate_key(r["key"], target_tab="tab_b")
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 403)
        self.assertEqual(v["authorized_tabs"], ["tab_a"])

    def test_tab_in_scope_ok(self):
        r = self.store.create_key(name="bot", agent_id="agent-1",
                                  authorized_tabs=["tab_a"])
        v = self.store.validate_key(r["key"], target_tab="tab_a")
        self.assertTrue(v["ok"])

    def test_empty_authorized_tabs_means_all_tabs(self):
        r = self.store.create_key(name="bot", agent_id="agent-1")  # 默认空列表
        v = self.store.validate_key(r["key"], target_tab="any_tab")
        self.assertTrue(v["ok"])


if __name__ == "__main__":
    unittest.main()
