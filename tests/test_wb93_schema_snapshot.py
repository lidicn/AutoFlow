# -*- coding: utf-8 -*-
"""
WB93 · T007 §3 收口守卫 —— MCP 工具返回契约「schema 快照」

T007 §3 深扫发现 1 处真实漂移（`get_entity_state` 返回 `{ok,source,state{...}}` 嵌套信封，
但 docstring 旧版写扁平）+ 3 处文档欠完整（propose_dsl / list_entities / run_e2e_trace
实际返回键比 docstring 多）。§3 已 doc 校正（commit 9f47798），本文件把该契约**锁成回归守卫**，
防止未来重构把返回形状改回扁平或把文档键名回退。

守卫强度分两层：
- 运行时契约（最强）：直接调用 `autoflow_get_entity_state`，用 FakeHA 注入实体态，
  断言返回信封确为 `{ok, source, state{...}}` 且顶层绝无 `entity_id`（这正是 T007 §3.1 锁定的漂移点）。
- 文档契约快照：断言其余 3 工具的 docstring 仍包含 T007 §3.2 实证到的真实返回键（total/offset、
  fully_verified/verdict、trace/triggered/entity_warnings），防止文档回退到欠完整状态。

纯内存、不触真实 HA / 不写 prod。
"""
import json
import sys

import pytest

sys.path.insert(0, "E:/NAS/autoflow/src")

from autoflow_gateway import mcp_server


class _FakeHA:
    """最小化只读 HA 替身：仅实现 get_entity_state 用到的 get_state。"""

    def __init__(self, state):
        self._state = state

    def get_state(self, eid):
        return self._state


class _FakeGW:
    def __init__(self, state):
        self.ha = _FakeHA(state)


# ───────────── 运行时契约：get_entity_state 嵌套信封（T007 §3.1 漂移点）─────────────
def test_get_entity_state_nested_envelope(monkeypatch):
    live = {
        "entity_id": "light.yeelink_cn_555003624_lamp22_s_2",
        "state": "off",
        "attributes": {"friendly_name": "米家智能显示器挂灯1S 灯"},
        "last_changed": "2026-08-29T17:34:03.154171+00:00",
        "last_updated": "2026-08-29T17:34:03.154171+00:00",
        "context": {"id": "01M179BTWJFSBQPWFJKQDPQ3JG", "parent_id": None, "user_id": None},
    }
    monkeypatch.setattr(mcp_server, "Gateway", lambda: _FakeGW(live))

    out = mcp_server.autoflow_get_entity_state(
        "light.yeelink_cn_555003624_lamp22_s_2"
    )
    resp = json.loads(out)

    # 信封外层
    assert resp.get("ok") is True
    assert resp.get("source") == "live"
    # HA 实体结构必须嵌套在 state 下
    assert isinstance(resp.get("state"), dict)
    assert resp["state"]["entity_id"] == "light.yeelink_cn_555003624_lamp22_s_2"
    assert resp["state"]["state"] == "off"
    # 关键漂移点：顶层不得直接暴露 entity_id（T007 §3.1 旧 docstring 误导调用方写 resp["entity_id"]）
    assert "entity_id" not in resp, "漂移回归：顶层不应出现 entity_id，应走 resp['state']['entity_id']"


def test_get_entity_state_docstring_warns_nested(monkeypatch):
    """§3.1 doc 校正文本必须保留：明确告知调用方走 resp['state']['entity_id']。"""
    doc = (mcp_server.autoflow_get_entity_state.__doc__ or "")
    assert 'resp["state"]["entity_id"]' in doc or "resp['state']['entity_id']" in doc, \
        "get_entity_state docstring 须明确嵌套取字段路径，防止调用方 KeyErrer 回归"


# ───────────── 文档契约快照：3 处「文档欠完整」工具的键名不得回退 ─────────────
def test_propose_dsl_docstring_documents_gate_keys():
    doc = (mcp_server.autoflow_propose_dsl.__doc__ or "")
    for kw in ("fully_verified", "verdict"):
        assert kw in doc, f"propose_dsl docstring 缺失真实返回键 {kw!r}（T007 §3.2）"


def test_list_entities_docstring_documents_paging_keys():
    doc = (mcp_server.autoflow_list_entities.__doc__ or "")
    for kw in ("total", "offset"):
        assert kw in doc, f"list_entities docstring 缺失分页键 {kw!r}（T007 §3.2）"


def test_run_e2e_trace_docstring_documents_trace_keys():
    doc = (mcp_server.autoflow_run_e2e_trace.__doc__ or "")
    for kw in ("trace", "triggered", "entity_warnings"):
        assert kw in doc, f"run_e2e_trace docstring 缺失真实返回键 {kw!r}（T007 §3.2）"


# ───────────── 工具存在性快照：5 个 T007 抽测工具不得被误删 ─────────────
def test_t007_tools_registered():
    for name in (
        "autoflow_get_entity_state",
        "autoflow_verify_flow",
        "autoflow_propose_dsl",
        "autoflow_list_entities",
        "autoflow_run_e2e_trace",
    ):
        assert hasattr(mcp_server, name), f"T007 抽测工具丢失：{name}"
        assert callable(getattr(mcp_server, name)), f"{name} 不可调用"
