#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5 大子流程「离线编译 → 节点结构」端到端验证（#592/#593）。

不依赖 live NR / live HA：仅用 dsl_engine.compile_dsl 把含各能力调用的 DSL
编译成 NR flow dict，再断言编译产物节点类型正确。这是「能否跑通」的第一层闸门——
若编译产物节点类型/链接目标错，部署到 NR 后必然 unknown 或断连。

5 个能力（用户指定）：
- 历史查询  history_occurred  → subflow 实例型（type=subflow:<id>）
- bark      bark_push         → subflow 实例型（type=subflow:<id>，flat 平铺 msg.<k>）
- tts       demo_notify         → link_out 型（type=link out，links=[entry_link_id]）
- 彩云天气  llm_caiyun_weather → link_out 型
- anysearch anysearch_batch   → link_out 型

运行：python tests/test_subflow_e2e_compile.py
"""
import os
import sys
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from autoflow_gateway.dsl_engine import compile_dsl, DSLError
from autoflow_gateway.subflows import get_subflow


# 每个能力的 DSL + 期望调用形态（id 从注册表实时取，避免硬编码漂移）
def _expect(name):
    spec = get_subflow(name)
    assert spec is not None, f"注册表缺 {name}"
    return spec.call  # {"type":"subflow","subflow_id":...} | {"type":"link_out","entry_link_id":...}


DSL = {
    "history_occurred": """
场景: 测试历史查询
触发: sensor.front_door 有人
调用子流程: history_occurred(entity=light.study, start=昨天11:00, end=昨天12:00, state=on)
""",
    "bark_push": """
场景: 测试Bark
触发: sensor.front_door 有人
调用子流程: bark_push(title=书房提醒, body=有人来了)
""",
    "demo_notify": """
场景: 测试TTS
触发: sensor.front_door 有人
调用子流程: demo_notify(text=有人来了, room=书房, level=一般)
""",
    "llm_caiyun_weather": """
场景: 测试彩云天气
触发: sensor.front_door 有人
调用子流程: llm_caiyun_weather()
""",
    "anysearch_batch": """
场景: 测试AnySearch
触发: sensor.front_door 有人
调用子流程: anysearch_batch(keywords=`mac mini m5, 苹果眼镜`)
""",
}


def _compile(name):
    return compile_dsl(DSL[name])["nodes"]


# ── 1) 每个能力编译出正确节点类型 + 正确目标 id ─────────────────────────
def test_history_occurred_subflow_node():
    call = _expect("history_occurred")
    assert call["type"] == "subflow"
    nodes = _compile("history_occurred")
    subs = [n for n in nodes if n.get("type") == f"subflow:{call['subflow_id']}"]
    assert subs, f"缺 history_occurred 子流程实例（期望 type=subflow:{call['subflow_id']}）"
    # 前置 change 平铺 msg.entity / msg.start / msg.end / msg.state
    chg = [n for n in nodes if n["type"] == "change"][0]
    paths = [(r["p"], r["pt"]) for r in chg["rules"]]
    for k in ("entity", "start", "end"):
        assert (k, "msg") in paths, f"history flat 应平铺 msg.{k}，实得 {paths}"


def test_bark_push_subflow_node():
    call = _expect("bark_push")
    assert call["type"] == "subflow"
    nodes = _compile("bark_push")
    subs = [n for n in nodes if n.get("type") == f"subflow:{call['subflow_id']}"]
    assert subs, f"缺 bark_push 子流程实例（期望 type=subflow:{call['subflow_id']}）"
    chg = [n for n in nodes if n["type"] == "change"][0]
    paths = [(r["p"], r["pt"]) for r in chg["rules"]]
    assert ("title", "msg") in paths and ("body", "msg") in paths


def test_demo_notify_linkout_node():
    call = _expect("demo_notify")
    assert call["type"] == "link_out"
    _assert_linkout(_compile("demo_notify"), call["entry_link_id"])


def test_weather_linkout_node():
    call = _expect("llm_caiyun_weather")
    assert call["type"] == "link_out"
    _assert_linkout(_compile("llm_caiyun_weather"), call["entry_link_id"])


def test_anysearch_linkout_node():
    call = _expect("anysearch_batch")
    assert call["type"] == "link_out"
    _assert_linkout(_compile("anysearch_batch"), call["entry_link_id"])


def _assert_linkout(nodes, entry_link_id):
    outs = [n for n in nodes if n.get("type") == "link out"]
    assert outs, "缺 link out 节点"
    # 关键：link out 必须指向注册表的 entry_link_id，否则 NR 里断连
    assert outs[0].get("links") == [entry_link_id], \
        f"link out links 应==[{entry_link_id}]，实得 {outs[0].get('links')}"
    # 前置 change 把入参塞进 msg.payload（payload 风格）
    chg = [n for n in nodes if n["type"] == "change"][0]
    assert any(r.get("p") == "payload" for r in chg["rules"]), "link_out 前置 change 应设 msg.payload"


# ── 2) 单一 flow 内 5 个能力共存（接近真实黑箱场景）─────────────────────
def test_all_five_together():
    dsl = "\n".join([
        "场景: 五合一验证",
        "触发: sensor.front_door 有人",
        "调用子流程: history_occurred(entity=light.study, start=昨天11:00, end=昨天12:00, state=on)",
        "调用子流程: bark_push(title=测试, body=五合一)",
        "调用子流程: demo_notify(text=五合一验证, room=书房, level=一般)",
        "调用子流程: llm_caiyun_weather()",
        "调用子流程: anysearch_batch(keywords=`mac mini m5`)",
    ])
    flow = compile_dsl(dsl)
    nodes = flow["nodes"]
    # 类型计数：2 个 subflow 实例 + 3 个 link out（各带前置 change，共 5 change）
    subs = [n for n in nodes if str(n.get("type", "")).startswith("subflow:")]
    outs = [n for n in nodes if n.get("type") == "link out"]
    chgs = [n for n in nodes if n["type"] == "change"]
    assert len(subs) == 2, f"应有 2 个 subflow 实例，实得 {len(subs)}"
    assert len(outs) == 3, f"应有 3 个 link out，实得 {len(outs)}"
    assert len(chgs) == 5, f"应有 5 个前置 change，实得 {len(chgs)}"
    # 三个 link out 的目标 id 全部正确
    targets = {outs[0].get("links")[0], outs[1].get("links")[0], outs[2].get("links")[0]}
    expected = {
        _expect("demo_notify")["entry_link_id"],
        _expect("llm_caiyun_weather")["entry_link_id"],
        _expect("anysearch_batch")["entry_link_id"],
    }
    assert targets == expected, f"link out 目标集应为 {expected}，实得 {targets}"


# ── 3) 负向：未知子流程应编译失败（C_SUBFLOW_UNKNOWN）→ 不会部署出 unknown 节点 ──
def test_unknown_subflow_rejected():
    bad = "场景: x\n触发: sensor.front_door 有人\n调用子流程: not_a_real_cap()\n"
    try:
        compile_dsl(bad)
        assert False, "未知子流程应编译失败"
    except DSLError as e:
        assert e.code in ("C_SUBFLOW_UNKNOWN", "C_SUBFLOW_ARG"), f"应报未知子流程，实得 {e.code}"


if __name__ == "__main__":
    test_history_occurred_subflow_node()
    test_bark_push_subflow_node()
    test_demo_notify_linkout_node()
    test_weather_linkout_node()
    test_anysearch_linkout_node()
    test_all_five_together()
    test_unknown_subflow_rejected()
    print("✅ test_subflow_e2e_compile 全部通过")
