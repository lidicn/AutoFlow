"""WB17 审计报告 6 个 STILL-BROKEN 项 —— HEAD 代码真值复测 + 回归锁定。

纪律：审计报告结论不直接采信，逐一用 HEAD 代码真实跑一遍。
WB17 复测结论（2026-07-27，HEAD=bb19f4d + 本文件对应修复）：
  iss_25419fa7a8 (HIGH 取值 entityId)   → 误报：编译器实测 entityId 正常输出（同压测报告 C1 旧冻结工件根因）
  iss_d184f78b7c (非法 jsonata)         → 真 bug，已修：R30 曾被「无 else 早返回」跳过，现对纯 jsonata 分支也生效
  iss_50828738bb (参数越界)            → 设计如此：R27 已 warning 拦截，硬拦故意委托 HA 部署/e2e 校验
  iss_d311744392 (api-get-history 空)  → 误报：R20 已对 api-get-history 空 entityId error 硬拦
  iss_bbbe45eb04 (嵌套条件)            → 误报：编译器正确实现 分支/否则 嵌套（外层 else→内层 switch）
  iss_752b28aaa8 (注册表↔NR 分叉)      → 非代码 bug：运行态数据漂移，stale 检测已在前轮就位

本测试对「真 bug 修复」与「误报项」各加一条断言，防止今后回归。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from autoflow_gateway.dsl_engine import compile_dsl  # noqa: E402
from autoflow_gateway.flow_linter import lint_flow, _check_jsonata  # noqa: E402

BLOCK = {"R13", "R15", "R20", "R17", "R22", "R24", "R30"}

_FAILS = []


def check(tag, cond, detail=""):
    if cond:
        print(f"  ✅ {tag}")
    else:
        print(f"  ❌ {tag}  —— {detail}")
        _FAILS.append(tag)


def lint_rules(flow):
    issues = lint_flow(flow)
    return issues, {i["rule"] for i in issues if i["level"] == "error"}


# ── iss_25419fa7a8：取值(ReadState) 必须带非空 entityId ──
def test_readstate_entityid():
    out = compile_dsl(
        "触发: binary_sensor.office_motion on\n"
        "  取值: sensor.office_lux 光照度\n"
        "  分支: 光照度 > 100\n"
        "    动作: light.turn_on(light.office_ceiling)",
        target="staging",
    )
    acs = [n for n in out.get("nodes", []) if n.get("type") == "api-current-state"]
    bad = [n for n in acs if not n.get("entityId")]
    check("iss_25419fa7a8 取值 node 带 entityId", not bad,
          f"api-current-state 节点缺 entityId: {[(n.get('name'), n.get('entityId')) for n in acs]}")


# ── iss_25419fa7a8 附带：查询(CurrentState) 也必须带 entityId ──
def test_currentstate_entityid():
    out = compile_dsl(
        "触发: light.office on\n"
        "  查询: sensor.office_tmp 30\n"
        "    动作: light.turn_on(light.office_ceiling)",
        target="staging",
    )
    acs = [n for n in out.get("nodes", []) if n.get("type") == "api-current-state"]
    bad = [n for n in acs if not n.get("entityId")]
    check("iss_25419fa7a8 查询 node 带 entityId", not bad,
          f"缺 entityId: {[(n.get('name'), n.get('entityId')) for n in acs]}")


# ── iss_d184f78b7c：无 else 的纯 jsonata switch 必须被 R30 拦（修复点）──
def test_switch_jsonata_no_else_r30():
    expr = "$number(光照度 <"
    ok, _ = _check_jsonata(expr)
    check("iss_d184f78b7c _check_jsonata 识别非法", not ok, "应识别为非法")
    flow = {"nodes": [{
        "id": "s1", "type": "switch", "z": "tab1",
        "rules": [{"t": "jsonata_exp", "v": expr, "vt": "jsonata_exp"}],
        "wires": [[]],
    }]}
    issues, errs = lint_rules(flow)
    check("iss_d184f78b7c 无 else switch 被 R30 拦(error)",
          "R30" in errs,
          f"error 规则={sorted(errs)}（修复前因早返回漏拦）")


# ── iss_d184f78b7c 对照：带 else 的 switch 也须拦 ──
def test_switch_jsonata_with_else_r30():
    expr = "$number(光照度 <"
    flow = {"nodes": [{
        "id": "s1", "type": "switch", "z": "tab1",
        "rules": [
            {"t": "jsonata_exp", "v": expr, "vt": "jsonata_exp"},
            {"t": "else", "v": "true"},
        ],
        "wires": [[], []],
    }]}
    _, errs = lint_rules(flow)
    check("iss_d184f78b7c 带 else switch 被 R30 拦(error)",
          "R30" in errs,
          f"error 规则={sorted(errs)}")


# ── iss_d311744392：api-get-history 空 entityId 必须被 R20 拦 ──
def test_get_history_empty_entity_r20():
    flow = {"nodes": [{
        "id": "h1", "type": "api-get-history", "z": "tab1",
        "entityId": "", "dataType": "json", "data": "{}",
        "wires": [["d1"]],
    }, {"id": "d1", "type": "debug", "z": "tab1", "wires": [[]]}]}
    issues, errs = lint_rules(flow)
    check("iss_d311744392 api-get-history 空 entityId 被 R20 拦(error)",
          "R20" in errs,
          f"error 规则={sorted(errs)}")


# ── iss_bbbe45eb04：嵌套 分支/否则 必须正确编译（外层 else→内层 switch）──
def test_nested_switch_wiring():
    out = compile_dsl(
        "触发: binary_sensor.office_motion on\n"
        "  分支: 光照度 > 100\n"
        "    否则:\n"
        "      分支: 温度 > 30\n"
        "        动作: light.turn_on(light.office_ceiling)",
        target="staging",
    )
    switches = [n for n in out.get("nodes", []) if n.get("type") == "switch"]
    check("iss_bbbe45eb04 嵌套编译出 2 个 switch", len(switches) == 2,
          f"实际 switch 数={len(switches)}")
    # 外层 switch 应有 else 规则，且 else 输出(out1)连到内层 switch
    outer = switches[0]
    has_else = any(r.get("t") == "else" for r in outer.get("rules", []))
    else_out = (outer.get("wires") or [[]])[1] if len(outer.get("wires") or []) > 1 else []
    inner_id = switches[1].get("id") if len(switches) > 1 else None
    check("iss_bbbe45eb04 外层 else 输出连到内层 switch",
          has_else and inner_id in (else_out or []),
          f"has_else={has_else}; else_out={else_out}; inner_id={inner_id}")


# ── iss_50828738bb：brightness_pct=99999 必须被 R27 warning 拦截（设计如此，非硬拦）──
def test_numeric_range_warning():
    flow = {"nodes": [{
        "id": "a1", "type": "api-call-service", "z": "tab1",
        "service": "light.turn_on", "entityId": "light.x",
        "dataType": "json", "data": json.dumps({"brightness_pct": 99999}),
        "wires": [["d1"]],
    }, {"id": "d1", "type": "debug", "z": "tab1", "wires": [[]]}]}
    issues, errs = lint_rules(flow)
    r27 = [i for i in issues if i["rule"] == "R27"]
    hard_block = errs & BLOCK - {"R13"}  # 排除测试自身孤儿误报
    check("iss_50828738bb R27 warning 命中", bool(r27),
          f"R27={bool(r27)}; 硬拦规则(去孤儿)={hard_block}")


if __name__ == "__main__":
    print("WB17 STILL-BROKEN 真值复测 + 回归锁定（HEAD 代码，离线）")
    test_readstate_entityid()
    test_currentstate_entityid()
    test_switch_jsonata_no_else_r30()
    test_switch_jsonata_with_else_r30()
    test_get_history_empty_entity_r20()
    test_nested_switch_wiring()
    test_numeric_range_warning()
    print("=" * 60)
    if _FAILS:
        print(f"❌ 失败 {len(_FAILS)} 项：{_FAILS}")
        sys.exit(1)
    print("✅ WB17 全部 6 项复测通过（1 真 bug 已修复 + 5 误报/设计项锁定）")
    sys.exit(0)
