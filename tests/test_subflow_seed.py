#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seed_managed_subflows 幂等 seed 单测（#578/#587，离线）。

验证：网关启动时把预置子流程写入 subflow_registry，覆盖两类：
  - subflow 实例型（bark_push + 4 history，共 5，需 nr_subflow_id，bark 含 BARK_* env）；
  - link_out 型（SUBFLOWS 中 call.type=="link_out" 且 preload=True 的能力：
    weather / anysearch，网关只发 link out 到 entry_link_id，无 NR 子流程实例）。
  demo_notify 虽是 link_out 但 preload=False（仅注册、不随启动预载到用户面板）；
  apisay 等豆包能力已随豆包下线移除。故实际预载条数由 EXPECT_SEEDED 动态推导（当前 7），
  二次 seed 不重复、不覆盖（保护手动改动）。
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
from autoflow_gateway.subflows import (
    seed_managed_subflows, get_subflow, SUBFLOWS, _MANAGED_SUBFLOW_KEYS,
)

# 从单一真相源 SUBFLOWS 推导期望的 link_out 能力 key，避免硬编码漂移
# 从单一真相源 SUBFLOWS 推导期望的 link_out 能力 key，避免硬编码漂移。
# ⚠️ demo_notify 是 link_out 但 preload=False（仅注册、不随网关启动预载到用户面板，
# 见 subflows.SubflowSpec.preload 与 seed_managed_subflows 的 preload 跳过分支），
# 故 seed 实际只写入 PRELOAD_LINKOUT_KEYS（不含 demo_notify）。
LINKOUT_KEYS = {k for k, s in SUBFLOWS.items() if (s.call or {}).get("type") == "link_out"}
PRELOAD_LINKOUT_KEYS = {k for k, s in SUBFLOWS.items()
                        if (s.call or {}).get("type") == "link_out"
                        and getattr(s, "preload", True)}
# subflow 实例型（需 nr_subflow_id）与预载 link_out 型合计 = 网关启动时实际 seed 的条数。
# 用 _MANAGED_SUBFLOW_KEYS 单一真相源（subflows.py），避免与产品清单两处硬编码漂移。
SUBFLOW_KEYS = set(_MANAGED_SUBFLOW_KEYS)
EXPECT_SEEDED = len(SUBFLOW_KEYS) + len(PRELOAD_LINKOUT_KEYS)


class TestSeedManagedSubflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = GatewayConfig()
        self.cfg.data_dir = self.tmp
        self.store = TaskStore(self.cfg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seed_creates_ten_managed(self):
        r = seed_managed_subflows(self.store)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["seeded"], EXPECT_SEEDED)
        self.assertEqual(r["skipped"], 0)
        rows = self.store.list_subflows(source_type="managed")
        self.assertEqual(len(rows), EXPECT_SEEDED)
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, SUBFLOW_KEYS | PRELOAD_LINKOUT_KEYS)
        for row in rows:
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["source_type"], "managed")
            self.assertEqual(row["owner"], "system")
            self.assertIsInstance(row["input_schema"], list)
            self.assertIsInstance(row["env_requirements"], list)

    def test_seed_linkout_rows_correct_shape(self):
        seed_managed_subflows(self.store)
        for key in PRELOAD_LINKOUT_KEYS:
            meta = self.store.get_subflow_meta(key)
            self.assertIsNotNone(meta, key)
            self.assertEqual(meta["kind"], "link_out", key)
            self.assertTrue(meta["entry_link_id"], key)          # 非空入口
            self.assertIsNone(meta["nr_subflow_id"], key)        # link_out 无 NR 实例
            self.assertEqual(meta["env_requirements"], [], key)
            self.assertIsInstance(meta["input_schema"], list, key)

    def test_bark_has_env_and_params(self):
        seed_managed_subflows(self.store)
        meta = self.store.get_subflow_meta("bark_push")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["nr_subflow_id"], "b0bbc86abb2172a5")
        self.assertEqual(meta["kind"], "subflow")
        self.assertEqual(meta["env_requirements"],
                         ["BARK_SERVER", "BARK_KEY", "BARK_CIPHER_KEY", "BARK_CIPHER_IV"])
        names = {p["name"] for p in meta["input_schema"]}
        self.assertIn("title", names)
        self.assertIn("body", names)
        # get_subflow 仍能命中（含编译器可用）
        spec = get_subflow("bark_push")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.call["subflow_id"], "b0bbc86abb2172a5")

    def test_idempotent_second_seed_skips(self):
        seed_managed_subflows(self.store)
        # 手动改一条（模拟用户调整），二次 seed 不应覆盖
        self.store.register_subflow(
            "bark_push", title="我改过的标题", nr_subflow_id="b0bbc86abb2172a5",
            source_type="managed", input_schema=[], env_requirements=[],
            owner="system", status="disabled", spec_ref="bark_push")
        r2 = seed_managed_subflows(self.store)
        self.assertEqual(r2["seeded"], 0)
        self.assertEqual(r2["skipped"], EXPECT_SEEDED)
        meta = self.store.get_subflow_meta("bark_push")
        # 手动改动被保留（未被二次 seed 覆盖）
        self.assertEqual(meta["title"], "我改过的标题")
        self.assertEqual(meta["status"], "disabled")
        self.assertEqual(meta["input_schema"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
