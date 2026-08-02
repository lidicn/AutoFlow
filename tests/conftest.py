"""C23 — mock NR + HA stub 脚手架 (tests/conftest.py)

仓库此前无 conftest；本文件提供可复用的 FakeNR / FakeHA stub，
让「部署前自检 / 真机回读」类测试无需真 Node-RED / HA 即可跑（CB 接管 WB2 失联期自测）。
"""
import sys
from pathlib import Path

# ★ 必须排在任何 autoflow_gateway 导入之前：把【本仓库】src 顶到 sys.path 首位。
# 否则 site-packages 里的 editable 安装（_editable_impl_autoflow_gateway.pth，
# 指向另一处旧仓库副本）会劫持包解析 —— conftest 先于 test module 被导入，
# 此刻包已从旧副本载入并缓存进 sys.modules，各 test 文件里的
# sys.path.insert 再插也无效，结果是"测试跑的根本不是当前仓库的代码"。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from autoflow_gateway.lib.nr_client import NodeRedClient


class FakeNRClient:
    """内存版 NR 客户端 stub：记录 deploy / read，不触真机。"""

    def __init__(self, flows=None):
        self._flows = list(flows or [])
        self.deployed = []

    def list_flows(self):
        return list(self._flows)

    def create_or_update_flow(self, flow_json, *, force=False):
        self.deployed.append(flow_json)
        return {"ok": True}

    def read_flow(self, fid):
        return {"id": fid, "nodes": []}


class FakeHA:
    """内存版 HA stub：get/set state 不触真 HA。"""

    def __init__(self, states=None):
        self._states = dict(states or {})

    def get_state(self, entity):
        return self._states.get(entity, "unknown")

    def set_state(self, entity, value):
        self._states[entity] = value
        return value


@pytest.fixture
def fake_nr():
    return FakeNRClient()


@pytest.fixture
def fake_ha():
    return FakeHA()


@pytest.fixture(autouse=True)
def _autoflow_env(monkeypatch):
    """锁定非生产默认值，避免误绑 0.0.0.0（与 C13 硬化呼应）。"""
    monkeypatch.setenv("AF_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("AF_DEPLOY_POLICY", "review_all")


@pytest.fixture
def err_base():
    from autoflow_gateway.errors import not_found, ambiguous_count
    return {"not_found": not_found, "ambiguous": ambiguous_count}
