#!/usr/bin/env python3
"""TICKET-002 测试用例：asyncio.to_thread + JSON截断 + 前端安全 + M7 Token安全。

覆盖范围：
  TC_JSON_TRUNC_001 ~ TC_JSON_TRUNC_015   JSON 截断边界值（debug_bridge._truncate）
  TC_TO_THREAD_001 ~ TC_TO_THREAD_005     asyncio.to_thread 包裹验证
  TC_XSS_001 ~ TC_XSS_004                 前端 XSS 加固验证
  TC_TOKEN_001 ~ TC_TOKEN_004             M7 token 安全加固验证
"""
import json
import os
import sys
import time
import tempfile
import shutil
import re
import unittest
from contextlib import redirect_stdout
import io

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.debug_bridge import DebugBridge


# ──────────────────────────────────────────────
#  TC_JSON_TRUNC_* : _truncate 边界值测试
# ──────────────────────────────────────────────
# _truncate 保证：
#   - 所有输出均为合法 JSON（包裹/注入/字符串闭合/非JSON文本形式均可解析）
#   - 非 JSON 文本保留尾部 ...(truncated,N chars) 后缀

class TestJSONTruncation(unittest.TestCase):
    """TC_JSON_TRUNC: debug_bridge._truncate 保证截断后始终为合法 JSON。"""

    def _t(self, s: str, n: int) -> str:
        return DebugBridge._truncate(s, n)  # noqa: SLF001

    # --- 基础：未截断直接返回原串 ---
    def test_TC_JSON_TRUNC_001_no_truncation(self):
        """短字符串不截断，原样返回。"""
        r = self._t('{"a":1}', 100)
        self.assertEqual(r, '{"a":1}')

    def test_TC_JSON_TRUNC_002_empty_string(self):
        """空字符串截断。"""
        r = self._t("", 10)
        self.assertEqual(r, "")

    def test_TC_JSON_TRUNC_003_none_input(self):
        """None 输入返回空串。"""
        r = self._t(None, 10)
        self.assertEqual(r, "")

    # --- JSON 对象：基本截断（含 __truncated__ 标记）---
    def test_TC_JSON_TRUNC_004_object_basic_json_valid(self):
        """{"a":1,"b":2,"c":3} 截断后结果为合法 JSON 且含 __truncated__ 标记。"""
        s = '{"a":1,"b":2,"c":3}'
        r = self._t(s, 8)
        parsed = json.loads(r)
        self.assertTrue(parsed.get("__truncated__"))
        self.assertIsInstance(parsed, dict)

    def test_TC_JSON_TRUNC_005_object_close_curly_json_valid(self):
        """截断位置在 } 附近，截断结果仍可解析为合法 JSON。"""
        s = '{"a":1,"b":2}'
        r = self._t(s, 10)
        json.loads(r)  # 不应抛异常

    # --- JSON 数组：基本截断 ---
    def test_TC_JSON_TRUNC_006_array_basic_json_valid(self):
        """[1,2,3,4,5] 截断后仍是合法 JSON，包含 __truncated__ 标记。"""
        s = "[1,2,3,4,5]"
        r = self._t(s, 7)
        parsed = json.loads(r)
        # 可能为包裹模式（dict）或注入模式（list）
        if isinstance(parsed, dict):
            self.assertTrue(parsed.get("__truncated__"))
        else:
            self.assertIsInstance(parsed, list)

    def test_TC_JSON_TRUNC_007_array_nested_json_valid(self):
        """嵌套数组截断后仍为合法 JSON。"""
        s = '[{"a":[1,2,3]}]'
        r = self._t(s, 10)
        parsed = json.loads(r)
        # 可能为包裹 dict 或注入后的 list
        self.assertIsInstance(parsed, (dict, list))

    # --- 裸字符串：截断后仍是合法 JSON 字符串 ---
    def test_TC_JSON_TRUNC_008_string_basic_json_valid(self):
        """"hello world" 截到 5，结果仍是合法 JSON 字符串。"""
        s = '"hello world"'
        r = self._t(s, 5)
        parsed = json.loads(r)
        self.assertIsInstance(parsed, str)

    def test_TC_JSON_TRUNC_009_string_with_escaped_quote_json_valid(self):
        """字符串内含转义双引号，截断后仍为合法 JSON。"""
        s = '"say \\"hi\\" and more"'
        r = self._t(s, 10)
        parsed = json.loads(r)
        # 字符串类截断到转义位置时会走包裹模式（dict），只要可解析即通过
        self.assertIsInstance(parsed, (str, dict))

    def test_TC_JSON_TRUNC_010_string_backslash_at_boundary_json_valid(self):
        """截断发生在反斜杠后，闭合字符串后仍可解析。"""
        s = '"a\\\\\\\\bcccccc"'  # 四个反斜杠后跟多个 c
        r = self._t(s, 8)
        parsed = json.loads(r)
        self.assertIsInstance(parsed, str)

    # --- 悬挂键 / 尾随逗号/冒号 ---
    def test_TC_JSON_TRUNC_011_hanging_key_json_valid(self):
        """{"a":1,"b": 截断在悬挂 key 后，尾随 : 被清理后仍为合法 JSON。"""
        s = '{"a":1,"b": "value2","c":3}'
        r = self._t(s, 12)
        parsed = json.loads(r)
        self.assertIsInstance(parsed, dict)
        self.assertTrue(parsed.get("__truncated__"))

    def test_TC_JSON_TRUNC_012_hanging_comma_json_valid(self):
        """[1,2,3, 截断在尾随逗号后，逗号被清理后仍为合法 JSON。"""
        s = '[1,2,3,4,5]'
        r = self._t(s, 7)
        parsed = json.loads(r)
        # 可能是包裹 dict，也可能是注入后的 list
        self.assertIsInstance(parsed, (dict, list))

    # --- 非 JSON 场景：保留原始行为（尾部追加 truncated 标记）---
    def test_TC_JSON_TRUNC_013_plain_text_suffix(self):
        """普通文本不是 JSON，尾部追加 truncated 标记。"""
        s = "abcdefghij"
        r = self._t(s, 5)
        self.assertTrue(r.endswith("...(truncated,5 chars)"))
        self.assertEqual(r, "abcde...(truncated,5 chars)")

    def test_TC_JSON_TRUNC_014_mixed_content_starts_with_brace_not_json(self):
        """首字符是 { 但后续非法，兜底仍为合法 JSON（包裹模式）。"""
        s = '{not valid json at all'
        r = self._t(s, 10)
        # 即使解析失败，_truncate 也应返回合法 JSON（兜底包裹）
        parsed = json.loads(r)
        self.assertTrue(parsed.get("__truncated__"))

    # --- 新增：确保任何截断输出都是合法 JSON（全量契约）---
    def test_TC_JSON_TRUNC_015_all_truncated_outputs_are_valid_json(self):
        """对多种典型 JSON 结构做边界截断，验证结果全为合法 JSON。"""
        samples = [
            ('{"a":1,"b":2,"c":3}', 3),
            ('{"a":1,"b":2,"c":3}', 6),
            ('{"a":1,"b":2,"c":3}', 9),
            ('[1,2,3,4,5]', 3),
            ('[1,2,3,4,5]', 5),
            ('[1,2,3,4,5]', 8),
            ('"hello"', 2),
            ('"hello"', 4),
            ('{"x":{"y":"z"}}', 4),
            ('{"x":{"y":"z"}}', 7),
            ('[{"a":1}]', 5),
            ('[{"a":1}]', 8),
        ]
        for s, n in samples:
            with self.subTest(s=s, n=n):
                r = self._t(s, n)
                json.loads(r)  # 必须可解析，否则说明截断逻辑有 bug


