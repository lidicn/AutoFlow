#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebUI 自愈重试次数（selfheal_budget）控制：PUT /api/settings 落盘 + GET /api/config 回带 + 网关即时消费。

覆盖 DEV-selfheal-naming-webui-control：
  - PUT {selfheal_budget:5} → 落盘 feature_flags.json 且 GET /api/config 回带 5；
  - PUT {selfheal_budget:99} / "abc" / -1 → 400 被拒、不落盘；
  - WebUI 改后网关读到的预算即新值（无需重启）：budget=2 时连续 2 次失败第 3 次被拒。
"""
import os
import sys
import json
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest
from autoflow_gateway.config import GatewayConfig, load_feature_flags
from autoflow_gateway.gateway import Gateway

try:
    from starlette.testclient import TestClient
    from autoflow_gateway.webui import build_webui_asgi
    _OK = True
    _ERR = ""
except Exception as _e:  # pragma: no cover
    _OK = False
    _ERR = str(_e)
    TestClient = build_webui_asgi = None

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要 starlette+mcp：{_ERR}")


@pytest.fixture
def env():
    # token_only 模式：TestClient（loopback）免登录跑写端点，与现网 password_only 行为解耦
    _prev = os.environ.get("AF_WEBUI_TOKEN_MODE")
    os.environ["AF_WEBUI_TOKEN_MODE"] = "token_only"
    tmp = tempfile.mkdtemp(prefix="af_sh_")
    cfg = GatewayConfig(data_dir=tmp, env="staging")
    gw = Gateway(cfg)
    app = build_webui_asgi(cfg, gateway=gw)
    client = TestClient(app)
    client.__enter__()
    try:
        yield client, gw, cfg
    finally:
        client.__exit__(None, None, None)
        shutil.rmtree(tmp, ignore_errors=True)
        if _prev is None:
            os.environ.pop("AF_WEBUI_TOKEN_MODE", None)
        else:
            os.environ["AF_WEBUI_TOKEN_MODE"] = _prev


def test_set_selfheal_budget_persists_and_reads_back(env):
    client, gw, cfg = env
    r = client.put("/api/settings", json={"selfheal_budget": 5})
    assert r.status_code == 200
    assert r.json()["selfheal_budget"] == 5
    # GET /api/config 回带新值
    c = client.get("/api/config")
    assert c.status_code == 200
    assert c.json()["selfheal_budget"] == 5
    # 落盘 feature_flags.json
    assert load_feature_flags(cfg).get("selfheal_budget") == 5
    # 网关即时读到新值（无需重启）
    assert load_feature_flags(gw.cfg).get("selfheal_budget") == 5


def test_set_selfheal_budget_out_of_range_rejected(env):
    client, gw, cfg = env
    assert client.put("/api/settings", json={"selfheal_budget": 99}).status_code == 400
    assert client.put("/api/settings", json={"selfheal_budget": "abc"}).status_code == 400
    assert client.put("/api/settings", json={"selfheal_budget": -1}).status_code == 400
    # 越界不被落盘
    assert "selfheal_budget" not in load_feature_flags(cfg)


def test_gateway_consumes_new_budget_without_restart(env, monkeypatch):
    """WebUI 设 budget=2 后，网关立即以 2 为上限（连续 2 次失败第 3 次被拒），无需重启。"""
    client, gw, cfg = env
    assert client.put("/api/settings", json={"selfheal_budget": 2}).status_code == 200

    import autoflow_gateway.gateway as gwmod
    base = {"id": "f_sh", "label": "x",
            "nodes": [{"id": "n1", "type": "inject", "z": "f_sh", "wires": [[]]}]}
    monkeypatch.setattr(gw.nr, "get_flow", lambda fid: base)
    calls = []
    monkeypatch.setattr(
        gw, "modify_flow",
        lambda *a, **k: (calls.append(1) or {"ok": False, "stage": "node_gate", "error": "x"}))
    monkeypatch.setattr(gwmod, "snapshot_flow",
                        lambda *a, **k: os.path.join(tempfile.mkdtemp(), "snap.json"))

    for _ in range(2):
        rr = gw.apply_flow("f_sh", {"node_patches": [{"match": {"id": "n1"}, "set": {}}]},
                           mode="C", agent_id="wb1")
        assert rr["ok"] is False
    r3 = gw.apply_flow("f_sh", {"node_patches": [{"match": {"id": "n1"}, "set": {}}]},
                       mode="C", agent_id="wb1")
    assert r3["stage"] == "selfheal_budget_exhausted"
    assert len(calls) == 2          # 第 3 次被拒，未写回
