#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK_tab_enabled_state 验收单测：查看/启停 tab 流程（AC1-AC12）。

用 FakeNR 注入 type:tab / disabled / z 节点，跑 list_tabs / get_flow / set_tab_state
全链路，覆盖：
  AC1  tab 字段+节点数降序
  AC2  count_disabled
  AC3  only_disabled 过滤
  AC4  get_flow 返回 disabled 与 list_tabs 一致
  AC5  仅 type:tab（剔除 subflow/config）；label 空回退「未命名」
  AC7/AC10  set_tab_state_submit 落待确认（pending_id / needs_approval）；unknown flow_id→unknown=True
  AC8  批准后真正翻 NR tab.disabled（启/禁双向）
  AC9  节点 payload 字节不变（启→禁→启 往返）
  AC11  写工具仅 _DEPLOY_KNIVES（black 隐藏）；读工具三面板通用
  AC12  禁用核心 tab 拦截；启用核心 tab 放行
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import tempfile
import autoflow_gateway.config as cfgmod
from autoflow_gateway.config import set_feature_flag
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.ha_layer import HALayer
from autoflow_gateway.nr_layer import NRLayer
from autoflow_gateway.defense import DefenseLayer


def _make_gateway(tabs):
    """构造 Gateway：nr_layer 包一层 NRLayer（与生产一致，提供 .client 与读写透传）。"""
    tmp = tempfile.mkdtemp(prefix="af_tab_")
    os.environ["AUTOFLLOW_DATA_DIR"] = tmp
    cfg = cfgmod.get_config()
    gw = Gateway(
        config=cfg,
        ha_layer=HALayer(config=cfg, backend=_FakeHA()),
        nr_layer=NRLayer(config=cfg, backend=_TabFakeNR(tabs)),
    )
    return gw


class _FakeHA:
    """set_tab_state 路径不依赖 HA，给个最小 stub。"""
    def get_states(self, domain=None):
        return []
    def call_service(self, domain, service, data):
        return {"ok": True, "noop": True}


class _TabFakeNR:
    """模拟 Node-RED：扁平 flows 数组，tab + 子节点（z 指向 tab id）。"""
    def __init__(self, items):
        # items 可能混有非 tab 节点（subflow/config），按 type 分流
        self._tabs = [dict(t) for t in items if t.get("type") == "tab"]
        self._extra = [dict(t) for t in items if t.get("type") != "tab"]
        self._by_id = {t["id"]: t for t in self._tabs}
        self.puts = []          # 记录 put_flow_raw 调用
        self._server = "server_x"

    # ── GET /flows 扁平数组 ──
    def list_flows(self):
        out = []
        for t in self._tabs:
            out.append({"id": t["id"], "type": "tab",
                        "label": t.get("label", ""), "disabled": t["disabled"]})
            for n in t.get("nodes", []):
                out.append(dict(n))
        for e in self._extra:
            out.append(dict(e))
        return out

    # ── GET /flow/{id} ──
    def get_flow(self, fid):
        if fid in self._by_id:
            t = self._by_id[fid]
            return {"id": t["id"], "type": "tab", "label": t.get("label", ""),
                    "disabled": t["disabled"], "nodes": list(t.get("nodes", []))}
        raise RuntimeError(f"404 flow {fid}")

    # ── PUT /flow/{id} 直写（仅 set_tab_state_execute 用）──
    def put_flow_raw(self, fid, flow_data):
        self.puts.append((fid, flow_data))
        if fid in self._by_id:
            t = self._by_id[fid]
            t["disabled"] = bool(flow_data.get("disabled", False))
            t["nodes"] = list(flow_data.get("nodes", []))
            return {"id": fid, "raw": {"ok": True}}
        raise RuntimeError(f"404 flow {fid}")

    def _get_default_server(self):
        return self._server