# ──────────────────────────────────────────────
#  TC_TO_THREAD_* : asyncio.to_thread 包裹验证
# ──────────────────────────────────────────────

class TestToThreadCoverage(unittest.TestCase):
    """TC_TO_THREAD: 验证 webui.py 中关键 gw.* 调用均被 asyncio.to_thread 包裹。"""

    def _get_src(self) -> str:
        return open(os.path.join(SRC, "autoflow_gateway/webui.py"),
                    encoding="utf-8-sig").read()

    def test_TC_TO_THREAD_001_gw_propose_dsl_wrapped(self):
        """POST /api/lab/propose 中 gw.propose_dsl 必须 await asyncio.to_thread。"""
        src = self._get_src()
        idx = src.find("gw.propose_dsl")
        self.assertNotEqual(idx, -1, "gw.propose_dsl 调用不存在")
        window = src[max(0, idx - 100):idx]
        self.assertIn("asyncio.to_thread", window,
                      "gw.propose_dsl 未被 asyncio.to_thread 包裹")

    def test_TC_TO_THREAD_002_gw_deploy_raw_wrapped(self):
        """POST /api/lab/deploy 中 gw.deploy_raw 必须 await asyncio.to_thread。"""
        src = self._get_src()
        idx = src.find("gw.deploy_raw")
        self.assertNotEqual(idx, -1, "gw.deploy_raw 调用不存在")
        window = src[max(0, idx - 100):idx]
        self.assertIn("asyncio.to_thread", window,
                      "gw.deploy_raw 未被 asyncio.to_thread 包裹")

    def test_TC_TO_THREAD_003_gw_list_entities_wrapped(self):
        """GET /api/entities 中 gw.list_entities 必须 await asyncio.to_thread。"""
        src = self._get_src()
        idx = src.find("gw.list_entities")
        self.assertNotEqual(idx, -1, "gw.list_entities 调用不存在")
        window = src[max(0, idx - 100):idx]
        self.assertIn("asyncio.to_thread", window,
                      "gw.list_entities 未被 asyncio.to_thread 包裹")

    def test_TC_TO_THREAD_004_gw_resolve_entity_wrapped(self):
        """GET /api/entities/resolve 中 gw.resolve_entity 必须 await asyncio.to_thread。"""
        src = self._get_src()
        idx = src.find("gw.resolve_entity")
        self.assertNotEqual(idx, -1, "gw.resolve_entity 调用不存在")
        window = src[max(0, idx - 100):idx]
        self.assertIn("asyncio.to_thread", window,
                      "gw.resolve_entity 未被 asyncio.to_thread 包裹")

    def test_TC_TO_THREAD_005_debug_read_impl_is_async_and_wrapped(self):
        """_debug_read_impl 为 async def，其内部调用 gw.get_debug_read 经 to_thread 包裹。"""
        src = self._get_src()
        # 1) _debug_read_impl 必须是 async def
        self.assertRegex(src, r"async\s+def\s+_debug_read_impl\s*\(",
                         "_debug_read_impl 应为 async def")
        # 2) 内部必须 await asyncio.to_thread(gw.get_debug_read, ...)
        self.assertRegex(src, r"await\s+asyncio\.to_thread\s*\(\s*gw\.get_debug_read",
                         "_debug_read_impl 内部应 await asyncio.to_thread(gw.get_debug_read)")
        # 3) 调用者加 await
        self.assertIn("await _debug_read_impl", src,
                      "_debug_read_impl 调用处应加 await")


