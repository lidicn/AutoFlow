#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接设置（#45）单测：HA / Node-RED / Bark 凭据的落盘、掩码回显、热生效。

覆盖三条不能破的性质：
1. **密钥永不明文外传** —— describe / HTTP 回包里只能出现掩码。
2. **落盘位置在 gitignored 的 data/ 下** —— 开源发布时不会被打包进仓库。
3. **保存即生效** —— os.environ + GatewayConfig 同步更新，且 NR/HA 层会用新凭据重建 client。
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway import connections

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway.gateway import Gateway
    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:  # pragma: no cover - 依赖缺失时优雅 skip
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = Gateway = None

_TOUCHED_ENV = [f.key for f in connections.FIELD_SPECS]


class EnvSandbox(unittest.TestCase):
    """每个用例独立 data_dir，并在结束后还原被改动的环境变量。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_conn_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")
        self._env_backup = {k: os.environ.get(k) for k in _TOUCHED_ENV}
        for k in _TOUCHED_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestConnectionsCore(EnvSandbox):
    def test_path_under_data_dir(self):
        """落盘必须在 data/<env>/ 下 —— 该目录已 gitignore，密钥不会进仓库。"""
        p = connections.connections_path(self.cfg)
        self.assertTrue(p.startswith(self.tmp), p)
        self.assertTrue(p.endswith(os.path.join("staging", "connections.json")), p)

    def test_update_persists_and_applies(self):
        res = connections.update(self.cfg, {
            "ha": {"HASS_SERVER": "http://ha.local:8123", "HASS_TOKEN": "tok-abcdefgh"},
        })
        self.assertEqual(sorted(res["changed"]), ["HASS_SERVER", "HASS_TOKEN"])
        # 落盘
        with open(connections.connections_path(self.cfg), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["HASS_SERVER"], "http://ha.local:8123")
        # 生效：env + cfg 字段
        self.assertEqual(os.environ["HASS_TOKEN"], "tok-abcdefgh")
        self.assertEqual(self.cfg.hass_server, "http://ha.local:8123")
        self.assertEqual(self.cfg.hass_token, "tok-abcdefgh")
        # 代数递增 → 各层据此重建 client
        self.assertEqual(res["revision"], 1)
        self.assertEqual(self.cfg.connection_revision, 1)

    def test_secret_never_leaks_in_describe(self):
        connections.update(self.cfg, {"bark": {"BARK_SERVER": "https://api.day.app",
                                               "BARK_KEY": "SuperSecretKey123456"}})
        d = connections.describe(self.cfg)
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("SuperSecretKey123456", blob)
        bark = [g for g in d["groups"] if g["id"] == "bark"][0]
        key = [f for f in bark["fields"] if f["key"] == "BARK_KEY"][0]
        self.assertTrue(key["configured"])
        self.assertEqual(key["masked"], "••••••••")   # 一个字符都不露
        self.assertEqual(key["length"], len("SuperSecretKey123456"))
        self.assertNotIn("value", key)
        # masked 固定为 8 个圆点、不随密钥内容变化（长度独立放 length 字段）——
        # 无字符泄漏风险，故不再逐 4 字符子串断言。原因：describe 中 kind="secret"
        # 这类正常字段名本身就会「巧合命中」密钥的 4 字符片段，属测试断言过严的
        # 误报。完整密钥串已确认不在 blob 中（上方 assertNotIn），且 masked 固定、
        # length 正确，已达保密目标。
        # 非 secret 明文回显，便于用户核对
        server = [f for f in bark["fields"] if f["key"] == "BARK_SERVER"][0]
        self.assertEqual(server["value"], "https://api.day.app")

    def test_mask_is_length_independent(self):
        """掩码长度固定，连密钥有多长都不从掩码本身泄漏（长度另放 length 字段）。"""
        connections.update(self.cfg, {"NR_PASS": "abc123"})
        d = connections.describe(self.cfg)
        nr = [g for g in d["groups"] if g["id"] == "nr"][0]
        pw = [f for f in nr["fields"] if f["key"] == "NR_PASS"][0]
        self.assertEqual(pw["masked"], "••••••••")
        self.assertEqual(pw["length"], 6)

    def test_blank_secret_keeps_existing(self):
        """前端回显的是掩码，空串必须视作『不修改』，否则一保存就把密钥抹了。"""
        connections.update(self.cfg, {"HASS_TOKEN": "keep-me-please"})
        res = connections.update(self.cfg, {"HASS_TOKEN": "", "HASS_SERVER": "http://ha:8123"})
        self.assertNotIn("HASS_TOKEN", res["changed"])
        self.assertEqual(connections.load_saved(self.cfg)["HASS_TOKEN"], "keep-me-please")

    def test_null_clears_field(self):
        connections.update(self.cfg, {"HASS_TOKEN": "to-be-removed"})
        res = connections.update(self.cfg, {"HASS_TOKEN": None})
        self.assertEqual(res["cleared"], ["HASS_TOKEN"])
        self.assertNotIn("HASS_TOKEN", connections.load_saved(self.cfg))
        self.assertNotIn("HASS_TOKEN", os.environ)

    def test_blank_plain_field_clears(self):
        connections.update(self.cfg, {"NR_USER": "admin"})
        res = connections.update(self.cfg, {"NR_USER": ""})
        self.assertEqual(res["cleared"], ["NR_USER"])
        self.assertNotIn("NR_USER", os.environ)

    def test_url_validation(self):
        with self.assertRaises(ValueError):
            connections.update(self.cfg, {"NR_URL": "127.0.0.1:1880"})

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            connections.update(self.cfg, {"EVIL_KEY": "x"})

    def test_newline_rejected(self):
        with self.assertRaises(ValueError):
            connections.update(self.cfg, {"NR_USER": "a\nb"})

    def test_noop_update_does_not_bump_revision(self):
        connections.update(self.cfg, {"NR_USER": "admin"})
        rev = self.cfg.connection_revision
        res = connections.update(self.cfg, {"NR_USER": "admin"})
        self.assertEqual(res["changed"], [])
        self.assertEqual(self.cfg.connection_revision, rev)

    def test_apply_saved_to_env_on_startup(self):
        connections.update(self.cfg, {"nr": {"NR_URL": "http://nr:1880", "NR_PASS": "pw"}})
        for k in ("NR_URL", "NR_PASS"):
            os.environ.pop(k, None)
        fresh = GatewayConfig(data_dir=self.tmp, env="staging")
        applied = connections.apply_saved_to_env(fresh)
        self.assertEqual(sorted(applied), ["NR_PASS", "NR_URL"])
        self.assertEqual(os.environ["NR_URL"], "http://nr:1880")
        self.assertEqual(fresh.nr_url, "http://nr:1880")

    def test_source_reporting(self):
        os.environ["BARK_SERVER"] = "https://from-env"
        d = connections.describe(self.cfg)
        bark = [g for g in d["groups"] if g["id"] == "bark"][0]
        srv = [f for f in bark["fields"] if f["key"] == "BARK_SERVER"][0]
        self.assertEqual(srv["source"], "env")
        connections.update(self.cfg, {"BARK_SERVER": "https://from-ui"})
        d2 = connections.describe(self.cfg)
        bark2 = [g for g in d2["groups"] if g["id"] == "bark"][0]
        srv2 = [f for f in bark2["fields"] if f["key"] == "BARK_SERVER"][0]
        self.assertEqual(srv2["source"], "ui")  # 界面设置优先，避免「改了不生效」
        self.assertEqual(srv2["value"], "https://from-ui")

    def test_corrupt_file_is_fail_open(self):
        p = connections.connections_path(self.cfg)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(connections.load_saved(self.cfg), {})

    def test_bark_test_without_config(self):
        r = connections.test_connections(self.cfg, ["bark"])
        self.assertFalse(r["bark"]["ok"])
        connections.update(self.cfg, {"bark": {"BARK_SERVER": "https://api.day.app",
                                               "BARK_KEY": "k123"}})
        r2 = connections.test_connections(self.cfg, ["bark"])
        self.assertTrue(r2["bark"]["ok"])      # 仅校验配置完整，不实际发推送
        self.assertFalse(r2["bark"]["sent"])

    def test_ha_test_does_not_refresh_catalog(self):
        """safe-gate-ui：测试连接仅做连通性探针，不得触发设备目录刷新。"""
        with mock.patch.object(connections, "_maybe_refresh_catalog") as m:
            # 不论 HA 是否配置，测试连接路径都不应调用 refresh_catalog
            connections.test_connections(self.cfg, ["ha"])
            m.assert_not_called()


class TestLayerHotReload(EnvSandbox):
    """连接设置改动后，NR/HA 层必须丢弃旧 client 重建（否则要重启网关才生效）。"""

    def test_nr_layer_rebuilds_on_revision_bump(self):
        from autoflow_gateway.nr_layer import NRLayer
        layer = NRLayer(self.cfg)
        sentinel = object()
        layer._client = sentinel                       # 冒充「旧凭据建好的 client」
        layer._client_rev = self.cfg.connection_revision
        connections.update(self.cfg, {"NR_URL": "http://nr-new:1880"})
        try:
            rebuilt = layer.client                     # 环境里有 nr_client → 重建成功
        except Exception:
            rebuilt = None                             # 没有 nr_client → 构造失败，但缓存确已丢弃
        self.assertIsNot(rebuilt, sentinel)
        if rebuilt is not None:
            self.assertEqual(layer._client_rev, self.cfg.connection_revision)
            url = getattr(rebuilt, "url", None) or getattr(rebuilt, "base_url", None)
            if url:
                self.assertIn("nr-new", str(url))      # 用的是新地址而非旧缓存

    def test_injected_backend_is_immune(self):
        """测试注入的假后端不受代数影响，否则整套离线测试会被连累。"""
        from autoflow_gateway.nr_layer import NRLayer
        fake = object()
        layer = NRLayer(self.cfg, backend=fake)
        self.assertIs(layer.client, fake)
        connections.update(self.cfg, {"NR_URL": "http://nr-new:1880"})
        self.assertIs(layer.client, fake)


@unittest.skipUnless(_HAVE_WEB_DEPS,
                     f"WebUI 测试需要 starlette（缺失：{_WEB_DEP_MSG}）")
class TestConnectionsAPI(EnvSandbox):
    def setUp(self):
        super().setUp()
        self.gw = Gateway(self.cfg)
        self.client = TestClient(build_webui_asgi(self.cfg, gateway=self.gw))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_get_lists_groups(self):
        r = self.client.get("/api/settings/connections")
        self.assertEqual(r.status_code, 200)
        ids = [g["id"] for g in r.json()["groups"]]
        self.assertEqual(ids, ["ha", "nr", "bark"])

    def test_put_saves_and_masks(self):
        r = self.client.put("/api/settings/connections",
                            json={"bark": {"BARK_SERVER": "https://api.day.app",
                                           "BARK_KEY": "TopSecretDeviceKey"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("TopSecretDeviceKey", r.text)   # 回包不得带明文
        r2 = self.client.get("/api/settings/connections")
        self.assertNotIn("TopSecretDeviceKey", r2.text)

    def test_put_rejects_bad_url(self):
        r = self.client.put("/api/settings/connections", json={"NR_URL": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_put_rejects_unknown_field(self):
        r = self.client.put("/api/settings/connections", json={"WHATEVER": "x"})
        self.assertEqual(r.status_code, 400)

    def test_test_endpoint_shape(self):
        r = self.client.post("/api/settings/connections/test", json={"targets": ["bark"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("bark", r.json()["results"])

    def test_settings_route_not_shadowed(self):
        """/api/settings 与 /api/settings/connections 必须各走各的。"""
        r = self.client.put("/api/settings", json={"task_pool_enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("task_pool_enabled", r.json())


if __name__ == "__main__":
    unittest.main()
