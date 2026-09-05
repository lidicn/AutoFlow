"""子流程端口线归一化回归测试（#670）。

根因：经 MCP autoflow_create_subflow 提交的 agent 常把子流程端口误写成「流节点语法」
（把端口当普通节点连线），例如 in/out 端口的 wires 写成 "fn1" / ["fn1"] / [["fn1"]]，
而 NR 5.x 子流程端口要求对象格式 {"id": <node>, "port": <n>}（输出端口必带 port）。
网关原样透传 → 端口实际未连接、子流程输入/输出悬空。

本测试验证 build_subflow_entries 在组装时把端口 wires 归一化为标准对象格式，
且已正确的对象格式原样保留。
"""
import unittest

from autoflow_gateway.lib.nr_client import NodeRedClient


class TestSubflowPortWireNormalization(unittest.TestCase):
    def setUp(self):
        self.nc = NodeRedClient()

    def _build(self, in_ports, out_ports, nodes=None):
        entries = self.nc.build_subflow_entries(
            "sf_test", "测试子流程", in_ports, out_ports, nodes or [])
        return entries[0]  # def entry

    def test_string_array_wires_normalized(self):
        """端口 wires 写成 [["fn1"]]（流节点语法）→ 标准对象格式。"""
        sf = self._build(
            in_ports=[{"x": 40, "y": 60, "wires": [["fn1"]]}],
            out_ports=[{"x": 260, "y": 60, "wires": [["fn1"]]}],
        )
        self.assertEqual(sf["in"][0]["wires"], [{"id": "fn1"}])
        self.assertEqual(sf["out"][0]["wires"], [{"id": "fn1", "port": 0}])

    def test_bare_string_wires_normalized(self):
        """端口 wires 写成单字符串 "fn1" → 对象格式。"""
        sf = self._build(
            in_ports=[{"wires": "fn1"}],
            out_ports=[{"wires": "fn1"}],
        )
        self.assertEqual(sf["in"][0]["wires"], [{"id": "fn1"}])
        self.assertEqual(sf["out"][0]["wires"], [{"id": "fn1", "port": 0}])

    def test_single_object_wires_normalized(self):
        """端口 wires 写成单对象 {"id":"fn1"} → 输出端口补 port=0。"""
        sf = self._build(
            in_ports=[{"wires": {"id": "fn1"}}],
            out_ports=[{"wires": {"id": "fn1"}}],
        )
        self.assertEqual(sf["in"][0]["wires"], [{"id": "fn1"}])
        self.assertEqual(sf["out"][0]["wires"], [{"id": "fn1", "port": 0}])

    def test_correct_object_form_preserved(self):
        """已是标准对象格式 → 原样保留，不做多余改动。"""
        sf = self._build(
            in_ports=[{"x": 40, "y": 40, "wires": [{"id": "n_parse"}]}],
            out_ports=[{"x": 40, "y": 120, "wires": [{"id": "n_calc", "port": 0}]}],
        )
        self.assertEqual(sf["in"][0]["wires"], [{"id": "n_parse"}])
        self.assertEqual(sf["out"][0]["wires"], [{"id": "n_calc", "port": 0}])

    def test_internal_node_wires_untouched(self):
        """内部节点自身的 wires 不应被改动（末节点空 wires 是合法写法）。"""
        nodes = [{"id": "fn1", "type": "function", "outputs": 1, "wires": [[]], "z": ""}]
        entries = self.nc.build_subflow_entries(
            "sf_test", "测试子流程",
            [{"wires": [["fn1"]]}], [{"wires": [["fn1"]]}], nodes)
        internals = entries[1:]
        fn1 = [n for n in internals if n["id"] == "fn1"][0]
        # build_subflow_entries 仅给内部节点补 z，不动 wires
        self.assertEqual(fn1["wires"], [[]], "内部节点 wires 被意外改动")
        self.assertEqual(fn1["z"], "sf_test", "内部节点 z 应被设为子流程 id")

    def test_multi_output_ports(self):
        """多输出端口：各自带 port 序号。"""
        sf = self._build(
            in_ports=[{"wires": [["fn1"]]}],
            out_ports=[
                {"wires": [["fn1"]]},
                {"wires": [["fn2"]]},
            ],
        )
        self.assertEqual(sf["out"][0]["wires"], [{"id": "fn1", "port": 0}])
        self.assertEqual(sf["out"][1]["wires"], [{"id": "fn2", "port": 0}])


if __name__ == "__main__":
    unittest.main()