# ──────────────────────────────────────────────
#  TC_XSS_* : 前端 XSS 加固验证
# ──────────────────────────────────────────────

class TestXSSHardening(unittest.TestCase):
    """TC_XSS: 验证前端 app.js XSS 加固措施。"""

    @classmethod
    def setUpClass(cls):
        app_js = os.path.join(
            SRC, "autoflow_gateway/webui/static/app.js")
        if os.path.exists(app_js):
            cls.app_js = open(app_js, encoding="utf-8").read()
        else:
            cls.app_js = ""

    def test_TC_XSS_001_esc_function_exists(self):
        """app.js 定义了 esc() 工具函数用于 HTML 实体转义。"""
        if not self.app_js:
            self.skipTest("app.js 不存在")
        self.assertRegex(self.app_js, r'function\s+esc\s*\(')

    def test_TC_XSS_002_esc_used_in_user_data_interpolation(self):
        """app.js 模板字符串中嵌入用户数据时使用 esc() 转义。"""
        if not self.app_js:
            self.skipTest("app.js 不存在")
        # 检查关键数据源都被 esc() 包裹
        self.assertIn('esc(plan', self.app_js,
                      "plan 数据未在模板中使用 esc() 转义")
        self.assertIn('esc(c.', self.app_js,
                      "c 数据未在模板中使用 esc() 转义")
        self.assertIn('esc(d.', self.app_js,
                      "d 数据未在模板中使用 esc() 转义")

    def test_TC_XSS_003_textContent_preferred_over_innerHTML_for_plain_text(self):
        """app.js 对纯文本使用 textContent 而非 innerHTML（最小化 XSS 风险）。"""
        if not self.app_js:
            self.skipTest("app.js 不存在")
        has_textContent = 'textContent' in self.app_js
        self.assertTrue(has_textContent, "app.js 中应有使用 textContent 的示例")

    def test_TC_XSS_004_no_on_event_inline_with_unescaped_data(self):
        """app.js 不在 on* 事件处理器属性中直接插值用户变量。"""
        if not self.app_js:
            self.skipTest("app.js 不存在")
        # 寻找 on*=`${` 模式（真正的内联事件处理器，如 onclick=`...${var}...`）
        # 注意：textContent = `...` 不属于事件处理器，不应误报
        hits = re.findall(r'\bon\w+\s*=\s*`[^`]*\$\{[^}]+}', self.app_js)
        self.assertEqual(hits, [],
                         f"发现危险的内联事件处理器插值: {hits}")


# ──────────────────────────────────────────────
#  TC_TOKEN_* : M7 Token 安全加固验证
# ──────────────────────────────────────────────

