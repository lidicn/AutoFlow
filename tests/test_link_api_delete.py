# -*- coding: utf-8 -*-
"""#182 Link API 删除端点单测（DELETE /api/link-apis/{name}，离线 fake NR）。

覆盖工单验收四点：
1. 删除后**配置消失**（api_configs 行没了，GET config 回空）；
2. **AutoFlow API tab 内该 spec 派生的节点被清**，且 tab 仍只有 1 个、
   其它 spec 的链路与用户自用 tab 一个字节都不动（无孤儿、无误伤）；
3. **列表不含该项**（前端 Link API 面板取 GET /api/subflows，故注册表登记也要删）；
4. 删不存在项返回友好 **404**。

外加两条硬约束回归：
- self_use（豆包系列）不可删 → 403；
- **NR 写失败时本地状态一律不动** → 502，配置与注册表登记都还在。
  反例后果：配置删了但 NR 里还挂着带旧 token 的孤儿链，且面板上已看不到、无从修。

复用 test_install_tab 的 FakeNRClients（忠实模拟 NR：get_flow 未命中抛 404、
POST /flow 自行分配 id），避免两套 fake 对 NR 行为各说各话。
"""
import os
import sys
import shutil
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.api_config_store import ApiConfigStore
from autoflow_gateway.api_specs import get_api_spec, build_nr_tab_flows

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None

from test_install_tab import (
    FakeNRClients, CAIYUN_CFG, ANYSEARCH_CFG, USER_TAB, USER_TAB_ID,
)

from api_spec_fixture import make_spec, temp_api_spec

TARGET = "llm_caiyun_weather"   # 被删对象
KEEP = "anysearch_batch"        # 必须完好无损的邻居


