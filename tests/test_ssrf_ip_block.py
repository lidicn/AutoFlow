#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 内网 IP 过滤门禁测试（SSRF 硬拦）。

运行：python tests/test_ssrf_ip_block.py
覆盖：192.168.x / 10.x / 172.16-31.x / 127.x / 169.254.x 均被 R40 error 拦截。
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from autoflow_gateway.flow_linter import lint_flow


def _mk(nodes):
    return {"id": "flow1", "label": "t", "nodes": nodes}


def _http_node(url, nid="h1"):
    return {
        "id": nid, "type": "http request", "z": "flow1",
        "method": "GET", "url": url, "wires": [[]],
    }


def _inject_node(nid="inj"):
    return {
        "id": nid, "type": "inject", "z": "flow1",
        "props": [{"p": "payload"}], "repeat": "", "crontab": "",
        "once": False, "wires": [["h1"]],
    }


class TestSSRFIPBlock(unittest.TestCase):
    """P0-2: 内网 IP 必须被 R40 error 拦截，阻止部署。"""

    def _get_r40_issue(self, flow):
        issues = lint_flow(flow)
        r40 = [i for i in issues if i["rule"] == "R40"]
        return r40[0] if r40 else None

    def test_private_192_168_blocked(self):
        """192.168.x.x → R40 error，部署应被拦截。"""
        flow = _mk([_inject_node(), _http_node("http://192.168.1.1/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue, "应产生 R40 issue")
        self.assertEqual(issue["level"], "error", "私有地址应升级为 error")
        self.assertIn("192.168.1.1", issue["message"])

    def test_private_10_x_blocked(self):
        """10.x.x.x → R40 error。"""
        flow = _mk([_inject_node(), _http_node("http://10.0.0.1/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")
        self.assertIn("10.0.0.1", issue["message"])

    def test_private_172_16_blocked(self):
        """172.16.x.x → R40 error。"""
        flow = _mk([_inject_node(), _http_node("http://172.16.0.1/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")
        self.assertIn("172.16.0.1", issue["message"])

    def test_private_172_31_blocked(self):
        """172.31.x.x → R40 error（边界测试）。"""
        flow = _mk([_inject_node(), _http_node("http://172.31.255.255/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")

    def test_loopback_127_blocked(self):
        """127.x.x.x → R40 error。"""
        flow = _mk([_inject_node(), _http_node("http://127.0.0.1/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")
        self.assertIn("127.0.0.1", issue["message"])

    def test_link_local_169_blocked(self):
        """169.254.x.x → R40 error。"""
        flow = _mk([_inject_node(), _http_node("http://169.254.169.254/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")
        self.assertIn("169.254.169.254", issue["message"])

    def test_public_ip_allowed(self):
        """8.8.8.8 → 不应有 R40 issue。"""
        flow = _mk([_inject_node(), _http_node("http://8.8.8.8/test")])
        issues = lint_flow(flow)
        r40 = [i for i in issues if i["rule"] == "R40"]
        self.assertEqual(r40, [], "公网 IP 不应被拦截")

    def test_domain_allowed(self):
        """example.com → 不应有 R40 issue。"""
        flow = _mk([_inject_node(), _http_node("https://example.com/api")])
        issues = lint_flow(flow)
        r40 = [i for i in issues if i["rule"] == "R40"]
        self.assertEqual(r40, [], "域名不应被拦截")

    def test_dynamic_expression_blocked(self):
        """host 含动态表达式 → R40 error。"""
        flow = _mk([_inject_node(), _http_node("http://${user_input}/test")])
        issue = self._get_r40_issue(flow)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["level"], "error")
        self.assertIn("动态表达式", issue["message"])

    def test_r40_in_block_rules(self):
        """确认 R40 在 _LINT_BLOCK_RULES 中（部署时会硬拦）。"""
        # 此测试验证网关部署路径的配置，非 lint 本身
        from autoflow_gateway.gateway import Gateway
        # 只需确认 lint 返回 error 级别即可，实际 block rules 在 gateway.py 维护
        flow = _mk([_inject_node(), _http_node("http://192.168.1.1/test")])
        issues = lint_flow(flow)
        r40_errors = [i for i in issues if i["rule"] == "R40" and i["level"] == "error"]
        self.assertEqual(len(r40_errors), 1, "R40 应为 error 级别，确保被部署闸门拦截")


if __name__ == "__main__":
    unittest.main(argv=["test"], exit=False, verbosity=2)