def _sample_tabs():
    return [
        {"id": "tab_living", "type": "tab", "label": "客厅语音播报", "disabled": False, "nodes": [
            {"id": "n1", "type": "inject", "z": "tab_living"},
            {"id": "n2", "type": "api-current-state", "z": "tab_living"},
            {"id": "n3", "type": "switch", "z": "tab_living"},
        ]},
        {"id": "tab_bedroom", "type": "tab", "label": "卧室灯", "disabled": True, "nodes": [
            {"id": "n4", "type": "inject", "z": "tab_bedroom"},
        ]},
        {"id": "tab_empty", "type": "tab", "label": "", "disabled": False, "nodes": []},
        # 非 tab 节点：应被 list_tabs 剔除
        {"id": "sub_flow_a", "type": "subflow", "name": "某子流程", "disabled": False},
        {"id": "cfg_mqtt", "type": "mqtt-broker", "name": "MQTT", "disabled": False, "z": "tab_living"},
        # 核心受保护 tab（label 含 core，AC12）
        {"id": "core_heartbeat", "type": "tab", "label": "core 心跳桥接", "disabled": False, "nodes": [
            {"id": "n5", "type": "inject", "z": "core_heartbeat"},
        ]},
    ]


class TestListTabs(unittest.TestCase):
    def setUp(self):
        # 清 list_tabs 模块级缓存（类属性，防跨用例 TTL 串味）
        Gateway._tab_list_cache = {"ts": 0.0, "data": None}
        self.gw = _make_gateway(_sample_tabs())

    def test_ac1_fields_and_sort(self):
        r = self.gw.list_tabs()
        self.assertTrue(r["ok"])
        tabs = r["tabs"]
        # 4 个真实 tab（剔除 1 subflow + 1 config）：living/bedroom/empty/core
        self.assertEqual(len(tabs), 4)
        for t in tabs:
            self.assertIn("id", t)
            self.assertIn("label", t)
            self.assertIn("disabled", t)
            self.assertIn("node_count", t)
            self.assertIn("source", t)
        # 按节点数降序：客厅(3 inject+1 config=4) > 卧室(1) = core(1) > 未命名(0)
        counts = [t["node_count"] for t in tabs]
        self.assertEqual(counts, [4, 1, 1, 0])
        self.assertEqual(tabs[0]["id"], "tab_living")

    def test_ac2_count_disabled(self):
        r = self.gw.list_tabs()
        # 仅卧室(初始 disabled=True) → count_disabled=1
        self.assertEqual(r["count_disabled"], 1)

    def test_ac3_only_disabled(self):
        r = self.gw.list_tabs(only_disabled=True)
        self.assertEqual(len(r["tabs"]), 1)
        self.assertEqual(r["tabs"][0]["id"], "tab_bedroom")
        self.assertTrue(r["tabs"][0]["disabled"])

    def test_ac3_keyword(self):
        r = self.gw.list_tabs(keyword="客厅")
        self.assertEqual([t["id"] for t in r["tabs"]], ["tab_living"])
        r2 = self.gw.list_tabs(keyword="bed")
        self.assertEqual([t["id"] for t in r2["tabs"]], ["tab_bedroom"])

    def test_ac5_only_tab_and_unnamed_fallback(self):
        r = self.gw.list_tabs()
        ids = [t["id"] for t in r["tabs"]]
        self.assertNotIn("sub_flow_a", ids)   # subflow 剔除
        self.assertNotIn("cfg_mqtt", ids)      # config 剔除
        # 空 label 回退「未命名」
        unnamed = [t for t in r["tabs"] if t["id"] == "tab_empty"][0]
        self.assertEqual(unnamed["label"], "未命名")


class TestGetFlowDisabled(unittest.TestCase):
    def setUp(self):
        Gateway._tab_list_cache = {"ts": 0.0, "data": None}
        self.gw = _make_gateway(_sample_tabs())

    def test_ac4_disabled_consistent_with_list(self):
        lt = self.gw.list_tabs()
        by_id = {t["id"]: t for t in lt["tabs"]}
        for tid in ("tab_living", "tab_bedroom"):
            gf = self.gw.get_flow(tid)
            self.assertTrue(gf["ok"])
            self.assertIn("disabled", gf)
            self.assertEqual(gf["disabled"], by_id[tid]["disabled"])

    def test_ac4_empty_tab_get_flow_error(self):
        # tab_empty 无节点 → get_flow 返回错误（禁用信息由 list_tabs 提供）
        gf = self.gw.get_flow("tab_empty")
        self.assertFalse(gf["ok"])


