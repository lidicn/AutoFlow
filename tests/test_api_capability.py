# -*- coding: utf-8 -*-
"""API 类能力（外部子流程 link-out 形态）回归锁。

★ 历史变更（勿倒退）：本文件原先锁的是 `llm_doubao_chat` / `llm_doubao_image` ——
  当时网关把「拼请求体 + http 调用 + 取 reply」内联成隐藏 http request 节点。
  豆包 4 spec 已于 cb43830 / 0f4940a 按 P0 有意移除（能力归 doubao-butler），
  `http_api` 形态随之整体下线。现存 API 类能力（`llm_caiyun_weather` /
  `anysearch_batch`）改为 **link-out 形态**：DSL 里 `调用子流程:` 编译成
  link out → 外部子流程 tab 的入口 link in，网关不再内联 http 节点。
  故本文件按现存实现重写，锁的是「API 能力可达 + 参数注入 + help 可见 + 闸放行」。

验证：
- `anysearch_batch` 在注册表里是 link_out 形态，指向入口 link in。
- 编译器产物含 link out + 入参 change 节点，无 error 级 lint。
- dsl_help 自动收录该能力（MCP help 指导）。
- staging 闸门对该能力放行（无实体后置条件），外部调用被分支感知记录。
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

# 现存 API 类能力：link-out 形态（非 http_api）
CAP = "anysearch_batch"
CAP_ENTRY_LINK = "af_anysearch_in"

DSL_API = """场景: 测试资讯搜索能力
触发: inject
调用子流程: anysearch_batch(keywords=智能家居, max_results=5)
"""


class TestApiCapability(unittest.TestCase):
    def _gw(self):
        reset_config()
        return Gateway()

    def test_capability_registered(self):
        self.assertIn(CAP, sf.SUBFLOWS)
        spec = sf.SUBFLOWS[CAP]
        self.assertEqual(spec.call["type"], "link_out")
        self.assertEqual(spec.call["entry_link_id"], CAP_ENTRY_LINK)
        self.assertIn("keywords", spec.params)
        self.assertTrue(spec.params["keywords"].required)

    def test_compile_emits_link_out_and_params(self):
        flow = compile_dsl(DSL_API, target="staging")
        types = [n["type"] for n in flow["nodes"]]
        self.assertIn("link out", types)
        outs = [n for n in flow["nodes"] if n["type"] == "link out"]
        self.assertTrue(any(CAP_ENTRY_LINK in str(o.get("links") or o.get("name") or "")
                            for o in outs),
                        f"link out 未指向 {CAP_ENTRY_LINK}：{outs}")
        # 入参应被注入 msg.payload（change 节点）
        changes = [n for n in flow["nodes"] if n["type"] == "change"]
        body_ok = any(
            "keywords" in (r.get("to", "") or r.get("t", ""))
            for n in changes for r in n.get("rules", [])
        )
        self.assertTrue(body_ok, "入参未设进 msg.payload")
        errs = [i for i in flow.get("lint", []) if i.get("level") == "error"]
        self.assertFalse(errs, errs)

    def test_help_lists_capability(self):
        gw = self._gw()
        helpd = gw.dsl_help()
        names = [s["name"] for s in helpd["subflows"]]
        self.assertIn(CAP, names)

    def test_staging_gate_passes_api_capability(self):
        gw = self._gw()
        # 纯外部 API 能力（无 HA 实体状态变更），无实体后置条件 → 闸门放行，
        # 外部调用被分支感知记录。
        gate = gw.run_staging_gate(DSL_API, [], vhass_store=None)
        self.assertTrue(gate["passed"], gate)
        self.assertTrue(
            any(CAP in c for c in gate.get("external_calls", [])),
            f"{CAP} 未被记录：{gate.get('external_calls')}",
        )


if __name__ == "__main__":
    unittest.main()
