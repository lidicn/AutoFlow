"""编译期自检测试（A3）。

运行：python tests/test_compile_lint.py
验证 compile 产出 flow["lint"]；compile_dsl_strict 拦截 error 级反模式。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.dsl_engine import (
    compile_dsl, compile_dsl_strict, DSLError,
)

DSL_HOME = """
场景: 回家开灯与播报
触发: sensor.front_door 有人
动作: light.turn_on(客厅主灯, brightness=80)
调用子流程: demo_notify(text=欢迎回家, room=客厅)
"""

DSL_BARK = """
场景: 测试Bark
触发: sensor.front_door 有人
调用子流程: bark_push(title=提醒, body=有人来了)
"""

# 故意用 $defined（NR JSONata 不存在）→ 应被 R5 抓到
DSL_BAD_BUILD = """
场景: 坏构建
触发: sensor.x 变化
构建: 'hello ' & $defined(msg.name)
"""


def test_normal_scene_lint_clean():
    flow = compile_dsl(DSL_HOME)
    assert "lint" in flow
    errors = [i for i in flow["lint"] if i["level"] == "error"]
    assert not errors, f"正常场景不应有 error 级 lint：{errors}"


def test_bark_scene_lint_clean():
    flow = compile_dsl(DSL_BARK)
    errors = [i for i in flow["lint"] if i["level"] == "error"]
    assert not errors, f"Bark 场景不应有 error 级 lint：{errors}"


def test_bad_build_caught_by_lint():
    flow = compile_dsl(DSL_BAD_BUILD)
    rules = [i["rule"] for i in flow["lint"]]
    assert "R5" in rules, f"应抓到 $defined 误用(R5)，实得 {rules}"


def test_strict_raises_on_error():
    raised = False
    try:
        compile_dsl_strict(DSL_BAD_BUILD)
    except DSLError as e:
        raised = True
        assert "R5" in str(e)
    assert raised, "compile_dsl_strict 应在 error 级 lint 时抛 DSLError"


def test_strict_passes_clean():
    # 正常/Bark 场景 strict 不抛
    compile_dsl_strict(DSL_HOME)
    compile_dsl_strict(DSL_BARK)


if __name__ == "__main__":
    test_normal_scene_lint_clean()
    test_bark_scene_lint_clean()
    test_bad_build_caught_by_lint()
    test_strict_raises_on_error()
    test_strict_passes_clean()
    print("✅ test_compile_lint 全部通过")
