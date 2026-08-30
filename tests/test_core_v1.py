#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow Core v1.0 守卫测试（发行版质量闸）。

覆盖四道防线：
  1. 脱敏守卫：core/ 发行物绝不携带内网 IP / 账密 / JWT / 机器路径（D3）；
  2. 配置守卫：无配置时报错可操作（agent 可自愈），不静默用默认值；
  3. 行为守卫：inventory 只读、write_flow 快照+回读校验（不符即熔断）、
     inject_and_read context 桥自愈闭环、compact 剔渲染坐标保 z；
  4. doctor 验收输出形状（一句话安装的验收标准）。

全部走本进程内 fake NR HTTP server（ThreadingHTTPServer），零外部依赖。
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE_SCRIPT = os.path.join(REPO, "core", "skill", "scripts", "nr_client.py")
CORE_SKILL = os.path.join(REPO, "core", "skill", "SKILL.md")
CORE_INSTALL = os.path.join(REPO, "core", "INSTALL.md")

TAB_AF = "tab_af_demo_0001"
TAB_USER = "tab_user_manual_01"
FN_ID = "fn0000000000002"
DBG_ID = "dbg000000000003"
INJ_ID = "inj000000000001"

# GET /flows 扁平数组：tab 条目 + z 指向 tab 的节点 + config/subflow 条目
FLOWS_FLAT = [
    {"id": TAB_AF, "type": "tab", "label": "af_demo"},
    {"id": TAB_USER, "type": "tab", "label": "客厅灯（手工）"},
    {"id": "srv000000000001", "type": "server"},          # config 节点：无 z
    {"id": "sf0000000000001", "type": "subflow", "name": "af_hist_x"},
    {"id": INJ_ID, "type": "inject", "z": TAB_AF, "wires": [[FN_ID]], "x": 10, "y": 20},
    {"id": FN_ID, "type": "function", "z": TAB_AF, "wires": [[DBG_ID]],
     "func": "global.set('af_dbg', msg.payload); return msg;", "x": 1, "y": 2},
    {"id": DBG_ID, "type": "debug", "z": TAB_AF, "wires": [[]], "x": 3, "y": 4},
    {"id": "n_user_00000001", "type": "inject", "z": TAB_USER, "wires": [[]], "x": 5, "y": 5},
    {"id": "n_user_00000002", "type": "change", "z": TAB_USER, "wires": [[]], "x": 6, "y": 6},
]


def _flow_full(tab_id, label):
    nodes = [n for n in FLOWS_FLAT if n.get("z") == tab_id]
    return {"id": tab_id, "label": label, "nodes": nodes}


class _State:
    def __init__(self):
        self.requests = []            # (method, path)
        self.inject_seen = False
        self.context_vals = {}        # (store,key) -> value（inject 后生效）
        self.put_bodies = {}
        self.posted_flows = None      # POST /flows 的整包 payload（deploy_all/restore）
        self.flows_state = {}         # flow_id -> 最近 PUT 的内容（有状态回显）
        self.drop_one_on_get = False


