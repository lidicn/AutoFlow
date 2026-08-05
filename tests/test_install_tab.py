# -*- coding: utf-8 -*-
"""A3（#170）install-tab 端点单测。

覆盖 install_link_api_tab_endpoint（POST /api/link-apis/install-tab）：
- 缺配置：任一候选 spec 缺必填参数 → 400 并给出 missing 清单（含 spec 名/标题/缺项）；
- 配置齐：200 且增量合并写 NR：
  * 真值注入：彩云 http 节点 url 已替换 token/经纬度（无 <CAIYUN> 占位符残留）；
    anysearch 构造请求体节点已注入 Bearer <ANYSEARCH_API_KEY>（无占位符残留）；
  * 入口节点 id 正确（af_weather_in / af_anysearch_in）；
  * allow_prod=True 透传给 NR client；
- 幂等：重复调用 nodes_added=0、nodes_total 不变（按节点 id 去重，不重复生成）；
- 豆包排除：self_use 的 doubao 系列不进候选，响应 specs 不含 doubao、节点不含 af_apisay_in；
- 不碰无关 flow：get_flow / create_or_update_flow 只针对 'af_api_tab' 这一个 flow_id
  （绝不触碰 1990 等其它既有 flow，硬约束）。

用内存 fake NR client 验证增量合并行为，无真实 NR 依赖。
"""
import os
import sys
import json
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
    """内存 fake NR client：记录 install-tab 端点到底对哪些 flow 做了读写。

    支持 get_flow（返回已存 flow，未存则空 nodes）与 create_or_update_flow
    （落盘到内存 + 记录调用，含 allow_prod）。足以验证「增量合并 + 只碰 af_api_tab」。
    """
    def __init__(self):
        self.flows = {}          # flow_id -> flow_data
        self.get_flow_calls = []      # 记录每次 get_flow 的 flow_id
        self.create_calls = []       # (flow_id, force, allow_prod)

    def get_flow(self, flow_id, use_cache=True):
        self.get_flow_calls.append(flow_id)
        if flow_id in self.flows:
            return self.flows[flow_id]
        return {"id": flow_id, "nodes": []}

    def create_or_update_flow(self, flow_id, flow_data, force, allow_prod):
        self.create_calls.append((flow_id, force, allow_prod))
        # 落盘：模拟 NR 持久化，供第二次调用的 get_flow 返回、触发幂等路径
        self.flows[flow_id] = flow_data
        return {"id": flow_id, "ok": True, "created": True}


