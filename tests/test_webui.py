#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebUI 端点 + MCP 身份鉴权中间件单测（unittest + starlette TestClient）。"""
import os
import sys
import json
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.identity import AgentStore

# WebUI/MCP 端点测试是「离线」的（进程内 starlette TestClient，不触真实 NR/HA），
# 仅依赖 starlette + mcp 框架包。缺包时优雅 skip，而非让整套离线测试崩溃。
try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    from autoflow_gateway.mcp_server import build_app
    _HAVE_WEB_DEPS = True
    _WEB_DEP_MSG = ""
except ImportError as _e:
    _HAVE_WEB_DEPS = False
    _WEB_DEP_MSG = str(_e)
    TestClient = build_webui_asgi = build_app = None


class TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_web_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(_HAVE_WEB_DEPS,
                      f"WebUI/MCP 测试需要 starlette+mcp（缺失：{_WEB_DEP_MSG}）；"
                      f"用系统 Python 3.13.2 或 pip install starlette mcp 后运行。")
class TestWebUI(TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gw = Gateway(self.cfg)
        self.app = build_webui_asgi(self.cfg, gateway=self.gw)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_health_and_config(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["env"], "staging")

    def test_agents_crud_via_api(self):
        r = self.client.post("/api/agents", json={"name": "deepseek++", "tier": "staging"})
        self.assertEqual(r.status_code, 201)
        code = r.json()["agent"]["identity_code"]
        self.assertTrue(code.startswith("af_"))
        # 列表含之
        ids = [a["name"] for a in self.client.get("/api/agents").json()["agents"]]
        self.assertIn("deepseek++", ids)
        # 重复名 409
        r2 = self.client.post("/api/agents", json={"name": "deepseek++"})
        self.assertEqual(r2.status_code, 409)
        # 重置码
        aid = r.json()["agent"]["agent_id"]
        new = self.client.post(f"/api/agents/{aid}/regen").json()["identity_code"]
        self.assertNotEqual(new, code)
        # 吊销
        self.assertTrue(self.client.post(f"/api/agents/{aid}/revoke").json()["ok"])

    def test_proposals_and_promote(self):
        r = self.client.post("/api/proposals", json={
            "agent_id": "agt", "title": "建议X", "kind": "idea", "content": "c", "tags": ["ha"]})
        self.assertEqual(r.status_code, 201)
        pid = r.json()["proposal"]["id"]
        # promote raw -> candidate
        p = self.client.post(f"/api/proposals/{pid}/promote").json()["proposal"]
        self.assertEqual(p["status"], "candidate")
        # promote candidate -> public
        p = self.client.post(f"/api/proposals/{pid}/promote").json()["proposal"]
        self.assertEqual(p["status"], "public")
        self.assertIsNotNone(p["public_path"])

    def test_notes_crud_via_api(self):
        r = self.client.post("/api/notes", json={"title": "想法", "body": "内容", "tags": ["照明"]})
        self.assertEqual(r.status_code, 201)
        nid = r.json()["note"]["id"]
        self.assertEqual(len(self.client.get("/api/notes").json()["notes"]), 1)
        self.client.put(f"/api/notes/{nid}", json={"body": "改"})
        self.assertEqual(self.client.get("/api/notes").json()["notes"][0]["body"], "改")
        self.assertTrue(self.client.delete(f"/api/notes/{nid}").json()["ok"])

    def test_index_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_deploy_policy_endpoints_and_proposal_enrichment(self):
        """P4-A：config 回显 deploy_policy；settings 运行时切换（含 fail-safe）；
        list_proposals 按策略+来源给每条提案打 requires_review。"""
        # 默认 config 含 deploy_policy = review_all
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deploy_policy"], "review_all")

        # 切换为 compiler_auto（PUT /api/settings）
        r = self.client.put("/api/settings", json={"deploy_policy": "compiler_auto"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(r.json()["deploy_policy"], "compiler_auto")
        # config 视图回显同步
        self.assertEqual(self.client.get("/api/config").json()["deploy_policy"], "compiler_auto")

        # 未知值 → 400 fail-safe 拒绝
        r = self.client.put("/api/settings", json={"deploy_policy": "auto_pilot"})
        self.assertEqual(r.status_code, 400)
        # 拒绝后值不变
        self.assertEqual(self.client.get("/api/config").json()["deploy_policy"], "compiler_auto")

        # 造两条提案：compiler(可信) + raw(需审)，走与 WebUI 相同的 ProposalStore
        from autoflow_gateway.proposals import ProposalStore
        ps = ProposalStore(self.cfg)
        ps.submit("a", "编译流", "skill", json.dumps({"dsl": "x"}),
                  tags=[], source="compiler", spec="x")
        ps.submit("a", "手写流", "skill", json.dumps({"type": "raw_flow", "flow": {}}),
                  tags=[], source="raw", spec="y")

        # compiler_auto：compiler 免审、raw 需审
        r = self.client.get("/api/proposals")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deploy_policy"], "compiler_auto")
        items = {p["source"]: p for p in r.json()["proposals"]}
        self.assertIn("compiler", items)
        self.assertIn("raw", items)
        self.assertEqual(items["compiler"]["requires_review"], False)
        self.assertEqual(items["raw"]["requires_review"], True)
        # P4-B 渲染所需的 spec 字段随提案返回
        self.assertIn("spec", items["compiler"])
        self.assertIn("spec", items["raw"])

        # 复位回 review_all：两条都需审
        self.client.put("/api/settings", json={"deploy_policy": "review_all"})
        r = self.client.get("/api/proposals")
        for p in r.json()["proposals"]:
            self.assertEqual(p["requires_review"], True)

    def test_diagnostics_endpoint(self):
        """P4-C：GET /api/diagnostics 聚合 env/health、计数、最近 trace、最近评测任务。"""
        # 触发一次 _slog（list_pending 内部 emit list_pending.done），让环形缓冲非空
        self.gw.list_pending()
        # 直接种一条 fake 评测任务到内存表，验证 list_golden_jobs 的投影（不真跑评测，避免触真实 NR/HA）
        import autoflow_gateway.gateway as _gwmod
        with _gwmod._GOLDEN_JOBS_LOCK:
            _gwmod._GOLDEN_JOBS["gtest123"] = {
                "job_id": "gtest123", "status": "done", "scenario": "1",
                "mode": "black", "backend": "ds_bridge",
                "started_at": 1700000000.0, "finished_at": 1700000060.0,
                "result": {"ok": True, "acceptance": "PASS"},
                "events": [{"ts": 1, "phase": "x", "msg": "y"}],
            }

        r = self.client.get("/api/diagnostics")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["env"], "staging")
        self.assertIn("deploy_policy", d)
        self.assertIn("nr_url", d)
        self.assertIn("hass_server", d)
        self.assertIn("mcp", d)
        self.assertIn("mcp_white", d)
        self.assertIn("mcp_admin", d)

        # 计数含关键键
        c = d["counts"]
        for k in ("agents", "pending_ops", "deployed_flows",
                  "proposals_total", "proposals_deployed", "proposals_by_status"):
            self.assertIn(k, c)
        self.assertIsInstance(c["proposals_by_status"], dict)

        # 最近 trace：环形缓冲应有内容，且每条含 stage
        traces = d["traces"]
        self.assertIsInstance(traces, list)
        self.assertTrue(any(t.get("stage") for t in traces), "trace 应含 stage 字段")

        # 最近评测任务：投影正确
        jobs = d["golden_jobs"]
        self.assertIsInstance(jobs, list)
        self.assertTrue(any(j["job_id"] == "gtest123" for j in jobs), "应回显种入的评测任务")
        j = next(x for x in jobs if x["job_id"] == "gtest123")
        self.assertEqual(j["kind"], "golden")
        self.assertEqual(j["scenario"], "1")
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["ok"], True)
        self.assertEqual(j["n_events"], 1)
        # summary 取 acceptance 结果
        self.assertIn("PASS", j["summary"])

    def test_device_catalog_endpoints(self):
        """safe-gate-ui：GET /api/catalog、GET /api/entities、POST /api/catalog/import 行为正确。"""
        from unittest.mock import Mock
        # 桩：用离线假数据隔离真实 HA/NR 拉取，只验证端点编排
        self.gw.state.get_device_catalog = lambda: {
            "version": 1, "freshness": "2026-08-03T10:00:00",
            "entities": {"light.office": {"friendly_name": "书房灯"}},
        }
        self.gw.list_entities = Mock(return_value={
            "entities": [{"entity_id": "light.office", "friendly_name": "书房灯",
                          "area": "书房", "domain": "light"}],
            "matched_count": 1, "total": 1, "keyword": "灯", "limit": 20, "available": True,
        })
        self.gw.refresh_catalog = Mock(return_value={
            "mode": "full", "entity_total": 1, "fetched": 1, "changed": 0,
            "added": 1, "gone_marked": 0, "freshness": "2026-08-03T10:00:00",
        })

        # GET /api/catalog → {total, freshness, last_import_at}
        r = self.client.get("/api/catalog")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["last_import_at"], "2026-08-03T10:00:00")

        # GET /api/entities?keyword= → 透传网关 list_entities 结果
        r = self.client.get("/api/entities?keyword=灯&limit=20")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["entities"]), 1)

        # POST /api/catalog/import → 显式刷新且 full=True
        r = self.client.post("/api/catalog/import")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["total"], 1)
        self.gw.refresh_catalog.assert_called_once_with(full=True)

    def test_catalog_import_error_surfaced(self):
        """refresh_catalog 异常时端点返回 500 且 ok=False，不静默。"""
        from unittest.mock import Mock
        self.gw.refresh_catalog = Mock(side_effect=RuntimeError("HA 不可达"))
        r = self.client.post("/api/catalog/import")
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.json()["ok"])
        self.assertIn("导入失败", r.json()["error"])

    def test_webui_token_constant_time_compare(self):
        """P0-1 (S-1)：token 用常量时间比较。正确 token 放行、错误(含同前缀)拒绝。

        验证 hmac.compare_digest 路径生效：不只「相等才过」，且错误 token 一律 403。
        """
        import os
        tok = "af_test_secret_token_xyz"
        os.environ["AF_WEBUI_TOKEN"] = tok
        try:
            app = build_webui_asgi(self.cfg, gateway=self.gw)
            client = TestClient(app)
            with client:
                # 正确 token（query 参数）
                self.assertEqual(client.get("/api/health", params={"token": tok}).status_code, 200)
                # 错误 token（整串不同）
                self.assertEqual(client.get("/api/health", params={"token": "wrong"}).status_code, 403)
                # 同前缀不同尾缀（时序攻击关心的边界）
                self.assertEqual(client.get("/api/health", params={"token": tok + "x"}).status_code, 403)
                self.assertEqual(client.get("/api/health", params={"token": tok[:-1]}).status_code, 403)
                # 空 token
                self.assertEqual(client.get("/api/health", params={"token": ""}).status_code, 403)
        finally:
            os.environ.pop("AF_WEBUI_TOKEN", None)

    def test_webui_non_local_no_token_returns_403(self):
        """P0-3 (S-4)：未配置 token 时，非本机/回环 IP 访问 /api 一律 403；本机放行。"""
        import asyncio

        async def _raw_call(app, path, client_ip):
            scope = {
                "type": "http", "method": "GET", "path": path,
                "query_string": b"", "headers": [], "client": (client_ip, 1234),
            }
            captured = {}

            async def _receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def _send(message):
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    captured["body"] = message.get("body", b"")

            await app(scope, _receive, _send)
            return captured.get("status"), captured.get("body", b"")

        app = build_webui_asgi(self.cfg, gateway=self.gw)
        # 远程 IP（公网文档段 203.0.113.0/24，RFC 5737 示例地址）
        status_remote, _ = asyncio.run(_raw_call(app, "/api/health", "203.0.113.5"))
        self.assertEqual(status_remote, 403)
        # 本机/回环放行
        status_local, _ = asyncio.run(_raw_call(app, "/api/health", "127.0.0.1"))
        self.assertEqual(status_local, 200)
        status_local6, _ = asyncio.run(_raw_call(app, "/api/health", "::1"))
        self.assertEqual(status_local6, 200)
        # 反向代理场景：X-Forwarded-For 非回环也应拒
        async def _raw_call_xff(app, path, xff):
            scope = {
                "type": "http", "method": "GET", "path": path,
                "query_string": b"", "headers": [(b"x-forwarded-for", xff.encode())],
                "client": ("127.0.0.1", 1234),
            }
            captured = {}

            async def _receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def _send(message):
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    captured["body"] = message.get("body", b"")

            await app(scope, _receive, _send)
            return captured.get("status")

        self.assertEqual(asyncio.run(_raw_call_xff(app, "/api/health", "203.0.113.9")), 403)
        self.assertEqual(asyncio.run(_raw_call_xff(app, "/api/health", "127.0.0.1")), 200)

    def test_webui_non_local_spoofed_xff_loopback_still_403(self):
        """S-4 反 spoofing：公网直连 Peer 伪造 X-Forwarded-For: 127.0.0.1 仍 403。

        这是 S-4 最关键保证：远端攻击者直连（Peer 为公网 IP）不得用伪造 XFF 伪装回环绕过 403。
        """
        import asyncio

        async def _raw_call_xff(app, path, client_ip, xff):
            scope = {
                "type": "http", "method": "GET", "path": path,
                "query_string": b"", "headers": [(b"x-forwarded-for", xff.encode())],
                "client": (client_ip, 1234),
            }
            captured = {}

            async def _receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def _send(message):
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    captured["body"] = message.get("body", b"")

            await app(scope, _receive, _send)
            return captured.get("status")

        app = build_webui_asgi(self.cfg, gateway=self.gw)
        # 公网 Peer 伪造 XFF 回环 → 仍 403（Peer 非回环，绝不采信 XFF）
        self.assertEqual(
            asyncio.run(_raw_call_xff(app, "/api/health", "203.0.113.7", "127.0.0.1")), 403
        )
        self.assertEqual(
            asyncio.run(_raw_call_xff(app, "/api/health", "198.51.100.9", "::1")), 403
        )


