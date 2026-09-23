#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-01 回归：fix-nodes 修复表达式安全求值（替代 exec/compile，杜绝代码注入）。

锁死：
  · 合法表达式（字面量 / 容器 / 基础算术）可安全写入节点 dict；
  · 任何含调用 / 导入 / 越界目标 / 读取的表达式必须被 ValueError 拒绝，绝不执行。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.lib.nr_client import _safe_apply_fix_expr, _safe_fix_value


class TestSafeFixExpr(unittest.TestCase):
    def test_benign_subscript_literal(self):
        n = {"type": "x"}
        _safe_apply_fix_expr('n["outputs"] = 1', n)
        self.assertEqual(n["outputs"], 1)

    def test_benign_nested_container_and_arith(self):
        n = {}
        _safe_apply_fix_expr('n["cfg"] = {"a": [1, 2, 3], "b": 2 + 3}', n)
        self.assertEqual(n["cfg"], {"a": [1, 2, 3], "b": 5})

    def test_benign_attribute_assignment(self):
        n = {}
        _safe_apply_fix_expr('n.active = True', n)
        self.assertIs(n["active"], True)

    def test_malicious_rejected(self):
        evils = [
            '__import__("os").system("echo pwned")',
            'open("/etc/passwd").read()',
            'n["x"] = __import__("os").environ',
            'exec("x")',
            'n = 1',                       # 目标不是 n[...]/n.x
            'y["x"] = 1',                  # 目标不是 n
            'n["x"] = n["y"]',             # 值含下标读取
            'n["x"] = (lambda: 1)()',      # 调用
        ]
        for evil in evils:
            with self.subTest(evil=evil):
                with self.assertRaises(ValueError):
                    _safe_apply_fix_expr(evil, {})

    def test_safe_value_rejects_call_and_attr(self):
        import ast
        with self.assertRaises(ValueError):
            _safe_fix_value(ast.parse("__import__('os')", mode="eval").body)
        with self.assertRaises(ValueError):
            _safe_fix_value(ast.parse("foo.bar", mode="eval").body)


if __name__ == "__main__":
    unittest.main()
