"""C28 · debug_read 结构化错误闭环（C6+C7 收尾）单元测试。

对齐 handoff/C28_handoff.md 三场景 + fail-open：
  - A 真实流无 node   -> ok:True, status:"empty", count:0（正确，非错误）
  - B 真实流 + 坏 node -> ok:False, error.code:"NOT_FOUND", 带 node_id（延展）
  - C 不存在的 flow    -> ok:False, error.code:"NOT_FOUND", 带 flow_id
  - D NR 不可达        -> 优雅降级 ok:True, status:"empty"（不 500 / 不误报 NOT_FOUND）

fake NR backend 经 NRLayer(config, backend=FakeNR) 注入（CONTRACTS.md §1 DI 缝），
与真实 NodeRedClient.get_flow 行为一致：不存在抛 RuntimeError('...404...')。
用 tmp_path 隔离 data_dir；debug_bridge_enabled=False 密封不连真 NR ws。
"""
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.config import get_config


class FakeNR:
    def __init__(self, flows):
        self._flows = flows  # flow_id -> flow dict

    def get_flow(self, flow_id):
        f = self._flows.get(flow_id)
        if f is None:
            raise RuntimeError(f"GET /flow/{flow_id} -> 404: not found")
        return f

    def list_flows(self):
        return list(self._flows.values())


class DownNR:
    """NR 不可达：非 404 异常（NRLayer 会吞异常转 ok:False）。"""

    def get_flow(self, flow_id):
        raise RuntimeError("urllib.error.URLError: <urlopen error getaddrinfo failed>")

    def list_flows(self):
        return []


def _gw(flows, tmp_path, backend=None):
    cfg = get_config()
    cfg.data_dir = str(tmp_path)
    cfg.debug_bridge_enabled = False  # 密封：不连真 NR ws
    nr = NRLayer(config=cfg, backend=backend or FakeNR(flows))
    return Gateway(config=cfg, nr_layer=nr)


def test_A_real_flow_no_node(tmp_path):
    flows = {"real": {"id": "real", "nodes": [{"id": "n1"}]}}
    gw = _gw(flows, tmp_path)
    res = gw.get_debug_read(flow_id="real")
    assert res["ok"] is True
    assert res["status"] == "empty"
    assert res["count"] == 0


def test_C_nonexistent_flow(tmp_path):
    gw = _gw({}, tmp_path)
    res = gw.get_debug_read(flow_id="flow_does_not_exist_zzz")
    assert res["ok"] is False
    assert res["error"]["code"] == "NOT_FOUND"
    assert res["error"]["category"] == "not_found"
    assert res["error"]["flow_id"] == "flow_does_not_exist_zzz"


def test_B_real_flow_bad_node(tmp_path):
    flows = {"real": {"id": "real", "nodes": [{"id": "n1"}]}}
    gw = _gw(flows, tmp_path)
    res = gw.get_debug_read(flow_id="real", node_id="no_such_node")
    assert res["ok"] is False
    assert res["error"]["code"] == "NOT_FOUND"
    assert res["error"]["node_id"] == "no_such_node"


def test_D_nr_unreachable_failopen(tmp_path):
    gw = _gw(None, tmp_path, backend=DownNR())
    res = gw.get_debug_read(flow_id="any")
    # 不应误报 NOT_FOUND，应 fail-open 回 ok:True 空结果
    assert res["ok"] is True
    assert res["status"] == "empty"
