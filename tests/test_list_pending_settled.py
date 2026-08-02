#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autoflow_list_pending 已部署提案不再谎报待办（WB72 F9 / iss_3fc501da8c）。

旧行为：按 status="raw" 一把捞。提案被人类在 WebUI 批准部署后只写
deployed_flow_id、status 仍是 "raw"，于是**已经部署、已经真实生效**的提案
永远滞留在 pending 里 —— 队列谎报待办，agent 也无从确认自己那条批没批。
WebUI 侧列表（gateway.py:3448）早有「有 deployed_flow_id 就不算待审」这条过滤，
本测试锁定 MCP 侧补齐同一契约后的行为，防止两条呈现路径再次漂移。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_lp_")

from autoflow_gateway import mcp_server as ms          # noqa: E402
from autoflow_gateway.proposals import ProposalStore   # noqa: E402

# 两个用例共用同一个 DB（同一 DATA_DIR），靠**不同 agent 身份**互相隔离
# ——list(agent_id=...) 按身份过滤，避免彼此的提案串味。
# 身份须非 test/infra 前缀，否则被 list() 默认隐藏（_is_test_agent）。
AID_A = "agt_listpending_a"
AID_B = "agt_listpending_b"


class _FakeGW:
    """只提供 list_pending 与 cfg —— autoflow_list_pending 用到的全部依赖。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def list_pending(self, agent_id=None):
        return []               # 确认闸侧空，聚焦提案侧行为


def _call(monkeypatch, store, aid):
    monkeypatch.setattr(ms, "_gw", lambda: _FakeGW(store.cfg))
    monkeypatch.setattr(ms, "get_current_agent",
                        lambda: type("A", (), {"mode": "white", "agent_id": aid})())
    return json.loads(ms.autoflow_list_pending())


def test_deployed_proposal_moves_out_of_pending(monkeypatch):
    store = ProposalStore()
    undone = store.submit(AID_A, "还没批的场景", "skill", "dsl-a").id
    done = store.submit(AID_A, "已经部署的场景", "skill", "dsl-b").id
    store.mark_deployed(done, "flow_deadbeef")

    res = _call(monkeypatch, store, AID_A)

    pend_ids = {p["id"] for p in res["pending"] if p.get("source") == "proposal"}
    assert undone in pend_ids, res
    assert done not in pend_ids, "已部署的提案不得再算待办"

    settled = {s["id"]: s for s in res["settled"]}
    assert done in settled, res
    assert settled[done]["deployed_flow_id"] == "flow_deadbeef"
    assert settled[done]["state"] == "deployed"
    assert res["settled_total"] == 1


def test_settled_is_capped_but_total_is_honest(monkeypatch):
    """settled 回吐有条数上限（防上下文炸弹），但总数如实告知。"""
    store = ProposalStore()
    n = ms._SETTLED_LIMIT + 5
    for i in range(n):
        # content 必须逐条不同：submit 对 (agent_id, content) 做 120s 窗口去重，
        # 内容相同会被折叠成同一条记录，测不出条数上限。
        pid = store.submit(AID_B, f"历史已部署-{i}", "skill", f"dsl-{i}").id
        store.mark_deployed(pid, f"flow_{i}")

    res = _call(monkeypatch, store, AID_B)

    assert res["pending"] == [], res
    assert len(res["settled"]) == ms._SETTLED_LIMIT
    assert res["settled_total"] == n


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行（依赖 monkeypatch fixture）")