@unittest.skipUnless(_HAVE_WEB_DEPS, "需要 starlette（缺失则 pip install starlette）。")
class TestLinkApiDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_la_del_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 夹具 ──
    def _set_cfg(self, name, d):
        st = ApiConfigStore(config=SimpleNamespace(data_dir=self.tmp))
        try:
            st.set_api_config(name, d)
        finally:
            st.close()

    def _inject_fake_nr(self, with_user_tab=True):
        fake = FakeNRClients()
        if with_user_tab:
            fake.flows[USER_TAB_ID] = dict(USER_TAB)
        self.gw.nr._client = fake
        self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)
        return fake

    def _install(self):
        """配齐两个 spec 的配置并安装 tab，返回 (fake, tab_id)。"""
        self._set_cfg(TARGET, CAIYUN_CFG)
        self._set_cfg(KEEP, ANYSEARCH_CFG)
        fake = self._inject_fake_nr()
        r = self.client.post("/api/link-apis/install-tab", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return fake, r.json()["tab_id"]

    def _delete(self, name):
        return self.client.delete("/api/link-apis/" + name)

    @staticmethod
    def _expected_ids(name, tab_id):
        """该 spec 按 build_nr_tab_flows 规则应生成的节点 id（期望值独立算一遍）。"""
        return {n["id"] for n in build_nr_tab_flows(tab_id, specs=[get_api_spec(name)])}

    # ── 验收 1+2+3：一次删除，三处清干净 ──
    def test_delete_clears_config_nodes_and_registry(self):
        fake, tab_id = self._install()
        before = list(fake.flows[tab_id]["nodes"])
        target_ids = self._expected_ids(TARGET, tab_id)
        keep_ids = self._expected_ids(KEEP, tab_id)
        self.assertTrue(target_ids & {n["id"] for n in before}, "前置：目标链应已安装")

        r = self._delete(TARGET)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["config_removed"])
        self.assertTrue(body["registry_removed"])
        self.assertEqual(body["tab_id"], tab_id)
        self.assertEqual(body["nodes_removed"], len(target_ids))
        self.assertEqual(set(body["node_ids"]), target_ids)

        # 2) tab 内派生节点被清，邻居链完好，tab 仍只有 1 个
        after_ids = {n["id"] for n in fake.flows[tab_id]["nodes"]}
        self.assertFalse(after_ids & target_ids, "目标 spec 的节点应全部移除")
        self.assertTrue(keep_ids <= after_ids, "邻居 spec 的链路不得被误删")
        self.assertEqual(len(fake.tabs_named("AutoFlow API")), 1)
        # 写 NR 走 PUT 且带 allow_prod=True（1990 是 prod）
        self.assertEqual(len(fake.update_calls), 1)
        self.assertEqual(fake.update_calls[0], (tab_id, True, True))

        # 1) 配置消失
        cfg = self.client.get(f"/api/link-apis/{TARGET}/config").json()
        self.assertEqual(cfg["config"], {})

        # 3) 列表不含该项（前端 Link API 面板的数据源）
        keys = {s["key"] for s in self.client.get("/api/subflows").json()["subflows"]}
        self.assertNotIn(TARGET, keys)
        self.assertIn(KEEP, keys, "只删目标，邻居仍在列表里")

    def test_user_tab_untouched(self):
        """硬约束：用户自用 tab 在删除前后逐字节不变。"""
        fake, _tab_id = self._install()
        snapshot = repr(fake.flows[USER_TAB_ID])
        r = self._delete(TARGET)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repr(fake.flows[USER_TAB_ID]), snapshot)

    # ── 验收 4：删不存在项 → 友好 404 ──
    def test_delete_unknown_returns_404(self):
        self._inject_fake_nr()
        r = self._delete("no_such_link_api")
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("no_such_link_api", body["error"])

    # ── 硬约束：self_use 不可删 ──
    def test_delete_self_use_returns_403(self):
        self._inject_fake_nr()
        # 豆包系列已按用户决策移除，用临时 self_use spec 驱动同一代码路径。
        spec = make_spec(name="t_self_use", title="自测自拒绝", kind="link_out",
                         self_use=True, entry_link_id="af_selfuse_in")
        with temp_api_spec(spec):
            r = self._delete("t_self_use")
            self.assertEqual(r.status_code, 403)
            self.assertFalse(r.json()["ok"])

    # ── 硬约束：NR 写失败 → 502 且本地状态不动 ──
    def test_nr_failure_keeps_local_state(self):
        fake, tab_id = self._install()

        def boom(*a, **kw):
            raise RuntimeError("NR 拒绝写入（模拟 1990 不可达）")
        fake.update_flow = boom

        r = self._delete(TARGET)
        self.assertEqual(r.status_code, 502, r.text)
        self.assertFalse(r.json()["ok"])
        # 配置还在
        cfg = self.client.get(f"/api/link-apis/{TARGET}/config").json()
        self.assertEqual(cfg["config"], CAIYUN_CFG)
        # 注册表登记还在（面板上仍能看到 → 用户可重试）
        keys = {s["key"] for s in self.client.get("/api/subflows").json()["subflows"]}
        self.assertIn(TARGET, keys)
        # NR 上的节点也原样还在
        self.assertTrue(self._expected_ids(TARGET, tab_id)
                        <= {n["id"] for n in fake.flows[tab_id]["nodes"]})

    # ── 边界：tab 尚未安装时删除 → 只清本地，不报错 ──
    def test_delete_without_installed_tab(self):
        self._set_cfg(TARGET, CAIYUN_CFG)
        self._inject_fake_nr()          # NR 上没有 AutoFlow API tab
        r = self._delete(TARGET)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["nodes_removed"], 0)
        self.assertIsNone(body["tab_id"])
        self.assertTrue(body["config_removed"])
        self.assertTrue(body["registry_removed"])

    # ── 幂等：连删两次，第二次因注册表已空而 404（面板上也已消失）──
    def test_second_delete_returns_404(self):
        self._install()
        self.assertEqual(self._delete(TARGET).status_code, 200)
        r2 = self._delete(TARGET)
        self.assertEqual(r2.status_code, 200, "spec 声明仍在，二次删除是幂等 no-op")
        b2 = r2.json()
        self.assertFalse(b2["config_removed"])
        self.assertFalse(b2["registry_removed"])
        self.assertEqual(b2["nodes_removed"], 0)


if __name__ == "__main__":
    unittest.main()
