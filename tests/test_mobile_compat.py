#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TICKET-002 修复验证：移动端兼容性测试。

验收标准：
  - style.css 中 textarea/input/select 的 font-size >= 16px
  - touch-action 设置存在
  - 移动端媒体查询包含必要样式
"""
import os
import re
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
STYLE_CSS_PATH = os.path.join(SRC, "autoflow_gateway", "webui", "static", "style.css")


class TestMobileCompat:
    """测试移动端兼容性。"""

    def _extract_media_block(self, content: str, max_width: int) -> str:
        """从 CSS 内容中提取指定 max-width 的媒体查询块（处理嵌套大括号）。"""
        # 找到 @media (max-width: Npx) 的起始位置
        pattern = rf'@media\s*\([^)]*max-width\s*:\s*{max_width}px'
        match = re.search(pattern, content)
        if not match:
            return ""

        # 找到对应的 { 并计数平衡
        start = match.end()
        brace_count = 0
        i = start
        while i < len(content):
            if content[i] == '{':
                brace_count += 1
            elif content[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    return content[start:i]
            i += 1
        return ""

    def test_input_font_size_16px(self):
        """input/textarea/select 在移动端应有 font-size >= 16px。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        mobile_block = self._extract_media_block(content, 768)
        assert mobile_block, "未找到 @media (max-width: 768px) 媒体查询"

        # 检查 input/select/textarea 的 font-size
        assert 'font-size: 16px' in mobile_block, (
            "移动端缺少 font-size: 16px 规则\n"
            "iOS 要求 >= 16px 避免自动缩放"
        )
        # 确认规则针对 input/select/textarea
        assert any(sel in mobile_block for sel in ['input', 'select', 'textarea']), \
            "font-size: 16px 规则未应用于 input/select/textarea"

    def test_touch_action_set(self):
        """button/btn/a 应设置 touch-action: manipulation。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        # 查找 touch-action 规则
        assert 'touch-action' in content, "style.css 缺少 touch-action 设置"

        # 检查是否在移动端媒体查询内
        mobile_match = re.search(
            r'@media\s*\([^)]*max-width[^)]*\).*?touch-action',
            content,
            re.DOTALL
        )

        assert mobile_match, "touch-action 未设置在移动端媒体查询内"

    def test_mobile_media_query_exists(self):
        """应存在移动端媒体查询（max-width: 768px）。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        assert '@media (max-width: 768px)' in content, \
            "缺少 @media (max-width: 768px) 媒体查询"

    def test_sidebar_hidden_on_mobile(self):
        """移动端 sidebar 应隐藏。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        mobile_block = self._extract_media_block(content, 768)
        assert mobile_block, "未找到 768px 媒体查询块"

        assert 'display: none' in mobile_block and '.sidebar' in mobile_block, \
            "移动端应隐藏 sidebar"

    def test_bottomnav_visible_on_mobile(self):
        """移动端 bottomnav 应显示。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        mobile_block = self._extract_media_block(content, 768)
        assert mobile_block, "未找到 768px 媒体查询块"

        assert 'display: flex' in mobile_block and '.bottomnav' in mobile_block, \
            "移动端应显示 bottomnav"

    def test_touch_target_min_44px(self):
        """按钮触摸目标应 >= 44px。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        mobile_block = self._extract_media_block(content, 768)
        assert mobile_block, "未找到 768px 媒体查询块"

        # 检查 min-height 或 min-width
        has_44px = 'min-height: 44px' in mobile_block or 'min-width: 44px' in mobile_block
        assert has_44px, "移动端按钮缺少 min-44px 触摸目标尺寸"

    def test_safe_area_variables(self):
        """应使用 safe area CSS 变量。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        assert '--sat' in content or '--sab' in content, \
            "缺少 safe area CSS 变量 (--sat/--sab)"

    def test_no_horizontal_scroll(self):
        """移动端不应有水平滚动。"""
        with open(STYLE_CSS_PATH, encoding='utf-8') as f:
            content = f.read()

        # 检查是否有 overflow-x: auto/scroll 在移动端
        mobile_blocks = re.findall(
            r'@media\s*\([^)]*max-width[^)]*\)\s*\{([^}]*)\}',
            content,
            re.DOTALL
        )

        for block in mobile_blocks:
            if 'overflow-x' in block:
                assert 'hidden' in block or 'auto' not in block, \
                    "移动端可能存在水平滚动风险"
