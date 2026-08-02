#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""get_flow 只读回看 + propose_dsl(strict) 硬拦 回归测试。

覆盖：
  get_flow（gateway.get_flow，只读回看已部署 flow）：
    A. 正常：返回 flow_json(nodes) + source + label + node_count。
    B. 空 flow_id → ok=False（fail-fast）。
    C. flow 不存在（NR 抛错）→ ok=False（文案提示 NR 取 flow 失败）。
    D. flow 无节点 → ok=False（可能 flow_id 拼错或空 tab）。
    E. catalog 无 meta → source=None 仍 ok=True（source 仅辅助信息，不阻断）。
  propose_dsl(strict)：
    F. strict=True 且 lint 含 warning/error → 阻断（ok=False, stage=lint_strict, strict_blocked=True）。
    G. strict=False 且 lint 含 warning → 不阻断（ok=True，lint 随回执透出）。
    H. 干净 flow：strict=True/False 都 ok=True（不误杀零告警 flow）。
"""
import os
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_getflow_strict_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp
os.environ["NR_HA_SERVER_ID"] = ""

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config, get_config
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.ha_layer import HALayer


class FakeNR:
    def __init__(self, flows=None):
        self._flows = flows or {}

    def get_flow(self, fid):
        if fid not in self._flows:
            raise KeyError(f"flow not found: {fid}")
        return self._flows[fid]

    def get_default_server_id(self):
        return "server_auto"

    def validate_flow(self, flow):
        return []


class FakeState:
    def __init__(self, meta=None):
        self._meta = meta or {}

    def get_flow_meta(self, fid):
        return self._meta.get(fid)


class FakeHA:
    def get_states(self, domain=None):
        return []

    def get_areas(self):
        return {}

    def entity_areas(self):
        return {}

    def get_state(self, eid):
        raise RuntimeError("not found")

    def call_service(self, d, s, data):
        return {"called": f"{d}.{s}"}


def make_gw(flows=None, meta=None):
    reset_config()
    cfg = get_config()
    gw = G.Gateway(config=cfg,
                   ha_layer=HALayer(config=cfg, backend=FakeHA()),
                   nr_layer=NRLayer(config=cfg, backend=FakeNR(flows=flows)))
    # 覆盖真实 SharedState，仅验证 get_flow_meta 取数（不影响 get_flow 的只读语义）
    gw.state = FakeState(meta=meta)
    return gw


class TestGetFlow(unittest.TestCase):
    def test_get_flow_ok(self):
        flows = {"abc": {"id": "abc", "label": "书房专注",
                         "nodes": [{"id": "abc", "type": "tab", "z": "abc"},
                                   {"id": "s1", "type": "switch", "z": "abc"}]}}
        gw = make_gw(flows=flows, meta={"abc": {"source": "compiler"}})
        r = gw.get_flow("abc")
        self.assertTrue(r["ok"])
        self.assertEqual(r["flow_id"], "abc")
        self.assertEqual(r["node_count"], 2)
        self.assertEqual(r["source"], "compiler")
        self.assertEqual(r["label"], "书房专注")
        self.assertEqual(len(r["flow_json"]["nodes"]), 2)

    def test_get_flow_empty_id(self):
        gw = make_gw()
        r = gw.get_flow("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "get_flow")

    def test_get_flow_not_found(self):
        gw = make_gw(flows={})
        r = gw.get_flow("missing")
        self.assertFalse(r["ok"])
        self.assertIn("NR 取 flow 失败", r["error"])

    # ── WB5#1a：注册表 ↔ NR 分叉检测 ──
    def test_get_flow_stale_when_catalog_has_meta(self):
        # 注册表仍标记该 flow 已部署，但 NR 已无此 flow（404）→ 明确 stale=True
        gw = make_gw(flows={}, meta={"gone": {"source": "compiler", "deployed_at": "x"}})
        r = gw.get_flow("gone")
        self.assertFalse(r["ok"])
        self.assertTrue(r["stale"], "注册表有记录但 NR 404 应标记 stale")
        self.assertIn("hint", r)
        self.assertIn("NR 取 flow 失败", r["error"])

    def test_get_flow_not_found_no_stale(self):
        # flow_id 本就不存在（注册表也无记录）→ ok=False 但不标 stale
        gw = make_gw(flows={}, meta={})
        r = gw.get_flow("never_existed")
        self.assertFalse(r["ok"])
        self.assertNotIn("stale", r, "注册表无记录不应误标 stale")
        self.assertIn("NR 取 flow 失败", r["error"])

    def test_get_flow_no_nodes(self):
        gw = make_gw(flows={"e": {"id": "e", "label": "空", "nodes": []}})
        r = gw.get_flow("e")
        self.assertFalse(r["ok"])
        self.assertIn("flow 无节点", r["error"])

    def test_get_flow_source_none(self):
        flows = {"x": {"id": "x", "label": "无meta",
                       "nodes": [{"id": "x", "type": "tab", "z": "x"}]}}
        gw = make_gw(flows=flows, meta={})  # catalog 无该 flow 的 meta
        r = gw.get_flow("x")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["source"])


_WARN = [{"level": "warning", "rule": "R26", "node_id": "n1",
          "message": "变量作用域不一致"}]
_CLEAN = []

# by-design：编译器为每个 flow 生成的手动测试 inject 节点（R22 warning，strict 应放行）
_R22_INJECT = [{"level": "warning", "rule": "R22", "node_id": "inj1",
                "node_type": "inject", "message": "inject 节点未配置自动触发"}]
# 非 inject 的 R22 warning（如 http request 缺 url）：真实告警，strict 应拦
_R22_HTTP = [{"level": "warning", "rule": "R22", "node_id": "h1",
              "node_type": "http request", "message": "http request url 为空"}]


class TestProposeDslStrict(unittest.TestCase):
    def _gw(self):
        gw = make_gw()
        # 闸门置为恒通过，使 strict=False 路径能跑完（本测试只关心 strict 拦不拦）
        gw.run_staging_gate = lambda *a, **k: {"passed": True, "stage": "ok"}
        return gw

    def _fake_compile(self):
        scene = SimpleNamespace(name="测试场景", expected=[])
        flow = {"nodes": [{"id": "t", "type": "tab", "z": "t"}]}
        return scene, flow

    def test_strict_blocks_on_warning(self):
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_WARN):
            r = gw.propose_dsl("场景: x", "agent1", strict=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "lint_strict")
        self.assertTrue(r["strict_blocked"])
        self.assertIn("R26", r["blocked_by"])
        self.assertEqual(r["lint_warning_count"], 1)

    def test_strict_false_passes_warning(self):
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_WARN):
            r = gw.propose_dsl("场景: x", "agent1", strict=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lint_warning_count"], 1)  # 随回执透出，不阻断

    def test_strict_true_clean_no_block(self):
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_CLEAN):
            r_strict = gw.propose_dsl("场景: x", "agent1", strict=True)
            r_loose = gw.propose_dsl("场景: x", "agent1", strict=False)
        self.assertTrue(r_strict["ok"])
        self.assertTrue(r_loose["ok"])

    # ── #501：strict 排除 by-design R22(inject 手动测试节点) ──
    def test_strict_excludes_r22_inject(self):
        # R22(inject 缺自动触发) 属编译器生成的手动测试节点，strict=True 不应误杀事件驱动自动化
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_R22_INJECT):
            r = gw.propose_dsl("场景: x", "agent1", strict=True)
        self.assertTrue(r["ok"])  # 不阻断
        self.assertNotIn("blocked_by", r)  # 成功路径根本不带 blocked_by
        # R22 仍随回执透出（透明，让人看见）
        rules = [v.get("rule") for v in r.get("lint", [])]
        self.assertIn("R22", rules)
        self.assertEqual(r["lint_warning_count"], 1)

    def test_strict_blocks_r22_non_inject(self):
        # 反向证明：非 inject 的 R22（http request 缺 url）仍是真实告警，strict 应拦
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_R22_HTTP):
            r = gw.propose_dsl("场景: x", "agent1", strict=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "lint_strict")
        self.assertIn("R22", r["blocked_by"])

    # ── #500：R_branch_required 内容触发（自由 DSL 含条件语义但产物无分支门）──
    def test_branch_required_content_trigger_blocks(self):
        # DSL 含条件语义（如果…）但编译产物无分支/条件门 → 硬拦（无论 strict 与否）
        gw = self._gw()
        scene, flow = self._fake_compile()  # 无 switch/api-current-state/time-range-switch
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_CLEAN):
            r = gw.propose_dsl("如果有人来就开灯", "agent1", strict=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "lint_branch_required")
        self.assertEqual(r["error"], "R_branch_required")
        rules = [v.get("rule") for v in r.get("lint_summary", [])]
        self.assertIn("R_branch_required", rules)

    def test_branch_required_no_cue_passes(self):
        # 反向证明：无条件的普通 flow（DSL 不含条件词）不被内容触发误拦
        gw = self._gw()
        scene, flow = self._fake_compile()
        with mock.patch("autoflow_gateway.dsl_engine.parse", return_value=scene), \
             mock.patch("autoflow_gateway.dsl_engine.compile", return_value=flow), \
             mock.patch("autoflow_gateway.gateway.lint_flow", return_value=_CLEAN):
            r = gw.propose_dsl("场景: 回家开灯", "agent1", strict=False)
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
