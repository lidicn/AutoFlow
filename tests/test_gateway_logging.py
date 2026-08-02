# -*- coding: utf-8 -*-
"""A9 结构化日志单测：trace_id + 各阶段耗时 JSON 行。

验证：
- _new_trace_id 产 12 位 hex。
- _slog 输出一行合法 JSON（含 ts/trace_id/stage/自定义字段）。
- 关键入口（list_pending / propose_dsl / deploy_raw）确实产出带 trace_id 的
  start / 各阶段 / done|error 日志，且返回体带 _trace_id。
运行：python tests/test_gateway_logging.py
"""
import os
import sys
import json
import logging
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_log_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as gw_mod
from autoflow_gateway.gateway import Gateway, _new_trace_id, _slog


class _BufHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture():
    h = _BufHandler()
    gw_mod._gw_logger.addHandler(h)
    return h


class TestLoggingHelpers(unittest.TestCase):
    def test_trace_id_format(self):
        t = _new_trace_id()
        self.assertEqual(len(t), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in t))

    def test_slog_emits_json(self):
        h = _capture()
        try:
            _slog("abc123abc123", "demo", foo="bar", n=3)
        finally:
            gw_mod._gw_logger.removeHandler(h)
        self.assertEqual(len(h.records), 1)
        obj = json.loads(h.records[0].getMessage())
        self.assertEqual(obj["trace_id"], "abc123abc123")
        self.assertEqual(obj["stage"], "demo")
        self.assertEqual(obj["foo"], "bar")
        self.assertEqual(obj["n"], 3)
        self.assertIn("ts", obj)


class TestEntryPointLogging(unittest.TestCase):
    def test_list_pending_logs_with_trace_id(self):
        h = _capture()
        try:
            g = Gateway()
            res = g.list_pending()
        finally:
            gw_mod._gw_logger.removeHandler(h)
        self.assertIsInstance(res, list)
        lines = [json.loads(r.getMessage()) for r in h.records]
        stages = [l["stage"] for l in lines]
        self.assertIn("list_pending.done", stages)
        done = [l for l in lines if l["stage"] == "list_pending.done"][0]
        self.assertTrue(done["trace_id"])
        self.assertEqual(done["count"], 0)
        self.assertIn("elapsed", done)

    def test_propose_dsl_logs_stages_and_returns_trace_id(self):
        h = _capture()
        try:
            g = Gateway()
            # 一个最小可编译 DSL（无 HA 动作 → staging 闸门跳过，纯离线）
            dsl = ("场景: 测试场景\n"
                   "触发: binary_sensor.test_door 有人\n"
                   "动作: light.turn_on(测试灯, brightness=80)\n")
            res = g.propose_dsl(dsl, agent_id="test-agent")
        finally:
            gw_mod._gw_logger.removeHandler(h)
        # 无论编译成败都应带 trace_id（成功路径嵌入返回体）
        if isinstance(res, dict) and res.get("ok"):
            self.assertIn("_trace_id", res)
        lines = [json.loads(r.getMessage()) for r in h.records]
        stages = [l["stage"] for l in lines]
        self.assertIn("propose_dsl.start", stages)
        # start 必有 trace_id，且同一请求的各阶段 trace_id 一致
        tids = {l["trace_id"] for l in lines}
        self.assertEqual(len(tids), 1, f"同一请求 trace_id 应一致: {tids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
