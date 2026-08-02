"""template_lib 测试：load / list / render（含 {{var|default}} 默认值回退）。

运行：python -m pytest tests/test_template_lib.py -q
也支持 run_tests.py（执行 __main__ 块）。
"""

import json
import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import template_lib as T


# ── 自带 fixtures（不依赖真实 templates 目录，避免环境漂移）──────────────
_SAMPLE = """---
name: sample_light
description: 测试用开灯模板
tags: [lighting, test]
params: sensor, light, brightness
---
场景: {{room}}开灯
触发: {{sensor}} 有人
动作: light.turn_on({{light}}, brightness={{brightness|100}})
预期:
  {{light}} = on
"""


def _write_tmp(name, body):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return d


def test_load_templates_parses_frontmatter():
    d = _write_tmp("sample_light.md", _SAMPLE)
    tpls = T.load_templates(d)
    assert len(tpls) == 1
    t = tpls[0]
    assert t.name == "sample_light"
    assert t.description == "测试用开灯模板"
    assert "lighting" in t.tags and "test" in t.tags
    assert "sensor" in t.params and "light" in t.params and "brightness" in t.params
    assert "light.turn_on" in t.body


def test_list_templates_returns_summaries():
    d = _write_tmp("sample_light.md", _SAMPLE)
    summaries = T.list_templates(d)
    assert isinstance(summaries, list) and len(summaries) == 1
    s = summaries[0]
    assert set(["name", "description", "tags", "params"]).issubset(s.keys())
    assert s["name"] == "sample_light"
    assert s["params"] == ["sensor", "light", "brightness"]


def test_render_fills_values():
    d = _write_tmp("sample_light.md", _SAMPLE)
    out = T.render_template("sample_light",
                            {"room": "书房", "sensor": "binary_sensor.study_door",
                             "light": "light.study_main"}, directory=d)
    assert "书房开灯" in out
    assert "binary_sensor.study_door 有人" in out
    assert "light.turn_on(light.study_main" in out
    # 无默认值的 brightness 被留空
    assert "brightness=" not in out.replace("brightness=100", "")


def test_render_default_fallback():
    d = _write_tmp("sample_light.md", _SAMPLE)
    out = T.render_template("sample_light",
                            {"room": "客厅", "sensor": "binary_sensor.living_motion",
                             "light": "light.living_main"}, directory=d)
    # brightness 未提供 → 用默认值 100
    assert "brightness=100" in out


def test_render_missing_template_raises():
    d = _write_tmp("sample_light.md", _SAMPLE)
    try:
        T.render_template("does_not_exist", {}, directory=d)
        assert False, "应抛 KeyError"
    except KeyError:
        pass


def test_real_template_dir_loads_seeds():
    """真实 templates 目录应至少有 8 个种子模板，且可被渲染。"""
    summaries = T.list_templates()
    names = {s["name"] for s in summaries}
    assert {"motion_to_light", "entry_announce", "tts_announce"}.issubset(names)
    assert {"scheduled_announce", "leave_turn_off", "conditional_brightness"}.issubset(names)
    # 每个种子都能至少用空值渲染出正文（strict=False 保留旧语义，仅验证可渲染）
    for s in summaries:
        rendered = T.render_template(s["name"], {}, strict=False)
        assert len(rendered.strip()) > 0


def test_new_templates_render_correctly():
    """新模板填充后 DSL 正文正确。"""
    # 定时播报
    out = T.render_template("scheduled_announce",
                            {"time": "08:00", "text": "早上好，新的一天开始了",
                             "room": "客厅"})
    assert "每天 08:00" in out
    assert "早上好" in out
    assert "demo_notify" in out

    # 离开关灯（含延时默认值）
    out = T.render_template("leave_turn_off",
                            {"sensor": "binary_sensor.door_main",
                             "light": "light.living_main",
                             "room": "客厅"})
    assert "binary_sensor.door_main 关" in out
    assert "light.turn_off(light.living_main)" in out
    assert "30 秒" in out  # delay 默认 30

    # 离开关灯（自定义延时）
    out = T.render_template("leave_turn_off",
                            {"sensor": "binary_sensor.door_main",
                             "light": "light.living_main",
                             "room": "客厅", "delay": "60"})
    assert "60 秒" in out

    # 多条件亮度开关
    out = T.render_template("conditional_brightness",
                            {"sensor": "binary_sensor.study_motion",
                             "light": "light.study_main",
                             "room": "书房"})
    assert "binary_sensor.study_motion 有人" in out
    assert "light.turn_on(light.study_main" in out
    assert "分支" in out
    assert "否则" in out


def test_new_templates_compile():
    """新模板渲染后可被 dsl_engine 成功编译（staging 模式）。"""
    from autoflow_gateway import dsl_engine

    # 定时播报
    dsl = T.render_template("scheduled_announce",
                            {"time": "08:00", "text": "早上好",
                             "room": "客厅"})
    flow = dsl_engine.compile_dsl(dsl, target="staging")
    assert flow["label"] == "客厅定时播报"
    assert any(n["type"] == "inject" for n in flow["nodes"])

    # 离开关灯
    dsl = T.render_template("leave_turn_off",
                            {"sensor": "binary_sensor.door_main",
                             "light": "light.living_main",
                             "room": "客厅"})
    flow = dsl_engine.compile_dsl(dsl, target="staging")
    assert flow["label"] == "客厅离开关灯"
    assert any(n["type"] == "delay" for n in flow["nodes"])

    # 多条件亮度开关
    dsl = T.render_template("conditional_brightness",
                            {"sensor": "binary_sensor.study_motion",
                             "light": "light.study_main",
                             "room": "书房"})
    flow = dsl_engine.compile_dsl(dsl, target="staging")
    assert flow["label"] == "书房亮度自适应开灯"
    assert any(n["type"] == "switch" for n in flow["nodes"])


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\ntemplate_lib: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
