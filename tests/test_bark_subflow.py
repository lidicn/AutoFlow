"""bark_push 子流程注册 + 编译产出测试（A1）。

运行：python tests/test_bark_subflow.py
不依赖 live NR —— 仅校验 subflows.py 注册 + dsl_engine 编译出的节点结构。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import compile_dsl
from autoflow_gateway.subflows import (
    get_subflow, BARK_SUBFLOW_ID,
)

DSL_BARK = """
场景: 测试Bark推送
触发: sensor.front_door 有人
调用子流程: bark_push(title=书房提醒, body=有人来了)
"""

DSL_BARK_OPT = """
场景: 测试Bark推送带可选
触发: sensor.front_door 有人
调用子流程: bark_push(title=警告, body=有人, bark_level=critical, bark_sound=minuet)
"""


def test_spec_registered():
    spec = get_subflow("bark_push")
    assert spec is not None
    assert spec.call["type"] == "subflow"
    assert spec.call["subflow_id"] == BARK_SUBFLOW_ID
    assert spec.param_style == "flat"


def test_resolve_required():
    spec = get_subflow("bark_push")
    ok = spec.resolve_args({"title": "t", "body": "b"})
    assert ok["title"] == "t" and ok["body"] == "b"
    # 缺必填应抛 ValueError
    try:
        spec.resolve_args({"title": "t"})
        assert False, "should raise on missing body"
    except ValueError:
        pass


def test_resolve_enum():
    spec = get_subflow("bark_push")
    ok = spec.resolve_args({"title": "t", "body": "b", "bark_level": "critical"})
    assert ok["bark_level"] == "critical"
    try:
        spec.resolve_args({"title": "t", "body": "b", "bark_level": "urgent"})
        assert False, "should raise on bad enum"
    except ValueError:
        pass


def test_compile_emits_flat_msg_fields():
    flow = compile_dsl(DSL_BARK)
    nodes = flow["nodes"]
    # 应有一个 change（设置入参）+ 一个 subflow 实例（其 type/flow 均为子流程 id）
    changes = [n for n in nodes if n["type"] == "change"]
    # NR 5.x 子流程实例的 type 必须是 "subflow:<id>"（带前缀）；
    # 旧写法裸 id 会被 NR 渲染成 unknown: <id>（编辑器红虚线、无法连线）。
    subs = [n for n in nodes if n.get("type") == f"subflow:{BARK_SUBFLOW_ID}"]
    assert changes, "缺少设置入参的 change 节点"
    assert subs, "缺少 Bark 子流程实例（type 应为 subflow:<id>）"
    # flat 模式：change 把 title/body 设到 msg.title / msg.body（非 msg.payload）
    rules = changes[0]["rules"]
    paths = [(r["p"], r["pt"]) for r in rules]
    assert ("title", "msg") in paths, f"应平铺 msg.title，实得 {paths}"
    assert ("body", "msg") in paths, f"应平铺 msg.body，实得 {paths}"
    assert not any(p == "payload" for p, _ in paths), "flat 模式不应出现 msg.payload"
    # subflow 实例引用正确：type 带 subflow: 前缀，且不应残留 NR 1.0 之前的遗留 flow 字段
    assert subs[0]["type"] == f"subflow:{BARK_SUBFLOW_ID}", \
        f"子流程实例 type 应为 subflow:{BARK_SUBFLOW_ID}，实得 {subs[0]['type']}"
    assert "flow" not in subs[0], f"不应残留遗留 flow 字段，实得 {subs[0]}"


def test_compile_optional_passthrough():
    flow = compile_dsl(DSL_BARK_OPT)
    changes = [n for n in flow["nodes"] if n["type"] == "change"]
    rules = {r["p"]: r["to"] for r in changes[0]["rules"]}
    assert rules.get("bark_level") == "critical"
    assert rules.get("bark_sound") == "minuet"


if __name__ == "__main__":
    test_spec_registered()
    test_resolve_required()
    test_resolve_enum()
    test_compile_emits_flat_msg_fields()
    test_compile_optional_passthrough()
    print("✅ test_bark_subflow 核心回归通过（注册/解析/编译）")
