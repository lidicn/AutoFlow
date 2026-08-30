#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB93 O1 回归：取值-label 分支三层修复（离线可验，不依赖 live NR）。

根因分层（区分「数据缺失」vs「重放时序」vs「求值器缺口」）：
  A.【编译布线缺陷→数据缺失】F11(89a616a) 在 _emit_read_state 引入桥接 change 时
     `return vid`（链尾），触发器/上游被 _emit_body 直连到 change —— 取值 acs 被
     绕过、真实 NR 上永远不执行 → payload.state 读触发器 payload 的 .state（恒
     undefined）→ msg.<field> 恒 undefined → 分支恒走 else（静默反向执行）。
     修复：返回 (nid, vid) 二元组，恢复「触发 → 取值 → 绑定 → 下游」执行序。
  B.【重放建模缺口】_vg_apply_change 不解析 tot="msg"（真实 NR 语义是 msg 路径
     引用），旧代码把 msg.亮度 写成字面量字符串 "payload.state"。
  C.【求值器缺口】_vg_lookup 不认 msg. 前缀；$number(<常量>) 被当变量名。
     两者导致取值-label 分支永远「无法本地求值」→ 保守命中 → 未充分验证（量化
     14.6% 中招，WB92 诊断）。

vhass 最小数据契约：取值节点重放只需 world(entityId) 返回实体状态字符串
（vhass store 种子即满足）；重放把实体态写 msg.payload.state，change 桥接写
msg.<label>，switch jsonata 读 msg.<label> —— 全链不依赖 websocket。
"""
import os
import sys
import json
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.gateway import (
    _vg_apply_change, _vg_eval_jsonata_expr, _vg_lookup,
)

A_ON = "light.turn_on(light.lamp)"
A_OFF = "light.turn_off(light.lamp)"

DSL = ("场景: 书房光照\n触发: inject\n"
       "取值: sensor.lumi 光照\n"
       "分支: $number(光照) < 10\n"
       f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")


def _by_type(flow, ntype, name_sub=None):
    out = []
    for n in flow["nodes"]:
        if n.get("type") != ntype:
            continue
        if name_sub and name_sub not in (n.get("name") or ""):
            continue
        out.append(n)
    return out


def _wire_targets(flow, node):
    byid = {n["id"]: n for n in flow["nodes"]}
    return [byid.get(x, {}).get("type", x)
            for o in (node.get("wires") or []) for x in o]


class TestO1CompileWiring(unittest.TestCase):
    """A 层守卫：触发器必须接取值链首（acs），不得直连桥接 change。"""

    def test_trigger_wires_to_value_node_entry(self):
        flow = compile_dsl(DSL)
        inj = _by_type(flow, "inject")[0]
        self.assertEqual(_wire_targets(flow, inj), ["api-current-state"],
                         "触发器必须接取值 acs（链首），不得绕过直连 change")

    def test_value_chain_is_serial(self):
        flow = compile_dsl(DSL)
        acs = _by_type(flow, "api-current-state")[0]
        self.assertEqual(_wire_targets(flow, acs), ["change"],
                         "取值 acs → 桥接 change 串行")
        chg = _by_type(flow, "change", "绑定")[0]
        self.assertEqual(_wire_targets(flow, chg), ["switch"],
                         "桥接 change → switch 串行")


class TestO1ReplayModeling(unittest.TestCase):
    """B 层守卫：重放 change 求值 tot="msg"。"""

    def test_apply_change_resolves_msg_ref(self):
        chg = {"type": "change", "rules": [
            {"t": "set", "p": "光照", "pt": "msg",
             "to": "payload.state", "tot": "msg"}]}
        m = _vg_apply_change(chg, {"payload": {"state": "8"}})
        self.assertEqual(m["光照"], "8", 'tot="msg" 须解析 msg 路径而非字面量')

    def test_apply_change_msg_ref_missing_is_none(self):
        chg = {"type": "change", "rules": [
            {"t": "set", "p": "x", "pt": "msg",
             "to": "payload.state", "tot": "msg"}]}
        m = _vg_apply_change(chg, {"payload": ""})
        self.assertIsNone(m["x"], "路径取不到值时与 NR 对齐置 None")


class TestO1Evaluator(unittest.TestCase):
    """C 层守卫：msg. 前缀 + 数值常量。"""

    def test_lookup_strips_msg_prefix(self):
        self.assertEqual(_vg_lookup({"光照": "50"}, "msg.光照"), "50")
        self.assertEqual(_vg_lookup({"payload": {"state": "42"}},
                                    "msg.payload.state"), "42")
        self.assertIsNone(_vg_lookup({"光照": "50"}, "msg.其他"))

    def test_number_constant_evaluates(self):
        self.assertEqual(_vg_eval_jsonata_expr("$number(1) > 0", {}), (True, True))
        self.assertEqual(_vg_eval_jsonata_expr("$number(50) < 100", {}), (True, True))
        self.assertEqual(_vg_eval_jsonata_expr("$number(5) < 1", {}), (False, True))

    def test_msg_prefixed_jsonata_evaluates(self):
        self.assertEqual(_vg_eval_jsonata_expr("$number(msg.光照) < 10",
                                               {"光照": "5"}), (True, True))
        self.assertEqual(_vg_eval_jsonata_expr("$number(msg.光照) < 10",
                                               {"光照": "50"}), (False, True))
        self.assertEqual(_vg_eval_jsonata_expr('msg.状态 = "on"',
                                               {"状态": "on"}), (True, True))


def _vhass(*rows):
    st = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    st.areas = seed["areas"]
    st.entities = {}
    for e in seed["entities"]:
        st.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return st


class TestO1EndToEndGate(unittest.TestCase):
    """全链守卫：取值-label 分支从「未充分验证」升为「放行」，且按世界态选对分支。"""

    def setUp(self):
        os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_o1_gate_")
        self.gw = G.Gateway()
        for eid in ("light.lamp", "sensor.lumi"):
            self.gw.state.add_mapping(eid, eid)

    def _gate(self, seed_state):
        rows = (("light.lamp", "灯", "书房", "off", {}),
                ("sensor.lumi", "光照", "书房", seed_state, {}))
        return self.gw.run_staging_gate(DSL, [], vhass_store=_vhass(*rows))

    def _replayed_services(self, r):
        return [str(s) for s in (r.get("replayed_services") or [])]

    def test_dark_world_hits_on_branch_fully_verified(self):
        r = self._gate("5")  # 5 < 10 → 开灯分支
        self.assertTrue(r.get("passed"))
        self.assertEqual(r.get("verdict"), "放行",
                         "取值-label 分支应可本地求值并放行（O1 修复前为未充分验证）")
        self.assertTrue(r.get("fully_verified"))
        self.assertFalse(r.get("warnings"))
        svcs = " ".join(self._replayed_services(r))
        self.assertIn("turn_on", svcs, "应重放开灯分支")
        self.assertNotIn("turn_off", svcs, "不应重放否则分支")

    def test_bright_world_hits_else_branch(self):
        r = self._gate("50")  # 50 >= 10 → 否则分支
        self.assertTrue(r.get("passed"))
        self.assertEqual(r.get("verdict"), "放行")
        self.assertTrue(r.get("fully_verified"))
        svcs = " ".join(self._replayed_services(r))
        self.assertIn("turn_off", svcs, "应重放否则分支")
        self.assertNotIn("turn_on", svcs, "不应重放开灯分支")


if __name__ == "__main__":
    unittest.main()
