#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ProposalStore.purge_test_proposals 单测：仅删 test/infra 提案，真实 agent 不动。"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_prop_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway.proposals import ProposalStore, _is_test_agent, Proposal


class TestPurgeTestProposals(unittest.TestCase):
    def setUp(self):
        self.store = ProposalStore()

    def test_is_test_agent(self):
        self.assertTrue(_is_test_agent("agent_test"))
        self.assertTrue(_is_test_agent("golden_test"))
        self.assertTrue(_is_test_agent("webui-spotcheck"))
        self.assertFalse(_is_test_agent("agt_realuser123"))

    def test_purge_only_test_agents(self):
        self.store.submit("agent_test", "t1", "idea", "x")
        self.store.submit("golden_test", "t2", "idea", "x")
        self.store.submit("webui-spotcheck", "t3", "idea", "x")
        self.store.submit("agt_realuser123", "real", "idea", "x")
        self.assertEqual(len(self.store.list(include_test=True)), 4)

        rep = self.store.purge_test_proposals(dry_run=True)
        self.assertEqual(rep["count"], 3)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(len(self.store.list(include_test=True)), 4)  # 未删

        rep2 = self.store.purge_test_proposals(dry_run=False)
        self.assertEqual(rep2["count"], 3)
        after = self.store.list(include_test=True)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].agent_id, "agt_realuser123")

    def test_purge_idempotent(self):
        self.store.submit("agent_test", "t", "idea", "x")
        self.store.purge_test_proposals(dry_run=False)
        self.assertEqual(self.store.purge_test_proposals(dry_run=False)["count"], 0)


class TestProposalSummary(unittest.TestCase):
    """to_summary：列表接口轻量视图，剔除撑体积的 flow/validation，保留卡片字段。"""

    def _mk(self, content: str) -> Proposal:
        return Proposal(
            id="pr_test_summary", agent_id="agt_x", title="T", kind="raw_flow",
            content=content, status="raw", tags=["a"], created_at="2026-07-29T00:00:00Z",
            decided_at=None, reviewer=None, public_path=None, deployed_flow_id=None,
            source="raw", spec="T",
        )

    def test_strips_bulk_fields_keeps_card_fields(self):
        fat = {
            "dsl": "触发: x\n动作: y",
            "type": "raw_flow",
            "node_count": 468,
            "lint_error_count": 19,
            "lint_warning_count": 81,
            "blocking_rules": ["R13"],
            "logic": {"unreachable_actions": ["a1", "a2"]},
            "flow": {"nodes": [{"id": f"n{i}"} for i in range(1000)]},  # 体积炸弹
            "validation": [{"ok": True} for _ in range(500)],          # 体积炸弹
        }
        import json
        p = self._mk(json.dumps(fat, ensure_ascii=False))
        s = p.to_summary()
        c = json.loads(s["content"])
        # 大字段必须被剔除
        self.assertNotIn("flow", c)
        self.assertNotIn("validation", c)
        # 卡片所需字段必须保留
        for k in ("dsl", "type", "node_count", "lint_error_count",
                  "lint_warning_count", "blocking_rules"):
            self.assertEqual(c[k], fat[k], f"字段 {k} 应保留")
        self.assertEqual(c["logic"]["unreachable_actions"], ["a1", "a2"])
        # 顶层字段保留
        self.assertEqual(s["id"], "pr_test_summary")
        self.assertEqual(s["status"], "raw")
        self.assertEqual(s["title"], "T")

    def test_malformed_content_passthrough(self):
        p = self._mk("}{ not json")
        s = p.to_summary()
        self.assertEqual(s["content"], "}{ not json")  # 不崩，原样返回

    def test_subflow_keeps_registration_fields(self):
        """子流程提案：to_summary 须保留注册所需轻量字段，剔除完整 definition.nodes。"""
        import json
        definition = {
            "id": "sf_my_calc",
            "nodes": [{"id": f"n{i}"} for i in range(12)],  # 体积炸弹须被剔除
            "in_ports": [], "out_ports": [],
        }
        content = {
            "type": "subflow",
            "dsl_name": "my_calc",
            "name": "我的计算",
            "description": "示例子流程",
            "definition": definition,
        }
        p = Proposal(
            id="pr_sf_summary", agent_id="agt_x", title="T", kind="subflow",
            content=json.dumps(content, ensure_ascii=False),
            status="raw", tags=["a"], created_at="2026-07-29T00:00:00Z",
            decided_at=None, reviewer=None, public_path=None, deployed_flow_id=None,
            source="subflow", spec="我的计算｜my_calc｜12 nodes",
        )
        s = p.to_summary()
        c = json.loads(s["content"])
        self.assertEqual(c["type"], "subflow")
        self.assertEqual(c["dsl_name"], "my_calc")
        self.assertEqual(c["name"], "我的计算")
        self.assertEqual(c["definition_id"], "sf_my_calc")
        self.assertEqual(c["node_count"], 12)
        # 完整 definition 必须被剔除（仅留 id/节点数），避免列表接口膨胀
        self.assertNotIn("definition", c)
        self.assertNotIn("nodes", c)
        # 顶层字段保留
        self.assertEqual(s["kind"], "subflow")
        self.assertEqual(s["source"], "subflow")


if __name__ == "__main__":
    unittest.main()