class TestSetTabState(unittest.TestCase):
    def setUp(self):
        Gateway._tab_list_cache = {"ts": 0.0, "data": None}
        self.gw = _make_gateway(_sample_tabs())

    def test_ac7_ac10_submit_pending_and_unknown(self):
        r = self.gw.set_tab_state_submit("tab_living", enabled=False, agent_id="agt_x")
        self.assertTrue(r["ok"])
        self.assertTrue(r["needs_approval"])
        self.assertIn("pending_id", r)
        # 待确认项真进了确认闸队列
        ops = self.gw.confirm.list_pending()
        self.assertTrue(any(o.id == r["pending_id"] for o in ops))

        # unknown flow_id → unknown=True，不落幽灵待确认
        bad = self.gw.set_tab_state_submit("no_such_flow", enabled=False, agent_id="agt_x")
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["unknown"])

    def test_ac8_disable_and_enable_roundtrip(self):
        fid = "tab_living"
        # 禁用
        sub = self.gw.set_tab_state_submit(fid, enabled=False, agent_id="agt_x")
        self.gw.approve(sub["pending_id"], "human")
        self.assertTrue(self.gw.nr.client._tabs[0]["disabled"])
        # 启用
        sub2 = self.gw.set_tab_state_submit(fid, enabled=True, agent_id="agt_x")
        self.gw.approve(sub2["pending_id"], "human")
        self.assertFalse(self.gw.nr.client._tabs[0]["disabled"])

    def test_ac9_node_bytes_unchanged(self):
        fid = "tab_bedroom"
        before = list(self.gw.nr.client._tabs[1]["nodes"])
        sub = self.gw.set_tab_state_submit(fid, enabled=False, agent_id="agt_x")
        self.gw.approve(sub["pending_id"], "human")
        # 节点内容原样（直写 put_flow_raw，跳过 _normalize_flow）
        after_disable = self.gw.nr.client._tabs[1]["nodes"]
        self.assertEqual(after_disable, before)
        # 再启用回原
        sub2 = self.gw.set_tab_state_submit(fid, enabled=True, agent_id="agt_x")
        self.gw.approve(sub2["pending_id"], "human")
        self.assertEqual(self.gw.nr.client._tabs[1]["nodes"], before)

    def test_ac9_put_flow_raw_called_with_full_nodes(self):
        fid = "tab_living"
        before_puts = len(self.gw.nr.client.puts)
        sub = self.gw.set_tab_state_submit(fid, enabled=False, agent_id="agt_x")
        self.gw.approve(sub["pending_id"], "human")
        self.assertGreater(len(self.gw.nr.client.puts), before_puts)
        _, payload = self.gw.nr.client.puts[-1]
        self.assertEqual(payload["id"], fid)
        self.assertIn("nodes", payload)  # 节点原样回写
        self.assertTrue(payload["disabled"])


class TestProtectedTab(unittest.TestCase):
    def setUp(self):
        Gateway._tab_list_cache = {"ts": 0.0, "data": None}
        self.gw = _make_gateway(_sample_tabs())

    def test_ac12_disable_core_blocked(self):
        r = self.gw.set_tab_state_submit("core_heartbeat", enabled=False, agent_id="agt_x")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("protected"))
        # 确认闸里没落这个待确认项
        self.assertFalse(any(o.operation == "set_tab_state"
                             and o.payload.get("flow_id") == "core_heartbeat"
                             for o in self.gw.confirm.list_pending()))

    def test_ac12_enable_core_allowed(self):
        # 启用核心 tab 不受限
        r = self.gw.set_tab_state_submit("core_heartbeat", enabled=True, agent_id="agt_x")
        self.assertTrue(r["ok"])
        self.assertTrue(r["needs_approval"])


class TestToolVisibility(unittest.TestCase):
    def test_ac11_black_hidden_write_tool_only(self):
        import autoflow_gateway.mcp_server as ms
        # 写工具在 _DEPLOY_KNIVES（black 隐藏，admin/原生手写可见）
        self.assertIn("autoflow_set_tab_state", ms._DEPLOY_KNIVES)
        # 读工具三面板通用，不在 _DEPLOY_KNIVES
        self.assertNotIn("autoflow_list_tabs", ms._DEPLOY_KNIVES)
        # 两个工具都已注册到 mcp 服务器
        self.assertTrue(hasattr(ms, "autoflow_list_tabs"))
        self.assertTrue(hasattr(ms, "autoflow_set_tab_state"))


if __name__ == "__main__":
    unittest.main()
