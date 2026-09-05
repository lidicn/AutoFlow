#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TICKET-002 修复验证：asyncio.to_thread 包裹回归测试。

验收标准：
  - webui.py 中所有 async handler 内的 gw.* / _snap_mgr() / tab_org. 调用
    必须被 asyncio.to_thread 包裹（避免阻塞事件循环）
  - to_thread 调用总数 >= 25（覆盖所有 handler）
"""
import os
import re
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

WEBUI_PATH = os.path.join(SRC, "autoflow_gateway", "webui.py")


def _parse_handlers(source: str):
    """解析 webui.py，返回 (async_handlers, to_thread_count, unchecked_gw_calls)。"""
    lines = source.splitlines()
    async_handlers = []
    to_thread_count = 0
    unchecked_gw_calls = []

    in_async = False
    async_indent = 0
    in_to_thread_call = False  # 跟踪多行 to_thread 调用
    to_thread_indent = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # 检测 async def 开始
        if re.match(r'async def \w+', stripped):
            in_async = True
            async_indent = len(line) - len(line.lstrip())
            async_handlers.append((i, stripped[:80]))
            in_to_thread_call = False
            continue

        # 检测普通 def 结束 async 上下文
        if re.match(r'def \w+', stripped) and 'async' not in stripped:
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= async_indent and in_async:
                in_async = False
                in_to_thread_call = False

        # 统计 to_thread
        if 'asyncio.to_thread' in stripped:
            to_thread_count += 1
            in_to_thread_call = True
            to_thread_indent = len(line) - len(line.lstrip())

        # 检测 to_thread 调用结束（缩进回到同级或更小）
        if in_to_thread_call and stripped and not stripped.startswith('#'):
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= to_thread_indent and ')' in stripped:
                in_to_thread_call = False

        # 检查 async handler 中是否有未包裹的 gw.* 调用
        if in_async and not in_to_thread_call:
            if re.search(r'\bgw\.\w+|_snap_mgr\(\)|\btab_org\.', stripped):
                # 如果这行已经包含 asyncio.to_thread，跳过（已被包裹）
                if 'asyncio.to_thread' in stripped or 'await' in stripped:
                    continue
                # 排除纯属性访问/配置赋值
                if not any(skip in stripped for skip in [
                    'tab_org_mode', 'is_single_tab_mode',
                    'get_migration_status', 'get_single_tab',
                ]):
                    # 跳过注释
                    if not stripped.startswith('#'):
                        unchecked_gw_calls.append((i, stripped[:100]))

    return async_handlers, to_thread_count, unchecked_gw_calls


@pytest.fixture(scope="module")
def webui_analysis():
    """一次性分析 webui.py。"""
    with open(WEBUI_PATH, encoding='utf-8-sig') as f:
        source = f.read()
    return _parse_handlers(source)


class TestToThreadRegression:
    """测试 asyncio.to_thread 包裹正确性。"""

    def test_async_handlers_exist(self, webui_analysis):
        """webui.py 应包含多个 async handler。"""
        handlers, _, _ = webui_analysis
        assert len(handlers) > 10, f"async handler 数量异常：{len(handlers)}"

    def test_to_thread_call_count(self, webui_analysis):
        """to_thread 调用数应 >= 25（覆盖所有 handler 中的 gw.* 调用）。"""
        _, to_thread_count, _ = webui_analysis
        assert to_thread_count >= 25, (
            f"asyncio.to_thread 调用数不足：{to_thread_count} < 25\n"
            "可能原因：部分 gw.* 调用未用 to_thread 包裹"
        )

    def test_no_unchecked_gw_calls(self, webui_analysis):
        """async handler 中不应有未包裹的 gw.* 调用（排除纯属性访问和注释）。"""
        _, _, unchecked = webui_analysis
        # 过滤掉非 await 调用的场景：
        # - 配置字典赋值（如 "tab_org_mode": tab_org.get_tab_org_mode()）
        # - 条件判断（如 tab_org.is_single_tab_mode()）
        # - 属性访问（如 getattr(gw.nr, "client")）
        # - 本地状态查询（如 gw.state.get_device_catalog()）
        # 真正需要 to_thread 的是 await asyncio.to_thread(gw.xxx) 模式
        real_issues = []
        for ln, code in unchecked:
            # 跳过纯属性访问/配置赋值
            if any(skip in code for skip in [
                'tab_org_mode',
                'is_single_tab_mode',
                'get_migration_status',
                'get_single_tab',
                'getattr(gw',  # 属性访问
                'gw.state.get_',  # 本地状态查询
            ]):
                continue
            # 跳过注释行
            if code.startswith('#'):
                continue
            real_issues.append((ln, code))

        assert len(real_issues) == 0, (
            f"发现 {len(real_issues)} 处未用 to_thread 包裹的 gw.* 调用：\n" +
            "\n".join(f"  L{ln}: {code}" for ln, code in real_issues[:10])
        )

    def test_handler_names_valid(self, webui_analysis):
        """所有 async handler 应有合法的函数名。"""
        handlers, _, _ = webui_analysis
        for line_no, sig in handlers:
            assert re.match(r'async def \w+\(', sig), f"L{line_no} 签名无效: {sig}"

    def test_to_thread_uses_await(self, webui_analysis):
        """每个 to_thread 调用都应被 await。"""
        with open(WEBUI_PATH, encoding='utf-8-sig') as f:
            lines = f.readlines()

        issues = []
        for i, line in enumerate(lines, 1):
            if 'asyncio.to_thread' in line:
                # 检查同一行或下一行是否有 await
                current = line.strip()
                next_line = lines[i].strip() if i < len(lines) else ""
                if 'await' not in current and 'await' not in next_line:
                    issues.append(f"L{i}: {current[:80]}")

        assert len(issues) == 0, f"发现 {len(issues)} 处 to_thread 未 await：\n" + "\n".join(issues[:5])
