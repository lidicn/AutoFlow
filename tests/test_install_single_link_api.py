# -*- coding: utf-8 -*-
"""#C 单条 Link API「安装到 Node-RED」端点单测。

覆盖 install_single_link_api_endpoint（POST /api/link-apis/{name}/install）：
- 成功：配置齐 → 200，且**只安装这一个** spec 的节点（不含其它候选）；
- 幂等：重复装同一 spec → 第二次 skipped、nodes_added==0、不新建 tab；
- 未知 name → 404；
- self_use 能力 → 403（与配置/删除端点同口径）；
- spec.needs_nr_flow() 为 False（http_api 内联 / 导入的 tab-link link_out）→ 400；
- 缺配置 → 400 并给出缺失清单（含该项 name，且不写 NR）。

复用 test_install_tab.FakeNRClients 的「忠实模拟」语义：get_flow 未命中抛 404、
POST /flow 自分配真实 id 并改写 z —— 这是 #177 行为的照妖镜。
"""
import os
import sys
import copy
import tempfile
import shutil
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.api_config_store import ApiConfigStore

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


class FakeNRClients:
    """内存 fake NR client：忠实模拟 Node-RED admin API 的关键行为。"""
    NEW_TAB_ID = "3c2d2af8c0878f6f"

    def __init__(self):
        self.flows = {}
        self.get_flow_calls = []
        self.create_calls = []
        self.update_calls = []
        self.list_flows_calls = 0
        self._seq = 0

    def list_flows(self):
        self.list_flows_calls += 1
        out = []
        for fid, fl in self.flows.items():
            out.append({"id": fid, "type": "tab", "label": fl.get("label", "")})
            out.extend(copy.deepcopy(fl.get("nodes", [])))
        return out

    def get_flow(self, flow_id, use_cache=True):
        self.get_flow_calls.append(flow_id)
        if flow_id not in self.flows:
            raise RuntimeError(f"HTTP 404 Not Found: /flow/{flow_id}")
        return copy.deepcopy(self.flows[flow_id])

    def create_or_update_flow(self, flow_id, flow_data, force, allow_prod):
        self.create_calls.append((flow_id, force, allow_prod))
        if flow_id in self.flows:
            self.flows[flow_id] = {**copy.deepcopy(flow_data), "id": flow_id}
            return {"id": flow_id, "created": False, "raw": None}
        self._seq += 1
        real = self.NEW_TAB_ID if self._seq == 1 else f"{self.NEW_TAB_ID}-{self._seq}"
        nodes = [dict(copy.deepcopy(n), z=real) for n in flow_data.get("nodes", [])]
        self.flows[real] = {**copy.deepcopy(flow_data), "id": real, "nodes": nodes}
        return {"id": real, "created": True, "raw": None}

    def update_flow(self, flow_id, flow_data, force=False, allow_prod=False):
        self.update_calls.append((flow_id, force, allow_prod))
        if flow_id not in self.flows:
            raise RuntimeError(f"HTTP 404 Not Found: /flow/{flow_id}")
        self.flows[flow_id] = {**copy.deepcopy(flow_data), "id": flow_id}
        return {"id": flow_id, "created": False, "raw": None}

    def tabs_named(self, label):
        return [fid for fid, fl in self.flows.items() if fl.get("label") == label]


CAIYUN_CFG = {"CAIYUN_TOKEN": "tokABC", "CAIYUN_LON": "116.40", "CAIYUN_LAT": "39.90"}
ANYSEARCH_CFG = {"ANYSEARCH_API_KEY": "akXYZ"}


