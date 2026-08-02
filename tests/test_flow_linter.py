#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A1 Flow Linter 单元测试（纯静态，不触真实 HA/NR）。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.flow_linter import lint_flow


def _mk(nodes):
    return {"id": "flow1", "label": "t", "nodes": nodes}


class TestR1SwitchDeadCode(unittest.TestCase):
    def test_else_before_positive_dead_code(self):
        """还原 ArcFace 原 bug：rules=[eq false, else, eq true] + checkall=false → 死代码。"""
        flow = _mk([{
            "id": "sw1", "type": "switch", "z": "flow1", "property": "payload.faces[0].matched",
            "propertyType": "jsonata", "checkall": False, "outputs": 3,
            "rules": [
                {"t": "eq", "v": "false", "vt": "bool"},
                {"t": "else"},
                {"t": "eq", "v": "true", "vt": "bool"},
            ],
            "wires": [[], [], []],
        }])
        issues = lint_flow(flow)
        r1 = [i for i in issues if i["rule"] == "R1" and i["level"] == "error"]
        self.assertEqual(len(r1), 1, issues)
        self.assertIn("死代码", r1[0]["message"])

    def test_else_last_ok(self):
        flow = _mk([{
            "id": "sw1", "type": "switch", "z": "flow1", "property": "payload.x",
            "checkall": False, "outputs": 2,
            "rules": [{"t": "eq", "v": "on", "vt": "str"}, {"t": "else"}],
            "wires": [[], []],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R1"], [])

    def test_else_first_but_checkall_true_is_warning(self):
        flow = _mk([{
            "id": "sw1", "type": "switch", "z": "flow1", "property": "payload.x",
            "checkall": True, "outputs": 2,
            "rules": [{"t": "else"}, {"t": "eq", "v": "on", "vt": "str"}],
            "wires": [[], []],
        }])
        r1 = [i for i in lint_flow(flow) if i["rule"] == "R1"]
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["level"], "warning")


class TestR2FunctionPath(unittest.TestCase):
    def test_function_reads_payload_object_warns(self):
        flow = _mk([{
            "id": "fn1", "type": "function", "z": "flow1",
            "func": "var f = msg.payload.object.faces[0]; return msg;",
            "outputs": 1, "wires": [[]],
        }])
        issues = lint_flow(flow)
        r2 = [i for i in issues if i["rule"] == "R2"]
        # 至少有一条 info（黑箱提示）+ 一条 warning（payload.object 反模式）
        self.assertTrue(any(i["level"] == "info" for i in r2))
        self.assertTrue(any(i["level"] == "warning" and "object" in i["message"] for i in r2))

    def test_function_flat_path_no_object_warning(self):
        flow = _mk([{
            "id": "fn1", "type": "function", "z": "flow1",
            "func": "var f = msg.payload.faces[0].matched; return msg;",
            "outputs": 1, "wires": [[]],
        }])
        issues = lint_flow(flow)
        r2 = [i for i in issues if i["rule"] == "R2" and i["level"] == "warning"
              and "object" in i["message"]]
        self.assertEqual(r2, [])


class TestR3HttpBody(unittest.TestCase):
    def test_http_json_no_upstream_setter_warns(self):
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["http1"]]},
            {"id": "http1", "type": "http request", "z": "flow1", "method": "POST",
             "bodyType": "json", "url": "http://x/api", "wires": [[]]},
        ])
        r3 = [i for i in lint_flow(flow) if i["rule"] == "R3"]
        self.assertEqual(len(r3), 1)

    def test_http_json_with_upstream_change_ok(self):
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload", "pt": "msg",
                        "to": "{\"a\":1}", "tot": "json"}],
             "wires": [["http1"]]},
            {"id": "http1", "type": "http request", "z": "flow1", "method": "POST",
             "bodyType": "json", "url": "http://x/api", "wires": [[]]},
        ])
        r3 = [i for i in lint_flow(flow) if i["rule"] == "R3"]
        self.assertEqual(r3, [])

    def test_http_get_no_body_ok(self):
        flow = _mk([
            {"id": "http1", "type": "http request", "z": "flow1", "method": "GET",
             "url": "http://x/", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R3"], [])

    def test_http_json_with_upstream_perproperty_setter_ok(self):
        # 网关 http_api 内联 setter 逐属性 set 到 payload.<field>（非整 payload），
        # 仍应识别为已构造请求体，不应误报 R3。
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1", "wires": [["ch1"]]},
            {"id": "ch1", "type": "change", "z": "flow1",
             "rules": [
                 {"t": "set", "p": "payload.prompt", "pt": "msg", "to": "一只猫", "tot": "jsonata"},
                 {"t": "set", "p": "payload.image", "pt": "msg", "to": "http://x/cat.jpg", "tot": "jsonata"},
             ],
             "wires": [["http1"]]},
            {"id": "http1", "type": "http request", "z": "flow1", "method": "POST",
             "bodyType": "json", "url": "http://x/vision", "wires": [[]]},
        ])
        r3 = [i for i in lint_flow(flow) if i["rule"] == "R3"]
        self.assertEqual(r3, [])


class TestR4Jsonata(unittest.TestCase):
    def test_broken_jsonata_warns(self):
        flow = _mk([{
            "id": "ch1", "type": "change", "z": "flow1",
            "rules": [{"t": "set", "p": "payload.x", "pt": "jsonata",
                       "to": "payload.(", "tot": "jsonata"}],
            "wires": [[]],
        }])
        r4 = [i for i in lint_flow(flow) if i["rule"] == "R4"]
        self.assertEqual(len(r4), 1)

    def test_valid_jsonata_passes(self):
        flow = _mk([{
            "id": "ch1", "type": "change", "z": "flow1",
            "rules": [{"t": "set", "p": "payload.x", "pt": "jsonata",
                       "to": "payload.faces[0].matched", "tot": "jsonata"}],
            "wires": [[]],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R4"], [])


class TestR10WireStructure(unittest.TestCase):
    def test_dangling_wire_reference(self):
        """R17：wires 指向不存在的节点 id（悬空连线）。（原 R10a 已拆为独立规则 R17）"""
        flow = _mk([
            {"id": "n1", "type": "inject", "z": "flow1", "wires": [["ghost_node"]]},
        ])
        r17 = [i for i in lint_flow(flow) if i["rule"] == "R17"]
        self.assertEqual(len(r17), 1)
        self.assertEqual(r17[0]["level"], "error")
        self.assertIn("不存在", r17[0]["message"])
        # 不应再误报为 R10（R10 现专指单 output 多数组误用）
        r10 = [i for i in lint_flow(flow) if i["rule"] == "R10"]
        self.assertEqual(r10, [])

    def test_dangling_to_existing_link_in_is_ok(self):
        """R17 豁免：wires 指向同一 flow 内真实存在的 link in 节点 → 不误报。"""
        flow = _mk([
            {"id": "n1", "type": "inject", "z": "flow1", "wires": [["lin"]]},
            {"id": "lin", "type": "link in", "z": "flow1", "wires": [[]]},
        ])
        r17 = [i for i in lint_flow(flow) if i["rule"] == "R17"]
        self.assertEqual(r17, [])

    def test_single_output_dual_array_error(self):
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "wires": [["light_node"], ["tts_node"]]},
            {"id": "light_node", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "tts_node", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r10 = [i for i in lint_flow(flow) if i["rule"] == "R10"]
        errs = [i for i in r10 if i["level"] == "error" and "单 output" in i["message"]]
        self.assertEqual(len(errs), 1)

    def test_single_output_multi_target_ok(self):
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "wires": [["light_node", "tts_node"]]},
            {"id": "light_node", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "tts_node", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r10 = [i for i in lint_flow(flow) if i["rule"] == "R10"]
        errs = [i for i in r10 if "单 output" in i.get("message", "")]
        self.assertEqual(errrs := errs, [])

    def test_switch_multi_output_ok(self):
        """switch 是真多 output 节点，多 wires 数组是正常的。"""
        flow = _mk([
            {"id": "sw", "type": "switch", "z": "flow1", "outputs": 2,
             "rules": [{"t": "eq", "v": "on"}, {"t": "else"}],
             "wires": [["on_node"], ["off_node"]]},
            {"id": "on_node", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "off_node", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r10 = [i for i in lint_flow(flow) if i["rule"] == "R10"]
        errs = [i for i in r10 if "单 output" in i.get("message", "")]
        self.assertEqual(errs, [])


class TestR11EntityIdFormat(unittest.TestCase):
    def test_valid_entity_id_passes(self):
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "entityId": ["light.living_room"],
             "wires": [[]]},
        ])
        r11 = [i for i in lint_flow(flow) if i["rule"] == "R11"]
        self.assertEqual(r11, [])

    def test_invalid_entity_id_warns(self):
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "entityId": ["Light.Living Room"],
             "wires": [[]]},
        ])
        r11 = [i for i in lint_flow(flow) if i["rule"] == "R11"]
        self.assertEqual(len(r11), 1)
        self.assertIn("格式", r11[0]["message"])

    def test_placeholder_entity_id_passes(self):
        flow = _mk([
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "entityId": ["REPLACE_WITH_HA_SERVER"],
             "wires": [[]]},
        ])
        r11 = [i for i in lint_flow(flow) if i["rule"] == "R11"]
        self.assertEqual(r11, [])

    def test_server_state_changed_entity_warns(self):
        flow = _mk([
            {"id": "trg", "type": "server-state-changed", "z": "flow1",
             "entities": {"entity": ["InvalidEntityName"]},
             "wires": [[]]},
        ])
        r11 = [i for i in lint_flow(flow) if i["rule"] == "R11"]
        self.assertEqual(len(r11), 1)


