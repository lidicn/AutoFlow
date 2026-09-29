# -*- coding: utf-8 -*-
"""第一批审计修复回归守卫（P0-1 / P0-2 / P1-4 / P1-9 + .bak 清理）。

来源：docs/AutoFlow-审计报告.md 第一批「廉价高价值」清单。
每条守卫都证明对应修复真的生效（而非「全绿」假象）：

- P0-1：ConfirmationGate 用类级共享锁（跨 Gateway 实例），list_pending 容忍坏 JSON。
- P0-2：nr_client.ensure_latest 默认关闭自动同步（不 import 期自改写源码）。
- P1-4：Gateway 在 nr_client 不可用时仍构造成功（debug_bridge fail-open 降级）。
- P1-9：autoflow_list_tasks 默认分页（50）+ 硬性上限（200），不再无限制倾倒。

运行：python tests/test_audit_first_batch.py   （仓库根，禁沙箱）
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_audit_batch_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

import pytest


# ───────────────────────── P0-1 ─────────────────────────
def test_confirm_gate_uses_shared_class_lock():
    from autoflow_gateway.confirm import ConfirmationGate
    g1 = ConfirmationGate()
    g2 = ConfirmationGate()
    # 跨实例共享同一把锁（否则每 new Gateway() 自带新锁 → 速率熔断可被并发绕过）
    assert g1._lock is g2._lock
    assert g1._lock is ConfirmationGate._lock


def test_list_pending_tolerates_corrupt_json():
    from autoflow_gateway.confirm import ConfirmationGate

    class _Cfg:
        data_dir = _tmp

        def env_subdir(self):
            return "audit_p0_1"

    g = ConfirmationGate(config=_Cfg())
    valid = {
        "id": "op_1", "agent_id": "a", "status": "pending", "operation": "x",
        "risk_level": "low", "summary": "s", "blast_radius": 1, "payload": {},
        "created_at": "2020-01-01T00:00:00+00:00",
    }
    g._atomic_write(g._path("op_1"), valid)
    # 写一个半截/损坏的 JSON——绝不能让 list_pending / request 速率熔断崩溃
    with open(g._path("op_corrupt"), "w", encoding="utf-8") as f:
        f.write("{not valid json")
    res = g.list_pending()
    assert [o.id for o in res] == ["op_1"]


# ───────────────────────── P0-2 ─────────────────────────
def test_ensure_latest_default_disabled(monkeypatch):
    from autoflow_gateway.lib import nr_client
    monkeypatch.delenv("NR_CLIENT_AUTOSYNC", raising=False)
    monkeypatch.delenv("NR_CLIENT_DISABLE_AUTOSYNC", raising=False)
    # 默认（无 env）自动同步关闭 → 返回 None，绝不 import 期自改写源码
    assert nr_client.ensure_latest(verbose=False) is None


def test_ensure_latest_optin_does_not_raise(monkeypatch):
    from autoflow_gateway.lib import nr_client
    monkeypatch.setenv("NR_CLIENT_AUTOSYNC", "1")
    # 显式开启时至少尝试（返回 True/False/None 之一），不抛异常
    assert nr_client.ensure_latest(verbose=False) in (True, False, None)


# ───────────────────────── P1-4 ─────────────────────────
def test_gateway_constructs_when_nr_client_unavailable(monkeypatch):
    import autoflow_gateway.gateway as gw_mod
    from autoflow_gateway.gateway import Gateway

    # 重置进程级单例，保证本测试确定性（不被其它用例的启用态污染）
    gw_mod._debug_bridge_singleton = None

    class _BrokenNR:
        @property
        def client(self):
            raise RuntimeError("NR unavailable")

    # nr_client 取不到 → 网关仍要起来（否则 _gw() 每次 new Gateway() 都抛 → 45 工具全 500）
    g = Gateway(nr_layer=_BrokenNR())
    assert g.debug_bridge is not None
    assert g.debug_bridge.enabled is False


# ───────────────────────── P1-9 ─────────────────────────
def test_autoflow_list_tasks_default_limit_and_cap(monkeypatch):
    import autoflow_gateway.mcp_server as mcp

    monkeypatch.setattr(mcp, "is_task_pool_enabled", lambda cfg: True)
    fake = [{"id": f"t{i}"} for i in range(300)]

    class _Tasks:
        def list(self, **kw):
            return list(fake)

    class _GW:
        tasks = _Tasks()

    monkeypatch.setattr(mcp, "_gw", lambda: _GW())

    # 默认 limit=50 → 不应一次性倾倒全部 300 条
    out = json.loads(mcp.autoflow_list_tasks())
    assert out["total"] == 300
    assert out["returned"] <= 200
    assert out["returned"] == 50  # 默认值精确生效

    # 显式超大 limit 也被硬性上限 200 截断
    out2 = json.loads(mcp.autoflow_list_tasks(limit=1000))
    assert out2["returned"] <= 200

    # 显式小 limit 受尊重
    out3 = json.loads(mcp.autoflow_list_tasks(limit=10))
    assert out3["returned"] == 10


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
