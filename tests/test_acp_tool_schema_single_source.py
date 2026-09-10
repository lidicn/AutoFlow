# -*- coding: utf-8 -*-
"""ACP 工具面「单一真相源」守卫（v2.0.12-2，收尾 A20/A27）。

背景：/acp 的工具面曾**手写**一份 JSON schema，与本端 MCP 实现并行维护，
结果漂移——手写 `delegate_to_memory_worker` 的参数写的是 `context`，
真实实现参数是 `context_json`（手写账本骗了调用方）。

现改为：ACP 工具面 = MCP 注册表（FastMCP `_tool_manager`）的**投影**，
schema 逐字取自 /mcp 生成的 `parameters`，字段名用 `inputSchema`（camel，
对齐对端 memory-agent 与 MCP 惯例；F-ACP-KEY 2026-09-10 裁决）。
本文件把四条不变量锁死：

1. **投影一致性**：ACP 每个工具的 inputSchema 与 MCP 注册表 diff=0（防再漂移）。
2. **字段名**：必须是 `inputSchema`（camel），不得回退到 `input_schema`。
3. **签名一致性**：schema 的 property 名 == 原始函数签名参数名；required == 无默认值参数。
4. **失败即抛**：映射引用不存在的 MCP 工具时 _build_acp_tools 必须 RuntimeError（不静默降级）。

运行：pytest tests/test_acp_tool_schema_single_source.py
"""
import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ.setdefault("AUTOFLLOW_DATA_DIR", tempfile.mkdtemp(prefix="af_acpschema_"))

import pytest

import autoflow_gateway.mcp_server as m


def _registry():
    return {t.name: t for t in m.mcp._tool_manager.list_tools()}


def _first_paragraph(doc: str) -> str:
    d = (doc or "").strip()
    return d.split("\n\n", 1)[0].strip() if d else ""


# ── 1. 投影一致性：ACP schema == MCP 注册表 schema ──────────────
def test_acp_schema_is_mcp_registry_projection():
    reg = _registry()
    assert m._ACP_TOOLS, "ACP 工具面不得为空"
    for acp in m._ACP_TOOLS:
        mcp_name = m._ACP_TOOL_MAP[acp["name"]]
        t = reg[mcp_name]
        # 逐字一致（diff=0）——这是防漂移的核心断言
        assert acp["inputSchema"] == t.parameters, \
            f"{acp['name']} 的 schema 与 MCP 注册表漂移"
        assert acp["description"] == _first_paragraph(t.description)


def test_acp_uses_camelcase_inputschema_key():
    """F-ACP-KEY（2026-09-10 裁决）：字段名统一为 inputSchema，跟随 memory-agent + MCP 惯例。"""
    for acp in m._ACP_TOOLS:
        assert "inputSchema" in acp, f"{acp['name']} 缺 inputSchema"
        assert "input_schema" not in acp, \
            f"{acp['name']} 仍用旧 snake 字段名 input_schema（已裁决对齐 camel）"


def test_acp_tools_names_stable():
    # ACP 对端稳定契约：暴露名不得轻易变动（改名=破坏 memory-agent 对端）
    assert [t["name"] for t in m._ACP_TOOLS] == [
        "list_entities", "get_entity_state",
        "list_automations", "delegate_to_memory_worker"]


# ── 2. 签名一致性：schema 名集/必填集 == 原始函数签名 ──────────────
def test_acp_schema_matches_raw_function_signature():
    reg = _registry()
    for acp in m._ACP_TOOLS:
        t = reg[m._ACP_TOOL_MAP[acp["name"]]]
        sig = inspect.signature(t.fn)
        params = [p for p in sig.parameters.values()
                  if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                inspect.Parameter.KEYWORD_ONLY)]
        names = {p.name for p in params}
        required = {p.name for p in params
                    if p.default is inspect.Parameter.empty}
        props = set(acp["inputSchema"].get("properties", {}).keys())
        assert props == names, \
            f"{acp['name']} schema 属性名 {props} != 签名参数 {names}"
        assert set(acp["inputSchema"].get("required", [])) == required, \
            f"{acp['name']} required 与签名默认值不一致"


# ── 3. 历史漂移点锁死：delegate 参数必须是 context_json ──────────────
def test_delegate_exposes_context_json_not_context():
    d = next(t for t in m._ACP_TOOLS if t["name"] == "delegate_to_memory_worker")
    props = set(d["inputSchema"].get("properties", {}))
    assert "context_json" in props, "delegate 应暴露 context_json（真实实现参数）"
    assert "context" not in props, "`context` 是已修复的历史漂移写法，不得回归"


def test_list_entities_exposes_pagination():
    # 手写版本漏了 offset，投影后必须与实现一致
    e = next(t for t in m._ACP_TOOLS if t["name"] == "list_entities")
    props = set(e["inputSchema"].get("properties", {}))
    assert {"domain", "area", "keyword", "limit", "offset"} <= props


# ── 4. 失败即抛（防静默降级成假绿）──────────────────────────────
def test_missing_mcp_tool_raises(monkeypatch):
    bad = dict(m._ACP_TOOL_MAP)
    bad["bogus_tool"] = "autoflow_this_tool_does_not_exist_xyz"
    monkeypatch.setattr(m, "_ACP_TOOL_MAP", bad)
    with pytest.raises(RuntimeError):
        m._build_acp_tools()


# ── 5. 端点接线：initialize 返回的工具面就是派生结果 ──────────────
def test_initialize_returns_derived_tools():
    r = m._acp_result_initialize({"id": 1})
    assert r["result"]["tools"] == m._ACP_TOOLS


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))