@unittest.skipUnless(_HAVE_WEB_DEPS, "单条安装测试需要 starlette。")
class TestInstallSingleLinkApi(unittest.TestCase):
    def setUp(self):
        # 同 test_import_link_api_from_tab：token_only 让 TestClient（loopback）绕过鉴权
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
        self.tmp = tempfile.mkdtemp(prefix="af_install_single_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self._extra_specs = []

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for s in self._extra_specs:
            try:
                type(s).__module__  # no-op
            except Exception:
                pass
        # 卸载运行时注入的临时 spec
        from autoflow_gateway import api_specs as _as
        for s in self._extra_specs:
            try:
                _as.API_SPECS.remove(s)
            except ValueError:
                pass
        os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 夹具 ──
    def _set_cfg(self, name, d):
        st = ApiConfigStore(config=SimpleNamespace(data_dir=self.tmp))
        try:
            st.set_api_config(name, d)
        finally:
            st.close()

    def _inject_fake_nr(self):
        fake = FakeNRClients()
        self.gw.nr._client = fake
        self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)
        return fake

    def _add_spec(self, **kw):
        from autoflow_gateway import api_specs as _as
        spec = _as.ApiSpec(**kw)
        _as.API_SPECS.append(spec)
        self._extra_specs.append(spec)
        return spec

    # ── #C 覆盖 ──
    def test_install_single_success_only_that_spec(self):
        """单装 caiyun → 200，且只生成 caiyun 节点，anysearch 不在其内。"""
        self._set_cfg("llm_caiyun_weather", CAIYUN_CFG)
        self._set_cfg("anysearch_batch", ANYSEARCH_CFG)   # 已配置但不应被装
        fake = self._inject_fake_nr()

        r = self.client.post("/api/link-apis/llm_caiyun_weather/install", json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["specs"], ["llm_caiyun_weather"])
        self.assertEqual(body["tab_id"], FakeNRClients.NEW_TAB_ID)

        nodes = fake.flows[FakeNRClients.NEW_TAB_ID]["nodes"]
        ids = {n["id"] for n in nodes}
        self.assertIn("af_weather_in", ids)
        self.assertNotIn("af_anysearch_in", ids)   # 单装绝不能顺带装别的
        self.assertEqual({n["z"] for n in nodes}, {FakeNRClients.NEW_TAB_ID})

    def test_install_single_idempotent(self):
        """连装两次同一条 → 第二次 skipped、nodes_added==0、不新建 tab。"""
        self._set_cfg("llm_caiyun_weather", CAIYUN_CFG)
        fake = self._inject_fake_nr()

        first = self.client.post("/api/link-apis/llm_caiyun_weather/install", json={})
        self.assertEqual(first.status_code, 200)
        self.assertGreater(first.json()["nodes_added"], 0)

        second = self.client.post("/api/link-apis/llm_caiyun_weather/install", json={})
        self.assertEqual(second.status_code, 200)
        sd = second.json()
        self.assertTrue(sd["ok"])
        self.assertEqual(sd["nodes_added"], 0)
        self.assertTrue(sd.get("skipped"))
        self.assertEqual(sd["tab_id"], first.json()["tab_id"])
        self.assertEqual(fake.tabs_named("AutoFlow API"), [FakeNRClients.NEW_TAB_ID])
        self.assertEqual(len(fake.create_calls), 1)

    def test_install_single_unknown_404(self):
        fake = self._inject_fake_nr()
        r = self.client.post("/api/link-apis/does_not_exist/install", json={})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_install_single_self_use_403(self):
        self._add_spec(name="t_probe_selfuse", title="探针-自用",
                       kind="link_out", entry_link_id="t_probe_in",
                       nr_tab=True, self_use=True)
        self._set_cfg("t_probe_selfuse", {"CAIYUN_TOKEN": "x", "CAIYUN_LON": "1",
                                          "CAIYUN_LAT": "2"})
        fake = self._inject_fake_nr()
        r = self.client.post("/api/link-apis/t_probe_selfuse/install", json={})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(fake.create_calls, [], "self_use 必须拦下，不写 NR")

    def test_install_single_not_needs_nr_flow_400(self):
        """http_api 内联（needs_nr_flow()=False）→ 400，说明无需安装。"""
        self._add_spec(name="t_probe_http", title="探针-内联", kind="http_api",
                       url="https://example.invalid/x", method="POST")
        fake = self._inject_fake_nr()
        r = self.client.post("/api/link-apis/t_probe_http/install", json={})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(fake.create_calls, [], "无需安装的 spec 不得写 NR")

    def test_install_single_missing_config_400(self):
        """caiyun 未配置 → 400 且 missing 含该项，不写 NR。"""
        fake = self._inject_fake_nr()
        r = self.client.post("/api/link-apis/llm_caiyun_weather/install", json={})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["ok"])
        names = {m["name"] for m in body["missing"]}
        self.assertIn("llm_caiyun_weather", names)
        self.assertEqual(fake.create_calls, [], "被 400 拦下时不应写 NR")


if __name__ == "__main__":
    unittest.main()
