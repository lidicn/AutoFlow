"""WB93 专项：F12 真机 e2e 验证基建 —— allow_prod 豁免路径。

prod e2e 受写护栏拦（staging=prod 1990）。run_e2e_trace_raw 已把 allow_prod 透传到
部署与回滚（create_or_update_flow / _safe_delete）。本测试用 FakeNR 锁死：
- allow_prod=True  → 部署带 allow_prod 透传（豁免生效）；
- allow_prod=False → 部署被写护栏拒（返回 拦截，未部署）。
（真机复跑需 WB2 在 staging NR 上显式 allow_prod，属运维程序，非代码缺口。）
"""
import os, sys
import pytest

sys.path.insert(0, r"E:\NAS\autoflow\src")
from autoflow_gateway import gateway as G


class FakeNR:
    def __init__(self):
        self.deployed = []
        self.deleted = []
        self.allow_prod_block = False

    def create_or_update_flow(self, fid, inst, force=False, allow_prod=False):
        if not allow_prod and self.allow_prod_block:
            raise RuntimeError("prod write guard: allow_prod required")
        self.deployed.append((fid, allow_prod))
        return {"id": fid}

    def trigger_inject(self, iid):
        return True

    def get_context(self, scope, key):
        return []

    def delete_context(self, scope, key):
        return True

    def get_default_server_id(self):
        return ""

    def delete_flow(self, fid, allow_prod=False):
        self.deleted.append((fid, allow_prod))
        return True


FLOW = {"nodes": [
    {"id": "n_inj", "type": "inject", "name": "t", "wires": [["n_dbg"]]},
    {"id": "n_dbg", "type": "debug", "name": "d", "wires": []},
]}


@pytest.fixture
def gw():
    g = G.Gateway()
    g.nr = FakeNR()
    return g


class TestF12E2EAllowProd:
    def test_allow_prod_threads_to_deploy(self, gw):
        gw.nr.allow_prod_block = True
        r = gw.run_e2e_trace_raw(FLOW, allow_prod=True)
        assert gw.nr.deployed, "allow_prod=True 应透传到部署"
        assert gw.nr.deployed[0][1] is True
        assert r["e2e"] in (True, False)  # 不依赖真实 trace

    def test_allow_prod_false_blocked_by_guard(self, gw):
        gw.nr.allow_prod_block = True
        r = gw.run_e2e_trace_raw(FLOW, allow_prod=False)
        assert r["verdict"] == "拦截", r
        assert gw.nr.deployed == [], "allow_prod=False 不应部署到 prod"
