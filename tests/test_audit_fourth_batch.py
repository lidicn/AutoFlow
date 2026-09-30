"""第四批审计修复守卫 —— P1-13（restart_gateway 自重启互斥锁）。

P1-13：并发 admin 请求同时进 autoflow_restart_gateway 会各自 spawn self_destruct 线程、
重复设 AppExit=Restart。修复：模块级 _RESTART_LOCK（threading.Lock）+ 非阻塞获取，
已在重启中的请求立即返回 restart_in_progress。

守卫含变异自证：把 _RESTART_LOCK 换成「acquire 永远返回 True」的假锁（模拟守卫失效），
断言守卫不再触发（返回 status != restart_in_progress）——证明 in_progress 返回确由锁守卫产生，
回退守卫会变红。
"""
import json

import pytest

import autoflow_gateway.mcp_server as ms


class _StubAgent:
    agent_id = "test-agent"
    mode = "developer"


@pytest.fixture
def _agent(monkeypatch):
    monkeypatch.setattr(ms, "get_current_agent", lambda: _StubAgent())


def test_restart_lock_is_threading_lock():
    assert isinstance(ms._RESTART_LOCK, type(__import__("threading").Lock()))


def test_restart_concurrent_guard_trips(_agent):
    """持有锁（模拟已有重启在发起）时，调用必须立即返回 restart_in_progress，不得发起新重启。"""
    assert ms._RESTART_LOCK.acquire(blocking=False), "前置：测试应能拿到锁"
    try:
        out = json.loads(ms.autoflow_restart_gateway())
    finally:
        ms._RESTART_LOCK.release()
    assert out.get("status") == "restart_in_progress", out
    assert out.get("ok") is False


def test_restart_guard_disabled_mutation(_agent):
    """变异：把锁换成 acquire 永远返回 True 的假锁（守卫失效），断言守卫不再触发。
    与 test_restart_concurrent_guard_trips 对照——证明 in_progress 返回确由锁守卫产生。"""

    class _FakeAlwaysLock:
        def acquire(self, blocking=True):
            return True  # 模拟守卫失效：永远认为「可发起」

        def release(self):
            pass

    orig = ms._RESTART_LOCK
    ms._RESTART_LOCK = _FakeAlwaysLock()
    try:
        out = json.loads(ms.autoflow_restart_gateway())
    finally:
        ms._RESTART_LOCK = orig  # 还原（避免泄漏到其它用例）
    # 守卫失效时不应再返回 restart_in_progress（会走到真实重启发起逻辑，测试环境无 nssm/ps1 → ok:False）
    assert out.get("status") != "restart_in_progress", out
