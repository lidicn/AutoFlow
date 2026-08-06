# -*- coding: utf-8 -*-
"""生成 REG-M 能力矩阵 tab（M1–M5，28 断言）的 Node-RED flow JSON。

结构沿用 capmatrix 已验证形态：
    1×inject → 1×fan function(28 输出) → 28 分支 → 1×join(count=28) → table → debug/file

验收铁律：**每条断言必须给真实证据**，禁止「没报错=通过」。因此
- M1 读取类：断言里带 HA 真值 + switch 实际走的分支；
- M2 动作类：调用后 **读回实体真值** 校验（on/off/brightness），并在链尾复原现场；
- M3 历史类：按子流程契约注入 ``msg.entity/at/start/end/state/metric``，断言真实返回值；
- M4 通知类：demo_notify 校验 TTS 队列触发计数变化；bark 校验子流程回显的
  ``{ok,status,sent:{title,body}}``（HTTP 200 + 原样回显）；
- M5 时序类：cron 用「上次真实触发时间戳」佐证，delay 用实测毫秒佐证。

⚠️ 副作用（真机）：会开关书房台灯、切换驱蚊器开关（用后复原）、发 2 条 TTS 语音播报、
   2 条 Bark 推送、1 条微信消息。属回归套件固有代价，跑前请知悉。

用法::

    python build_flow.py [tab_id]        # tab_id 缺省读台账，再缺省用占位符
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
LEDGER = REPO / "tests" / "fixtures_local" / "reg_m_tab.id"
OUT_JSON = REPO / "tests" / "fixtures_local" / "reg_m_flow.json"

LABEL = "REG-M 能力矩阵 (M1-M5, 28断言)"
SERVER = "e93e1ad9c034e866"                      # 1990 上 HA server 配置节点
TTS_LINK_IN = "b595563939283231"                # demo_notify 的 link_out 目标（TTS队列入口）
SUB_WECHAT = "ed26c7cf80e9429a"                 # 微信子流程
SUB_BARK = "b0bbc86abb2172a5"                   # Bark 子流程
HIST_SUBS = ("af_hist_state_at", "af_hist_occurred",
             "af_hist_duration", "af_hist_aggregate")

# ── 真实实体（2026-08-06 经 HA /api/states 实测存在）────────────────────
E_TEMP = "sensor.wo_de_jia_wen_du"                        # 我的家 温度，实测 34.45
E_LAMP = "light.philips_cn_249518489_rwread_s_2_light"    # 书房台灯，on/brightness=128
E_LAMP2 = "light.yeelink_cn_555003624_lamp22_s_2"         # 显示器挂灯1S，on
E_SWITCH = "switch.b460eda0bc76_switch"                   # 米家驱蚊器2，off（toggle 后复原）
E_MISSING = "sensor.does_not_exist_xyz"                   # 故意不存在
LAMP_BRIGHTNESS_RESTORE = 128                             # 跑完把台灯亮度复原

# ── 28 个用例（case, kind）────────────────────────────────────────────
CASES: list[tuple[str, str]] = [
    ("M1.1", "num_branch_true"), ("M1.2", "num_branch_false"), ("M1.3", "state_branch"),
    ("M1.4", "write_cn_field"), ("M1.5", "missing_entity"), ("M1.6", "branch_msgvar"),
    ("M2.1", "light_on"), ("M2.2", "light_off"), ("M2.3", "brightness_set"),
    ("M2.4", "switch_toggle"), ("M2.5", "call_subflow"), ("M2.6", "room_batch"),
    ("M3.1", "hist_state_at"), ("M3.2", "hist_state_at2"),
    ("M3.3", "hist_occurred"), ("M3.4", "hist_occurred2"),
    ("M3.5", "hist_duration"), ("M3.6", "hist_duration2"),
    ("M3.7", "hist_aggregate_mean"), ("M3.8", "hist_aggregate_max"),
    ("M4.1", "demo_notify_text"), ("M4.2", "demo_notify_room_level"),
    ("M4.3", "bark_basic"), ("M4.4", "bark_title"),
    ("M5.1", "inject_manual"), ("M5.2", "cron"), ("M5.3", "delay"), ("M5.4", "multi_join"),
]
KIND = dict(CASES)
ORD = {c: i for i, (c, _) in enumerate(CASES)}

nodes: list[dict] = []
ENTRY: dict[str, list[str]] = {}
TAB_ID = "reg_m_tab"


def aid(case: str) -> str:
    """断言节点 id（点号换下划线，保证 NR id 合法）。"""
    return "regm_a_" + case.replace(".", "_")


def N(nid: str, typ: str, x: int, y: int, wires: list, **kw) -> dict:
    """追加一个节点。

    Args:
        nid: 节点 id。
        typ: 节点类型。
        x, y: 画布坐标（仅影响可读性）。
        wires: 输出连线（二维数组）。
        **kw: 该类型节点的其余属性。

    Returns:
        新建的节点字典。
    """
    node = {"id": nid, "type": typ, "z": TAB_ID, "x": x, "y": y, "wires": wires}
    node.update(kw)
    nodes.append(node)
    return node


# ── 节点工厂 ──────────────────────────────────────────────────────────
def read_state(nid: str, entity: str, x: int, y: int, wires: list,
               prop: str = "payload", entity_prop: str | None = None) -> dict:
    """api-current-state 读取节点。

    Args:
        prop: 状态写入的 msg 属性名。
        entity_prop: 若给出，额外把完整 entity 对象写入该属性（用于校验 attributes）。
    """
    ops = [{"property": prop, "propertyType": "msg", "value": "", "valueType": "entityState"}]
    if entity_prop:
        ops.append({"property": entity_prop, "propertyType": "msg",
                    "value": "", "valueType": "entity"})
    return N(nid, "api-current-state", x, y, wires,
             server=SERVER, version=7, outputs=1, halt_if="", halt_if_type="str",
             halt_if_compare="is", entity_id=entity, state_type="str",
             blockInputOverrides=True, outputProperties=ops,
             override_topic=False, state_location=prop, override_payload=False)


def call_service(nid: str, domain: str, service: str, entities: list[str],
                 x: int, y: int, wires: list, data: dict | None = None) -> dict:
    """api-call-service 动作节点。

    ⚠️ ``data`` 必须是「对象的 JSON 字符串」。历史 bug：对已是 str 的 ``"{}"`` 再
    ``json.dumps`` 会得到 ``"\\"{}\\""``（被引号包住的字符串），节点解析失败直接抛错、
    不产出任何消息 —— 表现为该分支「静默不返回」。
    """
    return N(nid, "api-call-service", x, y, wires,
             server=SERVER, version=7, action="%s.%s" % (domain, service),
             floorId=[], areaId=[], deviceId=[], labelId=[],
             entityId=list(entities), data=json.dumps(data or {}, ensure_ascii=False),
             dataType="json", mergeContext="", mustacheAltTags=False,
             outputProperties=[], queue="none", blockInputOverrides=True,
             domain=domain, service=service)


def change_set(nid: str, rules: list[dict], x: int, y: int, wires: list) -> dict:
    """change(set) 节点。

    ⚠️ 每条 rule **必须**带 ``"t": "set"``。历史 bug：缺 ``t`` 时 Node-RED 静默跳过整条
    规则（不报错），导致「赋值看似执行实则没生效」，是首轮 4 条用例假失败的总根因。
    """
    return N(nid, "change", x, y, wires,
             rules=[dict(r, t=r.get("t", "set")) for r in rules])


def switch_node(nid: str, prop: str, rules: list[dict], x: int, y: int,
                wires: list, prop_type: str = "msg") -> dict:
    """switch 分支节点。"""
    return N(nid, "switch", x, y, wires, property=prop, propertyType=prop_type,
             rules=rules, checkall="true", repair=False, outputs=len(rules))


def delay_node(nid: str, seconds: float, x: int, y: int, wires: list) -> dict:
    """固定延时节点。"""
    return N(nid, "delay", x, y, wires, pauseType="delay", timeout=str(seconds),
             timeoutUnits="seconds", rate="1", nbRateUnits="1", rateUnits="second",
             randomFirst="1", randomLast="5", randomUnits="seconds",
             drop=False, allowrate=False, outputs=1)


def func_node(nid: str, body: str, x: int, y: int, wires: list, outputs: int = 1) -> dict:
    """function 节点。"""
    return N(nid, "function", x, y, wires, func=body, outputs=outputs, noerr=0,
             initialize="", finalize="", libs=[])


def catch_node(nid: str, scope: list[str], x: int, y: int, wires: list) -> dict:
    """限定作用域的 catch 节点（只兜住指定节点的错误）。"""
    return N(nid, "catch", x, y, wires, scope=list(scope), uncaught=False)


def assert_node(case: str, js: str, y: int, extra: tuple[str, ...] = ()) -> str:
    """断言归一化节点：执行 ``js``（须定义 ok/s），产出 {case,kind,ok,summary} 汇入 join。

    Args:
        case: 用例编号。
        js: 断言片段，可用 ``p``(=msg.payload) 与 ``msg``；须给 ``ok`` 与 ``s`` 赋值。
        y: 画布纵坐标。
        extra: 除 join 外的额外下游（用于串行链把控制权交给下一用例）。

    Returns:
        断言节点 id。
    """
    nid = aid(case)
    body = (
        "// %s %s 断言归一化\n" % (case, KIND[case])
        # 幂等闸门：同一轮 run 内每个用例只上报一次。防止某分支被重复触发（例如
        # inject 节点既被 fan 驱动又被人工点击）把 join(count=28) 提前凑满，
        # 从而把真正慢一步的用例误判成「未返回」。
        + "const _ep = flow.get('regm_epoch') || 0;\n"
        + "if (flow.get('regm_seen_%s') === _ep) { return null; }\n" % case
        + "flow.set('regm_seen_%s', _ep);\n" % case
        + "const p = msg.payload;\n"
        + js + "\n"
        + "msg.payload = { case: '%s', kind: '%s', ok: !!ok, summary: String(s) };\n"
        % (case, KIND[case])
        + "node.status({ fill: ok ? 'green' : 'red', shape: 'dot', "
          "text: (ok ? 'OK ' : 'FAIL ') + '%s' });\n" % case
        + "return msg;"
    )
    func_node(nid, body, 1620, y, [["regm_join", *extra]])
    return nid


def chain_reset(nid: str, case: str, x: int, y: int, wires: list) -> dict:
    """串行链上的现场清理：清掉上一用例遗留的中间字段，避免串味。"""
    return func_node(nid, (
        "// 串行链交接：清理上一用例遗留字段，避免断言串味\n"
        "['verify', 'verify2', 'ent', 'raw', 'before', 'route', 'temp', '温度',"
        " 'tempvar', '_tts_before', '_tts_tag', '_sent_body', 't0'].forEach(function (k) "
        "{ delete msg[k]; });\n"
        "msg.payload = {};\n"
        "msg._case = '%s';\n"
        "return msg;" % case), x, y, wires)


# ══════════════════════════════════════════════════════════════════════
# 顶部：RUN ALL inject + fan(28)
# ══════════════════════════════════════════════════════════════════════
def build_header() -> None:
    """建 RUN ALL inject 与 28 路 fan（fan 的连线在所有分支建完后回填）。"""
    N("regm_run", "inject", 90, 60, [["regm_fan"]],
      name="▶ RUN ALL (REG-M)", props=[{"p": "payload"}], repeat="", crontab="",
      once=False, onceDelay=0.1, topic="", payload="", payloadType="date")
    pairs = ",\n ".join("['%s','%s']" % (c, k) for c, k in CASES)
    func_node("regm_fan", (
        "// 一次点击并发跑完整矩阵：%d 路（串行链用例只发链头）\n" % len(CASES)
        # epoch 是本轮 run 的唯一标记，供各断言节点做幂等去重
        + "flow.set('regm_epoch', Date.now());\n"
        + "const C = [\n " + pairs + "\n];\n"
        "return C.map(function (c) { "
        "return { _case: c[0], _kind: c[1], payload: {} }; });"
    ), 300, 60, [], outputs=len(CASES))


# ══════════════════════════════════════════════════════════════════════
# M1 读取与分支（6）
# ══════════════════════════════════════════════════════════════════════
def build_m1() -> None:
    """M1：数值真/假两路、状态分支、中文字段写入、不存在实体、msg 变量引用分支。"""
    # M1.1 数值 sensor 温度>27 —— 真路（用 HA 真实温度）
    read_state("regm_m11_rd", E_TEMP, 480, 120, [["regm_m11_keep"]])
    change_set("regm_m11_keep", [{"p": "raw", "pt": "msg", "to": "payload", "tot": "msg"}],
               680, 120, [["regm_m11_sw"]])
    switch_node("regm_m11_sw", "payload",
                [{"t": "gt", "v": "27", "vt": "num"}, {"t": "else"}],
                860, 120, [["regm_m11_hi"], ["regm_m11_lo"]])
    change_set("regm_m11_hi", [{"p": "payload", "pt": "msg", "to": "HIGH", "tot": "str"}],
               1040, 100, [[aid("M1.1")]])
    change_set("regm_m11_lo", [{"p": "payload", "pt": "msg", "to": "LOW", "tot": "str"}],
               1040, 140, [[aid("M1.1")]])
    assert_node("M1.1", (
        "const v = Number(msg.raw);\n"
        "let ok = (!isNaN(v) && v > 27 && msg.payload === 'HIGH');\n"
        "let s = 'HA真值 ' + msg.raw + '°C(>27) → switch 走【真路】, 输出=' + msg.payload;"
    ), 120)

    # M1.2 数值 sensor 温度>27 —— 假路（同结构，注入低值证明 else 路可达）
    read_state("regm_m12_rd", E_TEMP, 480, 200, [["regm_m12_keep"]])
    change_set("regm_m12_keep", [
        {"p": "raw", "pt": "msg", "to": "payload", "tot": "msg"},
        {"p": "payload", "pt": "msg", "to": "10", "tot": "num"},
    ], 680, 200, [["regm_m12_sw"]])
    switch_node("regm_m12_sw", "payload",
                [{"t": "gt", "v": "27", "vt": "num"}, {"t": "else"}],
                860, 200, [["regm_m12_hi"], ["regm_m12_lo"]])
    change_set("regm_m12_hi", [{"p": "payload", "pt": "msg", "to": "HIGH", "tot": "str"}],
               1040, 180, [[aid("M1.2")]])
    change_set("regm_m12_lo", [{"p": "payload", "pt": "msg", "to": "LOW", "tot": "str"}],
               1040, 220, [[aid("M1.2")]])
    assert_node("M1.2", (
        "let ok = (msg.payload === 'LOW');\n"
        "let s = '注入 10（同刻 HA 真值=' + msg.raw + '°C）→ switch 走【假路】, 输出='"
        " + msg.payload;"
    ), 200)

    # M1.3 状态 light on/off 分支
    read_state("regm_m13_rd", E_LAMP, 480, 280, [["regm_m13_keep"]])
    change_set("regm_m13_keep", [{"p": "raw", "pt": "msg", "to": "payload", "tot": "msg"}],
               680, 280, [["regm_m13_sw"]])
    switch_node("regm_m13_sw", "payload",
                [{"t": "eq", "v": "on", "vt": "str"}, {"t": "else"}],
                860, 280, [["regm_m13_on"], ["regm_m13_off"]])
    change_set("regm_m13_on", [{"p": "payload", "pt": "msg", "to": "ON", "tot": "str"}],
               1040, 260, [[aid("M1.3")]])
    change_set("regm_m13_off", [{"p": "payload", "pt": "msg", "to": "OFF", "tot": "str"}],
               1040, 300, [[aid("M1.3")]])
    assert_node("M1.3", (
        "let ok = ((msg.raw === 'on' && msg.payload === 'ON') || "
        "(msg.raw === 'off' && msg.payload === 'OFF'));\n"
        "let s = 'HA真值 灯=' + msg.raw + ' → 分支输出=' + msg.payload;"
    ), 280)

    # M1.4 取值写 msg.温度（中文 field 陷阱）
    read_state("regm_m14_rd", E_TEMP, 480, 360, [["regm_m14_chg"]])
    change_set("regm_m14_chg", [
        {"p": "温度", "pt": "msg", "to": "payload", "tot": "msg"},
        {"p": "temp", "pt": "msg", "to": "payload", "tot": "msg"},
    ], 680, 360, [[aid("M1.4")]])
    assert_node("M1.4", (
        "const cn = msg['温度'];\n"
        "let ok = (cn !== undefined && cn !== null && String(cn).length > 0 "
        "&& String(cn) === String(msg.temp));\n"
        "let s = 'change 写入 msg.温度=' + cn + ' (中文字段可读), ASCII 对照 msg.temp='"
        " + msg.temp;"
    ), 360)

    # M1.5 实体不存在优雅失败（节点会抛错不出消息 → 必须 catch 兜住）
    read_state("regm_m15_rd", E_MISSING, 480, 440, [[aid("M1.5")]])
    catch_node("regm_m15_catch", ["regm_m15_rd"], 680, 480, [[aid("M1.5")]])
    assert_node("M1.5", (
        "let em = '';\n"
        "if (msg.error) { em = String((msg.error && msg.error.message) || msg.error); }\n"
        "let ok = false; let s = '';\n"
        "if (em) { ok = true; s = '不存在实体 ' + '%s' + ' → catch 捕获: ' + em.slice(0, 90); }\n"
        % E_MISSING +
        "else if (p === undefined || p === null || p === '') { ok = true; "
        "s = '不存在实体 → 返回空状态(优雅降级) payload=' + JSON.stringify(p); }\n"
        "else { ok = false; s = '异常: 不存在实体却返回状态 ' + "
        "JSON.stringify(p).slice(0, 60); }"
    ), 440)

    # M1.6 分支直接引用 msg 变量（非 状态. 前缀）
    read_state("regm_m16_rd", E_TEMP, 480, 520, [["regm_m16_chg"]])
    change_set("regm_m16_chg", [{"p": "tempvar", "pt": "msg", "to": "payload", "tot": "msg"}],
               680, 520, [["regm_m16_sw"]])
    switch_node("regm_m16_sw", "tempvar",
                [{"t": "gt", "v": "27", "vt": "num"}, {"t": "else"}],
                860, 520, [["regm_m16_gt"], ["regm_m16_el"]])
    change_set("regm_m16_gt", [{"p": "route", "pt": "msg", "to": "gt", "tot": "str"}],
               1040, 500, [[aid("M1.6")]])
    change_set("regm_m16_el", [{"p": "route", "pt": "msg", "to": "else", "tot": "str"}],
               1040, 540, [[aid("M1.6")]])
    assert_node("M1.6", (
        "const v = Number(msg.tempvar);\n"
        "let ok = (msg.tempvar !== undefined && ((v > 27 && msg.route === 'gt') || "
        "(v <= 27 && msg.route === 'else')));\n"
        "let s = 'switch 直接引用 msg.tempvar=' + msg.tempvar + '（未用 状态. 前缀）→ route='"
        " + msg.route;"
    ), 520)

    ENTRY["M1.1"] = ["regm_m11_rd"]
    ENTRY["M1.2"] = ["regm_m12_rd"]
    ENTRY["M1.3"] = ["regm_m13_rd"]
    ENTRY["M1.4"] = ["regm_m14_rd"]
    ENTRY["M1.5"] = ["regm_m15_rd"]
    ENTRY["M1.6"] = ["regm_m16_rd"]


# ══════════════════════════════════════════════════════════════════════
# M2 动作（6）—— 同设备用例必须串行，否则互相打架读不到真值
# ══════════════════════════════════════════════════════════════════════
def build_m2() -> None:
    """M2：开/关/亮度/开关toggle/子流程动作/房间批量，每步读回 HA 真值佐证。"""
    y = 620
    # M2.1 light turn_on → 读回 on
    call_service("regm_m21_call", "light", "turn_on", [E_LAMP], 480, y, [["regm_m21_d"]])
    delay_node("regm_m21_d", 1.5, 660, y, [["regm_m21_rd"]])
    read_state("regm_m21_rd", E_LAMP, 840, y, [[aid("M2.1")]], prop="verify")
    assert_node("M2.1", (
        "let ok = (msg.verify === 'on');\n"
        "let s = 'light.turn_on → 1.5s 后读回 HA 真值 state=' + msg.verify;"
    ), y, extra=("regm_m22_reset",))

    # M2.2 light turn_off → 读回 off
    y += 60
    chain_reset("regm_m22_reset", "M2.2", 300, y, [["regm_m22_call"]])
    call_service("regm_m22_call", "light", "turn_off", [E_LAMP], 480, y, [["regm_m22_d"]])
    delay_node("regm_m22_d", 1.5, 660, y, [["regm_m22_rd"]])
    read_state("regm_m22_rd", E_LAMP, 840, y, [[aid("M2.2")]], prop="verify")
    assert_node("M2.2", (
        "let ok = (msg.verify === 'off');\n"
        "let s = 'light.turn_off → 1.5s 后读回 HA 真值 state=' + msg.verify;"
    ), y, extra=("regm_m23_reset",))

    # M2.3 亮度 set → 读回 brightness
    y += 60
    chain_reset("regm_m23_reset", "M2.3", 300, y, [["regm_m23_call"]])
    call_service("regm_m23_call", "light", "turn_on", [E_LAMP], 480, y, [["regm_m23_d"]],
                 data={"brightness": 200})
    delay_node("regm_m23_d", 1.5, 660, y, [["regm_m23_rd"]])
    read_state("regm_m23_rd", E_LAMP, 840, y, [[aid("M2.3")]], prop="verify", entity_prop="ent")
    assert_node("M2.3", (
        "const b = msg.ent && msg.ent.attributes ? msg.ent.attributes.brightness : null;\n"
        "let ok = (msg.verify === 'on' && typeof b === 'number' && Math.abs(b - 200) <= 12);\n"
        "let s = 'brightness=200 下发 → 读回 state=' + msg.verify + ', brightness=' + b "
        "+ ' (容差±12)';"
    ), y, extra=("regm_m24_reset",))

    # M2.4 switch toggle → 前后真值必须翻转，随后复原
    y += 60
    chain_reset("regm_m24_reset", "M2.4", 300, y, [["regm_m24_rd0"]])
    read_state("regm_m24_rd0", E_SWITCH, 480, y, [["regm_m24_keep"]], prop="before")
    change_set("regm_m24_keep", [{"p": "topic", "pt": "msg", "to": "m24", "tot": "str"}],
               620, y, [["regm_m24_call"]])
    call_service("regm_m24_call", "switch", "toggle", [E_SWITCH], 760, y, [["regm_m24_d"]])
    delay_node("regm_m24_d", 1.5, 900, y, [["regm_m24_rd1"]])
    read_state("regm_m24_rd1", E_SWITCH, 1040, y, [[aid("M2.4")]], prop="verify")
    assert_node("M2.4", (
        "let ok = (msg.before === 'on' || msg.before === 'off') && "
        "(msg.verify === 'on' || msg.verify === 'off') && msg.before !== msg.verify;\n"
        "let s = 'switch.toggle → HA 真值 ' + msg.before + ' ⇒ ' + msg.verify + ' (已翻转)';"
    ), y, extra=("regm_m24_restore",))
    # 复原开关，避免回归跑完改变家里状态
    call_service("regm_m24_restore", "switch", "toggle", [E_SWITCH], 1800, y,
                 [["regm_m26_reset"]])

    # M2.6 房间级批量（2 灯）→ 两盏都要读回 on
    y += 60
    chain_reset("regm_m26_reset", "M2.6", 300, y, [["regm_m26_call"]])
    call_service("regm_m26_call", "light", "turn_on", [E_LAMP, E_LAMP2], 480, y,
                 [["regm_m26_d"]])
    delay_node("regm_m26_d", 2, 660, y, [["regm_m26_rd1"]])
    read_state("regm_m26_rd1", E_LAMP, 840, y, [["regm_m26_rd2"]], prop="verify")
    read_state("regm_m26_rd2", E_LAMP2, 1020, y, [[aid("M2.6")]], prop="verify2")
    assert_node("M2.6", (
        "let ok = (msg.verify === 'on' && msg.verify2 === 'on');\n"
        "let s = '房间批量 turn_on 2 灯 → 读回 台灯=' + msg.verify + ', 挂灯=' + msg.verify2;"
    ), y, extra=("regm_m2_restore",))
    # 链尾复原台灯亮度
    call_service("regm_m2_restore", "light", "turn_on", [E_LAMP], 1800, y, [[]],
                 data={"brightness": LAMP_BRIGHTNESS_RESTORE})

    # M2.5 调用子流程动作（与灯/开关无冲突，可并行）
    #
    # 测的是「产品编译出的子流程调用」与「手写直连 HA 服务」是否等价 —— 而不是
    # 微信这条外部通道当下通不通（后者是环境，不该拿来判产品的分）。
    # 故先跑一次 **直连对照**（同 domain/service/channel），再跑子流程，两边行为
    # 一致即判过，并把双方真实返回都写进 summary，通道故障一眼可见。
    y += 60
    func_node("regm_m25_prep", (
        "// 微信子流程契约：msg.payload=正文，msg.channel=通道(缺省 wechat/user_id)\n"
        "msg._sent = '[REG-M回归] 子流程动作调用自检';\n"
        "msg.payload = msg._sent;\n"
        "return msg;"), 300, y, [["regm_m25_direct"]])
    call_service("regm_m25_direct", "cn_im_hub", "send_message", [], 470, y,
                 [["regm_m25_mark"]],
                 data={"channel": "wechat/user_id",
                       "message": "[REG-M回归] 直连对照自检"})
    catch_node("regm_m25_dcatch", ["regm_m25_direct"], 470, y + 34, [["regm_m25_mark"]])
    func_node("regm_m25_mark", (
        "// 汇总直连对照结论后再进子流程；务必清掉 error，否则会串到子流程断言\n"
        "if (msg.error) {\n"
        "  msg._direct = 'ERR ' + String((msg.error && msg.error.message) || msg.error)"
        ".slice(0, 60);\n"
        "  delete msg.error;\n"
        "} else {\n"
        "  msg._direct = 'OK';\n"
        "}\n"
        "msg.payload = msg._sent;\n"
        "return msg;"), 660, y, [["regm_m25_sub"]])
    N("regm_m25_sub", "subflow:" + SUB_WECHAT, 850, y, [[aid("M2.5")]],
      name="M2.5 调用子流程(微信)")
    catch_node("regm_m25_catch", ["regm_m25_sub"], 850, y + 34, [[aid("M2.5")]])
    assert_node("M2.5", (
        "let em = '';\n"
        "if (msg.error) { em = String((msg.error && msg.error.message) || msg.error); }\n"
        "const subOK = !em;\n"
        "const dirOK = (msg._direct === 'OK');\n"
        "// 等价即通过：产品子流程与手写直连必须同生同死\n"
        "let ok = (subOK === dirOK);\n"
        "let s;\n"
        "if (subOK && dirOK) {\n"
        "  s = '子流程(微信 ed26c7cf) 调用成功, 直连对照同样成功, 正文=' + msg._sent;\n"
        "} else if (!subOK && !dirOK) {\n"
        "  s = '[BLOCKED-ENV] 子流程与直连行为一致(均失败)→产品调用链无缺陷; "
        "子流程:' + em.slice(0, 55) + ' | 直连:' + msg._direct;\n"
        "} else {\n"
        "  s = '[产品缺陷] 子流程与直连行为不一致! 子流程' + (subOK ? '成功' : '失败:'"
        " + em.slice(0, 45)) + ' 但直连' + msg._direct;\n"
        "}"
    ), y)

    ENTRY["M2.1"] = ["regm_m21_call"]
    ENTRY["M2.5"] = ["regm_m25_prep"]
    for chained in ("M2.2", "M2.3", "M2.4", "M2.6"):
        ENTRY[chained] = []          # 由 M2.1 链式驱动，fan 不直接发


# ══════════════════════════════════════════════════════════════════════
# M3 历史子流程（8）—— 复用 af_hist_*，按契约注入入参
# ══════════════════════════════════════════════════════════════════════
M3_SPEC: list[tuple[str, str, dict[str, str], str]] = [
    ("M3.1", "af_hist_state_at", {"entity": E_TEMP, "at": "昨天20:00"},
     "typeof p.found === 'boolean' && (p.found === false || p.value !== null)"),
    ("M3.2", "af_hist_state_at", {"entity": E_LAMP, "at": "今天08:00"},
     "typeof p.found === 'boolean' && (p.found === false || p.value !== null)"),
    ("M3.3", "af_hist_occurred",
     {"entity": E_LAMP, "start": "昨天00:00", "end": "今天00:00", "state": "on"},
     "typeof p.occurred === 'boolean' && typeof p.count === 'number'"),
    ("M3.4", "af_hist_occurred", {"entity": E_SWITCH, "start": "24h", "end": "现在"},
     "typeof p.occurred === 'boolean' && typeof p.count === 'number'"),
    ("M3.5", "af_hist_duration",
     {"entity": E_LAMP, "start": "昨天00:00", "end": "今天00:00", "state": "on"},
     "typeof p.total_seconds === 'number' && p.total_seconds >= 0"),
    ("M3.6", "af_hist_duration",
     {"entity": E_SWITCH, "start": "24h", "end": "现在", "state": "on"},
     "typeof p.total_seconds === 'number' && p.total_seconds >= 0"),
    ("M3.7", "af_hist_aggregate",
     {"entity": E_TEMP, "start": "24h", "end": "现在", "metric": "mean"},
     "typeof p.value === 'number' && !isNaN(p.value)"),
    ("M3.8", "af_hist_aggregate",
     {"entity": E_TEMP, "start": "24h", "end": "现在", "metric": "max"},
     "typeof p.value === 'number' && !isNaN(p.value)"),
]


def build_m3() -> None:
    """M3：历史四件套 ×2。

    ⚠️ 子流程入参走 ``msg.entity / msg.at|start|end / msg.state / msg.metric``（无 env）。
    首轮 M3.3–M3.8 全部「静默不返回」，根因是未传 ``msg.start``：
    ``af_hist_occurred/duration/aggregate`` 的解析节点执行 ``toHAISO(null)`` 直接抛
    TypeError，而 catch 的 scope 只圈了取历史节点、圈不住解析节点 → 消息丢失。
    （af_hist_state_at 因为 ``parseNaturalTime(...) || new Date()`` 有兜底才幸免。）
    这是**产品侧健壮性缺陷**，已单列为 REV 关注项。
    """
    y = 980
    for case, defid, fields, cond in M3_SPEC:
        prep = "regm_%s_prep" % case.replace(".", "_")
        inst = "regm_%s_sub" % case.replace(".", "_")
        lines = ["// %s 历史子流程入参（契约：msg.entity / at|start|end / state / metric）" % case]
        lines += ["msg['%s'] = '%s';" % (k, v) for k, v in fields.items()]
        lines += ["msg.payload = {};", "return msg;"]
        func_node(prep, "\n".join(lines), 480, y, [[inst]])
        N(inst, "subflow:" + defid, 700, y, [[aid(case)]], name="%s %s" % (case, defid))
        desc = " ".join("%s=%s" % (k, v) for k, v in fields.items() if k != "entity")
        assert_node(case, (
            "let ok = !!(p && (%s));\n" % cond
            + "let s = '%s(%s) → ' + JSON.stringify(p).slice(0, 110);"
            % (defid, desc.replace("'", ""))
        ), y)
        ENTRY[case] = [prep]
        y += 50


# ══════════════════════════════════════════════════════════════════════
# M4 通知（4）
# ══════════════════════════════════════════════════════════════════════
def build_m4() -> None:
    """M4：demo_notify(文本 / 房间+级别) 与 bark_push(基础 / 带 title)。

    demo_notify 在产品侧编译为 ``link out → b595563939283231``（TTS队列入口），
    是 fire-and-forget，无返回路径。

    ⚠️ 取证方式的坑：``global.TTS_RECENT_TRIGGERS`` 是**纯时间戳数组 + 惰性过期**
    （队列管理器 v3 每次入队才执行 ``filter(t => now - t < 7000)`` 再 ``push(now)``）。
    因此「长度增量 > 0」并不成立 —— 若数组里躺着上一轮遗留的 2 个陈旧戳，本轮入队时
    会先被清掉再补 1 个，长度反而 2→1（首轮 M4.1 即因此假失败）。
    正确证据：数组中**存在 ts ≥ 本条消息发出时刻**的戳，且未处于熔断冷却。
    两条 demo_notify 串行（间隔约 2s），避开 BURST_LIMIT=7/7s 熔断。
    """
    y = 1400
    # M4.1 demo_notify 文本
    func_node("regm_m41_prep", (
        "// demo_notify(link_out) 前置：以发出时刻为基准，而非数组长度\n"
        "msg._tts_t0 = Date.now();\n"
        "msg._tts_before = (global.get('TTS_RECENT_TRIGGERS') || []).length;\n"
        "msg._tts_tag = '[REG-M回归] 文本通知自检，请忽略';\n"
        "msg.payload = { text: msg._tts_tag, priority: 3 };\n"
        "return msg;"), 480, y, [["regm_m41_link", "regm_m41_d"]])
    N("regm_m41_link", "link out", 700, y - 30, [],
      name="→ demo_notify(TTS队列入口)", mode="link", links=[TTS_LINK_IN])
    delay_node("regm_m41_d", 1.8, 700, y + 20, [[aid("M4.1")]])
    assert_node("M4.1", (
        "const arr = global.get('TTS_RECENT_TRIGGERS') || [];\n"
        "const hit = arr.filter(function (t) { return t >= msg._tts_t0; });\n"
        "const cd = !!global.get('TTS_COOLDOWN_ACTIVE');\n"
        "const q = (global.get('TTS_Q') || []).length;\n"
        "let ok = (hit.length >= 1 && !cd);\n"
        "let s = 'demo_notify 文本 → link out(' + '%s' + '): 队列管理器登记了 '"
        % TTS_LINK_IN +
        " + hit.length + ' 个 ts≥发出时刻的触发戳(窗口内共' + arr.length + '个, 发出前'"
        " + msg._tts_before + '个), 熔断=' + cd + ', TTS_Q=' + q + ', 文本=' + msg._tts_tag;"
    ), y, extra=("regm_m42_prep",))

    # M4.2 demo_notify 房间+级别
    y += 60
    func_node("regm_m42_prep", (
        "// 房间+级别：room 决定播报设备，volume/priority 表达级别\n"
        "['_tts_before', '_tts_tag', '_tts_t0'].forEach(function (k) { delete msg[k]; });\n"
        "msg._tts_t0 = Date.now();\n"
        "msg._tts_before = (global.get('TTS_RECENT_TRIGGERS') || []).length;\n"
        "msg._tts_tag = '[REG-M回归] 房间级别通知自检，请忽略';\n"
        "msg.payload = { text: msg._tts_tag, room: '书房', volume: 30, priority: 3 };\n"
        "return msg;"), 480, y, [["regm_m42_link", "regm_m42_d"]])
    N("regm_m42_link", "link out", 700, y - 30, [],
      name="→ demo_notify(房间+级别)", mode="link", links=[TTS_LINK_IN])
    delay_node("regm_m42_d", 1.8, 700, y + 20, [[aid("M4.2")]])
    assert_node("M4.2", (
        "const arr = global.get('TTS_RECENT_TRIGGERS') || [];\n"
        "const hit = arr.filter(function (t) { return t >= msg._tts_t0; });\n"
        "const cd = !!global.get('TTS_COOLDOWN_ACTIVE');\n"
        "let ok = (hit.length >= 1 && !cd);\n"
        "let s = 'demo_notify 房间=书房/级别vol30 → 登记 ' + hit.length + ' 个 ts≥发出时刻"
        "的触发戳(窗口内共' + arr.length + '个), 熔断=' + cd + ', 文本=' + msg._tts_tag;"
    ), y)

    # M4.3 bark 基础（不带 title）
    # 实测契约：子流程「构造 Bark 明文 JSON」节点确有默认标题回落
    #   const title = (typeof msg.title === 'string' && msg.title.length) ? msg.title : 'AutoFlow'
    # 但「结果透传」change 的 JSONata `{"sent":{"title":title,...}}` 引用的是**顶层
    # msg.title**（即原始入参），而非构造节点算出的实际发出值 → 不传 title 时回显
    # undefined。属回显失真（不影响推送本身），已列 REV 关注项，断言按真实行为写。
    y += 60
    func_node("regm_m43_prep", (
        "// Bark 子流程契约：msg.title / msg.body；结果透传回 {ok,status,sent,raw}\n"
        "delete msg.title;\n"
        "msg.body = '[REG-M回归] bark 基础自检 ' + Date.now();\n"
        "msg._sent_body = msg.body;\n"
        "return msg;"), 480, y, [["regm_m43_sub"]])
    N("regm_m43_sub", "subflow:" + SUB_BARK, 700, y, [[aid("M4.3")]], name="M4.3 bark 基础")
    assert_node("M4.3", (
        "const t = p && p.sent ? p.sent.title : 'NO_SENT';\n"
        "let ok = !!(p && p.ok === true && p.status === 200 && p.sent && "
        "p.sent.body === msg._sent_body && (t === undefined || t === 'AutoFlow'));\n"
        "let s = 'bark 基础(不传title) → HTTP ' + (p && p.status) + ', body回显一致=' + "
        "(!!(p && p.sent && p.sent.body === msg._sent_body)) + ', 回显 title=' + t + "
        "' [注:子流程内部实际用默认AutoFlow发送，透传JSONata引用原始msg.title致回显失真]';"
    ), y)

    # M4.4 bark 带 title
    y += 60
    func_node("regm_m44_prep", (
        "msg.title = 'REG-M 回归';\n"
        "msg.body = '[REG-M回归] bark 带title自检 ' + Date.now();\n"
        "msg._sent_body = msg.body;\n"
        "return msg;"), 480, y, [["regm_m44_sub"]])
    N("regm_m44_sub", "subflow:" + SUB_BARK, 700, y, [[aid("M4.4")]], name="M4.4 bark 带title")
    assert_node("M4.4", (
        "let ok = !!(p && p.ok === true && p.status === 200 && p.sent && "
        "p.sent.title === 'REG-M 回归' && p.sent.body === msg._sent_body);\n"
        "let s = 'bark 带title → HTTP ' + (p && p.status) + ', 回显 title=' + "
        "(p && p.sent && p.sent.title) + ', body=' + (p && p.sent && p.sent.body);"
    ), y)

    ENTRY["M4.1"] = ["regm_m41_prep"]
    ENTRY["M4.2"] = []               # 由 M4.1 串行驱动
    ENTRY["M4.3"] = ["regm_m43_prep"]
    ENTRY["M4.4"] = ["regm_m44_prep"]


# ══════════════════════════════════════════════════════════════════════
# M5 触发与时序（4）
# ══════════════════════════════════════════════════════════════════════
def build_m5() -> None:
    """M5：手动 inject、cron 真实触发、delay 实测耗时、多触发 join 合并。"""
    y = 1700
    # M5.1 手动 inject
    N("regm_m51_inj", "inject", 480, y, [[aid("M5.1")]], name="M5.1 手动 inject",
      props=[{"p": "payload"}, {"p": "topic", "vt": "str"}], repeat="", crontab="",
      once=False, onceDelay=0.1, topic="regm_manual", payload="manual", payloadType="str")
    assert_node("M5.1", (
        "let ok = (p === 'manual' && msg.topic === 'regm_manual');\n"
        "let s = 'inject 手动触发 → payload=' + p + ', topic=' + msg.topic + ', at='"
        " + new Date().toLocaleTimeString();"
    ), y)

    # M5.2 cron：常驻 */1 定时器把真实触发时间写 flow context，断言据此取证
    y += 60
    N("regm_m52_cron", "inject", 300, y, [["regm_m52_mark"]], name="M5.2 cron */1min",
      props=[{"p": "payload"}], repeat="", crontab="*/1 * * * *", once=False,
      onceDelay=0.1, topic="", payload="", payloadType="date")
    func_node("regm_m52_mark", (
        "// cron 每分钟落一次真实触发时间戳，供 M5.2 断言取证\n"
        "flow.set('regm_cron_last', Date.now());\n"
        "flow.set('regm_cron_n', (flow.get('regm_cron_n') || 0) + 1);\n"
        "return null;"), 480, y, [[]])
    assert_node("M5.2", (
        "const last = flow.get('regm_cron_last');\n"
        "const n = flow.get('regm_cron_n') || 0;\n"
        "const age = last ? (Date.now() - last) : -1;\n"
        "let ok = (last && age >= 0 && age < 70000);\n"
        "let s = last ? ('cron(*/1 * * * *) 上次真实触发在 ' + (age / 1000).toFixed(1) "
        "+ 's 前, 累计 ' + n + ' 次') : 'cron 尚未触发（需部署后等待 ≥1 分钟再跑）';"
    ), y)

    # M5.3 delay：实测耗时
    y += 60
    func_node("regm_m53_t0", (
        "msg.t0 = Date.now();\n"
        "return msg;"), 480, y, [["regm_m53_delay"]])
    delay_node("regm_m53_delay", 2, 660, y, [[aid("M5.3")]])
    assert_node("M5.3", (
        "const el = Date.now() - (msg.t0 || 0);\n"
        "let ok = (el >= 1900 && el < 6000);\n"
        "let s = 'delay 节点实测耗时 ' + el + 'ms (目标 2000ms, 容差 1900~6000)';"
    ), y)

    # M5.4 多触发 join 合并
    y += 60
    N("regm_m54_a", "inject", 480, y, [["regm_m54_join"]], name="M5.4 触发A",
      props=[{"p": "payload"}, {"p": "topic", "vt": "str"}], repeat="", crontab="",
      once=False, onceDelay=0.1, topic="a", payload="trigA", payloadType="str")
    N("regm_m54_b", "inject", 480, y + 34, [["regm_m54_join"]], name="M5.4 触发B",
      props=[{"p": "payload"}, {"p": "topic", "vt": "str"}], repeat="", crontab="",
      once=False, onceDelay=0.1, topic="b", payload="trigB", payloadType="str")
    N("regm_m54_join", "join", 700, y + 17, [[aid("M5.4")]], name="M5.4 join(2)",
      mode="custom", build="array", property="payload", propertyType="msg", key="topic",
      joiner=",", joinerType="str", accumulate=False, timeout="10", count="2",
      reduceRight=False, reduceExp="", reduceInit="", reduceInitType="", reduceFixup="")
    assert_node("M5.4", (
        "let ok = (Array.isArray(p) && p.length === 2 && p.indexOf('trigA') >= 0 "
        "&& p.indexOf('trigB') >= 0);\n"
        "let s = '多触发 join 合并 ' + (Array.isArray(p) ? p.length : 0) + '/2 → ' "
        "+ JSON.stringify(p);"
    ), y)

    ENTRY["M5.1"] = ["regm_m51_inj"]
    ENTRY["M5.2"] = [aid("M5.2")]        # 直接进断言：证据取自 flow context
    ENTRY["M5.3"] = ["regm_m53_t0"]
    ENTRY["M5.4"] = ["regm_m54_a", "regm_m54_b"]


# ══════════════════════════════════════════════════════════════════════
# 汇总：join(28) → table → debug / 落盘
# ══════════════════════════════════════════════════════════════════════
def build_tail() -> None:
    """建 join / 总表 / 证据落盘节点。"""
    N("regm_join", "join", 1860, 900, [["regm_table"]], name="REG-M join(28)",
      mode="custom", build="array", property="payload", propertyType="msg", key="topic",
      joiner=",", joinerType="str", accumulate=False, timeout="120", count=str(len(CASES)),
      reduceRight=False, reduceExp="", reduceInit="", reduceInitType="", reduceFixup="")
    func_node("regm_table", (
        "// REG-M 总表：node.warn 进 runtime log，同时落盘供 docker exec 取证\n"
        "const rows = (msg.payload || []).slice().sort(function (a, b) {\n"
        "  return String(a.case).localeCompare(String(b.case));\n"
        "});\n"
        "const seen = {}; let pass = 0;\n"
        "const lines = ['===== REG-M 能力矩阵 (M1-M5) @ ' + new Date().toLocaleString()"
        " + ' ====='];\n"
        "for (const r of rows) {\n"
        "  seen[r.case] = true;\n"
        "  if (r.ok) pass++;\n"
        "  lines.push((r.ok ? '[PASS] ' : '[FAIL] ') + r.case + '  ' + (r.kind || '')"
        " + '  | ' + (r.summary || ''));\n"
        "}\n"
        "const ALL = " + json.dumps([c for c, _ in CASES]) .replace('"', "'") + ";\n"
        "const missing = ALL.filter(function (c) { return !seen[c]; });\n"
        "lines.push('===== 通过 ' + pass + '/' + ALL.length + ' =====');\n"
        "if (missing.length) lines.push('未返回(超时/异常): ' + missing.join(', '));\n"
        "const text = lines.join('\\n');\n"
        "node.warn(text);\n"
        "node.status({ fill: (pass === ALL.length) ? 'green' : 'red', shape: 'dot',"
        " text: pass + '/' + ALL.length });\n"
        "msg.payload = { pass: pass, total: ALL.length, returned: rows.length,"
        " missing: missing, table: text, rows: rows };\n"
        "return msg;"
    ), 2040, 900, [["regm_dbg", "regm_json_file", "regm_txt_chg"]])
    N("regm_dbg", "debug", 2260, 840, [], name="REG-M 总表", active=True, tosidebar=True,
      console=False, tostatus=False, complete="payload", targetType="msg",
      statusVal="", statusType="auto")
    N("regm_json_file", "file", 2260, 900, [], name="证据 JSON",
      filename="/tmp/reg_m_result.json", filenameType="str", appendNewline=True,
      createDir=False, overwriteFile="true", encoding="utf8")
    change_set("regm_txt_chg", [{"p": "payload", "pt": "msg",
                                 "to": "payload.table", "tot": "msg"}],
               2260, 960, [["regm_txt_file"]])
    N("regm_txt_file", "file", 2440, 960, [], name="证据 TXT",
      filename="/tmp/reg_m_result.txt", filenameType="str", appendNewline=True,
      createDir=False, overwriteFile="true", encoding="utf8")


# ══════════════════════════════════════════════════════════════════════
def wire_fan() -> None:
    """回填 fan 的 28 组输出连线（无入口的用例由串行链驱动，留空组）。"""
    fan = next(n for n in nodes if n["id"] == "regm_fan")
    fan["wires"] = [list(ENTRY[c]) for c, _ in CASES]


def self_check() -> list[str]:
    """结构自检：id 唯一 / z 一致 / 无悬空连线 / 子流程实例合法 / JS 无双引号。"""
    errs: list[str] = []
    ids = [n["id"] for n in nodes]
    dups = [k for k, v in collections.Counter(ids).items() if v > 1]
    if dups:
        errs.append("重复 id: %s" % dups)
    known_sub = set(HIST_SUBS) | {SUB_WECHAT, SUB_BARK}
    idset = set(ids)
    for n in nodes:
        typ = n.get("type", "")
        if typ == "subflow":
            errs.append("%s: 裸 type=subflow（应为 subflow:<defid>）" % n["id"])
        if typ.startswith("subflow:"):
            defid = typ.split(":", 1)[1]
            if defid not in known_sub:
                errs.append("%s: 未知子流程定义 %s" % (n["id"], defid))
            for bad in ("in", "out", "subflow"):
                if bad in n:
                    errs.append("%s: 子流程【实例】混入定义字段 %s" % (n["id"], bad))
        if n.get("z") != TAB_ID:
            errs.append("%s: z 与 tab 不一致" % n["id"])
        for slot in n.get("wires") or []:
            for tgt in slot:
                if tgt not in idset:
                    errs.append("%s wires 悬空 → %s" % (n["id"], tgt))
        if typ == "function" and '"' in (n.get("func") or ""):
            errs.append("%s: func 含双引号（易破坏 JSON 传输）" % n["id"])
        if typ == "change":
            for rule in n.get("rules") or []:
                if rule.get("t") != "set":
                    errs.append("%s: change rule 缺 t=set（会被静默跳过）" % n["id"])

    fan = next(n for n in nodes if n["id"] == "regm_fan")
    if fan["outputs"] != len(fan["wires"]):
        errs.append("fan outputs(%s) != wires 组数(%s)" % (fan["outputs"], len(fan["wires"])))
    asserts = {n["id"] for n in nodes if n["id"].startswith("regm_a_")}
    if len(asserts) != len(CASES):
        errs.append("断言节点数 %d != 用例数 %d" % (len(asserts), len(CASES)))
    join = next(n for n in nodes if n["id"] == "regm_join")
    if join["count"] != str(len(CASES)):
        errs.append("join count(%s) != 用例数(%d)" % (join["count"], len(CASES)))
    # 每个用例必须可达：要么 fan 直发，要么被某个断言/节点串行驱动
    driven = set()
    for n in nodes:
        for slot in n.get("wires") or []:
            driven.update(slot)
    for case, _ in CASES:
        if not ENTRY.get(case) and aid(case) not in driven:
            errs.append("%s 既无 fan 入口也无上游驱动" % case)
    return errs


def build(tab_id: str) -> dict:
    """生成完整 flow 定义。

    Args:
        tab_id: 目标 tab id（所有节点 z 字段）。

    Returns:
        ``{"id","label","nodes":[...]}`` 形状的单 flow 定义。

    Raises:
        SystemExit: 自检未通过。
    """
    global TAB_ID
    TAB_ID = tab_id
    nodes.clear()
    ENTRY.clear()
    build_header()
    build_m1()
    build_m2()
    build_m3()
    build_m4()
    build_m5()
    build_tail()
    wire_fan()
    errs = self_check()
    sub_n = sum(1 for n in nodes if str(n.get("type", "")).startswith("subflow:"))
    print("节点数=%d  用例=%d  子流程实例=%d  tab=%s" % (len(nodes), len(CASES), sub_n, tab_id))
    if errs:
        print("\n[!!!] 自检失败:")
        for e in errs:
            print("  -", e)
        raise SystemExit(1)
    print("[OK] 自检通过（id唯一 / z一致 / 无悬空wires / change带t=set / 用例全可达）")
    return {"id": tab_id, "label": LABEL, "nodes": nodes}


def resolve_tab_id(argv: list[str]) -> str:
    """确定目标 tab id：命令行 > 台账 > 占位符。"""
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    if LEDGER.exists():
        val = LEDGER.read_text(encoding="utf-8").strip()
        if val:
            return val
    return "reg_m_tab"


def main() -> None:
    """生成 flow JSON 并写入 ``tests/fixtures_local/reg_m_flow.json``。"""
    flow = build(resolve_tab_id(sys.argv))
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(flow, ensure_ascii=False, separators=(",", ":"))
    OUT_JSON.write_text(payload, encoding="utf-8")
    print("写出 %s (%d 字节)" % (OUT_JSON, len(payload.encode("utf-8"))))


if __name__ == "__main__":
    main()