class _H(BaseHTTPRequestHandler):
    state: _State = None

    def log_message(self, *a):        # 静默访问日志
        pass

    def _send(self, code, obj=None, raw=""):
        body = raw if raw else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self, method):
        st = self.state
        st.requests.append((method, self.path))
        p = self.path
        if method == "POST" and p == "/auth/token":
            return self._send(200, {"access_token": "test-token"})
        if method == "GET" and p == "/flows":
            return self._send(200, FLOWS_FLAT)
        if method == "GET" and p.startswith("/flow/"):
            fid = p.split("/flow/", 1)[1]
            label = "af_demo" if fid == TAB_AF else "客厅灯（手工）"
            # 有状态：优先回显最近一次 PUT 的内容（贴近真实 NR）
            fl = st.flows_state.get(fid) or _flow_full(fid, label)
            if st.drop_one_on_get and fid == TAB_AF:
                fl = dict(fl)
                fl["nodes"] = fl["nodes"][:-1]
            return self._send(200, fl)
        if method == "PUT" and p.startswith("/flow/"):
            fid = p.split("/flow/", 1)[1]
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            st.put_bodies[fid] = body
            st.flows_state[fid] = body      # 有状态回显
            return self._send(200, {})
        if method == "POST" and p == "/flows":
            n = int(self.headers.get("Content-Length", 0))
            st.posted_flows = json.loads(self.rfile.read(n) or b"[]")
            return self._send(200, {})
        if method == "POST" and p.startswith("/inject/"):
            st.inject_seen = True
            return self._send(200, raw=b"")
        if method == "DELETE" and p.startswith("/context/"):
            parts = p.split("/")          # /context/<store>/<key>
            st.context_vals.pop((parts[2], parts[3]), None)
            return self._send(204, raw=b"")
        if method == "GET" and p.startswith("/context/"):
            parts = p.split("/")
            key = (parts[2], parts[3])
            if key == ("global", "af_dbg") and st.inject_seen:
                return self._send(200, {"msg": json.dumps(
                    {"ok": True, "state": "on"}), "format": "object"})
            return self._send(200, {"msg": "(undefined)", "format": "undefined"})
        if method == "GET" and p == "/settings":
            return self._send(200, {"version": "4.0.0"})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


# ── 启动 fake server → 设 env → import 被测模块（顺序不可换）──────────
_srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
_H.state = _State()          # 挂 handler 类属性，handler 实例才能读到
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_PORT = _srv.server_address[1]
_TMP = tempfile.mkdtemp(prefix="core_v1_test_")

os.environ.update({
    "NR_URL": f"http://127.0.0.1:{_PORT}",
    "NR_USER": "testuser",
    "NR_PASS": "testpass",
    "NR_CORE_CONFIG_DIR": _TMP,
    "NR_CLIENT_DISABLE_AUTOSYNC": "1",
    "NR_CLIENT_SNAPSHOT_DIR": os.path.join(_TMP, "snapshots"),
})

_spec = importlib.util.spec_from_file_location("core_nr_client", CORE_SCRIPT)
core = importlib.util.module_from_spec(_spec)
sys.modules["core_nr_client"] = core
_spec.loader.exec_module(core)


def _client():
    return core.NodeRedClient()


@pytest.fixture(autouse=True)
def _reset_fake_state():
    """每个用例前重置 fake server 状态，保证用例互不污染。"""
    st = _H.state
    st.requests.clear()
    st.inject_seen = False
    st.put_bodies = {}
    st.posted_flows = None
    st.flows_state = {}
    st.drop_one_on_get = False
    yield


# ── 1. 脱敏守卫（D3）─────────────────────────────────────────────

# 私网段只写前两段（完整内网地址不得入库：本仓库将公开）
_SECRET_PATTERNS = ("192.168.", "100.112.", "longyin", "eyJhbGci",
                    "D:/Documents", "qclaw")


def test_core_release_scrubbed():
    """发行物不得携带内网 IP / 账密 / JWT / 机器路径。"""
    for path in (CORE_SCRIPT, CORE_SKILL, CORE_INSTALL):
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        for pat in _SECRET_PATTERNS:
            assert pat not in txt, f"{path} 泄漏敏感串: {pat}"


def test_no_hardcoded_username():
    """代码与 skill 文档不得内置用户名。"""
    for path in (CORE_SCRIPT, CORE_SKILL):
        with open(path, encoding="utf-8") as f:
            assert "lidicn" not in f.read(), f"{path} 内置用户名"


# ── 2. 配置守卫 ─────────────────────────────────────────────────

def test_missing_config_raises_actionable():
    """无 url 时报错必须给可操作指引（agent 能自愈），而非静默用默认值。"""
    core.NR_URL = ""
    try:
        with pytest.raises(RuntimeError, match="config.json"):
            core.NodeRedClient()
    finally:
        core.NR_URL = f"http://127.0.0.1:{_PORT}"


