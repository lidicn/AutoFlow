#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份层 / 提案 / 笔记 存储单测（unittest，不触真实 HA/NR）。"""
import os
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.identity import AgentStore
from autoflow_gateway.proposals import ProposalStore
from autoflow_gateway.notes import NoteStore


class TmpCfgMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_test_")
        self.cfg = GatewayConfig(data_dir=self.tmp, env="staging")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestIdentity(TmpCfgMixin, unittest.TestCase):
    def test_create_resolve_reject(self):
        store = AgentStore(self.cfg)
        agent, code = store.create_agent("deepseek++", "staging", "测试")
        self.assertTrue(code.startswith("af_"))
        self.assertIsNotNone(store.resolve_by_code(code))
        # 错误码拒绝
        self.assertIsNone(store.resolve_by_code("af_wrong"))
        self.assertIsNone(store.resolve_by_code(""))
        # 吊销后失效
        self.assertTrue(store.revoke_agent(agent.agent_id))
        self.assertIsNone(store.resolve_by_code(code))
        # 重名报错
        with self.assertRaises(ValueError):
            store.create_agent("deepseek++")

    def test_regen_invalidates_old(self):
        store = AgentStore(self.cfg)
        _, code = store.create_agent("trac", "prod")
        new = store.regenerate_code(store.get_agent_by_name("trac").agent_id)
        self.assertIsNotNone(new)
        self.assertIsNone(store.resolve_by_code(code))      # 旧码失效
        self.assertIsNotNone(store.resolve_by_code(new))   # 新码可用

    def test_list_and_last_seen(self):
        store = AgentStore(self.cfg)
        store.create_agent("a1")
        store.create_agent("a2")
        self.assertEqual(len(store.list_agents()), 2)
        a = store.get_agent_by_name("a1")
        store.record_last_seen(a.agent_id)
        self.assertIsNotNone(store.get_agent(a.agent_id).last_seen)

    def test_update_agent_fields_and_mode(self):
        store = AgentStore(self.cfg)
        agent, _ = store.create_agent("ds", "staging", mode="white")
        self.assertEqual(agent.mode, "white")
        # mode 为显式列：直接更新 mode（不再从 notes 魔法串推断）
        self.assertTrue(store.update_agent(agent.agent_id, mode="black"))
        self.assertEqual(store.get_agent(agent.agent_id).mode, "black")
        # 改 tier
        self.assertTrue(store.update_agent(agent.agent_id, tier="prod"))
        self.assertEqual(store.get_agent(agent.agent_id).tier, "prod")
        # 改 name
        self.assertTrue(store.update_agent(agent.agent_id, name="ds2"))
        self.assertEqual(store.get_agent_by_name("ds2").agent_id, agent.agent_id)
        # 不存在返回 False
        self.assertFalse(store.update_agent("agt_nope", notes="x"))
        # 空 name 报错
        with self.assertRaises(ValueError):
            store.update_agent(agent.agent_id, name="   ")


class TestProposals(TmpCfgMixin, unittest.TestCase):
    def test_lifecycle_and_public_file(self):
        store = ProposalStore(self.cfg)
        p = store.submit("agt_x", "支持场景优先级", "skill", "内容…", ["ha", "安全"])
        self.assertEqual(p.status, "raw")
        # raw -> candidate
        p = store.promote(p.id)
        self.assertEqual(p.status, "candidate")
        self.assertIsNone(p.public_path)
        # candidate -> public，落盘
        p = store.promote(p.id)
        self.assertEqual(p.status, "public")
        self.assertIsNotNone(p.public_path)
        self.assertTrue(os.path.exists(p.public_path))
        # 拒绝已 public 不行
        with self.assertRaises(ValueError):
            store.reject(p.id)
        # 文件内容含 frontmatter
        with open(p.public_path, "r", encoding="utf-8") as f:
            self.assertIn("source_agent: agt_x", f.read())

    def test_reject(self):
        store = ProposalStore(self.cfg)
        p = store.submit("agt_y", "标题", "idea", "x")
        p = store.reject(p.id, reason="不合适")
        self.assertEqual(p.status, "rejected")
        self.assertIn("不合适", p.content)


class TestNotes(TmpCfgMixin, unittest.TestCase):
    def test_crud(self):
        store = NoteStore(self.cfg)
        n = store.create("想法A", "先不落地", ["照明"])
        self.assertEqual(len(store.list()), 1)
        n2 = store.update(n.id, body="改一下", tags=["照明", "安全"])
        self.assertEqual(n2.body, "改一下")
        self.assertIn("安全", n2.tags)
        self.assertTrue(store.delete(n.id))
        self.assertEqual(len(store.list()), 0)

    def test_search_and_tag(self):
        store = NoteStore(self.cfg)
        store.create("LED 色温", "客厅", ["照明"])
        store.create("门锁联动", "门口", ["安全"])
        self.assertEqual(len(store.list(q="色温")), 1)
        self.assertEqual(len(store.list(tag="安全")), 1)


if __name__ == "__main__":
    unittest.main()
