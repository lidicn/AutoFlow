#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""issues 表 + TaskStore.report_issue/list_issues/resolve_issue 单测（unittest，不触真实 HA/NR）。"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.task_store import TaskStore


class TmpStoreMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = GatewayConfig()
        self.cfg.data_dir = self.tmp
        self.store = TaskStore(self.cfg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestIssues(TmpStoreMixin, unittest.TestCase):
    def test_report_and_list_roundtrip(self):
        r = self.store.report_issue(
            "agt_x", "客厅窗帘实体解析歧义", "resolve_entity('客厅窗帘') 返回0候选",
            task_id="hist2_01", severity="high", category="entity",
        )
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["issue_id"].startswith("iss_"))
        lst = self.store.list_issues()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["issue_id"], r["issue_id"])
        self.assertEqual(lst[0]["status"], "open")
        self.assertEqual(lst[0]["severity"], "high")
        self.assertEqual(lst[0]["category"], "entity")
        self.assertEqual(lst[0]["task_id"], "hist2_01")

    def test_report_rejects_empty_title_or_body(self):
        self.assertFalse(self.store.report_issue("a", "", "b")["ok"])
        self.assertFalse(self.store.report_issue("a", "t", "")["ok"])
        # agent_id 必填
        self.assertFalse(self.store.report_issue("", "t", "b")["ok"])

    def test_report_rejects_bad_severity_and_category(self):
        self.assertFalse(self.store.report_issue("a", "t", "b", severity="urgent")["ok"])
        self.assertFalse(self.store.report_issue("a", "t", "b", category="bogus")["ok"])
        # 大小写归一
        r = self.store.report_issue("a", "t", "b", severity="HIGH", category="DSL")
        self.assertTrue(r["ok"])
        row = self.store.list_issues()[0]
        self.assertEqual(row["severity"], "high")
        self.assertEqual(row["category"], "dsl")

    def test_list_filter_by_status_and_agent(self):
        self.store.report_issue("a1", "t1", "b1")
        self.store.report_issue("a2", "t2", "b2")
        self.assertEqual(len(self.store.list_issues(status="open")), 2)
        self.assertEqual(len(self.store.list_issues(status="resolved")), 0)
        self.assertEqual(len(self.store.list_issues(agent_id="a1")), 1)
        self.assertEqual(len(self.store.list_issues(agent_id="aX")), 0)

    def test_resolve_updates_status(self):
        r = self.store.report_issue("a", "t", "b")
        iid = r["issue_id"]
        self.assertTrue(self.store.resolve_issue(iid, "resolved")["ok"])
        self.assertEqual(self.store.list_issues(status="resolved")[0]["status"], "resolved")
        # 过滤联动
        self.assertEqual(len(self.store.list_issues(status="open")), 0)

    def test_resolve_rejects_bad_status_and_missing_id(self):
        r = self.store.report_issue("a", "t", "b")
        iid = r["issue_id"]
        self.assertFalse(self.store.resolve_issue(iid, "nope")["ok"])
        self.assertFalse(self.store.resolve_issue("iss_zzz", "open")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