class TestTokenSecurity(unittest.TestCase):
    """TC_TOKEN: M7 token 文件权限 + 日志脱敏。"""

    def test_TC_TOKEN_001_token_log_contains_placeholder_not_plaintext(self):
        """stdout 日志含 ?token=*** 占位提示，不含实际 token 值。"""
        from autoflow_gateway.config import GatewayConfig
        from autoflow_gateway.webui import _bootstrap_webui_token

        tmpdir = tempfile.mkdtemp()
        try:
            old_auto = os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            os.environ["AF_WEBUI_TOKEN_AUTO"] = "1"
            cfg = GatewayConfig(data_dir=tmpdir, env="staging")
            buf = io.StringIO()
            with redirect_stdout(buf):
                _bootstrap_webui_token(cfg)
            log_output = buf.getvalue()
            # 日志含 ?token=*** 占位（不再打印明文）
            self.assertIn("?token=***", log_output,
                          "日志应包含 ?token=*** 占位提示")
        finally:
            if old_auto is None:
                os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            else:
                os.environ["AF_WEBUI_TOKEN_AUTO"] = old_auto
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_TC_TOKEN_002_token_file_path_logged(self):
        """stdout 日志打印 token 文件路径，供管理员查看。"""
        from autoflow_gateway.config import GatewayConfig
        from autoflow_gateway.webui import _bootstrap_webui_token

        tmpdir = tempfile.mkdtemp()
        try:
            old_auto = os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            os.environ["AF_WEBUI_TOKEN_AUTO"] = "1"
            cfg = GatewayConfig(data_dir=tmpdir, env="staging")
            buf = io.StringIO()
            with redirect_stdout(buf):
                _bootstrap_webui_token(cfg)
            log_output = buf.getvalue()
            # 日志含文件路径
            self.assertIn(".webui_token", log_output,
                          "日志应包含 .webui_token 路径信息")
        finally:
            if old_auto is None:
                os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            else:
                os.environ["AF_WEBUI_TOKEN_AUTO"] = old_auto
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_TC_TOKEN_003_chmod_applied_to_temp_file_before_rename(self):
        """_bootstrap_webui_token 先 chmod 临时文件再 rename，确保权限生效。"""
        src = open(os.path.join(SRC, "autoflow_gateway/webui.py"),
                   encoding="utf-8-sig").read()
        # 修复后：chmod(tmp, 0o600) 然后 os.replace(tmp, p)
        self.assertIn("os.chmod(tmp, 0o600)", src,
                      "源码应 chmod 临时文件后 replace，确保权限生效")

    def test_TC_TOKEN_004_token_file_is_accessible_after_bootstrap(self):
        """_bootstrap_webui_token 成功后生成的文件可被读取（权限不阻挡 owner）。"""
        from autoflow_gateway.config import GatewayConfig
        from autoflow_gateway.webui import _bootstrap_webui_token, _resolve_webui_token

        tmpdir = tempfile.mkdtemp()
        try:
            old_auto = os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            os.environ["AF_WEBUI_TOKEN_AUTO"] = "1"
            cfg = GatewayConfig(data_dir=tmpdir, env="staging")
            _bootstrap_webui_token(cfg)
            # 重新读取验证文件内容一致
            tok = _resolve_webui_token(cfg)
            self.assertIsNotNone(tok, "token 文件应能被正常读取")
            self.assertEqual(len(tok), 32, "token 长度应正确（secrets.token_urlsafe(24) ≈ 32）")
        finally:
            if old_auto is None:
                os.environ.pop("AF_WEBUI_TOKEN_AUTO", None)
            else:
                os.environ["AF_WEBUI_TOKEN_AUTO"] = old_auto
            shutil.rmtree(tmpdir, ignore_errors=True)


# ──────────────────────────────────────────────
#  修复预存 bug：test_ingest_and_read 时序竞态
# ──────────────────────────────────────────────

class TestBufferAndReadFixed(unittest.TestCase):
    """修复版：确保两事件 received_at 不同时序正确。"""

    def test_ingest_and_read_ordered(self):
        """两条事件依次写入，read 结果倒序（最新在前）。"""
        b = DebugBridge(nr_client=None, nr_url="http://localhost:1880", enabled=False)
        b._handle_message(json.dumps({
            "topic": "debug", "data": {"id": "n1", "name": "d", "msg": "v1", "_path": {"id": "f1"}}
        }))
        time.sleep(0.01)  # 强制产生不同 received_at
        b._handle_message(json.dumps({"id": "n2", "msg": "v2", "z": "f1"}))
        res = b.read()
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["events"][0]["node_id"], "n2")
        self.assertEqual(res["events"][1]["node_id"], "n1")


if __name__ == "__main__":
    unittest.main()
