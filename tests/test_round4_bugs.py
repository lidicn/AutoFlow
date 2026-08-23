# -*- coding: utf-8 -*-
"""Round4 bug 回归（iss_c39a4e3913 / iss_51a8a3829b / iss_f9eed74f0b 相关代码修复）。

- Bug1 (iss_c39a4e3913, Medium)：动作参数用单引号/双引号包裹的字面量值（如
  brightness_pct='50'）此前被原样保留引号，编译成 {"brightness_pct":"'50'"}（字符串），
  HA 收到字符串而非数字，调用静默失败。修复：_action_param_value 对配对单/双引号
  外壳做去引号（反引号动态引用逻辑不变），去引号后交由 _coerce_scalar 做数值归一。

- Bug2 (iss_51a8a3829b, Medium)：用户按自然语言写「持续: N 分钟」顶层指令，但本引擎
  的持久等待语义挂在【触发】上（编译为 server-state-changed 的 for 等待），不是独立
  顶层指令。此前直接报 C_UNKNOWN_TOPLEVEL 硬墙；修复：对「持续」前缀特判，给出明确
  替代语法提示（指向「触发: <X> <state> 持续N分钟」）。

- Bug3 (iss_f9eed74f0b, Low)：空场景（仅触发无动作）gate 放行仅 R33 warning。现状设计
  为 fail-open（接受纯观测/调试流），本报告建议提升至拦截属产品决策，未改 compile 行为；
  此处仅固化「空场景仍可编译 + R33 正确提示」的现状，防止误伤合法流时被回归破坏。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_r4_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway.dsl_engine import compile_dsl, DSLError


def _action_params(dsl):
    r = compile_dsl(dsl, target="staging")
    flows = r.get("flows") or [r]
    for f in flows:
        for n in f.get("nodes", []):
            if n.get("type") == "api-call-service":
                raw = n.get("data") or n.get("params")
                if isinstance(raw, str):
                    import json as _json
                    try:
                        return _json.loads(raw)
                    except Exception:
                        return {}
                return raw or {}
    return {}


def test_bug1_single_quote_numeric_dequoted():
    """'50' → 50 (int)，不再是字符串 "'50'"."""
    p = _action_params("""场景: 单引号数字
触发: inject
动作: light.turn_on(light.x, brightness_pct='50')""")
    assert p.get("brightness_pct") == 50, p
    assert isinstance(p.get("brightness_pct"), int), p


def test_bug1_single_quote_string_dequoted_keeps_inner():
    """'hello world' → "hello world"（去外壳，内部空格保留，仍是字符串）."""
    p = _action_params("""场景: 单引号字符串
触发: inject
动作: notify.x(text='hello world')""")
    assert p.get("text") == "hello world", p
    assert isinstance(p.get("text"), str), p


def test_bug1_double_quote_dequoted():
    """双引号外壳同样去引号（报告提及单/双引号等效）。"""
    p = _action_params("""场景: 双引号数字
触发: inject
动作: light.turn_on(light.x, brightness_pct="80")""")
    assert p.get("brightness_pct") == 80, p


def test_bug1_backtick_dynamic_untouched():
    """反引号动态引用不被去引号逻辑影响（保持原动态引用语义）。"""
    p = _action_params("""场景: 反引号动态
触发: inject
动作: light.turn_on(light.x, brightness=`payload.level`)""")
    # 反引号裸名 → {{payload.level}} 模板（映射到 brightness 字段），不应被当字面量去引号
    assert "{{payload.level}}" in str(p.get("brightness")), p


def test_bug2_persist_toplevel_friendly_hint():
    """持续: N 分钟 顶层指令给出友好提示，指向正确语法，而非硬墙。"""
    try:
        compile_dsl("""场景: 书房持续3分钟开吊灯
触发: binary_sensor.motion on
持续: 3 分钟
动作: switch.turn_on(switch.x)""", target="staging")
        raise AssertionError("expected C_UNKNOWN_TOPLEVEL with friendly hint")
    except DSLError as e:
        assert e.code == "C_UNKNOWN_TOPLEVEL"
        assert "触发" in str(e) and "持续" in str(e), str(e)
        # 提示必须指明正确位置：挂在触发上
        assert "触发:" in str(e), str(e)


def test_bug2_persist_on_trigger_still_works():
    """对照：正确语法「触发: <X> <state> 持续N分钟」仍可编译（持久等待挂在触发）。"""
    r = compile_dsl("""场景: 书房持续3分钟开吊灯
触发: binary_sensor.motion on 持续3分钟
动作: switch.turn_on(switch.x)""", target="staging")
    assert r is not None


def test_bug3_empty_scene_compiles_with_r33():
    """空场景（仅触发）仍能编译（fail-open，不误伤合法纯观测流）；
    R33 warning 由 flow_linter 在 lint 阶段提示，此处仅固化 compile 不拦。"""
    r = compile_dsl("""场景: 空场景测试
触发: inject""", target="staging")
    assert r is not None
    # 编译产物应仅含触发入口、无 effectful 节点（供 R33 兜底提示）
    flows = r.get("flows") or [r]
    types = {n.get("type") for f in flows for n in f.get("nodes", [])}
    assert "api-call-service" not in types
