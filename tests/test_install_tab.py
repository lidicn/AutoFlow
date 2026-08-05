# -*- coding: utf-8 -*-
"""A3（#170）install-tab 端点单测 + risk-1（#177）tab id 丢失回归。

覆盖 install_link_api_tab_endpoint（POST /api/link-apis/install-tab）：
- 缺配置：任一候选 spec 缺必填参数 → 400 并给出 missing 清单（含 spec 名/标题/缺项）；
- 配置齐：200 且增量合并写 NR：
  * 真值注入：彩云 http 节点 url 已替换 token/经纬度（无 <CAIYUN> 占位符残留）；
    anysearch 构造请求体节点已注入 Bearer <ANYSEARCH_API_KEY>（无占位符残留）；
  * 入口节点 id 正确（af_weather_in / af_anysearch_in）；
  * allow_prod=True 透传给 NR client；
- 幂等：重复调用 nodes_added=0、NR 上仍只有 1 个「AutoFlow API」tab；
- 豆包排除：self_use 的 doubao 系列不进候选，响应 specs 不含 doubao、节点不含 af_apisay_in；
- 不碰无关 flow：既有的用户 tab 在安装前后逐字节不变（硬约束）。

risk-1（#177）回归重点：Node-RED 的 POST /flow 会自行分配 tab id 并忽略 body 里的
"id"。A3 初版拿字面量 "af_api_tab" 探测 → 恒 404 → 每次都新建重名 tab。故本文件的
fake NR 忠实模拟该行为：get_flow 未命中直接抛（404），POST 路径另发真实 id 并改写
节点 z。旧版 fake 未命中时返回空 flow，恰好把这个 bug 掩盖了。
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
    """内存 fake NR client：忠实模拟 Node-RED admin API 的关键行为。

    - get_flow：未命中抛异常（等价 404），**不返回空壳** —— 这是 #177 的照妖镜；
    - create_or_update_flow：flow 已存在走 PUT 原地更新；不存在则模拟 POST /flow，
      忽略调用方传的 id、自行分配真实 id 并把节点 z 改写为该真实 id；
    - list_flows：返回 GET /flows 那样的扁平数组（tab 对象 + 所有节点）。
    """

    NEW_TAB_ID = "3c2d2af8c0878f6f"   # 与 1990 现网实际 tab id 同形

    def __init__(self):
        self.flows = {}            # flow_id -> flow_data
        self.get_flow_calls = []   # 每次 get_flow 的 flow_id
        self.create_calls = []     # (flow_id, force, allow_prod)
        self.list_flows_calls = 0
        self._seq = 0

    # ── 读 ──
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

    # ── 写 ──
    def create_or_update_flow(self, flow_id, flow_data, force, allow_prod):
        self.create_calls.append((flow_id, force, allow_prod))
        if flow_id in self.flows:
            self.flows[flow_id] = {**copy.deepcopy(flow_data), "id": flow_id}
            return {"id": flow_id, "created": False, "raw": None}
        # 模拟 NR：POST /flow 不采纳 body 里的 id，自行分配并改写节点 z
        self._seq += 1
        real = self.NEW_TAB_ID if self._seq == 1 else f"{self.NEW_TAB_ID}-{self._seq}"
        nodes = [dict(copy.deepcopy(n), z=real) for n in flow_data.get("nodes", [])]
        self.flows[real] = {**copy.deepcopy(flow_data), "id": real, "nodes": nodes}
        return {"id": real, "created": True, "raw": None}

    # ── 断言辅助 ──
    def tabs_named(self, label):
        return [fid for fid, fl in self.flows.items() if fl.get("label") == label]


CAIYUN_CFG = {"CAIYUN_TOKEN": "tokABC", "CAIYUN_LON": "116.40", "CAIYUN_LAT": "39.90"}
ANYSEARCH_CFG = {"ANYSEARCH_API_KEY": "akXYZ"}

# 模拟 1990 上用户自用的无关 tab（豆包链路），安装全程不得被改动
USER_TAB_ID = "userprodtab0001"
USER_TAB = {
    "id": USER_TAB_ID,
    "label": "用户自用",
    "nodes": [{"id": "af_apisay_in", "type": "link in", "z": USER_TAB_ID,
               "name": "豆包 say 入口", "wires": []}],
}


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

    # ── 夹具 ──
    def _set_cfg(self, name, d):
        """写入 api_configs 表（与端点同库：同 data_dir 的 SQLite，WAL）。"""
        st = ApiConfigStore(config=SimpleNamespace(data_dir=self.tmp))
        try:
            st.set_api_config(name, d)
        finally:
            st.close()

    def _set_all_cfgs(self):
        self._set_cfg("llm_caiyun_weather", CAIYUN_CFG)
        self._set_cfg("anysearch_batch", ANYSEARCH_CFG)

    def _install(self):
        return self.client.post("/api/link-apis/install-tab", json={})

    def _inject_fake_nr(self):
        fake = FakeNRClients()
        self.gw.nr._client = fake
        self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)
        return fake

    @property
    def _ledger(self):
        return os.path.join(self.tmp, "af_api_tab.id")

    # ── A3 原有覆盖 ──
    def test_missing_config_returns_400_with_list(self):
        r = self._install()
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["ok"])
        names = {m["name"] for m in body["missing"]}
        self.assertIn("llm_caiyun_weather", names)
        self.assertIn("anysearch_batch", names)
        caiyun = next(m for m in body["missing"] if m["name"] == "llm_caiyun_weather")
        self.assertEqual(set(caiyun["missing"]),
                         {"CAIYUN_TOKEN", "CAIYUN_LON", "CAIYUN_LAT"})
        anysearch = next(m for m in body["missing"] if m["name"] == "anysearch_batch")
        self.assertEqual(anysearch["missing"], ["ANYSEARCH_API_KEY"])

    def test_install_success_injects_real_values(self):
        self._set_all_cfgs()
        fake = self._inject_fake_nr()

        r = self._install()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertGreater(body["nodes_added"], 0)
        # #177：响应必须回报 NR 分配的真实 id，而非种子 af_api_tab
        self.assertEqual(body["tab_id"], FakeNRClients.NEW_TAB_ID)
        self.assertTrue(body["tab_created"])

        _, force, allow_prod = fake.create_calls[0]
        self.assertTrue(allow_prod)

        nodes = fake.flows[FakeNRClients.NEW_TAB_ID]["nodes"]
        ids = {n["id"] for n in nodes}
        self.assertIn("af_weather_in", ids)
        self.assertIn("af_anysearch_in", ids)
        # 节点 z 全部对齐真实 tab id（不能残留种子 id）
        self.assertEqual({n["z"] for n in nodes}, {FakeNRClients.NEW_TAB_ID})

        caiyun_http = next(n for n in nodes if n["id"] == "af_weather_in_http")
        self.assertIn("tokABC", caiyun_http["url"])
        self.assertIn("116.40", caiyun_http["url"])
        self.assertIn("39.90", caiyun_http["url"])
        self.assertNotIn("<CAIYUN", caiyun_http["url"])

        anysearch_body = next(n for n in nodes if n["id"] == "af_anysearch_in_body")
        header_rule = next(
            (rule for rule in anysearch_body["rules"] if rule["p"] == "headers"), None)
        self.assertIsNotNone(header_rule, "anysearch body 节点应含 headers 规则")
        self.assertIn("akXYZ", header_rule["to"])
        self.assertNotIn("<ANYSEARCH", header_rule["to"])

    def test_doubao_self_use_excluded(self):
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        body = self._install().json()
        self.assertEqual(set(body["specs"]), {"llm_caiyun_weather", "anysearch_batch"})
        ids = {n["id"] for n in fake.flows[FakeNRClients.NEW_TAB_ID]["nodes"]}
        self.assertNotIn("af_apisay_in", ids)

    # ── risk-1（#177）回归 ──
    def test_second_install_creates_no_duplicate_tab(self):
        """验收①：连点两次，NR 上仍只有 1 个「AutoFlow API」tab，第二次 nodes_added==0。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()

        first = self._install().json()
        self.assertTrue(first["ok"])
        self.assertGreater(first["nodes_added"], 0)

        second = self._install().json()
        self.assertTrue(second["ok"])
        self.assertEqual(second["nodes_added"], 0)
        self.assertEqual(second["tab_id"], first["tab_id"])
        # 关键：只有一个 AutoFlow API tab（旧实现这里会变成 2 个）
        self.assertEqual(fake.tabs_named("AutoFlow API"), [FakeNRClients.NEW_TAB_ID])
        # 内容无变化 → 干脆不写 NR（最强幂等，prod 零打扰）
        self.assertTrue(second.get("skipped"))
        self.assertEqual(len(fake.create_calls), 1)

    def test_no_cross_tab_duplicate_node_ids(self):
        """验收②：af_weather_in / af_anysearch_in 不跨 tab 重复（子流程串台坑）。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        self._install()
        self._install()
        self._install()

        seen = {}
        for fid, fl in fake.flows.items():
            for n in fl.get("nodes", []):
                seen.setdefault(n["id"], set()).add(fid)
        for nid in ("af_weather_in", "af_anysearch_in"):
            self.assertEqual(len(seen.get(nid, set())), 1,
                             f"节点 {nid} 出现在多个 tab：{seen.get(nid)}")

    def test_adopts_existing_tab_without_ledger(self):
        """现网态：NR 上已有真实 tab、本地无台账 → 必须 list_flows 认领而非新建。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        # 预置一个已安装的 AutoFlow API tab（旧 token），且不写台账
        fake.flows[FakeNRClients.NEW_TAB_ID] = {
            "id": FakeNRClients.NEW_TAB_ID, "label": "AutoFlow API",
            "nodes": [{"id": "af_weather_in", "type": "link in",
                       "z": FakeNRClients.NEW_TAB_ID, "name": "旧入口", "wires": []},
                      {"id": "user_note", "type": "comment",
                       "z": FakeNRClients.NEW_TAB_ID, "name": "用户手写备注"}],
        }
        self.assertFalse(os.path.exists(self._ledger))

        body = self._install().json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tab_id"], FakeNRClients.NEW_TAB_ID)
        self.assertFalse(body["tab_created"])
        self.assertEqual(fake.tabs_named("AutoFlow API"), [FakeNRClients.NEW_TAB_ID])
        # 认领后回写台账，下次免扫描
        with open(self._ledger, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), FakeNRClients.NEW_TAB_ID)
        ids = {n["id"] for n in fake.flows[FakeNRClients.NEW_TAB_ID]["nodes"]}
        self.assertIn("user_note", ids)          # 用户节点绝不删
        self.assertIn("af_anysearch_in", ids)    # 缺的链补上

    def test_stale_ledger_falls_back_to_scan(self):
        """台账指向已不存在的 tab（换实例/被删）→ 重扫认领，不残留脏 id。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        with open(self._ledger, "w", encoding="utf-8") as f:
            f.write("deadbeefdeadbeef")
        fake.flows[FakeNRClients.NEW_TAB_ID] = {
            "id": FakeNRClients.NEW_TAB_ID, "label": "AutoFlow API", "nodes": []}

        body = self._install().json()
        self.assertEqual(body["tab_id"], FakeNRClients.NEW_TAB_ID)
        self.assertEqual(fake.tabs_named("AutoFlow API"), [FakeNRClients.NEW_TAB_ID])
        with open(self._ledger, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), FakeNRClients.NEW_TAB_ID)

    def test_config_change_refreshes_nodes(self):
        """改了 token 再装 → 既有节点就地刷新（否则用户改配置永远不生效）。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        self._install()

        self._set_cfg("llm_caiyun_weather", {**CAIYUN_CFG, "CAIYUN_TOKEN": "tokNEW"})
        body = self._install().json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["nodes_added"], 0)
        self.assertGreater(body["nodes_updated"], 0)
        nodes = fake.flows[FakeNRClients.NEW_TAB_ID]["nodes"]
        http = next(n for n in nodes if n["id"] == "af_weather_in_http")
        self.assertIn("tokNEW", http["url"])
        self.assertNotIn("tokABC", http["url"])
        self.assertEqual(fake.tabs_named("AutoFlow API"), [FakeNRClients.NEW_TAB_ID])

    def test_never_touches_unrelated_flow(self):
        """硬约束：1990 上用户自用 tab 在安装前后逐字节不变，且从不被写入。"""
        self._set_all_cfgs()
        fake = self._inject_fake_nr()
        fake.flows[USER_TAB_ID] = copy.deepcopy(USER_TAB)
        before = copy.deepcopy(fake.flows[USER_TAB_ID])

        self._install()
        self._install()

        self.assertEqual(fake.flows[USER_TAB_ID], before)
        self.assertNotIn(USER_TAB_ID, [fid for fid, _, _ in fake.create_calls])
        self.assertNotIn(USER_TAB_ID, fake.get_flow_calls)


if __name__ == "__main__":
    unittest.main()
