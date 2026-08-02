#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""link_out 型子流程能力纳入注册表单测（#585-#588，离线）。

覆盖：
  - subflow_registry schema 迁移（ADD COLUMN kind / entry_link_id，幂等）
  - register_subflow 校验：link_out 必填 entry_link_id、subflow 必填 nr_subflow_id、
    kind 非法被拒、大小写归一
  - get_subflow 从注册表读 link_out（含 status != active 不返回）
  - list_subflows 返回新列（kind / entry_link_id）
不触真实 HA/NR。
"""
import os
import sys
import sqlite3
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.task_store import TaskStore
from autoflow_gateway.subflows import get_subflow


def _columns(store, table="subflow_registry"):
    con = sqlite3.connect(store.db_path)
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    finally:
        con.close()


class TestLinkOutRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = GatewayConfig()
        self.cfg.data_dir = self.tmp
        self.store = TaskStore(self.cfg)   # 构造即触发 _init_db 迁移

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── schema 迁移 ──────────────────────────────────────────────
    def test_schema_migration_adds_columns(self):
        cols = _columns(self.store)
        self.assertIn("kind", cols)
        self.assertIn("entry_link_id", cols)

    def test_schema_migration_idempotent_on_reopen(self):
        # 同 db 重新构造一个 store，迁移不应报错且列仍在
        store2 = TaskStore(self.cfg)
        cols2 = _columns(store2)
        self.assertIn("kind", cols2)
        self.assertIn("entry_link_id", cols2)

    # ── register 校验 ──────────────────────────────────────────
    def test_register_linkout_requires_entry_link_id(self):
        r = self.store.register_subflow("cap_x", kind="link_out")
        self.assertFalse(r["ok"], r)
        self.assertIn("entry_link_id", r["error"])
        r2 = self.store.register_subflow("cap_x", kind="link_out", entry_link_id="lnk_1")
        self.assertTrue(r2["ok"], r2)
        meta = self.store.get_subflow_meta("cap_x")
        self.assertEqual(meta["kind"], "link_out")
        self.assertEqual(meta["entry_link_id"], "lnk_1")
        self.assertIsNone(meta["nr_subflow_id"])

    def test_register_subflow_requires_nr_subflow_id(self):
        r = self.store.register_subflow("sf_x", kind="subflow")
        self.assertFalse(r["ok"], r)
        self.assertIn("nr_subflow_id", r["error"])
        r2 = self.store.register_subflow("sf_x", kind="subflow", nr_subflow_id="abc123")
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(self.store.get_subflow_meta("sf_x")["nr_subflow_id"], "abc123")

    def test_register_rejects_bad_kind_and_normalizes(self):
        r = self.store.register_subflow("k", kind="weird")
        self.assertFalse(r["ok"], r)
        # 大小写归一
        r2 = self.store.register_subflow("k", kind="LINK_OUT", entry_link_id="e")
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(self.store.get_subflow_meta("k")["kind"], "link_out")

    # ── get_subflow 读 link_out（注册表分支）──────────────────────
    def test_get_subflow_reads_linkout_from_registry(self):
        self.store.register_subflow(
            "my_cap", kind="link_out", entry_link_id="entry123",
            status="active", source_type="imported",
            input_schema=[{"name": "text", "type": "str", "required": True,
                           "default": None, "enum": None, "desc": "t"}])
        spec = get_subflow("my_cap", registry_store=self.store)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.call, {"type": "link_out", "entry_link_id": "entry123"})
        # params 为 name→Param 映射，入参经 schema 还原
        self.assertIn("text", spec.params)
        self.assertEqual(spec.params["text"].default, None)
        self.assertTrue(spec.params["text"].required)

    def test_get_subflow_skips_disabled_linkout(self):
        self.store.register_subflow(
            "my_cap", kind="link_out", entry_link_id="entry123",
            status="disabled", source_type="imported")
        self.assertIsNone(get_subflow("my_cap", registry_store=self.store))

    # ── list 返回新列 ───────────────────────────────────────────
    def test_list_returns_kind_and_entry_link_id(self):
        self.store.register_subflow(
            "my_cap", kind="link_out", entry_link_id="entry123",
            status="active", source_type="imported")
        rows = self.store.list_subflows()
        row = next(r for r in rows if r["key"] == "my_cap")
        self.assertEqual(row["kind"], "link_out")
        self.assertEqual(row["entry_link_id"], "entry123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
