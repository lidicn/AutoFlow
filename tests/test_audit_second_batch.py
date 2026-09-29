"""审计报告「第二批」守卫（P1-3 / P1-5 / P1-7 / P1-8）。

第一批（P0-1/P0-2/P1-4/list_tasks 分页）见 test_audit_first_batch.py。
本文件锁定第二批四项：
  P1-3  _gw() 进程级单例 + reset_gateway()；TaskStore._init_db / seed_managed_subflows 幂等
  P1-5  TokenStatsStore 改 append-only JSONL + 类级锁（并发不丢计数、I/O 不放大）
  P1-7  /mcp-white 复用 /mcp 的同一个 StreamableHTTPSessionManager
  P1-8  ACP cancel_event 用 threading.Event（worker 线程跨线程 is_set 安全）

★ 守卫纪律（项目 MEMORY §5）：「全绿」≠「守卫有效」。接入后应做变异测试验证——
  临时把修复回退，确认本文件对应用例变红且报错直指问题，再按哈希还原。
"""
import asyncio
import json
import os
import threading

# conftest 已把仓库 src 顶到 sys.path 首位；这里保持与既有测试一致的导入方式。
from autoflow_gateway import mcp_server as ms
from autoflow_gateway import task_store as ts
from autoflow_gateway.token_stats import TokenStatsStore


def _today_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _jsonl_lines(store, day):
    p = os.path.join(store.data_dir, day + ".jsonl")
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


class _CfgStub:
    """TaskStore 只用到 data_dir / claim_ttl_seconds，给最小桩即可。"""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.claim_ttl_seconds = 1800


# ───────────────────────── P1-3 ─────────────────────────
def test_gateway_singleton_reused_and_reset():
    """_gw() 应复用进程级单例；reset_gateway() 后重建新实例。"""
    ms.reset_gateway()

    class _FakeGW:
        def __init__(self):
            self.tag = object()

    _orig = ms.Gateway
    ms.Gateway = lambda: _FakeGW()
    try:
        a = ms._gw()
        b = ms._gw()
        assert a is b, "_gw() 连续两次调用应返回同一个进程级单例"
        ms.reset_gateway()
        c = ms._gw()
        assert c is not a, "reset_gateway() 之后应重建全新 Gateway 实例"
    finally:
        ms.Gateway = _orig
        ms.reset_gateway()


def test_task_store_init_db_dedupe(tmp_path):
    """同一 db_path 反复构造 TaskStore，二次起不应再跑 DDL（连 sqlite）。"""
    d = tmp_path / "d1"
    s1 = ts.TaskStore(_CfgStub(d))
    assert os.path.abspath(s1.db_path) in ts._INITED_DB_PATHS, "首次构造后应登记已初始化"

    connects = []
    _real_connect = ts.sqlite3.connect
    ts.sqlite3.connect = lambda *a, **k: (connects.append(a), _real_connect(*a, **k))[1]
    try:
        s2 = ts.TaskStore(_CfgStub(d))
    finally:
        ts.sqlite3.connect = _real_connect

    assert s2 is not None
    assert len(connects) == 0, (
        f"同一 db_path 二次构造不应再跑 DDL，实得 {len(connects)} 次 sqlite 连接")


# ───────────────────────── P1-5 ─────────────────────────
def test_token_stats_writes_jsonl(tmp_path):
    """record() 应 O(1) 追加到当日 JSONL，get_stats 从 JSONL 聚合出正确结果。"""
    store = TokenStatsStore(str(tmp_path / "ts"))
    store.record("autoflow_x", "agent1", 100, 200)
    store.record("autoflow_y", "agent1", 10, 20, mode="raw")

    day = _today_str()
    lines = _jsonl_lines(store, day)
    assert len(lines) == 2, f"应追加 2 行 JSONL，实得 {len(lines)}"

    st = store.get_stats(days=1)
    assert st["total_calls"] == 2, st
    assert st["total_input_chars"] == 110, st
    assert st["total_output_chars"] == 220, st
    assert st["by_endpoint"]["autoflow_x"]["calls"] == 1, st["by_endpoint"]
    assert st["by_endpoint"]["autoflow_y"]["calls"] == 1, st["by_endpoint"]
    # 向后兼容字段仍在（WebUI 预算条依赖）
    assert st["budget"] == 200_000
    assert "budget_used_pct" in st and "today_rounds" in st


