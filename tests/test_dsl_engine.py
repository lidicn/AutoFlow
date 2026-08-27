"""dsl_engine 端到端测试（P3 MVP 闸门验证语料，来自 NR NR 真实场景）。

运行：python -m pytest tests/test_dsl_engine.py -q
不依赖 live NR —— 仅校验生成的结构（节点类型 / 连线 / 子流程解析 / 无 Function）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import dsl_engine as E
from autoflow_gateway.dsl_engine import (
    DSLError, parse, validate, compile, compile_dsl, RawNode,
    C_LABEL_UNDEFINED, C_DELAY_UNIT,
)
from autoflow_gateway.subflows import DEMO_NOTIFY_ENTRY_LINK_ID

# ── MVP 闸门场景：回家开灯+播报（无分支，最薄场景）──────────────────────
DSL_HOME_ARRIVE = """
场景: 回家开灯与播报
触发: sensor.front_door 有人
动作: light.turn_on(客厅主灯, brightness=80, kelvin=3000)
动作: light.turn_on(玄关灯)
调用子流程: demo_notify(text=欢迎回家, room=客厅)
"""

# ── 分支场景：有人开灯否则关灯 ──────────────────────────────────────────
DSL_MOTION_SWITCH = """
场景: 有人开灯否则关灯
触发: sensor.motion_living 有人
分支 msg.payload == "有人":
    动作: light.turn_on(客厅主灯)
否则:
    动作: light.turn_off(客厅主灯)
"""

# ── link_out 子流程场景：调用 demo_notify（验证 change 设参 + link out 指向真实 entry）──
DSL_WEIGH = """
场景: 称重播报
触发: sensor.mi_body_composition_scale_d22e_weight 变化
调用子流程: demo_notify(text=称重完成, room=客厅)
"""


def _types(flow):
    return [n["type"] for n in flow["nodes"]]


def _by_type(flow, t):
    return [n for n in flow["nodes"] if n["type"] == t]


def test_parse_home_arrive():
    s = parse(DSL_HOME_ARRIVE)
    assert s.name == "回家开灯与播报"
    assert s.trigger.kind == "state"
    assert s.trigger.entity == "sensor.front_door"
    assert s.trigger.state == "有人"
    # 2 actions + 1 subflow call
    actions = [b for b in s.body if hasattr(b, "domain")]
    subs = [b for b in s.body if hasattr(b, "name") and not hasattr(b, "domain")]
    assert len(actions) == 2
    assert len(subs) == 1 and subs[0].name == "demo_notify"


def test_compile_home_arrive_structure():
    flow = compile_dsl(DSL_HOME_ARRIVE)
    types = _types(flow)
    assert types[0] == "server-state-changed"       # 状态触发统一编译为 server-state-changed（含 for 持久等待，WB4 #2 修复）
    assert types.count("api-call-service") == 2     # 2 开灯
    assert types.count("change") == 1               # 设置 demo_notify 入参
    assert types.count("link out") == 1             # → TTS 入口
    # 铁律：绝不生成 Function
    assert "function" not in types
    # 子流程 link out 必须指向真实 TTS 入口
    lo = _by_type(flow, "link out")[0]
    assert lo["links"] == ["b595563939283231"]
    # change 节点 payload 含 text/room
    ch = _by_type(flow, "change")[0]
    payload = json.loads(ch["rules"][0]["to"])
    assert payload["text"] == "欢迎回家"
    assert payload["room"] == "客厅"


def test_compile_wires_chained():
    flow = compile_dsl(DSL_HOME_ARRIVE)
    # server-state-changed(状态触发) → action1 → action2 → change → link out 的线性连线
    by_id = {n["id"]: n for n in flow["nodes"]}
    trig = _by_type(flow, "server-state-changed")[0]
    nxt = trig["wires"][0][0]
    assert by_id[nxt]["type"] == "api-call-service"
    # 顺藤摸瓜到 link out
    seen = set()
    cur = nxt
    while cur and cur not in seen:
        seen.add(cur)
        ws = by_id[cur]["wires"]
        if not ws or not ws[0]:
            break
        cur = ws[0][0]
    assert by_id[cur]["type"] == "link out"


def test_compile_switch_with_else():
    flow = compile_dsl(DSL_MOTION_SWITCH)
    sw = _by_type(flow, "switch")
    assert len(sw) == 1
    assert sw[0]["outputs"] == 2          # 分支 + 否则
    # 两个分支各一个 api-call-service，且分别接到 switch 的两个输出
    svc = _by_type(flow, "api-call-service")
    assert len(svc) == 2
    # switch 输出 0 → turn_on, 输出 1 → turn_off
    out0 = sw[0]["wires"][0][0]
    out1 = sw[0]["wires"][1][0]
    assert by_name(flow, out0)["service"] == "turn_on"
    assert by_name(flow, out1)["service"] == "turn_off"


def test_compile_switch_single_quote_value_stripped():
    """回归(#a)：分支值用单引号（中文习惯 state='off'）时，编译器必须剥离引号。

    旧实现正则 ("?)([^"]*)\\2 只剥双引号，会把 'off' 连同单引号一起 capture →
    编译出 v="'off'"，真实 NR switch 与 gate 比较 msg.state('off')≠"'off'" 永不成立，
    分支恒走 else（闸门误拦 + 真实部署分支失效）。"""
    dsl = """