# ── 3. 行为守卫 ─────────────────────────────────────────────────

def test_inventory_read_only_and_ownership():
    """inventory 只发 GET；af_* 可写、user 流只读；subflow/config 不误计入 tab 节点数。"""
    st = _H.state
    st.requests.clear()
    rows = _client().inventory()
    writes = [(m, p) for m, p in st.requests
              if m in ("PUT", "DELETE", "POST") and "/auth/token" not in p]
    assert not writes, f"inventory 必须只读，发现写请求: {writes}"
    af = next(r for r in rows if r["id"] == TAB_AF)
    user = next(r for r in rows if r["id"] == TAB_USER)
    assert af["owner"] == "af" and af["writable"] is True and af["nodes"] == 3
    assert user["owner"] == "user" and user["writable"] is False and user["nodes"] == 2
    assert rows[0]["owner"] == "af"        # af 优先展示


def test_write_flow_snapshot_and_readback_ok():
    """write_flow：快照 → PUT → 回读校验通过；请求序列完整。"""
    st = _H.state
    st.drop_one_on_get = False
    st.requests.clear()
    data = _flow_full(TAB_AF, "af_demo")
    r = _client().write_flow(TAB_AF, data, label="t_ok")
    assert r["verified"] is True and r["nodes"] == 3 and r["snapshot"]
    methods = [m for m, _ in st.requests]
    assert methods.count("PUT") == 1 and "GET /flows" in [
        f"{m} {p}" for m, p in st.requests]


def test_write_flow_readback_mismatch_rolls_back():
    """回读节点数不符 → NRRollbackError，且错误信息附快照路径。"""
    st = _H.state
    st.drop_one_on_get = True
    try:
        with pytest.raises(core.NRRollbackError, match="快照"):
            _client().write_flow(TAB_AF, _flow_full(TAB_AF, "af_demo"))
    finally:
        st.drop_one_on_get = False


def test_inject_and_read_captures():
    """自愈闭环：触发 inject 后轮询读 context 捕获值。"""
    st = _H.state
    st.inject_seen = False
    val = _client().inject_and_read(INJ_ID, key="af_dbg",
                                    timeout=3.0, poll=0.1)
    assert val == {"ok": True, "state": "on"}


def test_inject_and_read_timeout_returns_none():
    """未捕获 → 超时返回 None（不抛异常，供 agent 走自愈分支）。"""
    st = _H.state
    st.inject_seen = False
    val = _client().inject_and_read(INJ_ID, key="af_dbg2",
                                    timeout=0.6, poll=0.2)
    assert val is None


def test_compact_strips_render_fields_keeps_z():
    """compact 剔 x/y/w/h 省 token；z 是 tab 归属关键字段，绝不可剔。"""
    out = core.NodeRedClient.compact(FLOWS_FLAT[4])
    assert "x" not in out and "y" not in out
    assert out["z"] == TAB_AF and out["wires"] == [[FN_ID]]


def test_compact_recursive_nested():
    """compact 递归作用于 dict/list 嵌套。"""
    out = core.NodeRedClient.compact({"a": [{"x": 1, "keep": 2}], "y": 3, "z": 4})
    assert out == {"a": [{"keep": 2}], "z": 4}


# ── 4. doctor 验收输出形状 ──────────────────────────────────────

def test_doctor_report_shape():
    """doctor：登录+连通为真、af/user 计数正确、prod 判定与 HA 提示存在。"""
    # 显式屏蔽本机可能存在的 HASS_* 环境变量，测「未配置」分支
    saved = (core.NodeRedClient._HA_SERVER, core.NodeRedClient._HA_TOKEN)
    core.NodeRedClient._HA_SERVER = ""
    core.NodeRedClient._HA_TOKEN = ""
    try:
        rep = _client().doctor()
        assert rep["login"] is True and rep["nr_reachable"] is True
        assert rep["flows"] == 2 and rep["af_flows"] == 1 and rep["user_flows"] == 1
        assert rep["is_prod"] is False
        assert rep["ha_assert_available"] is False
        assert any("HASS_SERVER" in i for i in rep["issues"])
    finally:
        (core.NodeRedClient._HA_SERVER, core.NodeRedClient._HA_TOKEN) = saved