class TestR12MissingZ(unittest.TestCase):
    def test_node_without_z_warns(self):
        flow = _mk([
            {"id": "n1", "type": "inject", "wires": [[]]},
        ])
        r12 = [i for i in lint_flow(flow) if i["rule"] == "R12"]
        self.assertEqual(len(r12), 1)
        self.assertIn("z", r12[0]["message"])

    def test_node_with_z_passes(self):
        flow = _mk([
            {"id": "n1", "type": "inject", "z": "flow1", "wires": [[]]},
        ])
        r12 = [i for i in lint_flow(flow) if i["rule"] == "R12"]
        self.assertEqual(r12, [])

    def test_global_config_without_z_ok(self):
        flow = _mk([
            {"id": "srv", "type": "server", "name": "HA"},
            {"id": "cfg", "type": "global-config"},
        ])
        r12 = [i for i in lint_flow(flow) if i["rule"] == "R12"]
        self.assertEqual(r12, [])


class TestR16DuplicateIds(unittest.TestCase):
    def test_duplicate_id_detected(self):
        flow = _mk([
            {"id": "dup", "type": "function", "z": "flow1", "wires": [[]]},
            {"id": "dup", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r16 = [i for i in lint_flow(flow) if i["rule"] == "R16"]
        self.assertEqual(len(r16), 1)
        self.assertEqual(r16[0]["level"], "error")
        self.assertIn("重复节点 id", r16[0]["message"])

    def test_unique_ids_ok(self):
        flow = _mk([
            {"id": "a", "type": "function", "z": "flow1", "wires": [["b"]]},
            {"id": "b", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R16"], [])

    def test_two_collisions_both_reported(self):
        flow = _mk([
            {"id": "x", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "x", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "y", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "y", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r16 = [i for i in lint_flow(flow) if i["rule"] == "R16"]
        self.assertEqual(len(r16), 2)


class TestR18SubflowDeadPorts(unittest.TestCase):
    def _sub(self, in_ports, out_ports):
        return {
            "id": "sub1", "type": "subflow", "name": "t", "z": "flow1",
            "in": in_ports, "out": out_ports,
        }

    def test_dead_in_port(self):
        sub = self._sub(
            [{"x": 0, "y": 0, "wires": []}],      # in 端口无连线 = 死端口
            [{"x": 0, "y": 0, "wires": [{"id": "n1"}]}],
        )
        flow = _mk([sub, {"id": "n1", "type": "debug", "z": "flow1", "wires": [[]]}])
        r18 = [i for i in lint_flow(flow) if i["rule"] == "R18"]
        self.assertEqual(len(r18), 1)
        self.assertEqual(r18[0]["level"], "error")
        self.assertIn("in", r18[0]["message"])
        self.assertIn("入口断连", r18[0]["message"])

    def test_dead_out_port(self):
        sub = self._sub(
            [{"x": 0, "y": 0, "wires": [{"id": "n1"}]}],
            [{"x": 0, "y": 0, "wires": []}],      # out 端口无连线 = 死端口
        )
        flow = _mk([sub, {"id": "n1", "type": "debug", "z": "flow1", "wires": [[]]}])
        r18 = [i for i in lint_flow(flow) if i["rule"] == "R18"]
        self.assertEqual(len(r18), 1)
        self.assertIn("永远不向外部发消息", r18[0]["message"])

    def test_connected_ports_ok(self):
        sub = self._sub(
            [{"x": 0, "y": 0, "wires": [{"id": "n1"}]}],
            [{"x": 0, "y": 0, "wires": [{"id": "n1"}]}],
        )
        flow = _mk([sub, {"id": "n1", "type": "debug", "z": "flow1", "wires": [[]]}])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R18"], [])

    def test_subflow_instance_not_flagged(self):
        """子流程**实例**（type=subflow:xxx）不应被 R18 误判。"""
        flow = _mk([
            {"id": "inst", "type": "subflow:abc123", "z": "flow1", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R18"], [])


class TestR21SwitchDeadBranch(unittest.TestCase):
    def _switch(self, wires):
        return {
            "id": "sw", "type": "switch", "z": "flow1",
            "property": "payload.x", "propertyType": "msg",
            "checkall": False, "outputs": len(wires),
            "rules": [{"t": "eq", "v": "on", "vt": "str"}, {"t": "else"}],
            "wires": wires,
        }

    def test_dead_branch_reported(self):
        """switch 第 2 条分支(否则)无连线 → R21 warning。"""
        flow = _mk([
            self._switch([["n2"], []]),
            {"id": "n2", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r21 = [i for i in lint_flow(flow) if i["rule"] == "R21"]
        self.assertEqual(len(r21), 1, lint_flow(flow))
        self.assertEqual(r21[0]["level"], "warning")
        self.assertIn("第 2 条分支", r21[0]["message"])

    def test_all_branches_connected_ok(self):
        flow = _mk([
            self._switch([["n2"], ["n3"]]),
            {"id": "n2", "type": "debug", "z": "flow1", "wires": [[]]},
            {"id": "n3", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R21"], [])

    def test_empty_rules_not_r21(self):
        """空 rules 由 R22(error) 报，R21 不应重复报。"""
        flow = _mk([{
            "id": "sw", "type": "switch", "z": "flow1",
            "outputs": 0, "rules": [], "wires": [],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R21"], [])
        self.assertTrue(any(i["rule"] == "R22" for i in lint_flow(flow)))


class TestR22RequiredFields(unittest.TestCase):
    def test_api_call_service_missing_service_error(self):
        flow = _mk([{
            "id": "svc", "type": "api-call-service", "z": "flow1",
            "server": "e93e1ad9c034e866", "service": "",
            "entityId": "light.kitchen", "wires": [[]],
        }])
        r22 = [i for i in lint_flow(flow) if i["rule"] == "R22"]
        self.assertEqual(len(r22), 1, lint_flow(flow))
        self.assertEqual(r22[0]["level"], "error")
        self.assertIn("service", r22[0]["message"])

    def test_api_call_service_ok(self):
        flow = _mk([{
            "id": "svc", "type": "api-call-service", "z": "flow1",
            "server": "e93e1ad9c034e866", "service": "light.turn_on",
            "entityId": "light.kitchen", "wires": [[]],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R22"], [])

    def test_switch_empty_rules_error(self):
        flow = _mk([{
            "id": "sw", "type": "switch", "z": "flow1",
            "outputs": 0, "rules": [], "wires": [],
        }])
        r22 = [i for i in lint_flow(flow) if i["rule"] == "R22"]
        self.assertEqual(len(r22), 1, lint_flow(flow))
        self.assertEqual(r22[0]["level"], "error")

    def test_http_empty_url_warning(self):
        flow = _mk([{
            "id": "h", "type": "http request", "z": "flow1",
            "method": "GET", "ret": "txt", "url": "", "wires": [[]],
        }])
        r22 = [i for i in lint_flow(flow) if i["rule"] == "R22"]
        self.assertEqual(len(r22), 1, lint_flow(flow))
        self.assertEqual(r22[0]["level"], "warning")

    def test_change_empty_rules_warning(self):
        flow = _mk([{
            "id": "c", "type": "change", "z": "flow1",
            "rules": [], "wires": [[]],
        }])
        r22 = [i for i in lint_flow(flow) if i["rule"] == "R22"]
        self.assertEqual(len(r22), 1, lint_flow(flow))
        self.assertEqual(r22[0]["level"], "warning")

    def test_inject_no_auto_trigger_warning(self):
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "", "crontab": "",
             "once": False, "wires": [["n"]]},
            {"id": "n", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r22 = [i for i in lint_flow(flow) if i["rule"] == "R22"]
        self.assertEqual(len(r22), 1, lint_flow(flow))
        self.assertEqual(r22[0]["level"], "warning")

    def test_inject_with_repeat_ok(self):
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "1", "crontab": "",
             "once": False, "wires": [["n"]]},
            {"id": "n", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R22"], [])

    def test_good_flow_no_r21_r22(self):
        """一个完整好 flow：inject→api-call-service→debug，wires 连通、必填齐全。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "5", "crontab": "",
             "once": False, "wires": [["svc"]]},
            {"id": "svc", "type": "api-call-service", "z": "flow1",
             "server": "e93e1ad9c034e866", "service": "light.turn_on",
             "entityId": "light.kitchen", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        issues = lint_flow(flow)
        self.assertEqual([i for i in issues if i["rule"] in ("R21", "R22")], [])


class TestR14Unreachable(unittest.TestCase):
    """B1（R14）：不可达节点 / 死代码检测（白盒手搓漏接节点）。

    仅在 b1_unreachable=True 时运行（autoflow_validate_flow / deploy 路径已开启）；
    level=warning，不升阻塞。
    """

    def _island_flow(self):
        # inj→a 是主链；b(function) 悬空，从任何触发源都不可达
        return _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "", "crontab": "",
             "once": False, "wires": [["a"]]},
            {"id": "a", "type": "change", "z": "flow1",
             "rules": [{"t": "set", "p": "payload", "pt": "msg", "to": "x", "tot": "str"}],
             "wires": [[]]},
            {"id": "b", "type": "function", "z": "flow1",
             "func": "return msg;", "wires": [[]]},
        ])

    def test_island_function_reported_when_enabled(self):
        issues = lint_flow(self._island_flow(), b1_unreachable=True)
        r14 = [i for i in issues if i["rule"] == "R14"]
        self.assertEqual(len(r14), 1, issues)
        self.assertEqual(r14[0]["node_id"], "b")
        self.assertEqual(r14[0]["level"], "warning")

    def test_island_not_reported_when_disabled(self):
        # 默认关闭，避免整实例/lint 跨实例 link 时刷屏
        issues = lint_flow(self._island_flow())
        self.assertEqual([i for i in issues if i["rule"] == "R14"], [])

    def test_reachable_function_ok(self):
        # function 接入主链 → 可达 → 不报 R14
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "", "crontab": "",
             "once": False, "wires": [["f"]]},
            {"id": "f", "type": "function", "z": "flow1",
             "func": "return msg;", "wires": [[]]},
        ])
        issues = lint_flow(flow, b1_unreachable=True)
        self.assertEqual([i for i in issues if i["rule"] == "R14"], [])


class TestR15Cycle(unittest.TestCase):
    """B2（R15）：紧致环检测（消息在节点间无限重入）。

    仅对「无节流节点打断」的紧致环报 error（落在 _LINT_BLOCK_RULES）；
    含 delay/trigger/link 等节流节点的自触发环属有意为之，不报。
    """

    def test_tight_cycle_blocked(self):
        # inject→f1→f2→f1 形成无节流的紧致环
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "", "crontab": "",
             "once": False, "wires": [["f1"]]},
            {"id": "f1", "type": "function", "z": "flow1",
             "func": "return msg;", "wires": [["f2"]]},
            {"id": "f2", "type": "function", "z": "flow1",
             "func": "return msg;", "wires": [["f1"]]},
        ])
        issues = lint_flow(flow)
        r15 = [i for i in issues if i["rule"] == "R15"]
        self.assertEqual(len(r15), 2, issues)
        self.assertTrue(all(i["level"] == "error" for i in r15))
        self.assertIn("死循环", r15[0]["message"])

    def test_throttle_cycle_allowed(self):
        # f1→delay→f1 含 delay（节流节点）→ 不报 R15
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "", "crontab": "",
             "once": False, "wires": [["f1"]]},
            {"id": "f1", "type": "function", "z": "flow1",
             "func": "return msg;", "wires": [["d"]]},
            {"id": "d", "type": "delay", "z": "flow1",
             "pauseType": "delay", "timeout": "1", "wires": [["f1"]]},
        ])
        issues = lint_flow(flow)
        self.assertEqual([i for i in issues if i["rule"] == "R15"], [])


class TestR23EventLoop(unittest.TestCase):
    """R23：事件环检测（触发器监听实体被其下游动作改回 → 经 HA 状态重入死循环）。

    wire 图里是 DAG（trigger→action 无回边），但运行时经 HA 状态回灌形成事件环；
    level=warning，不受 throttle 影响，不升阻塞。
    """

    def test_watched_entity_rewritten_reports(self):
        flow = _mk([
            {"id": "trig", "type": "server-state-changed", "z": "flow1",
             "entityId": "light.foo", "wires": [["act"]]},
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "server": "e93e1ad9c034e866", "service": "light.turn_on",
             "entityId": "light.foo",
             "data": '{"entity_id": "light.foo"}', "wires": [[]]},
        ])
        issues = lint_flow(flow)
        r23 = [i for i in issues if i["rule"] == "R23"]
        self.assertEqual(len(r23), 1, issues)
        self.assertEqual(r23[0]["node_id"], "trig")
        self.assertEqual(r23[0]["level"], "warning")

    def test_different_entity_no_event_loop(self):
        flow = _mk([
            {"id": "trig", "type": "server-state-changed", "z": "flow1",
             "entityId": "light.foo", "wires": [["act"]]},
            {"id": "act", "type": "api-call-service", "z": "flow1",
             "server": "e93e1ad9c034e866", "service": "light.turn_on",
             "entityId": "light.bar",
             "data": '{"entity_id": "light.bar"}', "wires": [[]]},
        ])
        issues = lint_flow(flow)
        self.assertEqual([i for i in issues if i["rule"] == "R23"], [])


class TestR24TriggerDuration(unittest.TestCase):
    def test_ifstate_with_duration_word_caught(self):
        """server-state-changed 的 ifState 混入时长词（『on 持续5分钟』）→ R24 error。"""
        flow = _mk([{
            "id": "trig", "type": "server-state-changed", "z": "flow1",
            "entities": {"entity": ["binary_sensor.foo"], "substring": [], "regex": []},
            "ifState": "on 持续5分钟", "for": "0", "forType": "num",
            "outputs": 1, "wires": [[]],
        }])
        issues = lint_flow(flow)
        r24 = [i for i in issues if i["rule"] == "R24" and i["level"] == "error"]
        self.assertEqual(len(r24), 1, issues)
        self.assertIn("时长词", r24[0]["message"])

    def test_ifstate_clean_ok(self):
        """干净 ifState（不带时长词）→ 不报 R24。"""
        flow = _mk([{
            "id": "trig", "type": "server-state-changed", "z": "flow1",
            "entities": {"entity": ["binary_sensor.foo"], "substring": [], "regex": []},
            "ifState": "on", "for": "5", "forType": "num", "forUnits": "minutes",
            "outputs": 1, "wires": [[]],
        }])
        issues = lint_flow(flow)
        self.assertEqual([i for i in issues if i["rule"] == "R24"], [])


class TestR32KeyEmptyParams(unittest.TestCase):
    def test_function_empty_func_error(self):
        """function 节点 func 为空 → R32 error（静默死节点，链路断于此却无任何报错）。"""
        flow = _mk([{
            "id": "fn", "type": "function", "z": "flow1",
            "func": "", "wires": [[]],
        }])
        r32 = [i for i in lint_flow(flow) if i["rule"] == "R32"]
        self.assertEqual(len(r32), 1, lint_flow(flow))
        self.assertEqual(r32[0]["level"], "error")
        self.assertIn("func", r32[0]["message"])

    def test_function_whitespace_func_error(self):
        flow = _mk([{
            "id": "fn", "type": "function", "z": "flow1",
            "func": "   \n  ", "wires": [[]],
        }])
        r32 = [i for i in lint_flow(flow) if i["rule"] == "R32"]
        self.assertEqual(len(r32), 1, lint_flow(flow))

    def test_function_with_code_ok(self):
        flow = _mk([{
            "id": "fn", "type": "function", "z": "flow1",
            "func": "return msg;", "wires": [[]],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R32"], [])

    def test_api_call_service_empty_domain_error(self):
        """api-call-service domain 为空 → R32 error（服务调用变成「.service」必败）。"""
        flow = _mk([{
            "id": "svc", "type": "api-call-service", "z": "flow1",
            "server": "e93e1ad9c034e866", "service": "turn_on",
            "domain": "", "entityId": "light.kitchen", "wires": [[]],
        }])
        r32 = [i for i in lint_flow(flow) if i["rule"] == "R32"]
        self.assertEqual(len(r32), 1, lint_flow(flow))
        self.assertEqual(r32[0]["level"], "error")
        self.assertIn("domain", r32[0]["message"])

    def test_api_call_service_domain_ok(self):
        flow = _mk([{
            "id": "svc", "type": "api-call-service", "z": "flow1",
            "server": "e93e1ad9c034e866", "service": "turn_on",
            "domain": "light", "entityId": "light.kitchen", "wires": [[]],
        }])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R32"], [])

    def test_r32_error_level(self):
        """R32 为 error 级，部署硬伤阻塞集应拦下（对齐 gateway/mcp_server 的 _LINT_BLOCK_RULES）。"""
        flow = _mk([{
            "id": "fn", "type": "function", "z": "flow1",
            "func": "", "wires": [[]],
        }])
        r32 = [i for i in lint_flow(flow) if i["rule"] == "R32"]
        self.assertEqual(r32[0]["level"], "error")


class TestR33NoopFlow(unittest.TestCase):
    def test_only_inject_debug_warns(self):
        """inject→debug 无任何 effectful 节点 → R33 warning（纯 stub / 观测流）。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "1", "crontab": "",
             "once": False, "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        r33 = [i for i in lint_flow(flow) if i["rule"] == "R33"]
        self.assertEqual(len(r33), 1, lint_flow(flow))
        self.assertEqual(r33[0]["level"], "warning")

    def test_with_api_call_service_no_r33(self):
        """含 service 调用节点 → 不报 R33（有真实副作用）。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "1", "crontab": "",
             "once": False, "wires": [["svc"]]},
            {"id": "svc", "type": "api-call-service", "z": "flow1",
             "server": "e93e1ad9c034e866", "service": "turn_on",
             "domain": "light", "entityId": "light.kitchen", "wires": [["dbg"]]},
            {"id": "dbg", "type": "debug", "z": "flow1", "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R33"], [])

    def test_empty_flow_no_r33(self):
        flow = {"id": "f", "label": "t", "nodes": []}
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R33"], [])

    def test_subflow_instance_no_r33(self):
        """子流程实例（subflow:xxx）算 effectful → 不报 R33。"""
        flow = _mk([
            {"id": "inj", "type": "inject", "z": "flow1",
             "props": [{"p": "payload"}], "repeat": "1", "crontab": "",
             "once": False, "wires": [["sub"]]},
            {"id": "sub", "type": "subflow:af_hist_state_at", "z": "flow1",
             "wires": [[]]},
        ])
        self.assertEqual([i for i in lint_flow(flow) if i["rule"] == "R33"], [])

    def test_r33_is_warning_not_blocking(self):
        """R33 是 warning，不阻塞部署（与 R32 error 形成对照）。"""
        flow = _mk([{"id": "d", "type": "debug", "z": "flow1", "wires": [[]]}])
        r33 = [i for i in lint_flow(flow) if i["rule"] == "R33"]
        self.assertEqual(len(r33), 1)
        self.assertEqual(r33[0]["level"], "warning")


if __name__ == "__main__":
    unittest.main()
