#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译器查注册表（#577）单测：imported 子流程名 → 编译出 subflow:<id> 节点。

验证 get_subflow 在 SUBFLOWS 未命中时，回退查 subflow_registry 表（active 的 imported
子流程），构造轻量 SubflowSpec；_emit_subflow 据此发射 type=subflow:<nr_subflow_id> 节点，
且入参平铺到 msg.<k>（与 introspect 推断的 msg.<x> 读取对齐）。
不依赖 live NR；离线用 TmpStore + set_registry_store。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.config import GatewayConfig
from autoflow_gateway.task_store import TaskStore
from autoflow_gateway.subflows import get_subflow, set_registry_store
from autoflow_gateway.dsl_engine import compile_dsl


class TestSubflowRegistryCompile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = GatewayConfig()
        self.cfg.data_dir = self.tmp
        self.store = TaskStore(self.cfg)
        set_registry_store(self.store)

    def tearDown(self):
        set_registry_store(None)  # 隔离：避免污染同进程其它测试
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_imported(self, key="my_ext", nr_id="sf_imported_01"):
        return self.store.register_subflow(
            key, title="我的导入子流程", nr_subflow_id=nr_id,
            source_type="imported",
            input_schema=[
                {"name": "device", "required": True, "type": "str",
                 "default": None, "enum": None, "desc": "设备"},
                {"name": "room", "required": False, "type": "str",
                 "default": "default", "enum": None, "desc": "房间"},
            ],
            env_requirements=["MY_TOKEN"],
            owner="user_x", status="active", spec_ref=None,
        )

    def test_get_subflow_falls_back_to_registry(self):
        self._register_imported()
        spec = get_subflow("my_ext")
        self.assertIsNotNone(spec, "注册表中的 imported 子流程应被 get_subflow 命中")
        self.assertEqual(spec.call["type"], "subflow")
        self.assertEqual(spec.call["subflow_id"], "sf_imported_01")
        self.assertEqual(spec.param_style, "flat")
        self.assertIn("device", spec.params)
        self.assertTrue(spec.params["device"].required)

    def test_get_subflow_prefers_builtin(self):
        # 预置（SUBFLOWS）优先级高于注册表：即便注册表有同名，仍返回预置
        self._register_imported(key="bark_push")
        spec = get_subflow("bark_push")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.call["subflow_id"], "b0bbc86abb2172a5")

    def test_unknown_still_raises(self):
        # 未注册且不在 SUBFLOWS → get_subflow 返回 None（编译器将抛 C_SUBFLOW_UNKNOWN）
        self.assertIsNone(get_subflow("totally_unknown_sf"))

    def test_compile_emits_subflow_instance(self):
        self._register_imported()
        dsl = """
场景: 调用导入子流程
触发: sensor.x 有人
调用子流程: my_ext(device=客厅灯, room=书房)
观测: 看结果
"""
        flow = compile_dsl(dsl)
        nodes = flow["nodes"]
        sub = [n for n in nodes if n.get("type") == "subflow:sf_imported_01"]
        self.assertTrue(sub, "应发射 type=subflow:sf_imported_01 实例节点")
        sub_node = sub[0]
        # 入参平铺到 msg.<k>：change 节点设置 msg.device / msg.room
        changes = [n for n in nodes if n.get("type") == "change"]
        self.assertTrue(changes, "应有设置入参的 change 节点")
        rules = changes[0].get("rules", [])
        set_fields = {r.get("p") for r in rules}
        self.assertIn("device", set_fields)
        self.assertIn("room", set_fields)
        # change → subflow 实例连线
        self.assertIn(sub_node["id"], changes[0]["wires"][0])

    def test_compile_rejects_unknown_subflow(self):
        from autoflow_gateway.dsl_engine import DSLError
        dsl = """
场景: 调未知子流程
触发: sensor.x 有人
调用子流程: ghost_sf(x=1)
"""
        with self.assertRaises(DSLError):
            compile_dsl(dsl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