场景: 单引号分支值
触发: sensor.motion on
取值: light.desk state
分支: state = 'off'
    动作: light.turn_on(light.desk)
否则:
    动作: light.turn_off(light.desk)
"""
    flow = compile_dsl(dsl)
    sw = _by_type(flow, "switch")
    assert len(sw) == 1
    rule = sw[0]["rules"][0]
    assert rule["t"] == "eq"
    assert rule["v"] == "off", f"分支值单引号必须被剥离，实际 v={rule['v']!r}"
    assert rule["vt"] == "str"


def by_name(flow, nid):
    return next(n for n in flow["nodes"] if n["id"] == nid)


def test_compile_tts_subflow():
    flow = compile_dsl(DSL_WEIGH)
    assert flow["nodes"][0]["type"] == "server-state-changed"  # 状态触发编译为 server-state-changed（含 for）
    lo = _by_type(flow, "link out")[0]
    assert lo["links"] == [DEMO_NOTIFY_ENTRY_LINK_ID]
    ch = _by_type(flow, "change")[0]
    payload = json.loads(ch["rules"][0]["to"])
    assert payload["text"] == "称重完成"
    assert payload["room"] == "客厅"


def test_parse_unknown_subflow():
    # 解析器在 parse 阶段即带行号捕获未知子流程（比 validate 更早、更精确）
    bad = "场景: x\n触发: inject\n调用子流程: nope(a=1)\n"
    try:
        parse(bad)
        assert False, "应当抛 DSLError"
    except DSLError as e:
        assert e.line == 3
        assert "nope" in str(e)
    # compile 同样拒绝
    try:
        compile_dsl(bad)
        assert False, "应当抛 DSLError"
    except DSLError:
        pass


def test_validate_missing_required_param():
    # demo_notify 缺 text —— WB24 F1 加固：parse 阶段直接抛异常（fail-fast）
    bad = "场景: x\n触发: inject\n调用子流程: demo_notify(room=客厅)\n"
    try:
        parse(bad)
        assert False, "缺必填参数应抛异常"
    except Exception as e:
        assert "text" in str(e), str(e)


def test_validate_enum_violation():
    # WB24 F1 加固：enum 违规 parse 阶段直接抛异常
    bad = "场景: x\n触发: inject\n调用子流程: demo_notify(text=hi, level=超高)\n"
    try:
        parse(bad)
        assert False, "enum 违规应抛异常"
    except Exception as e:
        assert "level" in str(e), str(e)


def test_parse_trigger_time_cron():
    dsl = "场景: x\n触发: 每天 08:00\n动作: light.turn_on(灯)\n"
    s = parse(dsl)
    assert s.trigger.kind == "time"
    assert s.trigger.cron == "0 8 * * *"
    dsl2 = "场景: x\n触发: 周一至周五 18:30\n动作: light.turn_on(灯)\n"
    assert parse(dsl2).trigger.cron == "30 18 * * 1-5"


def test_parse_error_line_number():
    # 第二行动作缺括号 → 应带行号
    bad = "场景: x\n触发: inject\n动作: light.turn_on 灯\n"
    try:
        parse(bad)
        assert False
    except DSLError as e:
        assert e.line == 3


def test_no_function_node_ever():
    # 任何场景都不应出现 Function 节点（铁律 §18.3）
    for dsl in (DSL_HOME_ARRIVE, DSL_MOTION_SWITCH, DSL_WEIGH):
        flow = compile_dsl(dsl)
        assert "function" not in _types(flow)


def test_parse_expected_block():
    # 预期: 块应解析为 scene.expected（[{entity_id,state}] 或 [{subflow}]）
    dsl = """场景: 书房入户播报
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯)
调用子流程: demo_notify(text=欢迎)
预期:
  light.study_main = on
  subflow: demo_notify
  demo_notify 被调用
"""
    s = parse(dsl)
    assert len(s.expected) == 3, s.expected
    assert {"entity_id": "light.study_main", "state": "on"} in s.expected, s.expected
    assert {"subflow": "demo_notify"} in s.expected, s.expected
    # 解析器应把 subflow 去重/归一化
    subs = [e for e in s.expected if e.get("subflow")]
    assert all(e["subflow"] == "demo_notify" for e in subs), subs


# ── prod 触发节点字段验证（状态别名 + 鲁棒性默认值）──────────────────────
DSL_MOTION_LIGHT_PROD = """
场景: 书房人体感应开灯
触发: binary_sensor.study_motion 有人
动作: light.turn_on(light.study_main, brightness=80)
预期:
  light.study_main = on
