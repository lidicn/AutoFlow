# -*- coding: utf-8 -*-
"""#C-tab：tab 链接逆生成 Link API —— WebUI 端点 + 注册闭环测试。

覆盖 import-from-url / register-from-tab：
- import-from-url：解析 URL + 只读自省 → 返回草稿（registerable/entry_id/params/suggested_key）
- inject 入口 → registerable=False（草稿阶段即拦下）
- register-from-tab：落注册表 kind=link_out，entry_link_id 权威来自服务端重自省
- 注册后 get_subflow 解析为 link_out（DSL 编译器可调用）
- 参数编辑：客户端传增删/改类型后的 params，服务端归一入库
- 非法参数名 / 缺 key / 未知 tab → 各自返回预期错误码
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
from autoflow_gateway.subflows import get_subflow

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _HAVE_WEB_DEPS = True
except ImportError:
    _HAVE_WEB_DEPS = False
    TestClient = build_webui_asgi = None


class FakeNRClients:
    """内存 fake NR client：只实现 tab 逆生成需要的 list_flows（扁平数组）。"""

    def __init__(self):
        self.flows = {}
        self.list_flows_calls = 0

    def list_flows(self):
        self.list_flows_calls += 1
        out = []
        for fid, fl in self.flows.items():
            out.append({"id": fid, "type": "tab", "label": fl.get("label", "")})
            out.extend(copy.deepcopy(fl.get("nodes", [])))
        return out

    def get_flow(self, flow_id, use_cache=True):
        raise RuntimeError(f"HTTP 404 Not Found: /flow/{flow_id}")


TAB_ID = "e70a201b5f004927"
USER_TAB = {
    "id": TAB_ID, "label": "智能语音播报队列",
    "nodes": [
        {"id": "tts_in", "type": "link in", "z": TAB_ID,
         "name": "TTS 入口", "wires": [["fn1"]]},
        {"id": "fn1", "type": "function", "z": TAB_ID,
         "func": "var m = msg; m.volume = msg.volume || 50; return m;"},
        {"id": "ch1", "type": "change", "z": TAB_ID,
         "rules": [{"to": "msg.payload", "p": "msg.voice"},
                   {"to": "msg.text", "p": "msg.text"}]},
    ],
}


@unittest.skipUnless(_HAVE_WEB_DEPS, "需要 starlette（缺失则 pip install starlette）。")
class TestImportLinkApiFromTab(unittest.TestCase):
    def setUp(self):
        # 回环 + token_only 模式：TestClient（client="testclient"）免登录跑写端点，
        # 与现网 password_only 行为解耦（本机初始化态），只验证本次功能。
        self._prev_mode = os.environ.get("AF_WEBUI_TOKEN_MODE")
        os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
        self.tmp = tempfile.mkdtemp(prefix="af_tab_link_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.fake = FakeNRClients()
        self.fake.flows[TAB_ID] = copy.deepcopy(USER_TAB)
        self.gw.nr._client = self.fake
        self.gw.nr._client_rev = getattr(self.cfg, "connection_revision", 0)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if self._prev_mode is None:
            os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        else:
            os.environ["AF_WEBUI_TOKEN_MODE"] = self._prev_mode
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── import-from-url：检测草稿 ──
    def test_import_from_url_draft(self):
        r = self.client.post(
            "/api/link-apis/import-from-url",
            json={"url": f"http://192.168.2.200:1990/#flow/{TAB_ID}"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["registerable"])
        self.assertEqual(d["entry_kind"], "link_in")
        self.assertEqual(d["entry_id"], "tts_in")
        names = {p["name"] for p in d["params"]}
        self.assertEqual(names, {"voice", "text", "volume"})
        # 中文标题无法 slugify → suggested_key None（需用户手填）
        self.assertIsNone(d["suggested_key"])

    def test_import_from_url_inject_not_registerable(self):
        self.fake.flows[TAB_ID] = {
            "id": TAB_ID, "label": "定时播报",
            "nodes": [{"id": "inj", "type": "inject", "z": TAB_ID, "wires": []}]}
        r = self.client.post(
            "/api/link-apis/import-from-url", json={"url": f"#flow/{TAB_ID}"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertFalse(d["registerable"])
        self.assertEqual(d["entry_kind"], "inject")

    # ── register-from-tab：落库闭环 ──
    def test_register_persists_link_out_and_resolves(self):
        det = self.client.post(
            "/api/link-apis/import-from-url",
            json={"url": f"#flow/{TAB_ID}"}).json()
        self.assertTrue(det["registerable"])

        params = [
            {"name": "text", "type": "str", "required": True, "desc": "播报文本"},
            {"name": "volume", "type": "int", "required": False, "desc": ""},
            {"name": "voice", "type": "str", "required": False, "desc": ""},
            {"name": "speed", "type": "float", "required": False, "desc": "语速"},
        ]
        r = self.client.post(
            "/api/link-apis/register-from-tab",
            json={"url": f"#flow/{TAB_ID}", "key": "TTS",
                  "title": "智能语音播报队列", "params": params})
        self.assertEqual(r.status_code, 201, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["key"], "TTS")
        self.assertEqual(d["entry_id"], "tts_in")

        # get_subflow 解析为 link_out（DSL 编译器可调用）
        spec = get_subflow("TTS", self.gw.tasks)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.call["type"], "link_out")
        self.assertEqual(spec.call["entry_link_id"], "tts_in")
        self.assertIn("text", spec.params)
        self.assertTrue(spec.params["text"].required)
        self.assertIn("speed", spec.params)

    def test_register_bad_param_name_400(self):
        r = self.client.post(
            "/api/link-apis/register-from-tab",
            json={"url": f"#flow/{TAB_ID}", "key": "TTS2",
                  "params": [{"name": "1bad", "type": "str"}]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("标识符", r.json()["error"])

    def test_register_requires_key_400(self):
        r = self.client.post(
            "/api/link-apis/register-from-tab",
            json={"url": f"#flow/{TAB_ID}", "params": []})
        self.assertEqual(r.status_code, 400)

    def test_register_unknown_tab_502(self):
        r = self.client.post(
            "/api/link-apis/register-from-tab",
            json={"url": "#flow/deadbeef", "key": "X"})
        # 服务端重自省拿不到 tab → 自省失败 502
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
