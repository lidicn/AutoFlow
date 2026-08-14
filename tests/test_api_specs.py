# -*- coding: utf-8 -*-
"""tab api spec 声明式单一真相源回归锁。

验证：
- API 能力只在 api_specs.API_SPECS 定义一次；subflows.SUBFLOWS 由其派生（无手搓副本）。
- 网关侧 SubflowSpec 字段正确（call 类型 / url / entry_link_id / params / help 文本）。
- build_nr_tab_flows 从同一份 spec 生成 NR「AutoFlow API」tab 节点（健壮版：
  校验必填 + HTTP 错误处理 + debug 观测），含三个接口：豆包 / 彩云天气 / anysearch。
- 生成的 tab 经 lint 无 R13/R15 硬伤。
- dsl_help 自动收录全部能力。

运行：python tests/test_api_specs.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_apispec_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway import api_specs as ap
from autoflow_gateway import subflows as sf
from autoflow_gateway.gateway import Gateway
from autoflow_gateway.config import reset_config
from autoflow_gateway.flow_linter import lint_flow


# 当前 API_SPECS 单一真相源中应包含的全部能力
ALL_SPECS = {
    "llm_doubao_chat", "llm_doubao_say", "llm_doubao_image",
    "llm_doubao_vision", "llm_caiyun_weather", "anysearch_batch",
}
# 需要生成 NR 后端流的（link_out / nr_tab）
NR_FLOW_SPECS = {"llm_doubao_say", "llm_caiyun_weather", "anysearch_batch"}


class TestApiSpecSingleSource(unittest.TestCase):
    def test_spec_is_single_source(self):
        # 全部能力都在 API_SPECS 中定义（单一真相源，不在此处另写副本）
        names = {s.name for s in ap.API_SPECS}
        self.assertEqual(names, ALL_SPECS)
        # SUBFLOWS 由 spec 派生（不是 subflows.py 里再手搓一份）
        for n in ALL_SPECS:
            self.assertIn(n, sf.SUBFLOWS)

    def test_chat_is_http_api(self):
        spec = sf.SUBFLOWS["llm_doubao_chat"]
        self.assertEqual(spec.call["type"], "http_api")
        self.assertEqual(spec.call["url"], "http://<NAS_IP>:1880/llm/chat")
        self.assertEqual(spec.call["extract"], "payload.reply")
        self.assertTrue(spec.description)
        self.assertTrue(spec.notes)
        self.assertIn("user_msg", spec.params)

    def test_say_is_link_out(self):
        spec = sf.SUBFLOWS["llm_doubao_say"]
        self.assertEqual(spec.call["type"], "link_out")
        self.assertEqual(spec.call["entry_link_id"], "af_apisay_in")
        self.assertIn("user_msg", spec.params)
        # chat/image/vision 不生成 NR 节点（http_api 由编译器内联）
        self.assertFalse(ap.get_api_spec("llm_doubao_chat").needs_nr_flow())
        self.assertFalse(ap.get_api_spec("llm_doubao_image").needs_nr_flow())
        self.assertFalse(ap.get_api_spec("llm_doubao_vision").needs_nr_flow())

    def test_caiyun_weather_spec(self):
        spec = sf.SUBFLOWS["llm_caiyun_weather"]
        self.assertEqual(spec.call["type"], "link_out")
        self.assertEqual(spec.call["entry_link_id"], "af_weather_in")
        raw = ap.get_api_spec("llm_caiyun_weather")
        self.assertEqual(raw.method.upper(), "GET")
        self.assertIn("caiyunapp.com", raw.url)
        self.assertEqual(raw.extract, "payload.result")
        # nr_tab=True 触发生成 NR 后端流
        self.assertTrue(raw.nr_tab)
        self.assertTrue(raw.needs_nr_flow())

    def test_anysearch_spec(self):
        spec = sf.SUBFLOWS["anysearch_batch"]
        self.assertEqual(spec.call["type"], "link_out")
        self.assertEqual(spec.call["entry_link_id"], "af_anysearch_in")
        raw = ap.get_api_spec("anysearch_batch")
        self.assertEqual(raw.method.upper(), "POST")
        self.assertEqual(raw.url, "https://api.anysearch.com/mcp")
        # 鉴权头 + JSON-RPC 请求体模板
        self.assertIn("Authorization", raw.nr_headers)
        self.assertIn("Bearer", raw.nr_headers["Authorization"])
        self.assertTrue(raw.nr_body_template)
        # 必填参数 keywords
        self.assertIn("keywords", spec.params)
        self.assertTrue(spec.params["keywords"].required)
        self.assertTrue(raw.needs_nr_flow())

    def test_build_nr_tab_flows_structure(self):
        tab_id = "TABX"
        nodes = ap.build_nr_tab_flows(tab_id)
        by_id = {n["id"]: n for n in nodes}
        # 无重复 id
        self.assertEqual(len(by_id), len(nodes))
        # 三个接口共 3 入口 + 每个 7~8 节点 = 24
        self.assertEqual(len(nodes), 24)
        # 每个节点的 z 都填了 tab_id
        self.assertTrue(all(n.get("z") == tab_id for n in nodes))
        # 三个入口 link in 存在且 id 与 spec.entry_link_id 对齐
        for eid in ("af_apisay_in", "af_weather_in", "af_anysearch_in"):
            self.assertEqual(by_id[eid]["type"], "link in")
            self.assertEqual(by_id[eid]["wires"], [[eid + "_validate"]])

    def test_build_tab_has_validation_and_error_handling(self):
        nodes = ap.build_nr_tab_flows("TABX")
        by_id = {n["id"]: n for n in nodes}
        for prefix in ("af_apisay_in", "af_weather_in", "af_anysearch_in"):
            # ① 校验必填 function（2 路输出：合法→0，缺失→1）
            v = by_id[prefix + "_validate"]
            self.assertEqual(v["type"], "function")
            self.assertEqual(v.get("outputs"), 2)
            self.assertIn("required", v["func"])
            # ② http request（ret=obj，返回原生对象供 JSONata 取值）
            h = by_id[prefix + "_http"]
            self.assertEqual(h["type"], "http request")
            self.assertEqual(h.get("ret"), "obj")
            # ③ 取返回值 change
            e = by_id[prefix + "_extract"]
            self.assertEqual(e["type"], "change")
            self.assertEqual(e["rules"][0]["p"], "payload.reply")
            # ④ 错误处理 function
            err = by_id[prefix + "_err"]
            self.assertEqual(err["type"], "function")
            self.assertIn("statusCode", err["func"])
            # ⑤ debug 观测
            self.assertEqual(by_id[prefix + "_dbg"]["type"], "debug")
            # ⑥ 末端 link out
            self.assertEqual(by_id[prefix + "_out"]["type"], "link out")

    def test_weather_is_get_skips_body(self):
        nodes = ap.build_nr_tab_flows("TABX")
        by_id = {n["id"]: n for n in nodes}
        # GET 不构造请求体 → 无 _body 节点，validate 直接连 http
        self.assertNotIn("af_weather_in_body", by_id)
        self.assertEqual(
            by_id["af_weather_in_validate"]["wires"],
            [["af_weather_in_http"], ["af_weather_in_out", "af_weather_in_dbg"]],
        )
        h = by_id["af_weather_in_http"]
        self.assertEqual(h["method"], "GET")
        self.assertIn("caiyunapp.com", h["url"])

    def test_anysearch_body_template_and_headers(self):
        nodes = ap.build_nr_tab_flows("TABX")
        by_id = {n["id"]: n for n in nodes}
        # POST 构造请求体：payload=JSON-RPC 信封，headers=Bearer
        body = by_id["af_anysearch_in_body"]
        self.assertEqual(body["type"], "change")
        rules = body["rules"]
        self.assertEqual(rules[0]["p"], "payload")
        self.assertEqual(rules[0]["tot"], "jsonata")
        self.assertIn("jsonrpc", rules[0]["to"])
        self.assertEqual(rules[1]["p"], "headers")
        self.assertEqual(rules[1]["tot"], "json")
        self.assertIn("Bearer", rules[1]["to"])
        self.assertEqual(by_id["af_anysearch_in_http"]["url"],
                         "https://api.anysearch.com/mcp")

    def test_doubao_say_assembles_into_tts(self):
        nodes = ap.build_nr_tab_flows("TABX")
        by_id = {n["id"]: n for n in nodes}
        # say 带 nr_assemble → 组装节点把 reply 包成 TTS 入参
        asm = by_id["af_apisay_in_assemble"]
        self.assertEqual(asm["type"], "change")
        self.assertEqual(asm["rules"][0]["tot"], "jsonata")
        self.assertIn("payload.reply", asm["rules"][0]["to"])
        # 末端 link out 指向 TTS 队列入口
        out = by_id["af_apisay_in_out"]
        self.assertEqual(out["links"], ["b595563939283231"])

    def test_built_tab_lint_has_no_hard_errors(self):
        nodes = ap.build_nr_tab_flows("TABX")
        issues = lint_flow({"nodes": nodes})
        hard = [i for i in issues if i.get("level") == "error"]
        self.assertEqual(hard, [],
                         f"生成 tab 存在硬伤：{[ (i.get('rule'),i.get('message')) for i in hard ]}")
        # 确认关键规则 R13(孤儿 api-call-service) / R15(紧环) 不触发
        blocked = {i.get("rule") for i in issues if i.get("level") == "error"}
        self.assertNotIn("R13", blocked)
        self.assertNotIn("R15", blocked)

    def test_help_lists_all(self):
        reset_config()
        gw = Gateway()
        helpd = gw.dsl_help()
        names = {s["name"] for s in helpd["subflows"]}
        for n in ALL_SPECS:
            self.assertIn(n, names)

    def test_every_spec_derives_valid_subflowspec(self):
        """A6 强化：API_SPECS 中每条 spec 都能派生合法 SubflowSpec（字段齐全）。"""
        for s in ap.API_SPECS:
            spec = ap.get_api_spec(s.name).to_subflow_spec()
            self.assertTrue(spec.title, f"{s.name} 缺 title")
            self.assertIsInstance(spec.call, dict, f"{s.name} call 非 dict")
            self.assertIn("type", spec.call, f"{s.name} call 缺 type")
            self.assertIn(spec.call["type"], ("http_api", "link_out"),
                          f"{s.name} call.type 非法: {spec.call['type']}")
            self.assertIsInstance(spec.params, dict, f"{s.name} params 非 dict")
            if spec.call["type"] == "link_out":
                self.assertTrue(spec.call.get("entry_link_id"),
                                f"{s.name} link_out 缺 entry_link_id")
            else:
                self.assertTrue(spec.call.get("url"), f"{s.name} http_api 缺 url")
                self.assertTrue(spec.call.get("extract"), f"{s.name} http_api 缺 extract")

    def test_built_tab_no_r16_r17_r18(self):
        """A6 + 新规则联动：生成的 tab 不含重复 id / 悬空连线 / 子流程死端口。"""
        nodes = ap.build_nr_tab_flows("TABX")
        issues = lint_flow({"nodes": nodes})
        bad = [i for i in issues if i.get("rule") in ("R16", "R17", "R18")]
        self.assertEqual(bad, [],
                         f"新规则误报/真伤：{[(i['rule'], i['message']) for i in bad]}")


if __name__ == "__main__":
    unittest.main()
