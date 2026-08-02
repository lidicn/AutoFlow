"""W2 提案状态机回归：分页 + 归档 + 噪声分析（隔离单测，不依赖网关运行时）。

覆盖 proposals.py W2-1/2/3 的 list(分页)+count、archive/unarchive、
analyze_raw_noise、auto_archive，并关键证明 SQL 测试过滤 _test_agent_sql
与 Python 侧 _is_test_agent 完全等价（保证分页/计数与返回行一致，不会因
Python 后过滤导致分页错位）。

隔离加载：优先复用会话中已导入的真实 autoflow_gateway.proposals；否则桩掉
相对 import .config，以唯一模块名加载 proposals.py，避免拉起整个网关、
且不污染真实包。
"""
import os
import sys
import types
import tempfile
import sqlite3
import hashlib
import importlib.util
from datetime import datetime, timezone, timedelta


def _load_proposals():
    if "autoflow_gateway.proposals" in sys.modules:
        return sys.modules["autoflow_gateway.proposals"]
    if "autoflow_gateway" not in sys.modules:
        pkg = types.ModuleType("autoflow_gateway"); pkg.__path__ = []; sys.modules["autoflow_gateway"] = pkg
    cfgmod = types.ModuleType("autoflow_gateway.config")
    cfgmod.get_config = lambda: (_ for _ in ()).throw(RuntimeError("unused"))
    sys.modules["autoflow_gateway.config"] = cfgmod
    spec = importlib.util.spec_from_file_location(
        "autoflow_gateway._proposals_w2_iso",
        os.path.join(os.path.dirname(__file__), "..", "src", "autoflow_gateway", "proposals.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["autoflow_gateway._proposals_w2_iso"] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_proposals()
ProposalStore = _mod.ProposalStore
_is_test_agent = _mod._is_test_agent
_test_agent_sql = _mod._test_agent_sql
_now = _mod._now


class _Cfg:
    def __init__(self):
        self.data_dir = tempfile.mkdtemp(prefix="afw2_")
        self.env = "test"


def _ch(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mk_store():
    return ProposalStore(_Cfg())


def _ins(store, pid, agent, status, content="{}", decided=None, archived=None,
         same_hash_as=None):
    h = _ch(content) if same_hash_as is None else _ch(same_hash_as)
    conn = store._conn()
    try:
        conn.execute(
            "INSERT INTO proposals (id,agent_id,title,kind,content,status,tags,"
            "created_at,decided_at,reviewer,public_path,deployed_flow_id,source,spec,"
            "content_hash,archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, agent, "title", "idea", content, status, "[]", _now(),
             decided, None, None, None, "unknown", "spec", h, archived),
        )
        conn.commit()
    finally:
        conn.close()


def _seed(store):
    _ins(store, "r1", "agt_alice", "raw")
    _ins(store, "r2", "agt_alice", "raw")  # 与 r1 同 content("{}") → 构成重复组
    _ins(store, "r3", "agt_bob", "candidate", decided=_iso(100))
    _ins(store, "r4", "agt_bob", "public", decided=_iso(200))
    _ins(store, "r5", "agt_alice", "raw", content='{"arch":1}', archived=_iso(5))
    _ins(store, "t1", "agent_test", "raw", content='{"t":1}')
    _ins(store, "t2", "corpus-verify", "raw", content='{"t":2}')


def _iso(days_back=None):
    d = datetime.now(timezone.utc) - timedelta(days=days_back or 0)
    return d.isoformat()


def test_sql_test_filter_equiv_is_test_agent():
    store = _mk_store(); _seed(store)
    pred, params = _test_agent_sql(negate=True)
    conn = store._conn()
    rows = conn.execute("SELECT id, agent_id FROM proposals").fetchall()
    conn.close()
    for r in rows:
        got = store._conn().execute(
            "SELECT id FROM proposals WHERE id=? AND " + pred, (r["id"], *params)
        ).fetchone() is not None
        assert got == (not _is_test_agent(r["agent_id"])), r["agent_id"]


def test_pagination_limit_offset_total():
    store = _mk_store(); _seed(store)
    exp = {"r1", "r2", "r3", "r4"}  # 真实且未归档
    assert {p.id for p in store.list()} == exp
    assert store.count() == 4
    p0 = store.list(limit=2, offset=0)
    p2 = store.list(limit=2, offset=2)
    assert len(p0) == 2 and len(p2) == 2
    assert {p.id for p in p0} | {p.id for p in p2} == exp
    assert store.count() == len(p0) + len(p2)
    assert store.count(include_test=True) == 6


def test_archive_unarchive_visibility():
    store = _mk_store(); _seed(store)
    assert store.count() == 4
    store.archive("r1")
    assert store.count() == 3
    store.unarchive("r1")
    assert store.count() == 4


def test_analyze_raw_noise_distribution():
    store = _mk_store(); _seed(store)
    rep = store.analyze_raw_noise(dry_run=True)
    assert rep["total_raw"] == 5
    assert rep["test_raw"] == 2
    assert rep["non_test_raw"] == 3
    assert rep["duplicate_groups"] == 1
    assert rep["duplicate_extra"] == 1


def test_auto_archive_dry_run_vs_write():
    store = _mk_store(); _seed(store)
    before = store.count()
    ar = store.auto_archive(dry_run=True, decided_days=10)
    assert sorted(ar["ids"]) == ["r3", "r4"]
    assert store.count() == before  # dry_run 不改库
    ar2 = store.auto_archive(dry_run=False, decided_days=10)
    assert sorted(ar2["ids"]) == ["r3", "r4"]
    assert store.count() == 2  # r1,r2 仍在
