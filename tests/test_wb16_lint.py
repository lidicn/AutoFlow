"""WB16 静默逻辑 bug 回归：非法 jsonata (R30) + 未定义字段引用 (R31)。

运行：run_tests.py 会自动逐文件跑 __main__ 块（离线硬门槛）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import flow_linter as L


def _api_current_state(nid, field):
    return {
        "id": nid, "type": "api-current-state", "name": f"取值 {field}",
        "outputProperties": [
            {"property": f"payload.{field}", "propertyType": "msg",
             "value": "", "valueType": "entityState"},
            {"property": "payload.state", "propertyType": "msg",
             "value": "", "valueType": "entityState"},
        ],
    }


def _switch(nid, rules, wires):
    return {
        "id": nid, "type": "switch", "name": "分支",
        "property": "payload", "propertyType": "msg", "checkall": "true",
        "rules": rules, "outputs": len(rules), "wires": wires,
    }


def _jsonata_rule(v):
    return {"t": "jsonata_exp", "v": v, "vt": "jsonata"}


def test_r30_ill_formed_jsonata_blocked():
    # 分支写明显非法的 jsonata（缺闭包 `)`）→ R30 error
    flow = {"nodes": [
        _api_current_state("r1", "光照度"),
        _switch("s1", [_jsonata_rule("$number(光照度 <"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    r30 = [i for i in issues if i.get("rule") == "R30"]
    assert r30, "非法 jsonata 应触发 R30"
    assert r30[0]["level"] == "error", "R30 应为 error（硬拦）"


def test_r30_valid_jsonata_ok():
    # 合法 jsonata（括号配平）→ 不报 R30
    flow = {"nodes": [
        _api_current_state("r1", "光照度"),
        _switch("s1", [_jsonata_rule("$number(光照度) < 10"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    assert not [i for i in issues if i.get("rule") == "R30"], "合法 jsonata 不应触发 R30"


def test_r31_undefined_field_warned():
    # 取值只定义 光照度，分支却引用 温度（未定义）→ R31 warning
    flow = {"nodes": [
        _api_current_state("r1", "光照度"),
        _switch("s1", [_jsonata_rule("$number(温度) < 10"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    r31 = [i for i in issues if i.get("rule") == "R31"]
    assert r31, "未定义字段引用应触发 R31"
    assert r31[0]["level"] == "warning"
    assert "温度" in r31[0]["message"]


def test_r31_defined_field_ok():
    # 分支引用已定义的 光照度 → 不报 R31
    flow = {"nodes": [
        _api_current_state("r1", "光照度"),
        _switch("s1", [_jsonata_rule("$number(光照度) < 10"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    assert not [i for i in issues if i.get("rule") == "R31"], "已定义字段不应触发 R31"


def test_r31_variable_field_ok():
    # 变量 亮度 经 change 写 flow.亮度；分支引用 亮度 应不报 R31
    flow = {"nodes": [
        {"id": "c1", "type": "change", "rules": [
            {"p": "亮度", "pt": "flow", "to": "70", "tot": "num"}]},
        _switch("s1", [_jsonata_rule("$number(亮度) > 10"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    assert not [i for i in issues if i.get("rule") == "R31"], "已声明变量不应触发 R31"


def test_r30_error_level_blocks():
    # R30 为 error 级，部署硬拦集会拦下（与 R22 同机制）
    flow = {"nodes": [
        _api_current_state("r1", "光照度"),
        _switch("s1", [_jsonata_rule("$number(光照度 <"), {"t": "else", "v": "true", "vt": "jsonata"}],
                [["a"], ["b"]]),
    ]}
    issues = L.lint_flow(flow)
    r30 = [i for i in issues if i.get("rule") == "R30"]
    assert r30 and r30[0]["level"] == "error"
    # 复刻 deploy_raw 的阻塞判定：error 级 R30 ∈ 硬拦集
    BLOCK = {"R13", "R15", "R20", "R17", "R22", "R24", "R30"}
    blocking = [v for v in issues if v.get("level") == "error" and v.get("rule") in BLOCK]
    assert blocking, "R30 应被部署硬拦集拦下"


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
    print(f"\nwb16_lint: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
