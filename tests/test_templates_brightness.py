"""A26(#round5)：模板亮度参数量纲契约（255 制 → 100 制 brightness_pct）。

背景：模板原用 `brightness={{brightness|100}}`（HA 255 制，默认 100 → 仅 ~39%），
与用户心智/官方 example 的 `brightness_pct=80`（100 制）不符。本工单把模板正文改成
`brightness_pct={{brightness|80}}` 等，保留 param 名 `brightness` 不动（渲染器按变量名匹配）。

运行：python -m pytest tests/test_templates_brightness.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import template_lib as T


def test_motion_to_light_uses_brightness_pct():
    """motion_to_light 渲染 brightness=80 → 输出 brightness_pct=80，而非 255 制 brightness=80。"""
    out = T.render_template("motion_to_light",
                            {"sensor": "binary_sensor.x", "light": "light.x",
                             "room": "书房", "brightness": "80"})
    assert "brightness_pct=80" in out, f"应输出 brightness_pct=80：{out}"
    assert "brightness=80" not in out, f"不应再输出 255 制 brightness=80：{out}"


def test_entry_announce_uses_brightness_pct():
    """entry_announce 同理。"""
    out = T.render_template("entry_announce",
                            {"sensor": "binary_sensor.x", "light": "light.x",
                             "room": "书房", "text": "欢迎回家", "brightness": "80"})
    assert "brightness_pct=80" in out, f"应输出 brightness_pct=80：{out}"
    assert "brightness=80" not in out, f"不应再输出 255 制 brightness=80：{out}"


def test_conditional_brightness_uses_brightness_pct():
    """conditional_brightness 两分支均应为 brightness_pct（100 制）。"""
    out = T.render_template("conditional_brightness",
                            {"sensor": "binary_sensor.x", "light": "light.x",
                             "room": "书房", "lux": "sensor.lux",
                             "day_brightness": "100", "night_brightness": "30",
                             "night_start": "22"})
    assert "brightness_pct=100" in out, f"白天分支应为 brightness_pct=100：{out}"
    assert "brightness_pct=30" in out, f"夜间分支应为 brightness_pct=30：{out}"
    assert "brightness=100" not in out, f"不应再输出 255 制 brightness=100：{out}"
    assert "brightness=30" not in out, f"不应再输出 255 制 brightness=30：{out}"


def test_brightness_default_is_pct_scale():
    """不传 brightness → 默认 80（100 制），输出 brightness_pct=80，与 gateway 既有示例一致。"""
    out = T.render_template("motion_to_light",
                            {"sensor": "binary_sensor.x", "light": "light.x", "room": "书房"})
    assert "brightness_pct=80" in out, f"默认值应为 100 制 80：{out}"


def test_template_lib_docstring_example_is_pct():
    """template_lib.py docstring 示例应已改为 brightness_pct（避免后续维护者照抄错量纲）。"""
    import inspect
    doc = inspect.getdoc(T) or ""
    assert "brightness_pct={{brightness|80}}" in doc, "docstring 示例须为 brightness_pct 量纲"


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
    print(f"\ntemplates_brightness: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
