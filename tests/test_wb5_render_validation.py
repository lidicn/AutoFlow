"""WB5 #2 回归：render_template 必填参数校验（strict 模式）。

覆盖四态：
  1. 漏必填 sensor → TemplateValidationError(missing=[...])
  2. 全参数齐全 → 正常渲染
  3. 多传未知键名 → TemplateValidationError(unknown=[...])
  4. 模板不存在 → KeyError

运行：run_tests.py 会自动逐文件跑 __main__ 块（离线硬门槛）。
"""

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import template_lib as T


_SAMPLE = """---
name: wb5_sample
description: WB5 必填校验测试模板
tags: [test]
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


def test_missing_required_sensor_raises():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    # room 与 light 缺失，sensor 提供
    try:
        T.render_template("wb5_sample", {"sensor": "binary_sensor.x"}, directory=d)
        assert False, "应抛 TemplateValidationError"
    except T.TemplateValidationError as e:
        assert "room" in e.missing
        assert "light" in e.missing
        assert "sensor" not in e.missing
        assert e.unknown == []


def test_full_params_render_ok():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    out = T.render_template("wb5_sample",
                            {"room": "书房", "sensor": "binary_sensor.study_door",
                             "light": "light.study_main"}, directory=d)
    assert "书房开灯" in out
    assert "binary_sensor.study_door 有人" in out
    assert "light.turn_on(light.study_main" in out
    # brightness 未给但有默认值 → 100
    assert "brightness=100" in out


def test_unknown_param_raises():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    try:
        T.render_template("wb5_sample",
                          {"room": "书房", "sensor": "binary_sensor.x",
                           "light": "light.x", "typo_key": "oops"}, directory=d)
        assert False, "应抛 TemplateValidationError"
    except T.TemplateValidationError as e:
        assert "typo_key" in e.unknown


def test_missing_and_unknown_together():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    try:
        T.render_template("wb5_sample", {"sensor": "binary_sensor.x",
                                         "bogus": "1"}, directory=d)
        assert False, "应抛 TemplateValidationError"
    except T.TemplateValidationError as e:
        assert "room" in e.missing and "light" in e.missing
        assert "bogus" in e.unknown


def test_nonexistent_template_raises_keyerror():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    try:
        T.render_template("no_such_template", {"sensor": "x"}, directory=d)
        assert False, "应抛 KeyError"
    except KeyError:
        pass


def test_strict_false_allows_empty():
    d = _write_tmp("wb5_sample.md", _SAMPLE)
    # strict=False 退化为旧行为：必填缺失只留空，不抛错
    out = T.render_template("wb5_sample", {"sensor": "binary_sensor.x"},
                            directory=d, strict=False)
    assert isinstance(out, str)
    assert len(out.strip()) > 0


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
    print(f"\nwb5_render_validation: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
