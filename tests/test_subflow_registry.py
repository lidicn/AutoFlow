#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subflow_registry 表 + TaskStore.register/list/get/delete 单测（unittest，不触真实 HA/NR）。

覆盖 #575：注册表 upsert、JSON 列（input_schema / env_requirements）往返解析、
source_type / status 校验与过滤、删除。是后续 Step3 编译器查表（resolve_subflow）
与 Step5 WebUI 导入的基础。
"""
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


class TestSubflowRegistry(TmpStoreMixin, unittest.TestCase):
    def _sample(self, key="bark_push", **kw):
        return self.store.register_subflow(
            key,
            title=kw.get("title", "Bark 推送"),
            nr_subflow_id=kw.get("nr_subflow_id", "b0bbc86abb2172a5"),
            source_type=kw.get("source_type", "managed"),
            input_schema=kw.get("input_schema", [
                {"name": "title", "required": True, "type": "str", "default": None,
                 "enum": None, "desc": "通知标题"},
                {"name": "body", "required": True, "type": "str", "default": None,
                 "enum": None, "desc": "通知正文"},
            ]),
            env_requirements=kw.get("env_requirements",
                                    ["BARK_SERVER", "BARK_KEY"]),
            owner=kw.get("owner", "system"),
            status=kw.get("status", "active"),
            spec_ref=kw.get("spec_ref", "bark_push"),
        )

    def test_register_and_get_roundtrip(self):
        r = self._sample()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["key"], "bark_push")
        meta = self.store.get_subflow_meta("bark_push")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["title"], "Bark 推送")
        self.assertEqual(meta["nr_subflow_id"], "b0bbc86abb2172a5")
        self.assertEqual(meta["source_type"], "managed")
        self.assertEqual(meta["owner"], "system")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["spec_ref"], "bark_push")
        # JSON 列已解析回对象
        self.assertIsInstance(meta["input_schema"], list)
        self.assertEqual(len(meta["input_schema"]), 2)
        self.assertEqual(meta["input_schema"][0]["name"], "title")
        self.assertTrue(meta["input_schema"][0]["required"])
        self.assertEqual(meta["env_requirements"], ["BARK_SERVER", "BARK_KEY"])
        # registered_at 已写入
        self.assertTrue(meta["registered_at"])

    def test_list_roundtrip(self):
        self._sample("bark_push")
        self._sample("history_state_at", source_type="managed",
                     nr_subflow_id="af_hist_state_at", spec_ref="history_state_at",
                     title="历史状态查询")
        self._sample("my_imported", source_type="imported",
                     nr_subflow_id="abc123", spec_ref=None,
                     title="用户导入", owner="user_x",
                     input_schema=[{"name": "x", "required": False, "type": "int",
                                    "default": 0, "enum": None, "desc": "x"}])
        all_rows = self.store.list_subflows()
        self.assertEqual(len(all_rows), 3)
        keys = [r["key"] for r in all_rows]
        self.assertEqual(keys, sorted(keys))  # 按 key 升序
        # 过滤 source_type
        imported = self.store.list_subflows(source_type="imported")
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["key"], "my_imported")
        # 过滤 status
        active = self.store.list_subflows(status="active")
        self.assertEqual(len(active), 3)
        pending = self.store.list_subflows(status="pending_review")
        self.assertEqual(len(pending), 0)

    def test_upsert_overwrites(self):
        self._sample("bark_push", status="pending_review")
        before = self.store.get_subflow_meta("bark_push")
        self.assertEqual(before["status"], "pending_review")
        # 再次 register 同 key → 覆盖（非新增）
        self._sample("bark_push", status="active",
                     input_schema=[{"name": "title", "required": True, "type": "str",
                                    "default": None, "enum": None, "desc": "t"}])
        self.assertEqual(len(self.store.list_subflows()), 1)
        after = self.store.get_subflow_meta("bark_push")
        self.assertEqual(after["status"], "active")
        self.assertEqual(len(after["input_schema"]), 1)

    def test_register_rejects_bad_source_type_and_status(self):
        self.assertFalse(
            self.store.register_subflow("k", source_type="bogus")["ok"])
        self.assertFalse(
            self.store.register_subflow("k", status="weird")["ok"])
        # 大小写归一（subflow 实例型必须提供 nr_subflow_id）
        r = self.store.register_subflow("k", source_type="MANAGED", status="Active",
                                        nr_subflow_id="nr1")
        self.assertTrue(r["ok"])
        meta = self.store.get_subflow_meta("k")
        self.assertEqual(meta["source_type"], "managed")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["nr_subflow_id"], "nr1")

    def test_register_rejects_empty_key(self):
        self.assertFalse(self.store.register_subflow("")["ok"])
        self.assertFalse(self.store.register_subflow(None)["ok"])
        self.assertFalse(self.store.register_subflow("   ")["ok"])

    def test_empty_json_columns_default_to_empty_list(self):
        # 极简导入用 link_out 型（fire-and-forget，无需 nr_subflow_id），仅登记入口
        r = self.store.register_subflow("minimal", source_type="imported",
                                        kind="link_out", entry_link_id="e1")
        self.assertTrue(r["ok"])
        meta = self.store.get_subflow_meta("minimal")
        self.assertEqual(meta["input_schema"], [])
        self.assertEqual(meta["env_requirements"], [])
        self.assertEqual(meta["kind"], "link_out")
        self.assertEqual(meta["entry_link_id"], "e1")
        self.assertIsNone(meta["nr_subflow_id"])
        self.assertIsNone(meta["spec_ref"])

    def test_delete_roundtrip(self):
        self._sample("bark_push")
        self.assertIsNotNone(self.store.get_subflow_meta("bark_push"))
        d = self.store.delete_subflow("bark_push")
        self.assertTrue(d["ok"], d)
        self.assertIsNone(self.store.get_subflow_meta("bark_push"))
        # 删不存在的 → 报错
        self.assertFalse(self.store.delete_subflow("nope")["ok"])
        self.assertFalse(self.store.delete_subflow("")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
