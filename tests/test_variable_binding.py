#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变量绑定回归（iss_185a55e085 / WB3 审计报告）：修「变量是死变量」。

覆盖三处根因：
- #505 变量写入 flow 上下文用原生类型（数值→tot=num、布尔/引号→str、JSON→json）；
- #506 动作参数引用变量 → 改用 dataType=jsonata 读 flow.<变量名>（保证数值落 HA 仍是数值）；
- #507 分支/条件/构建/子流程 JSONata 里的裸变量名绑定到 flow.<变量名>。

仅引用已声明变量名，未声明标识符（msg 字段、$函数）不受影响；已被 flow./msg./$ 前缀
修饰的不重复改写；mustache {{...}} 不碰。
"""
import os
import sys
import json
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl


def _by_type(flow, ntype):
    return [n for n in flow["nodes"] if n["type"] == ntype]


def _change_rules(flow):
    chg = _by_type(flow, "change")
    assert chg, "缺少 change 节点"
    return chg[0]["rules"]


class TestVarNativeType(unittest.TestCase):
    def test_numeric_var_stored_as_num(self):
        flow = compile_dsl("场景: x\n触发: inject\n变量: 亮度=70\n动作: light.turn_on(light.x)\n")
        rule = _change_rules(flow)[0]
        self.assertEqual(rule["t"], "set")
        self.assertEqual(rule["p"], "亮度")
        self.assertEqual(rule["pt"], "flow")
        self.assertEqual(rule["to"], "70")
        self.assertEqual(rule["tot"], "num", "数值变量应存原生 num，否则下游当字符串")

    def test_quoted_bool_var_stripped_to_str(self):
        flow = compile_dsl("场景: x\n触发: inject\n变量: 开 = 'on'\n动作: light.turn_on(light.x)\n")
        rule = _change_rules(flow)[0]
        self.assertEqual(rule["to"], "on", "引用引号应被剥离")
        self.assertEqual(rule["tot"], "str")

    def test_text_var_stays_str(self):
        flow = compile_dsl("场景: 变量驱动开灯\n触发: sensor.a 有人\n变量: room=书房\n动作: light.turn_on(light.study_main)\n")
        rule = _change_rules(flow)[0]
        self.assertEqual(rule["to"], "书房")
        self.assertEqual(rule["tot"], "str")


class TestVarInAction(unittest.TestCase):
    def test_action_param_var_becomes_jsonata_flow_ref(self):
        flow = compile_dsl("场景: x\n触发: inject\n变量: 亮度=70\n动作: light.turn_on(light.x, brightness_pct=亮度)\n")
        svc = _by_type(flow, "api-call-service")[0]
        self.assertEqual(svc["dataType"], "jsonata", "引用变量的动作必须走 jsonata 才能读到 flow 原生值")
        # dataType=jsonata 时 data 是 JSONata 表达式（非 JSON），flow.亮度 在运行时求值
        self.assertEqual(svc["data"], '{"brightness_pct": flow.亮度}')

    def test_action_mixed_literal_and_var(self):
        flow = compile_dsl("场景: x\n触发: inject\n变量: 亮度=70\n动作: light.turn_on(light.x, brightness_pct=亮度, transition=2)\n")
        svc = _by_type(flow, "api-call-service")[0]
        self.assertEqual(svc["dataType"], "jsonata")
        self.assertEqual(svc["data"], '{"brightness_pct": flow.亮度, "transition": 2}')

    def test_action_no_var_stays_json_with_numeric_coercion(self):
        flow = compile_dsl("场景: x\n触发: inject\n动作: light.turn_on(light.x, brightness_pct=80)\n")
        svc = _by_type(flow, "api-call-service")[0]
        self.assertEqual(svc["dataType"], "json", "无变量引用应保持原 json 形态")
        self.assertEqual(json.loads(svc["data"]), {"brightness_pct": 80}, "数值参数须归一为 JSON number")


class TestVarInJsonata(unittest.TestCase):
    def test_branch_jsonata_binds_bare_var(self):
        # 裸变量名(阈值) → $flowContext('阈值')；裸取值标签(温度) → msg.温度。
        # 注意：未声明标识符(如旧版的 s)会被 C_LABEL_UNDEFINED fail-closed 拦下，故此处两者都声明。
        dsl = ("场景: x\n触发: inject\n"
               "取值: sensor.temp 温度\n"
               "变量: 阈值=10\n"
               "分支: $number(温度) < 阈值\n    动作: light.turn_on(light.x)\n"
               "否则:\n    动作: light.turn_off(light.x)\n")
        flow = compile_dsl(dsl)
        sw = _by_type(flow, "switch")[0]
        jr = [r for r in sw["rules"] if r.get("vt") == "jsonata"][0]
        self.assertEqual(jr["v"], "$number(msg.温度) < $flowContext('阈值')",
                         "变量绑 $flowContext、取值标签绑 msg.<field>")
        self.assertNotIn("flow.阈值", jr["v"])
        self.assertNotIn("payload.温度", jr["v"])

    def test_build_jsonata_binds_var(self):
        dsl = ("场景: x\n触发: inject\n变量: 阈值=10\n"
               "构建: {\"limit\": 阈值}\n动作: light.turn_on(light.x)\n")
        flow = compile_dsl(dsl)
        chg = _by_type(flow, "change")
        build = [c for c in chg if c.get("name") == "构建请求体"][0]
        rule = build["rules"][0]
        self.assertEqual(rule["tot"], "jsonata")
        self.assertIn("$flowContext('阈值')", rule["to"])


if __name__ == "__main__":
    unittest.main()