# ── 5. T011 回单修复守卫（P0 还原 / A档红线 / P2 lint 误报）──────────

def test_deploy_all_subset_guard_message_is_precise():
    """T012 建议1：护栏(0) 报错须区分 tab/subflow 与节点数，别笼统都说成 tab/subflow。

    GET /flows 是扁平数组，缺失集合里既有 tab 也有其下节点，措辞不准会误导排障。
    """
    flows = [f for f in FLOWS_FLAT
             if f.get("id") != TAB_USER and f.get("z") != TAB_USER]
    with pytest.raises(core.NRGuardError) as ei:
        _client().deploy_all(flows, force=True)   # allow_partial 默认 False
    msg = str(ei.value)
    assert "tab/subflow 1 个" in msg, msg
    assert "节点 2 个" in msg, msg


def test_restore_snapshot_uses_atomic_deploy_all():
    """T011 [P0]：还原必须走 POST /flows 整包，绝不可逐条 PUT（会把实例写崩）。"""
    st = _H.state
    st.requests.clear()
    st.posted_flows = None
    snap = os.path.join(_TMP, "snap_restore.json")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"_meta": {"label": "t"}, "flows": FLOWS_FLAT}, f)
    r = _client().restore_snapshot(snap)
    assert r["restored_items"] == len(FLOWS_FLAT)
    assert st.posted_flows is not None, "必须整包 POST /flows"
    # 关键断言：不得出现针对节点 id 的 PUT（旧实现的写崩根因）
    put_ids = [p.split("/flow/")[1] for m, p in st.requests
               if m == "PUT" and p.startswith("/flow/")]
    assert put_ids == [], f"还原不得逐条 PUT flow，实得: {put_ids}"


def test_restore_snapshot_rejects_empty_payload():
    """空快照不得发起整实例部署（防清场）。"""
    snap = os.path.join(_TMP, "snap_empty.json")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"flows": []}, f)
    with pytest.raises(ValueError, match="无 flows"):
        _client().restore_snapshot(snap)


def test_write_flow_blocks_user_tab_by_default():
    """T011 §6 A档：非 af_* tab 默认拒绝写（红线从纪律升级为硬拦截）。"""
    st = _H.state
    st.drop_one_on_get = False
    with pytest.raises(core.NRGuardError, match="所有权红线"):
        _client().write_flow(TAB_USER, _flow_full(TAB_USER, "客厅灯（手工）"))


def test_write_flow_allows_user_tab_with_explicit_optin():
    """显式 opt-in 仍可写用户流（照常快照留底），保证不锁死合法用途。"""
    st = _H.state
    st.drop_one_on_get = False
    r = _client().write_flow(TAB_USER, _flow_full(TAB_USER, "客厅灯（手工）"),
                             allow_user_flow=True, label="t_optin")
    assert r["verified"] is True


def test_create_tab_blocks_non_af_label():
    """建 tab 同样受所有权红线约束。"""
    with pytest.raises(core.NRGuardError, match="af_"):
        _client().create_tab("我的场景")
    assert _client().create_tab("af_我的场景")["label"] == "af_我的场景"


def test_lint_no_false_positive_on_terminal_debug():
    """T011 [P2]：末端 debug 节点（wires=[]，无 outputs 字段）不得被 lint 误报。"""
    flow = {"id": TAB_AF, "label": "af_demo", "nodes": [
        {"id": INJ_ID, "type": "inject", "z": TAB_AF, "wires": [[DBG_ID]]},
        {"id": DBG_ID, "type": "debug", "z": TAB_AF, "wires": []},
    ]}
    r = _client().write_flow(TAB_AF, flow, label="t_debug")
    assert r["verified"] is True and r["nodes"] == 2
