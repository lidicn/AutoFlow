# -*- coding: utf-8 -*-
"""v3.0.0 #20 · 部署/验证/回滚审计（持久可回溯）回归。

验收门（ROADMAP #20）：任一次部署可回溯「**谁 / 何时 / 验证了什么 / 回滚了没**」。

补齐前只有：
  · 按 trace_id 读明细（`_read_apply_trace` / `autoflow_get_trace`）——**不知道 id 就查不到**；
  · 进程内 `_slog` 环形缓冲（cap 200、重启即失），且**人路径（提案/批量部署）根本没落盘**。

本测试锁定补齐的三件事：
  ① 审计**索引**（枚举，无需预知 trace_id）；
  ② 人路径（DEPLOY_PROPOSAL / DEPLOY_BATCH）与其整批回滚落盘；
  ③ 四要素齐全（who / when / verified / rolled_back）。

全程离线：索引层直接读写临时 data 目录；批量层用 `Gateway` + 内存假 NR + 可控
`deploy_proposal` stub（不重测 deploy_proposal 自身，其有独立测试）。
运行：pytest tests/test_deploy_audit.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_audit_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway.config import reset_config  # noqa: E402
reset_config()

import autoflow_gateway.gateway as G  # noqa: E402
from autoflow_gateway.gateway import Gateway  # noqa: E402


def _clear():
    """清空审计轨迹目录，保证用例间隔离（轨迹是 append-only 落盘，不会自清）。"""
    d = G._apply_trace_dir()
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if os.path.isfile(p):
            os.unlink(p)


# ── ① 索引层（无需 Gateway 实例）─────────────────────────────────────────

def test_index_exposes_four_elements():
    """索引行必须同时给出 谁/何时/验证了什么/回滚了没。"""
    _clear()
    G._write_apply_trace({
        "trace_id": "t_dep1", "flow_id": "af_light", "mode": "DEPLOY_PROPOSAL",
        "agent_id": "human", "ok": True, "applied": True, "pending": False,
        "stage": "deployed", "snapshot_path": "/snap/x", "label": "书房灯",
        "reason": "提案部署", "verified": {"gate_passed": True, "require_e2e": False},
    })
    out = G._list_apply_traces()
    assert out["ok"] is True and out["total"] == 1, out
    row = out["items"][0]
    assert row["trace_id"] == "t_dep1"
    assert row["agent_id"] == "human"               # 谁
    assert row["updated_at"]                        # 何时
    assert row["verified"]["gate_passed"] is True   # 验证了什么
    assert row["rolled_back"] is False              # 回滚了没
    assert row["event_count"] == 1
    assert row["snapshot_path"] == "/snap/x"


def test_rollback_event_flips_rolled_back():
    """同一 trace 追加 ROLLBACK 事件后，索引行必须标出「已回滚」。"""
    _clear()
    G._write_apply_trace({"trace_id": "t_rb", "flow_id": "af_a", "mode": "DEPLOY_PROPOSAL",
                          "agent_id": "human", "ok": True, "applied": True, "stage": "deployed"})
    G._write_apply_trace({"trace_id": "t_rb", "flow_id": "af_a", "mode": "ROLLBACK",
                          "agent_id": "human", "ok": True, "applied": True, "stage": "restored"})
    out = G._list_apply_traces(flow_id="af_a")
    assert out["total"] == 1, out
    row = out["items"][0]
    assert row["rolled_back"] is True, row          # 回滚了没 = 是
    assert row["event_count"] == 2
    # 顶层 mode 取**首个非空**（起始动作），故仍是 DEPLOY_PROPOSAL 而非 ROLLBACK
    assert row["mode"] == "DEPLOY_PROPOSAL", row


def test_filters_flow_agent_mode():
    _clear()
    G._write_apply_trace({"trace_id": "t1", "flow_id": "f1", "mode": "DEPLOY_PROPOSAL",
                          "agent_id": "human", "ok": True, "stage": "deployed"})
    G._write_apply_trace({"trace_id": "t2", "flow_id": "f2", "mode": "DIRECT",
                          "agent_id": "agent_x", "ok": True, "stage": "direct_write_applied"})
    assert G._list_apply_traces(flow_id="f1")["total"] == 1
    assert G._list_apply_traces(agent_id="agent_x")["total"] == 1
    assert G._list_apply_traces(mode="direct")["total"] == 1     # 大小写不敏感
    assert G._list_apply_traces(mode="DEPLOY_PROPOSAL")["total"] == 1
    assert G._list_apply_traces(mode="ROLLBACK")["total"] == 0
    assert G._list_apply_traces()["total"] == 2


def test_index_orders_newest_first_and_respects_limit():
    _clear()
    for i in range(3):
        G._write_apply_trace({"trace_id": f"t{i}", "flow_id": f"f{i}",
                              "mode": "DEPLOY_PROPOSAL", "agent_id": "human",
                              "ok": True, "stage": "deployed"})
    out = G._list_apply_traces(limit=2)
    assert out["total"] == 3 and out["count"] == 2, out
    ts = [r["updated_at"] for r in out["items"]]
    assert ts == sorted(ts, reverse=True), ts      # 倒序


def test_corrupt_file_skipped_not_fatal():
    """坏文件必须被跳过——审计是旁路，一条坏记录不能毁掉整张索引。"""
    _clear()
    G._write_apply_trace({"trace_id": "good", "flow_id": "f_ok", "mode": "DEPLOY_PROPOSAL",
                          "agent_id": "human", "ok": True, "stage": "deployed"})
    with open(os.path.join(G._apply_trace_dir(), "broken.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    with open(os.path.join(G._apply_trace_dir(), "notdict.json"), "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)                     # 合法 JSON 但非 dict
    out = G._list_apply_traces()
    assert out["ok"] is True, out
    assert out["total"] == 1 and out["items"][0]["trace_id"] == "good", out


def test_missing_dir_returns_empty(monkeypatch):
    monkeypatch.setattr(G, "_apply_trace_dir", lambda: os.path.join(_TMP, "does_not_exist"))
    out = G._list_apply_traces()
    assert out["ok"] is True and out["total"] == 0 and out["items"] == [], out


# ── ② 批量部署路径落盘 ───────────────────────────────────────────────────

def _make_gw(plan):
    """Gateway + 内存假 NR + 可控 deploy_proposal stub。plan: {pid: 返回字典}。"""
    gw = Gateway()
    log = []

    class _NR:
        def take_instance_snapshot(self, label):
            return f"/snap/{label}"

        def restore_instance_snapshot(self, path, allow_prod=False, allow_partial=True):
            log.append(("restore", path))
            return {"ok": True, "restored": True}

    gw.nr = _NR()
    gw.deploy_proposal = lambda pid, **kw: (
        log.append(("deploy", pid)) or
        dict(plan.get(pid, {"ok": True, "flow_id": "f_" + pid, "label": pid}))
    )
    return gw, log


def test_batch_success_writes_audit():
    _clear()
    gw, _ = _make_gw({"a": {"ok": True, "flow_id": "fa", "label": "A"}})
    r = gw.deploy_proposals(["a"])
    assert r["ok"] is True, r
    out = gw.list_apply_traces(mode="DEPLOY_BATCH")
    assert out["total"] == 1, out
    row = out["items"][0]
    assert row["stage"] == "deployed_batch"
    assert row["verified"]["deployed_count"] == 1
    assert row["rolled_back"] is False


def test_batch_partial_fail_writes_rollback_audit():
    _clear()
    gw, log = _make_gw({"a": {"ok": True, "flow_id": "fa", "label": "A"},
                        "b": {"ok": False, "stage": "gate", "error": "闸未通过"}})
    r = gw.deploy_proposals(["a", "b"])
    assert r["ok"] is False and r["rolled_back"] is True, r
    assert any(c[0] == "restore" for c in log)
    out = gw.list_apply_traces()
    assert out["total"] == 1, out
    row = out["items"][0]
    assert row["stage"] == "rolled_back_batch"
    assert row["rolled_back"] is True, row          # 回滚了没 = 是
    assert row["verified"]["failed_count"] == 1


def test_batch_dry_run_writes_no_rollback_audit():
    """dry_run 不拍快照也不回滚 → 不得伪造一次「已回滚」的审计记录。"""
    _clear()
    gw, _ = _make_gw({"a": {"ok": True}})
    r = gw.deploy_proposals(["a"], dry_run=True)
    assert r["ok"] is True and r["rolled_back"] is False, r
    out = gw.list_apply_traces()
    assert all(r_["rolled_back"] is False for r_ in out["items"]), out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
