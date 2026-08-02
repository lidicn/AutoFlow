#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autoflow_get_skill 只读自愈工具：路径安全 + 内容返回 + 失败分支。"""
import os
import sys
import json
import tempfile
import types
import unittest

sys.path.insert(0, "src")
import autoflow_gateway.mcp_server as m


def _make_gw(skills_dir: str):
    cfg = types.SimpleNamespace(skills_dir=skills_dir)
    return types.SimpleNamespace(cfg=cfg)


class TestSkillSelfHeal(unittest.TestCase):
    def test_get_skill_ok(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "autoflow.md"), "w", encoding="utf-8") as f:
            f.write("# autoflow\n实体必须真实。")
        m._gw = lambda: _make_gw(d)
        out = json.loads(m.autoflow_get_skill("autoflow"))
        self.assertTrue(out["ok"], out)
        self.assertIn("实体必须真实", out["content"])
        self.assertEqual(out["name"], "autoflow")
        self.assertGreater(out["bytes"], 0)

    def test_get_skill_not_found(self):
        d = tempfile.mkdtemp()
        m._gw = lambda: _make_gw(d)
        out = json.loads(m.autoflow_get_skill("autoflow-missing"))
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["error"])

    def test_get_skill_invalid_name(self):
        d = tempfile.mkdtemp()
        m._gw = lambda: _make_gw(d)
        for bad in ("", "../etc", "a/b", "con.txt"):
            out = json.loads(m.autoflow_get_skill(bad))
            self.assertFalse(out["ok"], (bad, out))

    def test_get_skill_unconfigured(self):
        m._gw = lambda: _make_gw("")
        out = json.loads(m.autoflow_get_skill("autoflow"))
        self.assertFalse(out["ok"])
        self.assertIn("skills_dir", out["error"])


if __name__ == "__main__":
    unittest.main()
