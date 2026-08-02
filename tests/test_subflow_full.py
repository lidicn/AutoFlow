#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SubflowRegistry Full 阶段（#582/#583/#584）单测：注册校验门 + 状态机 + 编译器参数校验。

- validate_subflow_registration：key 合法性 / 撞名 / schema / env 结构校验
- TaskStore.set_subflow_status：状态变更（枚举 + 存在性）
- C_SUBFLOW_ARG：managed 严格（未知参数报错）、imported 宽松（best-effort schema 不报未知）
不依赖 live NR/HA；离线用 TmpStore + monkeypatch SUBFLOWS。
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
from autoflow_gateway.subflows import (
    SubflowSpec, Param, validate_subflow_registration,
)
from autoflow_gateway.dsl_engine import (
    compile_dsl, DSLError, C_SUBFLOW_ARG, C_SUBFLOW_UNKNOWN,
)


class TmpStoreMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = GatewayConfig()
        self.cfg.data_dir = self.tmp
        self.store = TaskStore(self.cfg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestValidateSubflowRegistration(TmpStoreMixin, unittest.TestCase):
    def _ok(self, **kw):
        base = dict(key="my_api", nr_subflow_id="sf_1", source_type="imported",
                    title="", input_schema=[{"name": "x", "type": "str"}],
                    env_requirements=["TOK"])
        base.update(kw)
        return validate_subflow_registration(**base)

    def test_valid_normalizes(self):
        r = self._ok(title="")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["cleaned"]["key"], "my_api")
        self.assertEqual(r["cleaned"]["title"], "my_api")  # 空标题回退 key
        self.assertEqual(r["cleaned"]["input_schema"][0]["name"], "x")
        self.assertEqual(r["cleaned"]["env_requirements"], ["TOK"])

    def test_bad_key_format(self):
        self.assertFalse(self._ok(key="1x")["ok"])
        self.assertFalse(self._ok(key="my-api")["ok"])   # 连字符非法
        self.assertFalse(self._ok(key="")["ok"])

    def test_collision_with_builtin(self):
        # bark_push 在 SUBFLOWS（网关预置）→ 撞名拒绝
        self.assertFalse(self._ok(key="bark_push")["ok"])

    def test_bad_input_schema(self):
        self.assertFalse(self._ok(input_schema="notlist")["ok"])
        self.assertFalse(self._ok(input_schema=[{"type": "str"}])["ok"])  # 缺 name
        self.assertFalse(self._ok(input_schema=[{"name": 123}])["ok"])   # name 非字符串

    def test_bad_env(self):
        self.assertFalse(self._ok(env_requirements="notlist")["ok"])
        self.assertFalse(self._ok(env_requirements=[123])["ok"])         # 非法项
        # {name} 字典项应被规范化为字符串
        r = self._ok(env_requirements=[{"name": "TOK"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["cleaned"]["env_requirements"], ["TOK"])

    def test_imported_requires_nr_id(self):
        self.assertFalse(self._ok(nr_subflow_id="")["ok"])


class TestSetSubflowStatus(TmpStoreMixin, unittest.TestCase):
    def _register(self, key="st1", status="active"):
        return self.store.register_subflow(
            key, title=key, nr_subflow_id="sf_x", source_type="imported",
            input_schema=[{"name": "a"}], status=status, owner="u",
        )

    def test_transition_works(self):
        self._register()
        self.assertTrue(self.store.set_subflow_status("st1", "disabled")["ok"])
        self.assertEqual(self.store.get_subflow_meta("st1")["status"], "disabled")
        self.assertTrue(self.store.set_subflow_status("st1", "active")["ok"])
        self.assertEqual(self.store.get_subflow_meta("st1")["status"], "active")

    def test_bad_status_and_missing(self):
        self._register()
        self.assertFalse(self.store.set_subflow_status("st1", "weird")["ok"])
        self.assertFalse(self.store.set_subflow_status("nope", "disabled")["ok"])


class TestDeleteSubflow(TmpStoreMixin, unittest.TestCase):
    """delete_subflow（store 层，endpoint 注销的底层依赖）回归。

    注：managed 保护在 webui 端点层（delete_subflow_endpoint 拒绝 source_type=managed）；
    store 层不区分来源，此处仅验证删除能力本身。"""
    def _register(self, key="d1", source_type="imported",
                  nr_subflow_id="sf_x", kind="subflow", status="active"):
        return self.store.register_subflow(
            key, title=key, nr_subflow_id=nr_subflow_id, source_type=source_type,
            kind=kind, input_schema=[], status=status, owner="u",
        )

    def test_delete_removes_row(self):
        self._register()
        self.assertIsNotNone(self.store.get_subflow_meta("d1"))
        r = self.store.delete_subflow("d1")
        self.assertTrue(r["ok"])
        self.assertIsNone(self.store.get_subflow_meta("d1"))

    def test_delete_missing_returns_error(self):
        r = self.store.delete_subflow("nope")
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_delete_requires_key(self):
        self.assertFalse(self.store.delete_subflow("")["ok"])

    def test_delete_managed_at_store_level(self):
        # store 层不拦 managed（保护在端点层）；此处确认删除能力可用
        self._register(source_type="managed")
        self.assertTrue(self.store.delete_subflow("d1")["ok"])


class TestCSubflowArg(unittest.TestCase):
    def test_validate_args_strict_vs_lenient(self):
        # managed：strict 下未知参数报错
        m = SubflowSpec(name="m", title="m", call={"type": "subflow", "subflow_id": "x"},
                        params={"a": Param("a", required=True)}, source="managed")
        m.validate_args({"a": 1})                       # 合法
        with self.assertRaises(ValueError):
            m.validate_args({"a": 1, "bogus": 2}, strict=True)   # 未知参数
        with self.assertRaises(ValueError):
            m.validate_args({}, strict=True)                      # 缺必填
        # imported：宽松，未知参数不报
        imp = SubflowSpec(name="i", title="i", call={"type": "subflow", "subflow_id": "y"},
                          params={"a": Param("a")}, source="imported")
        imp.validate_args({"a": 1, "bogus": 2})         # 不抛（宽松）

    def test_managed_strict_at_compile(self):
        import autoflow_gateway.subflows as sf
        sf.SUBFLOWS["test_sf_managed"] = SubflowSpec(
            name="test_sf_managed", title="t",
            call={"type": "subflow", "subflow_id": "sf_test_m"},
            params={"a": Param("a", required=True)}, source="managed")
        try:
            dsl = ("场景: 测试\n触发: sensor.y 有人\n"
                   "调用子流程: test_sf_managed(a=1, bogus=2)\n")
            with self.assertRaises(DSLError) as cm:
                compile_dsl(dsl)
            self.assertEqual(cm.exception.code, C_SUBFLOW_ARG)
        finally:
            del sf.SUBFLOWS["test_sf_managed"]

    def test_imported_lenient_at_compile(self):
        import autoflow_gateway.subflows as sf
        tmp = tempfile.mkdtemp()
        cfg = GatewayConfig(); cfg.data_dir = tmp
        store = TaskStore(cfg)
        sf.set_registry_store(store)
        try:
            store.register_subflow(
                "test_imp", title="t", nr_subflow_id="sf_imp",
                source_type="imported",
                input_schema=[{"name": "device", "required": True}],
                status="active", owner="u")
            dsl = ("场景: 测试\n触发: sensor.y 有人\n"
                   "调用子流程: test_imp(device=客厅灯, bogus=9)\n")
            flow = compile_dsl(dsl)          # imported 宽松：未知参数不报错
            self.assertIn("nodes", flow)
        finally:
            sf.set_registry_store(None)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_subflow_still_unknown(self):
        # 回归：未知子流程仍抛 C_SUBFLOW_UNKNOWN（不被 C_SUBFLOW_ARG 吞掉）
        dsl = ("场景: 测试\n触发: sensor.y 有人\n调用子流程: ghost_sf(x=1)\n")
        with self.assertRaises(DSLError) as cm:
            compile_dsl(dsl)
        self.assertEqual(cm.exception.code, C_SUBFLOW_UNKNOWN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
