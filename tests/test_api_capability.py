# -*- coding: utf-8 -*-
"""API 能力（llm_doubao_chat）回归锁。

验证：
- 黑箱写 `调用子流程: llm_doubao_chat(user_msg=...)` 即可调用豆包对话，
  网关把「拼请求体 + http 调用 + 取 reply」内联为隐藏节点，agent 不碰 URL。
- 编译器产物含 http request（指向豆包中枢 /llm/chat）+ 返回值规整到 msg.payload.reply。
- dsl_help 自动收录该能力（MCP help 指导）。
- staging 闸门对该能力放行（无实体后置条件时判 PASS）。
运行：python tests/test_api_capability.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_api_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway import subflows as sf
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.config import reset_config

DSL_CHAT = """场景: 测试豆包对话能力
触发: inject
调用子流程: llm_doubao_chat(user_msg=用轻松语气提醒用户该休息了, user=大佬, scenario=书房)
提取: 回复 = payload.reply
调用子流程: demo_notify(text=`回复`, room=书房, level=一般)
"""


class TestApiCapability(unittest.TestCase):
    def _gw(self):
        reset_config()
        return Gateway()

    def test_capability_registered(self):
        self.assertIn("llm_doubao_chat", sf.SUBFLOWS)
        spec = sf.SUBFLOWS["llm_doubao_chat"]
        self.assertEqual(spec.call["type"], "http_api")
        self.assertTrue(spec.call["url"].endswith("/llm/chat"))

    def test_compile_emits_http_and_reply_extract(self):
        flow = compile_dsl(DSL_CHAT, target="staging")
        types = [n["type"] for n in flow["nodes"]]
        self.assertIn("http request", types)
        http = next(n for n in flow["nodes"] if n["type"] == "http request")
        self.assertEqual(http["url"], "http://<NAS_IP>:1880/llm/chat")
        self.assertEqual(http["method"], "POST")
        changes = [n for n in flow["nodes"] if n["type"] == "change"]
        body_ok = any(
            "user_msg" in (r.get("to", "")) and "scenario" in (r.get("to", ""))
            for n in changes
            for r in n.get("rules", [])
            if r.get("p") == "payload"
        )
        self.assertTrue(body_ok, "请求体未正确设进 msg.payload")
        errs = [i for i in flow.get("lint", []) if i.get("level") == "error"]
        self.assertFalse(errs, errs)

    def test_help_lists_capability(self):
        gw = self._gw()
        helpd = gw.dsl_help()
        names = [s["name"] for s in helpd["subflows"]]
        self.assertIn("llm_doubao_chat", names)

    def test_staging_gate_passes_chat(self):
        gw = self._gw()
        # 纯 API 能力（无 HA 实体状态变更），无实体后置条件 → 闸门放行，
        # 外部调用（demo_notify）被分支感知记录。
        gate = gw.run_staging_gate(DSL_CHAT, [], vhass_store=None)
        self.assertTrue(gate["passed"], gate)
        self.assertTrue(
            any("demo_notify" in c for c in gate.get("external_calls", [])),
            f"demo_notify 未被记录：{gate.get('external_calls')}",
        )


if __name__ == "__main__":
    unittest.main()