"""


def test_compile_prod_trigger_state_alias():
    """prod 模式：DSL「有人」应编译为 ifState='on'（非中文），且含鲁棒性默认值。"""
    flow = compile_dsl(DSL_MOTION_LIGHT_PROD, target="prod")
    trig = flow["nodes"][0]
    assert trig["type"] == "server-state-changed"
    # ① 状态别名：有人 → on
    assert trig["ifState"] == "on", f"期望 ifState='on'，实际={trig.get('ifState')}"
    assert trig["ifStateType"] == "str"
    assert trig["ifStateOperator"] == "is"
    # ② 鲁棒性默认值（防 HA 启动误触发）
    assert trig["outputs"] == 1
    assert trig["outputOnlyOnStateChange"] is True
    assert trig["ignorePrevStateNull"] is True
    assert trig["ignorePrevStateUnknown"] is True
    assert trig["ignorePrevStateUnavailable"] is True
    assert trig["ignoreCurrentStateUnknown"] is True
    assert trig["ignoreCurrentStateUnavailable"] is True
    # ③ for 必须是合法数值字符串（空串 "" 会触发 NR ConfigError: Invalid config value for 'for'）
    assert trig["for"] == "0", f"for 必须为 '0'，实际={trig.get('for')!r}"
    assert trig["forType"] == "num"
    # 实体不变
    assert trig["entities"]["entity"] == ["binary_sensor.study_motion"]


def test_compile_prod_trigger_duration():
    """『持续N分钟』应拆进 for，且 ifState 保持干净状态值（不再把时长词吞进 ifState）。"""
    dsl = """场景: 书房有人驻留开灯
触发: binary_sensor.study_motion on 持续5分钟
动作: light.turn_on(light.study_desk)
"""
    flow = compile_dsl(dsl, target="prod")
    trig = _by_type(flow, "server-state-changed")[0]
    # ① 干净状态值，不带时长词
    assert trig["ifState"] == "on", f"期望 ifState='on'，实际={trig.get('ifState')!r}"
    assert "持续" not in trig["ifState"], "时长词不应出现在 ifState 里"
    # ② 时长拆进 for（分钟）
    assert trig["for"] == "5", f"期望 for='5'，实际={trig.get('for')!r}"
    assert trig["forType"] == "num"
    assert trig["forUnits"] == "minutes"
    # ③ 单输出（DSL 不产出未命中分支）
    assert trig["outputs"] == 1


def test_compile_prod_trigger_duration_units():
    """小时/秒单位应折算为分钟写进 for。"""
    dsl_h = """场景: h
触发: binary_sensor.study_motion on 持续2小时
动作: light.turn_on(light.study_desk)
"""
    trig_h = _by_type(compile_dsl(dsl_h, target="prod"), "server-state-changed")[0]
    assert trig_h["for"] == "120", f"2小时应折算为120分钟，实际={trig_h.get('for')!r}"
    assert trig_h["forUnits"] == "minutes"

    dsl_s = """场景: s
触发: binary_sensor.study_motion on 持续30秒
动作: light.turn_on(light.study_desk)
"""
    trig_s = _by_type(compile_dsl(dsl_s, target="prod"), "server-state-changed")[0]
    assert trig_s["for"] == "0.5", f"30秒应折算为0.5分钟，实际={trig_s.get('for')!r}"


def test_compile_prod_trigger_off_alias():
    """「无人」「关」等也应映射为 off。"""
    dsl = """场景: 离家关灯
