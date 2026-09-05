"""C25-fix: AuditStore 零覆盖测试。

对齐 src/autoflow_gateway/audit.py 的 AuditStore：
  - AuditStore(gw) 包装一个具备 get_recent_traces(limit) 的 gateway
  - list(limit) -> 透传 gateway.get_recent_traces(limit)

此处用轻量 fake gateway，避免拉起完整 Gateway，保持单元测试隔离。
"""
from autoflow_gateway.audit import AuditStore


class _FakeGateway:
    def get_recent_traces(self, limit=100):
        return [{"t": i, "op": "apply"} for i in range(min(limit, 5))]


def test_list_returns_traces():
    store = AuditStore(_FakeGateway())
    traces = store.list(5)
    assert len(traces) == 5
    assert all(isinstance(t, dict) for t in traces)


def test_list_default_limit():
    store = AuditStore(_FakeGateway())
    traces = store.list()
    assert len(traces) == 5
