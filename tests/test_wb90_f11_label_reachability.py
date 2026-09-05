#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WB90 F11 标签可达性回归（DSL 编译器根因，离线可验，不依赖 live NR）。

F11 CRITICAL：取值/变量 标签在运行时不可达 → 分支/条件恒走 else（静默反向执行）。
三层根因：
  ① 取值 写入 payload.<field>（且真实 NR 的 api-current-state 对【自定义】outputProperty 不兑现，
     仅 payload.state 可靠），而分支裸标签 `光照` 解析为 msg.光照 → 从未写入 → NaN → 恒 else。
  ② 变量 裸名在 JSONata 里被错误改写为 flow.<name>（JSONata 中 flow 不是有效上下文引用，
     须用 $flowContext('name')），导致变量恒 undefined → 恒 else。
  ③ _bind_read_fields 把裸字段改写成 payload.<field>（不可达）。

修复后契约：
  · 取值：api-current-state(写 payload.state + payload.<field> 别名) + change「绑定 <field>」
          (msg.<field> <- payload.state 桥接) —— 裸标签可读 msg.<field>。
  · 变量：change「设置变量」仅写 flow 上下文（C2 契约保留），分支裸变量名 → $flowContext('name')。
  · 分支/条件 jsonata：裸取值标签 → msg.<label>；裸变量名 → $flowContext('name')。
  · 未定义标签仍 fail-closed(C_LABEL_UNDEFINED)（F5a/F5b 行为保留）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.dsl_engine import compile_dsl, DSLError

A_ON = "light.turn_on(灯)"
A_OFF = "light.turn_off(灯)"


def _by_type(flow, ntype, name_sub=None):
    out = []
    for n in flow["nodes"]:
        if n.get("type") != ntype:
            continue
        if name_sub and name_sub not in (n.get("name") or ""):
            continue
        out.append(n)
    return out


class TestWb90F11ReadStateLabel(unittest.TestCase):
    def test_read_state_jsonata_branch_reaches_msg_root(self):
        dsl = ("场景: 书房光照\n触发: inject\n"
               "取值: sensor.lumi 光照\n"
               "分支: $number(光照) < 10\n"
               f"  动作: {A_OFF}\n否则:\n  动作: {A_ON}\n")
        flow = compile_dsl(dsl)
        acs = _by_type(flow, "api-current-state")
        binds = _by_type(flow, "change", "绑定 光照")
        sws = _by_type(flow, "switch")
        self.assertEqual(len(acs), 1)
        self.assertEqual(len(binds), 1, "取值须生成桥接 change 节点把 payload.state 复制到 msg.光照")
        r = binds[0]["rules"][0]
        self.assertEqual(r.get("pt"), "msg")
        self.assertEqual(r.get("p"), "光照")
        self.assertEqual(r.get("to"), "payload.state")
        jr = [ru for ru in sws[0]["rules"] if ru.get("vt") == "jsonata" and "光照" in ru.get("v", "")]
        self.assertTrue(jr, "分支须含引用 光照 的 jsonata 规则")
        self.assertIn("msg.光照", jr[0]["v"])
        self.assertNotIn("payload.光照", jr[0]["v"])
        self.assertNotIn("flow.光照", jr[0]["v"])
        # 向后兼容：api-current-state 仍写 payload.state + payload.<field> 别名
        props = [o.get("property") for o in acs[0].get("outputProperties", [])]
        self.assertIn("payload.state", props)
        self.assertIn("payload.光照", props)

    def test_read_state_eq_branch_property_is_bare_field_msg_root(self):
        dsl = ("场景: 书房光照2\n触发: inject\n"
               "取值: sensor.lumi 光照\n"
               '分支: 光照 == "2"\n'
               f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")
        flow = compile_dsl(dsl)
        sw = _by_type(flow, "switch")[0]
        eq = [ru for ru in sw["rules"] if ru.get("vt") != "jsonata"]
        self.assertTrue(eq)
        self.assertEqual(eq[0].get("property"), "光照")
        self.assertEqual(sw.get("propertyType"), "msg")
        self.assertEqual(sw.get("property"), "光照")


class TestWb90F11VariableLabel(unittest.TestCase):
    def test_variable_writes_flow_context_only(self):
        dsl = ("场景: 阈值控制\n触发: inject\n"
               "变量: 阈值 = 8\n"
               "分支: $number(阈值) < 10\n"
               f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")
        flow = compile_dsl(dsl)
        lets = _by_type(flow, "change", "设置变量")
        self.assertEqual(len(lets), 1)
        flows = [r for r in lets[0]["rules"] if r.get("pt") == "flow" and r.get("p") == "阈值"]
        msgs = [r for r in lets[0]["rules"] if r.get("pt") == "msg" and r.get("p") == "阈值"]
        # C2 契约：变量只写 flow，不污染 msg 根
        self.assertEqual(len(flows), 1)
        self.assertEqual(len(msgs), 0)
        jr = [ru for ru in _by_type(flow, "switch")[0]["rules"]
              if ru.get("vt") == "jsonata" and "阈值" in ru.get("v", "")]
        self.assertTrue(jr)
        self.assertIn("$flowContext('阈值')", jr[0]["v"])
        self.assertNotIn("flow.阈值", jr[0]["v"])
        self.assertNotIn("msg.阈值", jr[0]["v"])

    def test_variable_in_build_body_flow_context(self):
        dsl = ("场景: x\n触发: inject\n变量: 阈值=10\n"
               '构建: {"limit": 阈值}\n动作: light.turn_on(light.x)\n')
        flow = compile_dsl(dsl)
        build = [c for c in _by_type(flow, "change") if c.get("name") == "构建请求体"][0]
        self.assertIn("$flowContext('阈值')", build["rules"][0]["to"])


class TestWb90F11BackwardCompat(unittest.TestCase):
    def test_extract_channel_unaffected(self):
        dsl = ("场景: 提取对照\n触发: inject\n"
               "取值: sensor.lumi 光照\n"
               "提取: 结果 = $number(光照)\n"
               "分支: $number(结果) < 10\n"
               f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")
        flow = compile_dsl(dsl)
        ext = _by_type(flow, "change", "提取字段")
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["rules"][0].get("pt"), "msg")
        jr = [ru for ru in _by_type(flow, "switch")[0]["rules"]
              if ru.get("vt") == "jsonata" and "结果" in ru.get("v", "")]
        self.assertTrue(jr)
        self.assertIn("结果", jr[0]["v"])
        self.assertNotIn("payload.结果", jr[0]["v"])
        self.assertNotIn("flow.结果", jr[0]["v"])

    def test_payload_state_control_still_compiles(self):
        dsl = ("场景: 控制\n触发: inject\n"
               "取值: sensor.lumi 光照\n"
               '分支: payload.state = "2"\n'
               f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")
        compile_dsl(dsl)  # 不应抛

    def test_undefined_label_still_fail_closed(self):
        with self.assertRaises(DSLError) as ctx:
            compile_dsl("场景: x\n触发: inject\n分支: $number(不存在) < 10\n"
                        f"  动作: {A_ON}\n否则:\n  动作: {A_OFF}\n")
        self.assertEqual(ctx.exception.code, "C_LABEL_UNDEFINED")


if __name__ == "__main__":
    unittest.main()
