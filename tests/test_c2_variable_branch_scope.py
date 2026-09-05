#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 回归：变量↔分支 作用域错配修复。

`变量: X = v` 写入 flow 上下文（`pt:"flow"`），此前 `分支: X = ...` 却从 msg 上下文读取
（propertyType:"msg"），导致变量分支永远读不到值、恒走 else。
修复：分支 LHS 命中场景变量时，switch 改读 flow 上下文。
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


class TestC2VariableBranchScope(unittest.TestCase):
    def test_variable_branch_reads_flow_context(self):
        dsl = """
场景: 变量分支作用域
触发: inject
变量: 压测开关 = 'on'
分支: 压测开关 = 'on'
    动作: light.turn_on(light.x)
否则:
    动作: light.turn_off(light.x)
"""
        flow = compile_dsl(dsl)
        change = _by_type(flow, "change")
        self.assertEqual(len(change), 1, "应设置变量 change 节点")
        self.assertEqual(change[0]["rules"][0]["pt"], "flow",
                         "变量必须写入 flow 上下文")

        sw = _by_type(flow, "switch")
        self.assertEqual(len(sw), 1, "应有一个分支 switch")
        self.assertEqual(sw[0]["property"], "压测开关")
        self.assertEqual(sw[0]["propertyType"], "flow",
                         "分支 LHS 命中变量时应改读 flow 上下文，而非 msg")

    def test_msg_field_branch_still_reads_msg(self):
        """对照组：分支引用 msg 字段（取值产出的字段，非变量）仍读 msg 上下文。"""
        dsl = """
场景: 取值分支作用域
触发: inject
取值: light.desk state
分支: state = 'off'
    动作: light.turn_on(light.desk)
否则:
    动作: light.turn_off(light.desk)
"""
        flow = compile_dsl(dsl)
        sw = _by_type(flow, "switch")
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw[0]["property"], "state")
        self.assertEqual(sw[0]["propertyType"], "msg",
                         "非变量字段(取值标签)分支应读 msg 根; 取值经桥接 change 落 msg.<field>")


if __name__ == "__main__":
    unittest.main()
