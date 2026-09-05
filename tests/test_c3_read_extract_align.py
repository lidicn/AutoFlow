#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C3 回归：取值→提取 路径错位修复。

旧实现 `取值 <实体> <字段>` 把实体 state 写到扁平 msg.<字段>，但 DSL 文档与黑箱 agent
都用『提取: X = payload.<字段>』/『提取: X = payload.state』读取 → 提取恒 undefined。
修复：`取值` 具名字段时同时写到 msg.payload.<字段> 与 msg.payload.state，
使下游 `提取: X = payload.state` / `提取: X = payload.<字段>` 都能解析到状态。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl


def _by_type(flow, ntype):
    return [n for n in flow["nodes"] if n["type"] == ntype]


def _read_state_props(flow):
    rs = _by_type(flow, "api-current-state")
    # 取 halt_if 为空（非门控）的 取值 节点
    rs = [n for n in rs if n.get("halt_if") == ""]
    if not rs:
        return set()
    return {o.get("property") for o in (rs[0].get("outputProperties") or [])}


class TestC3ReadExtractAlign(unittest.TestCase):
    def test_extract_via_payload_state(self):
        """取值 具名字段 + 提取 读 payload.state（报告中的 C3 原场景）。"""
        dsl = """
场景: 取值提取对齐
触发: inject
取值: sensor.x 光照
提取: 亮度值 = payload.state
"""
        flow = compile_dsl(dsl)
        props = _read_state_props(flow)
        self.assertIn("payload.光照", props, "取值必须把状态写到 payload.<字段>")
        self.assertIn("payload.state", props, "取值必须别名到 payload.state（C3 修复）")
        # 提取节点读 payload.state（与取值填充的位置对齐）
        ext = _by_type(flow, "change")
        ext = [n for n in ext if str(n.get("name", "")).startswith("提取")]
        self.assertTrue(ext, "应有 提取 节点")
        self.assertEqual(ext[0]["rules"][0]["to"], "payload.state",
                         "提取应读取 payload.state，与取值填充位置对齐")

    def test_extract_via_payload_field(self):
        """取值 字段 temperature + 提取 读 payload.temperature（黑箱常见写法）。"""
        dsl = """
场景: 取值提取字段对齐
触发: inject
取值: sensor.a temperature
提取: 温度 = payload.temperature
"""
        flow = compile_dsl(dsl)
        props = _read_state_props(flow)
        self.assertIn("payload.temperature", props,
                      "取值字段应写到 payload.temperature，使 提取 payload.temperature 可解析")

    def test_no_field_writes_payload(self):
        """无字段取值：整条 msg.payload = state（与文档'返回值在 msg.payload'一致）。"""
        dsl = """
场景: 无字段取值
触发: inject
取值: sensor.b
"""
        flow = compile_dsl(dsl)
        props = _read_state_props(flow)
        self.assertIn("payload", props, "无字段取值应把状态写到 msg.payload")


if __name__ == "__main__":
    unittest.main()
