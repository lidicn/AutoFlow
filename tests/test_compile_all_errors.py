#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.3.0 任务 3.1（DCD 议题七之 C）：一次性全错编译反馈。

零依赖运行：python tests/test_compile_all_errors.py

锁三条不变量：
  I-1 向后兼容：单错 DSL 抛出的首个 DSLError 与改造前一致（code/message 不变），extra_errors 为空；
  I-2 多错一次拿全：多错 DSL 仍抛第一个错，但 extra_errors 含其余错误，且信封输出 all_errors/error_count；
  I-3 无副作用：合法 DSL 解析正常，不触发任何收集路径。
"""
import os
import sys

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

from autoflow_gateway.dsl_engine import parse, DSLError
from autoflow_gateway.errors import _compile_error_envelope

# 合法基线（I-3）
DSL_OK = "场景: 测试\n触发: inject\n动作: light.turn_on(light.study_main)\n"

# 单错（I-1）：第 3 行动作格式错
DSL_ONE = "场景: 测试\n触发: inject\n动作: light.turn_on light.study_main\n"

# 多错（I-2）：第 3 行格式错 + 第 4 行未知顶层指令 + 第 5 行数组缺右括号
DSL_MANY = (
    "场景: 测试\n"
    "触发: inject\n"
    "动作: light.turn_on light.study_main\n"
    "这是个不存在的顶层指令\n"
    "动作: light.turn_on([light.a\n"
)


def test_ok_dsl_unchanged():
    """I-3：合法 DSL 正常解析，不进收集路径。"""
    scene = parse(DSL_OK)
    assert scene.name == "测试", scene.name
    assert scene.triggers, "应有触发器"


def test_single_error_primary_unchanged():
    """I-1：单错 DSL —— 首个错误与改造前逐字一致，且无额外错误。"""
    try:
        parse(DSL_ONE)
    except DSLError as e:
        assert e.line == 3, e.line
        assert "domain.service" in str(e), str(e)
        assert e.extra_errors == [], "单错时不应有额外错误，实际：%r" % (e.extra_errors,)
    else:
        raise AssertionError("单错 DSL 应当抛出 DSLError")


def test_multi_error_collected():
    """I-2：多错 DSL —— 仍抛第一个错，但其余错误被收集。"""
    try:
        parse(DSL_MANY)
    except DSLError as e:
        assert e.line == 3, "首个错误应仍是第 3 行，实际 %r" % (e.line,)
        assert len(e.extra_errors) >= 1, "应收集到其余错误，实际 %d 个" % len(e.extra_errors)
        # 收集到的错误应含行号，便于一次定位
        for x in e.extra_errors:
            assert isinstance(x.line, int), "额外错误应带行号，实际 %r" % (x.line,)
    else:
        raise AssertionError("多错 DSL 应当抛出 DSLError")


def test_envelope_exposes_all_errors():
    """I-2：信封把 extra_errors 暴露为 all_errors + error_count。"""
    try:
        parse(DSL_MANY)
        raise AssertionError("应当抛出 DSLError")
    except DSLError as e:
        env = _compile_error_envelope(e, fix=[{"op": "get_help"}])
        assert env["error_count"] >= 2, env["error_count"]
        assert len(env["all_errors"]) >= 1, env["all_errors"]
        first = env["all_errors"][0]
        for key in ("code", "line", "message", "hint"):
            assert key in first, "all_errors 项缺字段 %s" % key
        # 主 fix 通道未被破坏
        assert env["fix"] == [{"op": "get_help"}], env["fix"]


def test_envelope_single_error_has_empty_all_errors():
    """I-1：单错时 all_errors 为空、error_count==1，不给 agent 假信号。"""
    try:
        parse(DSL_ONE)
        raise AssertionError("应当抛出 DSLError")
    except DSLError as e:
        env = _compile_error_envelope(e)
        assert env["all_errors"] == [], env["all_errors"]
        assert env["error_count"] == 1, env["error_count"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa
            fail += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if fail else 0)
