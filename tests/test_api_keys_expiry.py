"""P0-1: API Key 过期校验返回 401 而非 403。

验收标准：
  - 过期 Key 返回 status=401（不是 403）
  - 有效 Key 正常工作（ok=True）
  - 未吊销的过期 Key 被拒绝，且日志中记录"已过期"
"""
import json
import tempfile
from datetime import datetime, timezone, timedelta

from autoflow_gateway.api_keys import APIKeyStore, _utcnow


def _past_iso(days_ago: int = 1) -> str:
    """返回几天前的 ISO 时间字符串（用于构造过期 key）。"""
    return (_utcnow() - timedelta(days=days_ago)).isoformat()


def _future_iso(days_ahead: int = 365) -> str:
    """返回几天后的 ISO 时间字符串（用于构造未过期 key）。"""
    return (_utcnow() + timedelta(days=days_ahead)).isoformat()


class _TmpStore:
    """临时目录下的 APIKeyStore，测试后自动清理。"""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="ak_test_")
        self.store = APIKeyStore(self._dir)

    def cleanup(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)


def test_expired_key_returns_401():
    """过期 key 应返回 ok=False + status=401。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="expired-key",
            agent_id="agent-1",
            expires_at=_past_iso(days_ago=1),
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is False
        assert val["status"] == 401, f"期望 401，实际 {val['status']}"
        assert "过期" in val["error"]
    finally:
        tmp.cleanup()


def test_valid_key_returns_200_ok():
    """未过期的 key 应验证通过。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="valid-key",
            agent_id="agent-1",
            expires_at=_future_iso(days_ahead=30),
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is True
        assert val["agent_id"] == "agent-1"
        assert val["name"] == "valid-key"
    finally:
        tmp.cleanup()


def test_no_expiry_key_works():
    """没有设置 expires_at 的 key 应正常工作。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="no-expiry-key",
            agent_id="agent-1",
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is True
    finally:
        tmp.cleanup()


def test_revoked_key_returns_403():
    """已吊销的 key 应返回 status=403（区别于过期的 401）。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="revoked-key",
            agent_id="agent-1",
            expires_at=_future_iso(days_ahead=30),
        )
        key = result["key"]
        key_id = result["key_id"]

        # 先吊销
        tmp.store.revoke_key(key_id)

        val = tmp.store.validate_key(key)
        assert val["ok"] is False
        assert val["status"] == 403, f"期望 403，实际 {val['status']}"
        assert "吊销" in val["error"]
    finally:
        tmp.cleanup()


def test_invalid_key_returns_401():
    """不存在的 key 应返回 status=401。"""
    tmp = _TmpStore()
    try:
        val = tmp.store.validate_key("af_pro_nonexistent_key_12345")
        assert val["ok"] is False
        assert val["status"] == 401
    finally:
        tmp.cleanup()


def test_naive_future_expires_at_is_rejected():
    """回归：naive 格式（无时区）的 expires_at 曾触发 TypeError 被 except pass 吞掉，
    导致密钥永不过期（fail-open）。现在必须 fail-closed 拒绝。
    即使 naive 时间在未来，也无法可靠比较 → 拒绝。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="naive-future-key",
            agent_id="agent-1",
            expires_at="2099-01-01T00:00:00",  # 未来时间但缺时区
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is False, "naive 格式应被拒绝，而不是静默放行"
        assert val["status"] == 401, f"期望 401，实际 {val['status']}"
    finally:
        tmp.cleanup()


def test_naive_past_expires_at_is_rejected():
    """naive 格式且已过期的 expires_at 应返回 401（回归：此前 TypeError 被吞掉 → 永不过期）。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="naive-past-key",
            agent_id="agent-1",
            expires_at="2020-01-01T00:00:00",
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is False
        assert val["status"] == 401, f"期望 401，实际 {val['status']}"
        assert "过期" in val["error"]
    finally:
        tmp.cleanup()


def test_unparseable_expires_at_is_rejected():
    """无法解析的 expires_at 应 fail-closed 拒绝，而不是静默放行（永不过期）。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="bad-format-key",
            agent_id="agent-1",
            expires_at="not-a-date",
        )
        key = result["key"]

        val = tmp.store.validate_key(key)
        assert val["ok"] is False
        assert val["status"] == 401, f"期望 401，实际 {val['status']}"
    finally:
        tmp.cleanup()


def test_empty_key_returns_401():
    """空 key 应返回 status=401。"""
    tmp = _TmpStore()
    try:
        val = tmp.store.validate_key("")
        assert val["ok"] is False
        assert val["status"] == 401
    finally:
        tmp.cleanup()


def test_expired_key_audit_log():
    """过期 key 的验证失败应写入审计日志。"""
    tmp = _TmpStore()
    try:
        result = tmp.store.create_key(
            name="log-test-key",
            agent_id="agent-1",
            expires_at=_past_iso(days_ago=1),
        )
        key = result["key"]
        key_id = result["key_id"]

        tmp.store.validate_key(key)  # 应失败

        logs = tmp.store.get_logs()
        expired_entries = [
            l for l in logs
            if l.get("key_id") == key_id and l.get("detail") == "已过期"
        ]
        assert len(expired_entries) >= 1, "审计日志中应记录'已过期'条目"
        assert expired_entries[0]["success"] is False
    finally:
        tmp.cleanup()