@unittest.skipUnless(_HAVE_WEB_DEPS, "A3 测试需要 starlette（缺失则 pip install starlette）。")
class TestInstallTab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_install_tab_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_cfg(self, name, d):
        """写入 api_configs 表（与端点同库：同 data_dir 的 SQLite，WAL）。"""
        st = ApiConfigStore(config=SimpleNamespace(data_dir=self.tmp))
        try:
            st.set_api_config(name, d)
        finally:
            st.close()

    def _install(self):
        return self.client.post("/api/link-apis/install-tab", json={})

    def _inject_fake_nr(self):
        fake = FakeNRClients()
        self.gw.nr._client = fake
        self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)
        return fake

    def test_missing_config_returns_400_with_list(self):
        # 一个都没配 → 400，missing 列出彩云 + anysearch 的缺项
        r = self._install()
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("missing", body)
        names = {m["name"] for m in body["missing"]}
        self.assertIn("llm_caiyun_weather", names)
        self.assertIn("anysearch_batch", names)
        # 彩云缺三项、anysearch 缺一项
        caiyun = next(m for m in body["missing"] if m["name"] == "llm_caiyun_weather")
        self.assertEqual(set(caiyun["missing"]),
                         {"CAIYUN_TOKEN", "CAIYUN_LON", "CAIYUN_LAT"})
        anysearch = next(m for m in body["missing"] if m["name"] == "anysearch_batch")
        self.assertEqual(anysearch["missing"], ["ANYSEARCH_API_KEY"])

    def test_install_success_injects_real_values(self):
        self._set_cfg("llm_caiyun_weather", {
            "CAIYUN_TOKEN": "tokABC",
            "CAIYUN_LON": "116.40",
            "CAIYUN_LAT": "39.90",
        })
        self._set_cfg("anysearch_batch", {"ANYSEARCH_API_KEY": "akXYZ"})
        fake = self._inject_fake_nr()

        r = self._install()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertGreater(body["nodes_added"], 0)

        # NR 只收到 af_api_tab 一个 flow 写入，且 allow_prod=True
        self.assertEqual(len(fake.create_calls), 1)
        flow_id, force, allow_prod = fake.create_calls[0]
        self.assertEqual(flow_id, "af_api_tab")
        self.assertTrue(allow_prod)
        nodes = fake.flows["af_api_tab"]["nodes"]
        ids = {n["id"] for n in nodes}

        # 入口节点 id 正确
        self.assertIn("af_weather_in", ids)
        self.assertIn("af_anysearch_in", ids)

        # 彩云 http 节点 url 已注入真值、无占位符残留
        caiyun_http = next(n for n in nodes if n["id"] == "af_weather_in_http")
        self.assertIn("tokABC", caiyun_http["url"])
        self.assertIn("116.40", caiyun_http["url"])
        self.assertIn("39.90", caiyun_http["url"])
        self.assertNotIn("<CAIYUN", caiyun_http["url"])

        # anysearch 构造请求体节点已注入 Bearer 真值、无占位符残留
        anysearch_body = next(n for n in nodes if n["id"] == "af_anysearch_in_body")
        header_rule = next(
            (rule for rule in anysearch_body["rules"] if rule["p"] == "headers"),
            None,
        )
        self.assertIsNotNone(header_rule, "anysearch body 节点应含 headers 规则")
        self.assertIn("akXYZ", header_rule["to"])
        self.assertNotIn("<ANYSEARCH", header_rule["to"])

    def test_idempotent_repeat_no_duplicate(self):
        self._set_cfg("llm_caiyun_weather", {
            "CAIYUN_TOKEN": "tokABC",
            "CAIYUN_LON": "116.40",
            "CAIYUN_LAT": "39.90",
        })
        self._set_cfg("anysearch_batch", {"ANYSEARCH_API_KEY": "akXYZ"})
        fake = self._inject_fake_nr()

        first = self._install().json()
        self.assertTrue(first["ok"])
        self.assertGreater(first["nodes_added"], 0)
        first_total = first["nodes_total"]

        # 第二次：get_flow 返回首次写入的 flow → 全部节点已存在 → nodes_added=0
        second = self._install().json()
        self.assertTrue(second["ok"])
        self.assertEqual(second["nodes_added"], 0)
        self.assertEqual(second["nodes_total"], first_total)

    def test_doubao_self_use_excluded(self):
        self._set_cfg("llm_caiyun_weather", {
            "CAIYUN_TOKEN": "tokABC",
            "CAIYUN_LON": "116.40",
            "CAIYUN_LAT": "39.90",
        })
        self._set_cfg("anysearch_batch", {"ANYSEARCH_API_KEY": "akXYZ"})
        fake = self._inject_fake_nr()

        body = self._install().json()
        # 响应 specs 仅含非 self_use 的彩云/anysearch，不含 doubao 系列
        self.assertEqual(set(body["specs"]), {"llm_caiyun_weather", "anysearch_batch"})
        # 节点里不能出现 doubao say 的入口（self_use 被排除，不生成任何 doubao 节点）
        ids = {n["id"] for n in fake.flows["af_api_tab"]["nodes"]}
        self.assertNotIn("af_apisay_in", ids)

    def test_only_touches_af_api_tab_flow(self):
        self._set_cfg("llm_caiyun_weather", {
            "CAIYUN_TOKEN": "tokABC",
            "CAIYUN_LON": "116.40",
            "CAIYUN_LAT": "39.90",
        })
        self._set_cfg("anysearch_batch", {"ANYSEARCH_API_KEY": "akXYZ"})
        fake = self._inject_fake_nr()

        self._install()
        # 绝不触碰 1990 等其它 flow：所有读写只针对 af_api_tab 这一个 flow_id
        self.assertTrue(all(fid == "af_api_tab" for fid in fake.get_flow_calls))
        self.assertTrue(all(fid == "af_api_tab" for fid, _, _ in fake.create_calls))
        self.assertNotIn("1990", fake.get_flow_calls)
        self.assertNotIn("1990", [fid for fid, _, _ in fake.create_calls])


if __name__ == "__main__":
    unittest.main()
