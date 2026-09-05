"""Regression: 自愈闭环（apply_flow / apply_rollback）在 PROD 环境下必须能自动写回。

复现并锁定 bug：prod 环境下自愈闭环写盘被 _guard_prod 熔断（"默认禁止写 prod"），
根因是 modify_flow 漏接 allow_prod 形参、内部 create_or_update_flow 不传 allow_prod，
导致 apply_flow(自愈写) 与 apply_rollback(自愈回滚) 在 AUTOFLLOW_ENV=prod 下死锁
（selfheal_auto_write 闸触发但写不进 → 自愈预算耗尽 → 永不自动修复）。

测试策略：
  - 设 AUTOFLLOW_ENV=prod（仅本测试进程，不污染全局），复刻真实 prod 护栏；
  - 用「忠实护栏替身」替换 nr.create_or_update_flow：
        若处于 prod 且未 opt-in allow_prod=True → 抛 NRGuardError（与真实护栏一致），
        modify_flow / apply_rollback 会把它吞成 ok=False(stage=deploy)，故敏感性断言看返回值；
  - 驱动真实 apply_flow / apply_rollback（**不替 modify_flow**），断言底层
    create_or_update_flow 收到 allow_prod=True，护栏不被触发；
  - 对照（敏感性）：modify_flow 默认 allow_prod=False → 护栏必须触发（返回值可见）；
  - 额外锁定两个 MCP 入口（autoflow_apply / autoflow_apply_rollback）确实向底层传 allow_prod=True，
    因为用户实际触发的是自愈闭环的 MCP 入口，而非裸调 gateway 方法。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from autoflow_gateway import gateway as gwmod
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.lib import nr_client as nr_mod
from autoflow_gateway.lib.nr_client import NRGuardError
from autoflow_gateway.identity import Agent

# mcp_server 在模块顶层 `from mcp.server.fastmcp import FastMCP`，但测试解释器未装 mcp 包。
# 注入最小桩，使真实 autoflow_apply / autoflow_apply_rollback 函数可被导入与调用
# （装饰器 @mcp.tool() 在桩里为恒等；不影响函数本体逻辑）。
import sys as _sys
import types as _types


def _install_mcp_stub():
    if "mcp.server.fastmcp" in _sys.modules:
        return

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def _deco(func):
                return func
            return _deco

        def admin_tool(self, *a, **k):
            def _deco(func):
                return func
            return _deco

    _mcp = _types.ModuleType("mcp")
    _server = _types.ModuleType("mcp.server")
    _fastmcp = _types.ModuleType("mcp.server.fastmcp")
    _fastmcp.FastMCP = _FastMCP
    _httpmgr = _types.ModuleType("mcp.server.streamable_http_manager")
    _httpmgr.StreamableHTTPSessionManager = object
    _mcp.server = _server
    _server.fastmcp = _fastmcp
    _server.streamable_http_manager = _httpmgr
    _sys.modules["mcp"] = _mcp
    _sys.modules["mcp.server"] = _server
    _sys.modules["mcp.server.fastmcp"] = _fastmcp
    _sys.modules["mcp.server.streamable_http_manager"] = _httpmgr


_install_mcp_stub()

# webui 模块顶层 import starlette（测试解释器未装）。mcp_server 只引用 build_webui_asgi
# （且仅在 run 入口用到），注入最小桩即可完成导入，不影响 autoflow_apply 逻辑。
_webui_stub = _types.ModuleType("autoflow_gateway.webui")


def _build_webui_asgi(*a, **k):
    return None


_webui_stub.build_webui_asgi = _build_webui_asgi
_sys.modules["autoflow_gateway.webui"] = _webui_stub

import autoflow_gateway.mcp_server as mcp_server  # noqa: E402  (需先装 mcp/webui 桩)


def _base_flow():
    return {
        "id": "f_apply", "label": "书房迎宾", "disabled": False,
        "nodes": [
            {"id": "n1", "type": "inject", "z": "f_apply", "wires": [["n2"]]},
            {"id": "n2", "type": "api-call-service", "z": "f_apply",
             "name": "开灯", "wires": [[]]},
        ],
    }


@pytest.fixture
def prod_env(monkeypatch):
    """激活真实 prod 护栏（仅本测试），并显式关闭全局放行开关作对照基线。"""
    monkeypatch.setenv("AUTOFLLOW_ENV", "prod")
    monkeypatch.setattr(nr_mod, "NR_ALLOW_PROD", False)
    yield


@pytest.fixture
def gw(tmp_path, monkeypatch):
    g = Gateway()
    monkeypatch.setattr(gwmod, "_apply_trace_dir", lambda: str(tmp_path / "apply_traces"))
    return g


def _stub_selfheal_path(gw, tmp_path, monkeypatch):
    """替掉 apply 外部依赖，但保留真实 modify_flow → create_or_update_flow 链路。

    nr.create_or_update_flow 用『忠实护栏替身』：复刻 _guard_prod 行为，
    以便本测试能真正验证 allow_prod 是否被正确透传。
    """
    calls = {"deploy": []}
    monkeypatch.setattr(gw.nr, "get_flow", lambda fid: _base_flow())

    def _inject_ha_server(target):
        return target, []
    monkeypatch.setattr(gw, "_inject_ha_server", _inject_ha_server)
    monkeypatch.setattr(gw, "_gate_node_types", lambda f: None)
    monkeypatch.setattr(gw, "_selfheal_budget_check",
                        lambda *a, **k: (True, {"retry_budget": 3,
                                                "failed_attempts_in_window": 0}))
    monkeypatch.setattr(gw, "_selfheal_budget_record", lambda *a, **k: None)

    def _create_or_update_flow(flow_id, flow, force=False, allow_prod=False):
        # 复刻真实护栏：prod 且未 opt-in → 熔断
        env = os.getenv("AUTOFLLOW_ENV", "staging").lower()
        is_prod = (env == "prod") or (os.getenv("NR_PROD") == "1")
        if is_prod and not (allow_prod or nr_mod.NR_ALLOW_PROD):
            raise NRGuardError("PROD GUARD TRIPPED")
        calls["deploy"].append({"flow_id": flow_id, "allow_prod": allow_prod})
        return {"id": flow_id}
    monkeypatch.setattr(gw.nr, "create_or_update_flow", _create_or_update_flow)
    return calls


def _make_agent(mode="expert"):
    return Agent(agent_id="ag-selfheal", name="selfheal", tier="prod",
                 status="active", identity_code_hash="x", created_at="now",
                 mode=mode)


# ───────────── modify_flow 单元：opt-in 与护栏触发 ─────────────

def test_modify_flow_allows_optin(prod_env, gw, tmp_path, monkeypatch):
    """modify_flow(allow_prod=True) 在 prod 下必须走通，底层收到 allow_prod=True。"""
    calls = _stub_selfheal_path(gw, tmp_path, monkeypatch)
    r = gw.modify_flow(
        "f_apply",
        node_patches=[{"match": {"id": "n2"}, "set": {"name": "x"}}],
        agent_id="a", allow_prod=True)
    assert r.get("ok") is True, f"opt-in 写盘失败：{r}"
    assert calls["deploy"][-1]["allow_prod"] is True


def test_modify_flow_guard_fires_without_optin(prod_env, gw, tmp_path, monkeypatch):
    """敏感性对照：modify_flow 默认 allow_prod=False 在 prod 下必须被护栏熔断。

    modify_flow 会把护栏异常吞成 ok=False(stage=deploy)，故断言返回值而非异常传播。
    证明本测试 env 真实激活了 prod 护栏，从而确认上面的『走通』是 fix 之功而非 env 失效。
    """
    calls = _stub_selfheal_path(gw, tmp_path, monkeypatch)
    r = gw.modify_flow(
        "f_apply",
        node_patches=[{"match": {"id": "n2"}, "set": {"name": "x"}}],
        agent_id="a")  # 默认 allow_prod=False
    assert r.get("ok") is False
    assert r.get("stage") == "deploy"
    assert "PROD GUARD" in (r.get("error") or "")
    assert not calls["deploy"], "护栏未生效：未 opt-in 却调用了 create_or_update_flow"


# ───────────── 自愈写回（apply_flow A/C 段）─────────────

def test_apply_flow_selfheal_threads_allow_prod(prod_env, gw, tmp_path, monkeypatch):
    """自愈写回在 prod 下必须走通，且底层 create_or_update_flow 收到 allow_prod=True。"""
    calls = _stub_selfheal_path(gw, tmp_path, monkeypatch)
    r = gw.apply_flow(
        "f_apply",
        {"node_patches": [{"match": {"id": "n2"}, "set": {"name": "开主卧灯"}}],
         "reason": "观测到灯没亮"},
        mode="A", agent_id="a", allow_prod=True)
    assert r.get("ok") is True, f"自愈写回失败：{r}"
    assert r.get("gate") == "selfheal_auto_write"
    assert calls["deploy"][-1]["allow_prod"] is True, \
        "prod 环境下自愈写回必须把 allow_prod=True 透传到 create_or_update_flow"


# ───────────── 自愈回滚（apply_rollback）─────────────

def test_apply_rollback_selfheal_threads_allow_prod(prod_env, gw, tmp_path, monkeypatch):
    """自愈回滚在 prod 下必须走通，且底层 create_or_update_flow 收到 allow_prod=True。"""
    calls = _stub_selfheal_path(gw, tmp_path, monkeypatch)
    # 造一个 apply 轨迹 + 快照文件（apply_rollback 从快照还原）
    snap_path = tmp_path / "snap_rollback.json"
    snap_path.write_text(json.dumps({"flow": _base_flow()}, ensure_ascii=False),
                         encoding="utf-8")
    monkeypatch.setattr(gwmod, "_read_apply_trace",
                        lambda tid: {"flow_id": "f_apply",
                                     "snapshot_path": str(snap_path)})
    r = gw.apply_rollback("trace_x", agent_id="a", allow_prod=True)
    assert r.get("ok") is True, f"自愈回滚失败：{r}"
    assert r.get("restored") is True
    assert calls["deploy"][-1]["allow_prod"] is True, \
        "prod 环境下自愈回滚必须把 allow_prod=True 透传到 create_or_update_flow"


# ───────────── MCP 入口（用户实际触发点）─────────────

def test_autoflow_apply_passes_allow_prod(prod_env, gw, tmp_path, monkeypatch):
    """autoflow_apply（自愈循环 MCP 入口）必须向 apply_flow 传 allow_prod=True。"""
    captured = {"allow_prod": []}

    def _apply_flow_spy(flow_id="", correction=None, mode="A", agent_id="x",
                        auto_approve=False, allow_prod=False, trace_id=None):
        captured["allow_prod"].append(allow_prod)
        return {"ok": True, "applied": True, "mode": mode}

    monkeypatch.setattr(gw, "apply_flow", _apply_flow_spy)
    monkeypatch.setattr(mcp_server, "_gw", lambda: gw)
    var = mcp_server.get_current_agent_var()
    tok = var.set(_make_agent(mode="expert"))
    try:
        out = json.loads(mcp_server.autoflow_apply(
            mode="A",
            correction_json=json.dumps(
                {"node_patches": [{"match": {"id": "n2"}, "set": {"name": "x"}}],
                 "reason": "r"}),
            flow_id="f_apply"))
    finally:
        var.reset(tok)
    assert out.get("ok") is True
    assert captured["allow_prod"] == [True], \
        "autoflow_apply 必须向 apply_flow 传 allow_prod=True"


def test_autoflow_apply_rollback_passes_allow_prod(prod_env, gw, tmp_path, monkeypatch):
    """autoflow_apply_rollback（自愈回滚 MCP 入口）必须向 apply_rollback 传 allow_prod=True。"""
    captured = {"allow_prod": []}

    def _rollback_spy(trace_id, agent_id="x", auto_approve=False, allow_prod=False):
        captured["allow_prod"].append(allow_prod)
        return {"ok": True, "restored": True}

    monkeypatch.setattr(gw, "apply_rollback", _rollback_spy)
    monkeypatch.setattr(mcp_server, "_gw", lambda: gw)
    var = mcp_server.get_current_agent_var()
    tok = var.set(_make_agent(mode="expert"))
    try:
        out = json.loads(mcp_server.autoflow_apply_rollback("trace_x"))
    finally:
        var.reset(tok)
    assert out.get("ok") is True
    assert captured["allow_prod"] == [True], \
        "autoflow_apply_rollback 必须向 apply_rollback 传 allow_prod=True"