@unittest.skipUnless(_HAVE_WEB_DEPS,
                      f"WebUI/MCP 测试需要 starlette+mcp（缺失：{_WEB_DEP_MSG}）；"
                      f"用系统 Python 3.13.2 或 pip install starlette mcp 后运行。")
class TestMCPAuth(TmpCfgMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.cfg)
        self.agent, self.code = self.store.create_agent("deepseek++", "staging")
        self.app = build_app(self.cfg, with_webui=True)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_anonymous_rejected(self):
        # 无 Authorization 头访问 /mcp → 401
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        self.assertEqual(r.status_code, 401)

    def test_authenticated_passes_middleware(self):
        # 带有效身份码 → 不再是 401（请求体非法会被 MCP 层处理，但不该是鉴权拒绝）
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                             headers={"Authorization": f"Bearer {self.code}"})
        self.assertNotEqual(r.status_code, 401)

    def test_wrong_code_rejected(self):
        r = self.client.post("/mcp", json={"x": 1},
                             headers={"Authorization": "Bearer af_invalid"})
        self.assertEqual(r.status_code, 401)

    def test_revoked_rejected(self):
        self.store.revoke_agent(self.agent.agent_id)
        r = self.client.post("/mcp", json={"x": 1},
                             headers={"Authorization": f"Bearer {self.code}"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
