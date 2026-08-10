# -*- coding: utf-8 -*-
"""http_api 型 API 能力回归锁。

原本这个文件锁的是 `llm_doubao_chat`。豆包四条 spec 已按用户决策从
`data/api_specs.json` 移除（见 WORKORDER_DEV_http_api_nas_ip），
但它承载的**产品不变量**依然成立、且必须继续被锁住：

- 黑箱写 `调用子流程: <http_api 能力>(...)` 即可调用，网关把「拼请求体 +
  http 调用 + 取 reply」内联为隐藏节点，agent 不碰 URL。
- 编译产物含 http request + 返回值规整到 msg.payload.reply。
- **A25**：url 里的 `<NAS_IP>` 系统占位符在编译产物中已解析成真主机，
  绝不把字面量下发生产（这正是此前 P0 现网必坏的根因）。
- dsl_help 自动收录该能力。
- staging 闸门对纯 API 能力放行。

故改为用测试自带 spec（tests/api_spec_fixture.py）驱动同一套真实代码，
与产品能力清单解耦。运行：python tests/test_api_capability.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")
sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 1)[0])

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_api_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from api_spec_fixture import make_spec, temp_api_spec
from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway import subflows as sf
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.config import reset_config

SPEC_NAME = "t_llm_chat"

DSL_CHAT = f"""场景: 测试 http_api 能力
触发: inject
调用子流程: {SPEC_NAME}(user_msg=用轻松语气提醒用户该休息了, user=大佬, scenario=书房)
提取: 回复 = payload.reply
调用子流程: demo_notify(text=`回复`, room=书房, level=一般)
"""


def _spec():
    return make_spec(
        name=SPEC_NAME,
        title="测试对话能力",
        kind="http_api",
        # 刻意留 <NAS_IP> 占位符：A25 要求它在派生/编译时被解析掉
        url="http://<NAS_IP>:1880/llm/chat",
        method="POST",
        extract="payload.reply",
        params={
            "user_msg": {"name": "user_msg", "required": True, "type": "str",
                         "desc": "用户消息内容"},
            "user": {"name": "user", "required": False, "default": "大佬",
                     "type": "str", "desc": "人设"},
            "scenario": {"name": "scenario", "required": False, "default": "书房",
                         "type": "str", "desc": "场景标签"},
        },
        description="测试用 http_api 能力",
    )


class TestApiCapability(unittest.TestCase):
    def _gw(self):
        reset_config()
        return Gateway()

    def test_capability_registered(self):
        with temp_api_spec(_spec()):
            self.assertIn(SPEC_NAME, sf.SUBFLOWS)
            spec = sf.SUBFLOWS[SPEC_NAME]
            self.assertEqual(spec.call["type"], "http_api")
            self.assertTrue(spec.call["url"].endswith("/llm/chat"))

    def test_a25_placeholder_resolved_in_derived_spec(self):
        """A25：派生 SubflowSpec 时 <NAS_IP> 必须已解析，不能原样透传。"""
        os.environ["NR_URL"] = "http://10.99.99.99:1880"
        try:
            with temp_api_spec(_spec()):
                url = sf.SUBFLOWS[SPEC_NAME].call["url"]
                self.assertNotIn("<NAS_IP>", url, f"占位符未解析：{url}")
                self.assertEqual(url, "http://10.99.99.99:1880/llm/chat")
        finally:
            os.environ.pop("NR_URL", None)

    def test_compile_emits_http_and_reply_extract(self):
        os.environ["NR_URL"] = "http://10.99.99.99:1880"
        try:
            with temp_api_spec(_spec()):
                flow = compile_dsl(DSL_CHAT, target="staging")
        finally:
            os.environ.pop("NR_URL", None)
        types = [n["type"] for n in flow["nodes"]]
        self.assertIn("http request", types)
        http = next(n for n in flow["nodes"] if n["type"] == "http request")
        # A25 核心断言：编译产物里绝不能出现占位符字面量
        self.assertNotIn("<NAS_IP>", http["url"], http["url"])
        self.assertEqual(http["url"], "http://10.99.99.99:1880/llm/chat")
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
        with temp_api_spec(_spec()):
            gw = self._gw()
            helpd = gw.dsl_help()
            names = [s["name"] for s in helpd["subflows"]]
            self.assertIn(SPEC_NAME, names)

    def test_staging_gate_passes_chat(self):
        with temp_api_spec(_spec()):
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