触发: binary_sensor.living_motion 无人
动作: light.turn_off(light.living_main)
"""
    flow = compile_dsl(dsl, target="prod")
    trig = flow["nodes"][0]
    assert trig["ifState"] == "off", f"期望 off，实际={trig.get('ifState')}"


def test_compile_prod_trigger_change_wildcard():
    """「变化」/* 不应设 ifState（任意变化都触发）。"""
    dsl = """场景: 传感器变化
触发: binary_sensor.door 变化
动作: light.turn_on(light.hall)
"""
    flow = compile_dsl(dsl, target="prod")
    trig = flow["nodes"][0]
    assert "ifState" not in trig  # wildcard 不设条件
    assert trig["outputs"] == 1   # 但鲁棒性字段仍在
    assert trig["outputOnlyOnStateChange"] is True


# ── §28.2 H 编译器节点扩展：查询(current_state) / 时间段(time_range) ──────
DSL_QUERY_STATE = """
场景: 仅灯灭时有人才开灯
触发: binary_sensor.living_motion 有人
查询: light.living_main off
动作: light.turn_on(light.living_main)
"""

DSL_TIME_RANGE = """
场景: 白天开夜灯
触发: sun.sun 日出
时间段: 07:00-23:00
动作: light.turn_on(light.living_main)
"""

DSL_GATE_ELSE = """
场景: 灯已亮则播报否则忽略
触发: binary_sensor.living_motion 有人
查询: light.living_main on
否则:
    调用子流程: demo_notify(text=灯已亮)
动作: light.turn_on(light.living_main)
"""

# FEEDBACK #9 回归：时间段门后跟 否则 块（此前 TimeRange 无 else_body 字段 → 编译期 AttributeError）
DSL_TIME_RANGE_ELSE = """
场景: 时段内开灯时段外关灯
触发: inject
时间段: 07:00-23:00
  动作: light.turn_on(light.living_main)
否则:
  动作: light.turn_off(light.living_main)
"""


def test_parse_current_state():
    s = parse(DSL_QUERY_STATE)
    cs = [b for b in s.body if hasattr(b, "entity") and hasattr(b, "state")]
    assert len(cs) == 1, s.body
    assert cs[0].entity == "light.living_main"
    assert cs[0].state == "off", "off 应原样保留（非中文别名）"


def test_parse_current_state_alias():
    dsl = "场景: x\n触发: inject\n查询: light.x 有人\n"
    s = parse(dsl)
    cs = [b for b in s.body if hasattr(b, "entity") and hasattr(b, "state")]
    assert cs[0].state == "on", "有人 → on 别名应解析"


def test_compile_current_state_node():
    """查询 编译为 api-current-state：不门控(outputs=1, halt_if="")、把 state 输出到
    msg.payload(outputProperties)，分支路由交由后续 switch 节点承担（FEEDBACK #8）。"""
    flow = compile_dsl(DSL_QUERY_STATE)
    cs = _by_type(flow, "api-current-state")
    assert len(cs) == 1
    n = cs[0]
    assert n["outputs"] == 1, "查询门不再门控，单输出"
    assert n["entityId"] == "light.living_main"
    assert n["state_value"] == "off"
    assert n["halt_if"] == "", "状态不符不再 halt，交由后续 switch 路由"
    assert n["halt_if_compare"] == "is"
    assert n["version"] == 7
    assert n["server"] == E.HA_SERVER_ID
    # FEEDBACK #8：实体态必须输出到 msg.payload，且 node 原生状态改写 msg.data 避免冲突
    assert n.get("outputProperties"), "outputProperties 不得为空"
    assert {"property": "payload", "propertyType": "msg",
            "value": "", "valueType": "entityState"} in n["outputProperties"]
    assert n.get("state_location") == "data"
    assert n.get("override_payload") is False
    # 下游必须有 switch 节点按 payload 分支（替代原 halt 门控）
    assert any(nd["type"] == "switch" for nd in flow["nodes"]), "应有 switch 分支节点"
    # 铁律：绝不生成 Function
    assert "function" not in _types(flow)


def test_parse_time_range():
    s = parse(DSL_TIME_RANGE)
    tr = [b for b in s.body if hasattr(b, "start") and hasattr(b, "end")]
    assert len(tr) == 1, s.body
    assert tr[0].start == "07:00"
    assert tr[0].end == "23:00"


def test_compile_time_range_node():
    """时间段 编译为 time-range-switch 节点（2 输出）。"""
    flow = compile_dsl(DSL_TIME_RANGE)
    tr = _by_type(flow, "time-range-switch")
    assert len(tr) == 1
    n = tr[0]
    assert n["startTime"] == "07:00"
    assert n["endTime"] == "23:00"
    assert n["outputs"] == 2
    assert "startFirst" not in n, "time-range-switch 无 startFirst 字段"
    assert "function" not in _types(flow)


def test_parse_time_range_else_body():
    """FEEDBACK #9：时间段 后的 否则 块应落进 TimeRange.else_body（此前该字段不存在）。"""
    s = parse(DSL_TIME_RANGE_ELSE)
    tr = [b for b in s.body if hasattr(b, "start") and hasattr(b, "end")]
    assert len(tr) == 1, s.body
    assert hasattr(tr[0], "else_body"), "TimeRange 必须有 else_body 字段"
    assert len(tr[0].body) == 1, "窗口内分支 1 个动作"
    assert len(tr[0].else_body) == 1, "窗口外分支 1 个动作"


def test_compile_time_range_else_wires():
    """FEEDBACK #9：时间段+否则 必须能编译（不再 AttributeError），
    且 out0=窗口内接主链、out1=窗口外接否则体首节点，两边都不是孤儿。"""
    flow = compile_dsl(DSL_TIME_RANGE_ELSE)          # 此前这行直接抛 AttributeError
    tr = _by_type(flow, "time-range-switch")
    assert len(tr) == 1
    n = tr[0]
    assert n["outputs"] == 2
    by_id = {x["id"]: x for x in flow["nodes"]}
    assert n["wires"][0], "out0(窗口内) 应连主链"
    assert n["wires"][1], "out1(窗口外) 应连否则体首节点"
    on_target = by_id[n["wires"][0][0]]
    off_target = by_id[n["wires"][1][0]]
    assert on_target["type"] == "api-call-service"
    assert off_target["type"] == "api-call-service"
    assert on_target["id"] != off_target["id"], "两分支须是不同节点"
    # 语义方向：out0=turn_on，out1=turn_off
    assert "turn_on" in json.dumps(on_target, ensure_ascii=False)
    assert "turn_off" in json.dumps(off_target, ensure_ascii=False)


def test_parse_gate_else_body():
    """查询 后的 否则 块应塞进该门节点的 else_body（非主链）。"""
    s = parse(DSL_GATE_ELSE)
    cs = [b for b in s.body if hasattr(b, "else_body")]
    assert len(cs) == 1, s.body
    assert len(cs[0].else_body) == 1, "否则 内仅 1 个子流程调用"
    sub = cs[0].else_body[0]
    assert getattr(sub, "name", None) == "demo_notify"


def test_compile_gate_else_wires():
    """查询门改为「不门控 + switch 路由」后：api-current-state output0 → switch，
    switch 的 out1(否则) 应连到 否则 体内的首个节点（替代原 halt 门控的 output1）。"""
    flow = compile_dsl(DSL_GATE_ELSE)
    cs = _by_type(flow, "api-current-state")
    assert cs, "应生成 api-current-state 节点"
    n = cs[0]
    assert n["outputs"] == 1, "查询门不再门控，单输出"
    by_id = {n["id"]: n for n in flow["nodes"]}
    # output0 应连到 switch 节点（分支路由）
    out0_target = n["wires"][0][0]
    assert by_id[out0_target]["type"] == "switch", "api-current-state output0 应接 switch"
    sw = by_id[out0_target]
    # switch 的 out1(否则) 应连到 否则 体首节点
    assert sw["wires"][1], "switch out1(否则) 应有连线"
    fail_target = sw["wires"][1][0]
    assert fail_target in by_id, "switch out1 应指向真实节点"
    # 否则 体的首节点是 change（为 demo_notify 设 payload），再 link out
    assert by_id[fail_target]["type"] in ("change", "link out", "api-call-service")


def test_time_range_bad_format_raises():
    bad = "场景: x\n触发: inject\n时间段: 7oo-23oo\n"
    try:
        parse(bad)
        assert False, "时间段格式错误应抛 DSLError"
    except DSLError as e:
        assert "HH:MM" in str(e)


def test_no_function_with_new_nodes():
    # 扩展节点同样不引入 Function（铁律 §18.3 全场景覆盖）
    for dsl in (DSL_QUERY_STATE, DSL_TIME_RANGE, DSL_GATE_ELSE):
        flow = compile_dsl(dsl)
        assert "function" not in _types(flow)


# ── C1 历史查询（旧『历史:』原语已废弃，2026-07-20 改为 调用子流程: history_*）──
DSL_HIST_SUBFLOW = """
场景: 查昨晚空调设定
触发: inject
调用子流程: history_state_at(entity=climate.书房空调, at=昨晚23:12, attribute=temperature)
提取: 设定温度 = payload.value
"""

def test_history_primitive_deprecated():
    """旧『历史:』原语应被高声拒绝并指向 history_* 子流程（而非撞『无法识别』硬墙）。"""
    try:
        compile_dsl("场景: x\n触发: inject\n历史: binary_sensor.x 24h\n")
        assert False, "旧 历史: 原语应被拒绝"
    except DSLError as e:
        assert "history_state_at" in str(e)
        assert "调用子流程" in str(e)


def test_history_subflow_compiles():
    """调用子流程: history_state_at 编译为 subflow 实例节点（请求/响应，非单向 link_out）。"""
    flow = compile_dsl(DSL_HIST_SUBFLOW)
    subs = [n for n in flow["nodes"] if n["type"].startswith("subflow:")]
    assert len(subs) == 1, _types(flow)
    n = subs[0]
    assert n["type"].startswith("subflow:"), n["type"]
    # 下游『提取』节点接到子流程实例输出口（请求/响应返回值透传）
    assert len(_by_type(flow, "change")) >= 1
    # 铁律：绝不生成 Function
    assert "function" not in _types(flow)


def test_history_subflow_no_semantic_gap():
    """用了 history_* 子流程 → 不报历史缺口。"""
    gaps = E.detect_semantic_gaps(DSL_HIST_SUBFLOW)
    assert gaps == [], gaps


# ── C2 首次触发原语 ───────────────────────────────────────────────
DSL_FIRST_TRIG = """
场景: 首次感应开灯
触发: binary_sensor.ling_pu 有人 首次
动作: light.turn_on(light.zhuwo)
"""

def test_parse_first_trigger():
    s = parse(DSL_FIRST_TRIG)
    t = s.triggers[0]
    assert t.first is True
    assert t.entity == "binary_sensor.ling_pu"
    assert t.state == "有人"        # 修饰词已剥离
    assert "首次" not in t.entity and "首次" not in t.state


def test_compile_first_trigger_prod():
    """首次 → prod 触发节点带上升沿 + 文档 comment 节点。"""
    flow = compile_dsl(DSL_FIRST_TRIG, target="prod")
    ssc = _by_type(flow, "server-state-changed")
    assert len(ssc) == 1
    assert ssc[0]["outputOnlyOnStateChange"] is True   # 上升沿
    cmt = _by_type(flow, "comment")
    assert len(cmt) == 1, "应生成『首次』语义边界说明节点"
    # staging 下首次修饰同样编译为 server-state-changed（不再降级 inject，WB4 #2 修复）
    flow2 = compile_dsl(DSL_FIRST_TRIG, target="staging")
    assert len(_by_type(flow2, "server-state-changed")) == 1
    assert len(_by_type(flow2, "comment")) == 1, "staging 也应生成『首次』语义边界说明节点"
    assert len(_by_type(flow2, "inject")) == 0


# ── B1 语义缺口高声拒绝（避免静默降级）─────────────────────────────
DSL_SILENT_DEGRADE = """
场景: 静默降级陷阱
触发: inject
取值: binary_sensor.ling_pu 昨晚首次时间
"""

def test_semantic_gap_rejected():
    """含历史/首次意图却未用对应原语 → compile_dsl 高声拒绝。"""
    try:
        compile_dsl(DSL_SILENT_DEGRADE)
        assert False, "应拦截静默降级"
    except DSLError as e:
        assert "语义缺口" in str(e)
        assert "history_state_at" in str(e)       # 提示正确替代原语


def test_semantic_gap_ok_with_history_subflow():
    """用了 history_* 子流程（即便 取值 行仍含历史意图词）→ 不报历史缺口。"""
    dsl = """场景: 正确用法
触发: inject
调用子流程: history_state_at(entity=climate.x, at=昨晚23:12)
取值: climate.x 昨晚温度
"""
    gaps = E.detect_semantic_gaps(dsl)
    assert gaps == [], gaps


def test_semantic_gap_detect_first_in_body():
    """body 里的首次/去重意图也应被捕获（即便 trigger 没写首次）。"""
    dsl = "场景: x\n触发: inject\n取值: sensor.x 去重后的首次\n"
    gaps = E.detect_semantic_gaps(dsl)
    assert any("首次" in g or "去重" in g for g in gaps), gaps


def test_image_vision_subflow_compile():
    """P1②：文生图/图生文 ApiSpec 经 dsl_engine 编译为正确内联 http_api 节点。

    验证：两个端点都生成独立 http request（url 正确）；文生图把响应 image_url
    规整进 payload.reply，图生文把 reply 规整进 payload.reply（与对话类一致）。
    """
    dsl = """
场景: 视觉能力
触发: 每天 20:00
调用子流程: llm_doubao_image(prompt=`一只赛博朋克风格的猫`)
提取: 图片链接 = payload.reply
调用子流程: llm_doubao_vision(prompt=`描述这张图`, image=`https://example.com/cat.jpg`)
提取: 回复 = payload.reply
"""
    flow = compile_dsl(dsl)
    nodes = flow["nodes"]
    urls = {n.get("url") for n in nodes if n.get("type") == "http request"}
    assert "http://<NAS_IP>:1880/llm/image" in urls, urls
    assert "http://<NAS_IP>:1880/llm/vision" in urls, urls
    # 文生图：提取节点把 image_url 规整进 payload.reply
    ext_img = [n for n in nodes if n.get("type") == "change"
               and n.get("name") == "取 llm_doubao_image 返回值"]
    assert ext_img, "缺少 image 提取节点"
    assert ext_img[0]["rules"][0]["to"] == "payload.image_url"
    # 图生文：提取节点把 reply 规整进 payload.reply（identity）
    ext_vis = [n for n in nodes if n.get("type") == "change"
               and n.get("name") == "取 llm_doubao_vision 返回值"]
    assert ext_vis, "缺少 vision 提取节点"
    assert ext_vis[0]["rules"][0]["to"] == "payload.reply"


# ── A4 语义缺口检测扩展：间隔触发 / 自然语言条件 / 直到…才 ───────────────
def test_gap_interval_trigger_rejected():
    """每隔 N 分钟/秒 间隔触发无对应原语 → 高声拒绝。"""
    dsl = "场景: 定时检查\n触发: 每隔 5 分钟\n动作: light.turn_on(灯)\n"
    try:
        compile_dsl(dsl)
        assert False, "应拦截间隔触发缺口"
    except DSLError as e:
        assert "语义缺口" in str(e)
        assert "每隔" in str(e)
        assert "间隔触发" in str(e)


def test_gap_natural_cond_rejected():
    """自然语言『如果…否则』未用 分支/否则 → 高声拒绝并给正确原语。"""
    dsl = "场景: x\n触发: inject\n如果 有人 就 开灯 否则 关灯\n"
    try:
        compile_dsl(dsl)
        assert False, "应拦截自然语言条件缺口"
    except DSLError as e:
        assert "语义缺口" in str(e)
        assert "分支" in str(e)        # 建议指向正确原语


def test_gap_until_then_rejected():
    """『直到…才』等待/持久意图 → 高声拒绝并给映射建议。"""
    dsl = "场景: x\n触发: inject\n直到 人回家 才 关灯\n"
    try:
        compile_dsl(dsl)
        assert False, "应拦截直到…才缺口"
    except DSLError as e:
        assert "语义缺口" in str(e)
        assert "直到" in str(e)
        assert "触发" in str(e)        # 建议指向 触发+动作 映射


def test_gap_valid_dsl_no_false_positive():
    """合法 DSL（含 注释 行带自然语言说明）不应误报新缺口。"""
    dsl = """场景: 合法场景含注释
触发: 每天 08:00
动作: light.turn_on(light.x)
注释: 如果外面下雨就别开窗
"""
    gaps = E.detect_semantic_gaps(dsl)
    assert gaps == [], gaps


def test_gap_comment_with_until_no_false_positive():
    """注释行里的『直到…才』不应误判（已排除合法 DSL 关键字前缀）。"""
    dsl = "场景: x\n触发: inject\n注释: 直到天黑才关灯\n动作: light.turn_on(灯)\n"
    gaps = E.detect_semantic_gaps(dsl)
    assert gaps == [], gaps


# ── A5 编译错误文案优化：带行号 + 修复建议 ───────────────────────────
def test_error_msg_has_suggestion():
    """常见解析错误文案应含『建议：』修复指引，且保留原锚点子串。"""
    cases = [
        ("场景: x\n触发: 乱写一通\n", "触发"),
        ("场景: x\n触发: inject\n动作: 灯\n", "动作"),       # 缺 domain.service
        ("场景: x\n触发: inject\n未知指令 abc\n", "顶层指令"),
        ("场景: x\n触发: inject\n时间段: 7oo\n", "HH:MM"),
    ]
    for dsl, anchor in cases:
        try:
            parse(dsl)
            assert False, f"应报错: {dsl!r}"
        except DSLError as e:
            msg = str(e)
            assert anchor in msg, f"缺少锚点 {anchor!r}: {msg}"
            assert "建议" in msg, f"缺少修复建议: {msg}"


def test_error_msg_line_number():
    """错误应带行号（第 N 行）。"""
    bad = "场景: x\n触发: inject\n动作: light.turn_on 灯\n"
    try:
        parse(bad)
        assert False
    except DSLError as e:
        assert e.line == 3
        assert "第 3 行" in str(e)


# ── Phase 4：原生节点逃逸（RawNode）──────────────────────────────────
DSL_RAW_SWITCH = """
场景: 复合条件开灯
触发: sensor.motion_living 有人
原生节点: {"type":"switch","name":"复合AND/OR","outputs":3,"property":"payload.cond","rules":[{"t":"eq","v":"a"},{"t":"eq","v":"b"},{"t":"else"}]}
动作: light.turn_on(客厅主灯)
"""


def test_raw_node_parse_and_compile():
    s = parse(DSL_RAW_SWITCH)
    raws = [b for b in s.body if isinstance(b, RawNode)]
    assert len(raws) == 1, "应解析出 1 个原生节点"
    assert raws[0].node_type == "switch"
    assert raws[0].config.get("outputs") == 3
    # 引擎托管字段被剥离：config 不含 id/z/x/y/wires/type
    assert "id" not in raws[0].config and "wires" not in raws[0].config
    flow = compile_dsl(DSL_RAW_SWITCH)
    types = _types(flow)
    assert "switch" in types, "编译产物应含原生 switch 节点"
    assert "function" not in types, "铁律：绝不生成 Function 节点"


def test_raw_node_forbidden_function():
    dsl = '场景: x\n触发: inject\n原生节点: {"type":"function","func":"return msg;"}'
    try:
        parse(dsl)
        raise AssertionError("应拒绝 function 类型原生节点")
    except DSLError:
        pass


def test_raw_node_forbidden_exec():
    dsl = '场景: x\n触发: inject\n原生节点: {"type":"exec","command":"ls"}'
    try:
        parse(dsl)
        raise AssertionError("应拒绝 exec 类型原生节点")
    except DSLError:
        pass


def test_raw_node_unknown_type_rejected():
    dsl = '场景: x\n触发: inject\n原生节点: {"type":"foobar"}'
    try:
        parse(dsl)
        raise AssertionError("应拒绝白名单外节点类型")
    except DSLError:
        pass


def test_raw_node_invalid_json():
    dsl = "场景: x\n触发: inject\n原生节点: {not json"
    try:
        parse(dsl)
        raise AssertionError("非法 JSON 应被拒")
    except DSLError:
        pass


def test_raw_node_indented_body():
    # 原生节点也可写在 分支/否则 体缩进下
    dsl = """
场景: 条件内嵌原生节点
触发: inject
分支 x == 1:
    原生节点: {"type":"change","rules":[{"t":"set","p":"payload.v","pt":"msg","to":"1","tot":"num"}]}
否则:
    动作: light.turn_off(客厅主灯)
"""
    s = parse(dsl)
    sw = s.body[0]
    assert hasattr(sw, "branches") and sw.branches
    raws = [b for b in sw.branches[0].body if isinstance(b, RawNode)]
    assert len(raws) == 1 and raws[0].node_type == "change"


def test_condition_gate_emits_state_to_payload():
    """FEEDBACK #8 回归：条件门(_emit_condition)生成的 api-current-state 必须把实体态
    输出到 msg.payload(outputProperties)，且不得用 halt_if 门控——否则当 state==on 时
    节点 halt 走空分支，下游 switch 永远拿不到 on，整条条件流静默断链。"""
    dsl = (
        "场景: 条件门输出属性\n"
        "触发: inject\n"
        "条件: switch.study_pc == on\n"
        "    动作: light.turn_on(灯)\n"
    )
    flow = compile(parse(dsl))
    acs = [n for n in flow["nodes"] if n["type"] == "api-current-state"]
    assert acs, "条件门应生成 api-current-state 节点"
    node = acs[0]
    assert node.get("outputProperties"), "outputProperties 不得为空"
    assert {"property": "payload", "propertyType": "msg",
            "value": "", "valueType": "entityState"} in node["outputProperties"], \
        f"outputProperties 须含 entityState→payload: {node['outputProperties']}"
    assert node.get("halt_if") == "", "条件门不得用 halt_if 门控"
    assert node.get("state_location") == "data", "须改写 msg.data 避开 payload 冲突"
    assert node.get("override_payload") is False
    # 下游须有 switch 按 payload 路由（而非依赖 halt 分支）
    assert any(n["type"] == "switch" for n in flow["nodes"]), \
        "条件门下游应有 switch 节点按 payload 分支"


def test_query_state_gate_emits_state_to_payload():
    """FEEDBACK #8 回归：查询门(_emit_current_state, 查询: 语法)生成的 api-current-state
    同样必须输出 state 到 msg.payload 且不用 halt_if 门控；分支路由由后续 switch 承担。"""
    dsl = (
        "场景: 查询状态门\n"
        "触发: inject\n"
        "查询: light.living_room on\n"
        "    动作: light.turn_on(灯)\n"
        "否则:\n"
        "    动作: light.turn_off(灯)\n"
    )
    flow = compile(parse(dsl))
    acs = [n for n in flow["nodes"] if n["type"] == "api-current-state"]
    assert acs, "查询门应生成 api-current-state 节点"
    node = acs[0]
    assert node.get("outputProperties"), "outputProperties 不得为空"
    assert {"property": "payload", "propertyType": "msg",
            "value": "", "valueType": "entityState"} in node["outputProperties"], \
        f"outputProperties 须含 entityState→payload: {node['outputProperties']}"
    assert node.get("halt_if") == "", "查询门不得用 halt_if 门控"
    assert node.get("state_location") == "data"
    assert node.get("override_payload") is False
    # 分支路由由后续 switch 节点承担（state==on → body, 否则 → else_body）
    switches = [n for n in flow["nodes"] if n["type"] == "switch"]
    assert switches, "查询门下游应有 switch 节点按 payload 分支"


# ── WB85 F1：分支引用未定义取值标签 → fail-closed ──────────────────────────
def test_wb85_f1_undefined_label_rejected():
    """分支条件引用未在『取值:』定义的标签 → 编译期 DSLError(C_LABEL_UNDEFINED)，
    消除『$number(undefined)→NaN→条件恒假→反向执行』的静默失败。"""
    dsl = """
场景: t
触发: 每天 08:00
取值: light.office 亮度
分支: $number(foo) > 10
  动作: light.turn_on(light.bedroom)
否则:
  动作: light.turn_off(light.bedroom)
"""
    try:
        compile_dsl(dsl)
        raise AssertionError("应抛 C_LABEL_UNDEFINED 却编译通过")
    except DSLError as e:
        assert e.code == C_LABEL_UNDEFINED, e.code
    # 已定义标签可正常编译，且被改写为 payload.<field>
    dsl_ok = """
场景: t
触发: 每天 08:00
取值: light.office 亮度
分支: $number(亮度) > 10
  动作: light.turn_on(light.bedroom)
否则:
  动作: light.turn_off(light.bedroom)
"""
    flow = compile_dsl(dsl_ok)
    sw = next(n for n in flow["nodes"] if n["type"] == "switch")
    assert any("payload.亮度" in (r.get("v", "") or "") for r in sw["rules"]), sw["rules"]


def test_wb85_f1_jsonata_safe_no_false_positive():
    """含 $number/$exists/payload 等合法 JSONata 的复杂分支条件不得误伤。"""
    dsl = """
场景: t
触发: 每天 08:00
取值: light.office 亮度
分支: $number(亮度) > 10 and $exists(payload.state)
  动作: light.turn_on(light.bedroom)
否则:
  动作: light.turn_off(light.bedroom)
"""
    # 不应抛 C_LABEL_UNDEFINED
    compile_dsl(dsl)


# ── WB85 F2：延时非法单位 → fail-closed ────────────────────────────────────
def test_wb85_f2_invalid_unit_rejected():
    """延时单位非法（如 光年）→ 编译期 DSLError(C_DELAY_UNIT)，不再静默默认成秒。"""
    dsl = """
场景: t
触发: 每天 08:00
延时: 5 光年
  动作: light.turn_on(light.bedroom)
"""
    try:
        compile_dsl(dsl)
        raise AssertionError("应抛 C_DELAY_UNIT 却编译通过")
    except DSLError as e:
        assert e.code == C_DELAY_UNIT, e.code


def test_wb85_f2_valid_unit_compiles():
    """合法单位（秒/分钟/小时/毫秒）正常编译，且秒数换算正确。"""
    for unit, expect_ms in [("5 秒", 5000), ("2 分钟", 120000), ("1 小时", 3600000),
                            ("500 毫秒", 500)]:
        dsl = f"""
场景: t
触发: 每天 08:00
延时: {unit}
  动作: light.turn_on(light.bedroom)
"""
        flow = compile_dsl(dsl)
        d = next(n for n in flow["nodes"] if n["type"] == "delay")
        assert d["timeout"] == str(expect_ms), (unit, d["timeout"])


if __name__ == "__main__":
    # 零依赖运行器（无 pytest 时可直接 `python test_dsl_engine.py`）
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    raise SystemExit(1 if failed else 0)
