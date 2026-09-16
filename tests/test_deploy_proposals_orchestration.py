# -*- coding: utf-8 -*-
"""#8 · 多 flow 安全部署编排（deploy_proposals）离线回归。

验证 ROADMAP #8 验收门「部分失败时已部署部分可一键回滚到部署前快照」：
- 全成功：仅拍一次整实例快照、不回滚、返回完整 deployed 清单。
- 任一带闸失败：立即 restore 整实例到批量快照、rolled_back=True，返回已部署+失败清单。
- dry_run：不拍快照、不回滚（预览语义）。
- 空 ids：友好拒绝、无副作用。

全程离线：gw.nr 用内存假后端记录快照/还原调用；gw.deploy_proposal 用可控 stub
（不重测 deploy_proposal 自身 —— 其有独立测试）。
运行：pytest tests/test_deploy_proposals_orchestration.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_dpo_")

from autoflow_gateway.gateway import Gateway
from autoflow_gateway.config import reset_config

reset_config()


def _make_gw(plan):
    """构造 Gateway + 内存假 nr（记录快照/还原）+ 可控 deploy_proposal stub。

    plan: {pid: <deploy_proposal 应返回的字典>}
    """
    gw = Gateway()
    log = []
    snaps = []

    class _NR:
        def take_instance_snapshot(self, label):
            p = f"/snap/{label}_{len(snaps)}"
            snaps.append(p)
            return p

        def restore_instance_snapshot(self, path, allow_prod=False, allow_partial=True):
            log.append(("restore", path))
            return {"ok": True, "restored": True}

    gw.nr = _NR()

    def _deploy(pid, **kw):
        log.append(("deploy", pid))
        return dict(plan.get(pid, {"ok": True, "flow_id": "f_" + pid, "label": pid}))

    gw.deploy_proposal = _deploy
    return gw, log, snaps


def test_all_success_no_rollback():
    plan = {"a": {"ok": True, "flow_id": "fa", "label": "A"},
            "b": {"ok": True, "flow_id": "fb", "label": "B"}}
    gw, log, snaps = _make_gw(plan)
    r = gw.deploy_proposals(["a", "b"])
    assert r["ok"] is True
    assert r["rolled_back"] is False
    assert [d["pid"] for d in r["deployed"]] == ["a", "b"]
    assert r["failed"] == []
    assert len(snaps) == 1, snaps            # ★ 单次整实例快照
    assert not any(c[0] == "restore" for c in log)


def test_second_fails_rolls_back():
    plan = {"a": {"ok": True, "flow_id": "fa", "label": "A"},
            "b": {"ok": False, "stage": "gate", "error": "staging 闸未通过"}}
    gw, log, snaps = _make_gw(plan)
    r = gw.deploy_proposals(["a", "b"])
    assert r["ok"] is False
    assert r["rolled_back"] is True
    assert [d["pid"] for d in r["deployed"]] == ["a"]   # 已部署部分保留在清单
    assert r["failed"][0]["pid"] == "b"
    assert r["failed"][0]["stage"] == "gate"
    assert any(c[0] == "restore" for c in log)          # ★ 整体回滚
    assert log[-1][0] == "restore"
    assert log[-1][1] == snaps[0]                       # 回滚到批量快照


def test_third_fails_rolls_back_to_batch_snapshot():
    plan = {"a": {"ok": True}, "b": {"ok": True},
            "c": {"ok": False, "stage": "conflict", "error": "同名流"}}
    gw, log, snaps = _make_gw(plan)
    r = gw.deploy_proposals(["a", "b", "c"])
    assert r["ok"] is False
    assert r["rolled_back"] is True
    assert len(r["deployed"]) == 2
    # 只回滚一次（到批量快照），不是逐条
    assert sum(1 for c in log if c[0] == "restore") == 1


def test_dry_run_no_snapshot_no_rollback():
    plan = {"a": {"ok": True}, "b": {"ok": True}}
    gw, log, snaps = _make_gw(plan)
    r = gw.deploy_proposals(["a", "b"], dry_run=True)
    assert r["ok"] is True
    assert len(snaps) == 0                       # ★ dry_run 不拍快照
    assert not any(c[0] == "restore" for c in log)


def test_empty_ids_rejected():
    gw, log, snaps = _make_gw({})
    r = gw.deploy_proposals([])
    assert r["ok"] is False
    assert len(snaps) == 0
    assert not any(c[0] == "deploy" for c in log)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
