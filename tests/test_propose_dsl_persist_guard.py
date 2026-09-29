# -*- coding: utf-8 -*-
"""回归守卫：propose_dsl 落档失败必须诚实暴露（ok=False + error），
绝不能再静默吞成 ok=True + proposal_id=None（fail-open + 静默丢提案）。

背景（两朵测试隔离债治理之一）：全量套件下 wb16 并发测试的 ProposalStore.submit 曾因
autoflow.db 被并发长连接竞争拖成只读而抛 attempt to write a readonly database，原代码
except 后 proposal_id=None 却仍 ok=True，既误导 agent 又静默丢提案。本守卫固化修复：
任何落档异常都必须让 propose_dsl 返回 ok=False 且携带 error，而不是假装成功。
"""
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLOW_DEBUG_BRIDGE"] = "0"
os.environ["NR_URL"] = "http://127.0.0.1:1"
os.environ["HASS_SERVER"] = "http://127.0.0.1:1"


class TestProposePersistGuard(unittest.TestCase):
    def setUp(self):
        from autoflow_gateway import gateway as G
        from autoflow_gateway import vhass as VH
        from autoflow_gateway.config import GatewayConfig
        self._tmp = tempfile.mkdtemp(prefix="af_persistguard_")
        cfg = GatewayConfig(data_dir=self._tmp, env="staging")
        self.gw = G.Gateway(config=cfg)
        # 复刻 wb16 的已知实体登记，确保实体校验通过、能走到落档路径
        self.gw.state.add_mapping("书房主灯", "light.study_main")
        self.gw.state.add_mapping("客厅主灯", "light.living_room_main")
        for _eid in ("light.study_main", "light.living_room_main",
                     "binary_sensor.study_door", "device_tracker.me"):
            self.gw.state.add_mapping(_eid, _eid)
        self._vhass = VH.VHassStore()
        seed = VH.build_seed_from_entities([
            ("light.study_main", "书房主灯", "书房", "off", {}),
            ("binary_sensor.study_door", "书房门", "书房", "off", {}),
            ("light.living_room_main", "客厅主灯", "客厅", "off", {}),
            ("device_tracker.me", "我", "大门", "not_home", {}),
        ])
        self._vhass.areas = seed["areas"]
        self._vhass.entities = {e["entity_id"]: VH.VHassStore._normalize(e)
                                for e in seed["entities"]}
        self._dsl = (
            "场景: 书房入户播报2\n"
            "触发: binary_sensor.study_door 有人\n"
            "动作: light.turn_on(书房主灯, brightness=80)\n"
            "调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)\n"
        )

    def _propose(self):
        return self.gw.propose_dsl(self._dsl, "agent_guard",
                                   [{"entity_id": "light.study_main", "state": "on"}],
                                   vhass_store=self._vhass)

    def test_normal_persist_succeeds(self):
        # 控制组：正常落档应 ok=True 且 proposal_id 非空
        r = self._propose()
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r.get("proposal_id"), r)

    def test_persist_failure_is_not_silently_swallowed(self):
        # 守卫：落档抛异常必须 ok=False + error，绝不可 ok=True + proposal_id=None
        from autoflow_gateway.proposals import ProposalStore
        orig = ProposalStore.submit
        def _boom(self_store, *a, **k):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        with mock.patch.object(ProposalStore, "submit", _boom):
            r = self._propose()
        self.assertFalse(r.get("ok"), r)
        self.assertIsNone(r.get("proposal_id"), r)
        self.assertIn("error", r)
        self.assertTrue(r.get("error"), r)
        self.assertIn("readonly", (r.get("error") or "").lower() +
                      ((r.get("gate") or {}).get("note") or "").lower(), r)

    def test_readonly_db_surface_failure(self):
        # 真实场景复刻：把 autoflow.db 设为只读后落档，必须 ok=False 而非 ok=True
        from autoflow_gateway.proposals import ProposalStore
        from autoflow_gateway.config import GatewayConfig
        store = ProposalStore(GatewayConfig(data_dir=self._tmp, env="staging"))
        store.submit("agent_seed", "seed", "skill", "{}")  # 先建库
        os.chmod(store.db_path, 0o444)  # 只读
        r = self._propose()
        self.assertFalse(r.get("ok"), r)
        self.assertIsNone(r.get("proposal_id"), r)
        self.assertTrue(r.get("error"), r)


if __name__ == "__main__":
    unittest.main()