def test_token_stats_migrates_legacy_json(tmp_path):
    """旧 token_stats.json 应被一次性迁移为历史日 JSONL 行并归档，历史数据不丢。"""
    d = str(tmp_path / "ts2")
    os.makedirs(d, exist_ok=True)
    legacy_day = "2000-01-01"
    legacy = {
        "daily": {
            legacy_day: {
                "calls": 7, "input_chars": 400, "output_chars": 400,
                "by_endpoint": {"autoflow_legacy": {
                    "calls": 7, "input_chars": 400, "output_chars": 400}},
                "by_agent": {}, "by_mode": {},
            }
        },
        "total": {"calls": 7, "input_chars": 400, "output_chars": 400},
    }
    with open(os.path.join(d, "token_stats.json"), "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    store = TokenStatsStore(d)
    st = store.get_stats(days=10000)  # 窗口足够大以覆盖历史日

    assert not os.path.isfile(os.path.join(d, "token_stats.json")), "迁移后旧文件应归档"
    assert os.path.isfile(os.path.join(d, legacy_day + ".jsonl")), "应生成历史日 JSONL"
    assert st["by_endpoint"].get("autoflow_legacy", {}).get("calls") == 7, (
        f"历史端点统计应保留: {st['by_endpoint']}")
    assert st["total_calls"] >= 7, st["total_calls"]


def test_token_stats_record_concurrent_safe(tmp_path):
    """并发 record() 不应丢计数（类级锁 + append 追加）。"""
    store = TokenStatsStore(str(tmp_path / "ts3"))
    n_threads, per = 5, 20

    def _work(i):
        for _ in range(per):
            store.record("autoflow_t", f"agent{i}", 1, 1)

    threads = [threading.Thread(target=_work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    day = _today_str()
    lines = _jsonl_lines(store, day)
    expected = n_threads * per
    assert len(lines) == expected, f"并发 record 应全部落盘，期望 {expected} 实得 {len(lines)}"
    st = store.get_stats(days=1)
    assert st["total_calls"] == expected, st


# ───────────────────────── P1-7 ─────────────────────────
def test_mcp_white_reuses_user_session_manager(tmp_path):
    """/mcp-white 应复用 /mcp 的同一个 SessionManager（不再各自持有会话表）。"""
    from autoflow_gateway.config import GatewayConfig

    created = []
    _OrigSM = ms.StreamableHTTPSessionManager

    class _RecSM(_OrigSM):
        def __init__(self, *a, **k):
            created.append(self)
            super().__init__(*a, **k)

    # build_app 返回的是 auth.wrap() 包装后的 ASGI 函数，拿不到 .routes；
    # 这里记录 Starlette 实例（build_app 内部 from starlette.applications import Starlette
    # 在调用时取属性，故补丁生效），用于比对各 path 绑定的 SessionManager。
    import starlette.applications as sa
    _OrigStarlette = sa.Starlette
    captured = {}

    class _RecStarlette(_OrigStarlette):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.setdefault("app", self)

    sa.Starlette = _RecStarlette
    ms.StreamableHTTPSessionManager = _RecSM
    try:
        cfg = GatewayConfig()
        cfg.data_dir = str(tmp_path / "gw")
        cfg.make_dirs()
        ms.build_app(cfg, with_webui=False)
        app = captured.get("app")

        assert len(created) == 2, (
            f"应只创建 2 个 SessionManager（user + admin），实际 {len(created)}")
        assert app is not None, "应构造出 Starlette 实例"

        sm_by_path = {}
        for r in app.routes:
            p = getattr(r, "path", None)
            if p in (cfg.mcp_path, cfg.mcp_white_path):
                sm_by_path[p] = r.endpoint.sm

        assert cfg.mcp_path in sm_by_path, "未找到 /mcp 路由"
        assert cfg.mcp_white_path in sm_by_path, "未找到 /mcp-white 路由"
        assert sm_by_path[cfg.mcp_path] is sm_by_path[cfg.mcp_white_path], (
            "/mcp-white 应复用 /mcp 的同一个 SessionManager")
    finally:
        ms.StreamableHTTPSessionManager = _OrigSM
        sa.Starlette = _OrigStarlette


# ───────────────────────── P1-8 ─────────────────────────
def test_acp_cancel_event_uses_threading_event(monkeypatch):
    """ACP cancel_event 必须是 threading.Event（worker 线程跨线程 is_set 安全）。

    asyncio.Event 不是线程安全的：_acp_agent_run 经 asyncio.to_thread 跑在 worker 线程里
    调用 is_cancelled() → Event.is_set()。用 asyncio.Event 属跨线程误用。
    """
    _RealTE = threading.Event

    class _RecTE(_RealTE):
        pass

    monkeypatch.setattr(threading, "Event", _RecTE, raising=True)

    app = ms._ACPApp.__new__(ms._ACPApp)
    frames = []

    async def _send(m):
        frames.append(m)

    sid = "test-sid-p1-8"
    req = {"id": 1, "method": "prompt",
           "params": {"sessionId": sid,
                      "messages": [{"role": "user", "content": "你好"}]}}
    try:
        asyncio.run(app._handle_prompt(req, _send))
        sess = ms._ACP_SESSIONS.get(sid)
        assert sess is not None, "prompt 应创建会话"
        assert isinstance(sess.cancel_event, threading.Event), (
            f"cancel_event 应为 threading.Event，实得 {type(sess.cancel_event)}")
        assert not isinstance(sess.cancel_event, asyncio.Event), (
            "cancel_event 不得是 asyncio.Event（非线程安全）")
    finally:
        ms._ACP_SESSIONS.pop(sid, None)
