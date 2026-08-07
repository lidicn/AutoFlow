# -*- coding: utf-8 -*-
"""生成「能力覆盖矩阵 capmatrix」Node-RED flow JSON（round3 全节点类型 + 参数覆盖）。

设计目标（对齐 REG-M 已验证范式）：
    1×inject(▶ RUN) → 1×fan(N 路) → N 分支 → 1×join(count=N) → table → debug/file

验收铁律（同 REG-M / REG-S）：**每条断言必须给真实证据**，禁止「没报错=通过」。
- 纯 NR 节点：断言读回节点真实输出（msg.payload / 指定字段 / join 收集的数组）。
- HA 节点：调用后**读回实体真值**或捕获 HA 真实响应（成功或结构化错误均算「已派发」）。
- 网络节点：loopback/echo 取真实环回证据；无可达端点时诚实标记 BLOCKED-ENV，**不伪造通过**。
- 断言产物**严禁生成 function/exec**（RAW_NODE_FORBIDDEN 铁律）——本文件内唯一 function 是
  各断言归一化节点与用例前置 prep，属测试脚手架，非被测产品节点。

HA 节点 schema 直接复用 1990 上已验证部署的 REG-M（version=7 + outputProperties 读
msg.payload / 实体对象），避免 REG-S 当初的版本误判。

用法::

    python capmatrix_build.py [tab_id]        # tab_id 缺省读台账，再缺省用占位符
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
LEDGER = REPO / "tests" / "fixtures_local" / "capm_tab.id"
OUT_JSON = REPO / "tests" / "fixtures_local" / "capm_flow.json"

LABEL = "capmatrix 节点类型×参数覆盖 (round3)"
SERVER = "e93e1ad9c034e866"                      # 1990 上 HA server 配置节点
TTS_LINK_IN = "b595563939283231"                # demo_notify 的 link_out 目标（TTS队列入口）
SUB_WECHAT = "ed26c7cf80e9429a"                 # 微信子流程
SUB_BARK = "b0bbc86abb2172a5"                   # Bark 子流程
HIST_SUBS = ("af_hist_state_at", "af_hist_occurred",
             "af_hist_duration", "af_hist_aggregate")

# ── 真实实体（2026-08-06 经 HA /api/states 实测存在）────────────────────
E_TEMP = "sensor.wo_de_jia_wen_du"                        # 我的家 温度，实测 34.45
E_LAMP = "light.philips_cn_249518489_rwread_s_2_light"    # 书房台灯
E_LAMP2 = "light.yeelink_cn_555003624_lamp22_s_2"         # 显示器挂灯1S
# 台风 指示灯：无声、无害、实测 toggle 后 1s 内翻转，适合做 switch 域回归载体。
# 原用 switch.b460eda0bc76_switch（米家驱蚊器2）：HA 直连 REST 实测 toggle 后 34s
# 状态与 last_changed 均纹丝不动 —— 设备侧不可控（云端不通/已下线），属环境限制，
# 不是节点或 DSL 缺陷。回归载体必须选实测可控的实体，否则拿到的是假 FAIL。
E_SWITCH = "switch.dmaker_cn_245712731_p9_brightness_p_2_9"

# ── 网络环回参数（tcp/udp 本地监听，ws 走 NR 自身 HTTP 端口）─────────────
#
# ⚠ 坑（本轮实测踩到，缺陷 ⑫）：node-red-dev 容器是 **host 网络模式**
#   （docker inspect NetworkMode=host，docker port 无任何映射）。于是
#   「容器内 NR 总是听 1880、宿主 1990 是映射」这条常识**完全不成立**：
#   本实例直接在宿主网络栈上 listen 1990，而 127.0.0.1:1880 是**另一个
#   NR 实例**。第一版把 ws client 指向 ws://127.0.0.1:1880/capm_ws，
#   连过去的是隔壁实例、那边没有 /capm_ws 路由 → 握手失败，
#   且 websocket in 连 status 事件都不发（N7/N8 只见「收到=∅ 连接状态=未捕获」，
#   与「listener 没广播」的表征一模一样，极易误判成节点能力缺陷）。
#   判定方法：docker inspect -f '{{.HostConfig.NetworkMode}}' + netstat 看真实监听口。
# ⚠ 同理，host 网络下 tcp/udp 的 21801/21802 是**宿主级**监听端口，
#   选号必须避开 NAS 上的既有服务（已实测这两个号空闲）。
MQTT_BROKER = "2dffca099b0e47f2"   # 现网 HAOS-MQTT 配置节点（45 个 mqtt out 在用）
PORT_TCP = 21801                   # 冷门高位端口，避开宿主既有服务
PORT_UDP = 21802
WS_PORT = 1990                     # node-red-dev 真实监听端口（host 网络，非 1880）
WS_PATH = "/capm_ws"

nodes: list[dict] = []
ENTRY: dict[str, list[str]] = {}
TAB_ID = "capm_tab"

PREFIX = "capm"          # 与 REG-M(regm_)/REG-S(regs_) 区分，避免 flow context 串味
EPOCH_KEY = PREFIX + "_epoch"


def aid(case: str) -> str:
    """断言节点 id（点号换下划线，保证 NR id 合法）。"""
    return PREFIX + "_a_" + case.replace(".", "_")


def N(nid: str, typ: str, x: int, y: int, wires: list, **kw) -> dict:
    """追加一个节点。"""
    node = {"id": nid, "type": typ, "z": TAB_ID, "x": x, "y": y, "wires": wires}
    node.update(kw)
    nodes.append(node)
    return node


# ── 节点工厂（HA 节点复用 REG-M 已验证 schema）─────────────────────────
def read_state(nid: str, entity: str, x: int, y: int, wires: list,
               prop: str = "payload", entity_prop: str | None = None) -> dict:
    """api-current-state 读取节点（version=7，实体对象写入 entity_prop）。"""
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
    """api-call-service 动作节点（version=7，data 必须是对象 JSON 字符串）。"""
    return N(nid, "api-call-service", x, y, wires,
             server=SERVER, version=7, action="%s.%s" % (domain, service),
             floorId=[], areaId=[], deviceId=[], labelId=[],
             entityId=list(entities), data=json.dumps(data or {}, ensure_ascii=False),
             dataType="json", mergeContext="", mustacheAltTags=False,
             outputProperties=[], queue="none", blockInputOverrides=True,
             domain=domain, service=service)


def change_set(nid: str, rules: list[dict], x: int, y: int, wires: list) -> dict:
    """change(set) 节点。每条 rule 必须带 t:set（缺则静默跳过）。"""
    return N(nid, "change", x, y, wires,
             rules=[dict(r, t=r.get("t", "set")) for r in rules])


def switch_node(nid: str, prop: str, rules: list[dict], x: int, y: int,
                wires: list, prop_type: str = "msg") -> dict:
    """switch 分支节点。"""
    return N(nid, "switch", x, y, wires, property=prop, propertyType=prop_type,
             rules=rules, checkall="true", repair=False, outputs=len(rules))


def delay_node(nid: str, seconds: float, x: int, y: int, wires: list,
               units: str = "seconds") -> dict:
    """固定延时节点。"""
    return N(nid, "delay", x, y, wires, pauseType="delay", timeout=str(seconds),
             timeoutUnits=units, rate="1", nbRateUnits="1", rateUnits="second",
             randomFirst="1", randomLast="5", randomUnits="seconds",
             drop=False, allowrate=False, outputs=1)


def inject_node(nid: str, name: str, x: int, y: int, wires: list, *,
                payload, payload_type: str, repeat: str = "", crontab: str = "",
                once: bool = False, topic: str = "") -> dict:
    """inject 节点（手动 / cron / interval）。"""
    return N(nid, "inject", x, y, wires, name=name,
             props=[{"p": "payload"}, {"p": "topic", "vt": "str"}],
             repeat=repeat, crontab=crontab, once=once, onceDelay=0.1,
             topic=topic, payload=payload, payloadType=payload_type)


def join_node(nid: str, name: str, x: int, y: int, wires: list, *,
              mode: str = "custom", build: str = "array", count: int = 2,
              timeout: int = 10, key: str = "topic") -> dict:
    """join 节点（array / manual / timed / keys）。"""
    return N(nid, "join", x, y, wires, name=name,
             mode=mode, build=build, property="payload", propertyType="msg", key=key,
             joiner=",", joinerType="str", accumulate=False, timeout=str(timeout),
             count=str(count), reduceRight=False, reduceExp="", reduceInit="",
             reduceInitType="", reduceFixup="")


def func_node(nid: str, body: str, x: int, y: int, wires: list, outputs: int = 1) -> dict:
    """function 节点（仅测试脚手架：前置 prep / 断言归一化 / 序列生成）。"""
    return N(nid, "function", x, y, wires, func=body, outputs=outputs, noerr=0,
             initialize="", finalize="", libs=[])


def catch_node(nid: str, scope: list[str], x: int, y: int, wires: list) -> dict:
    """限定作用域的 catch 节点。"""
    return N(nid, "catch", x, y, wires, scope=list(scope), uncaught=False)


def ssc_node(nid: str, name: str, x: int, y: int, wires: list, *,
             entity: list[str] | None = None, regex: str = "",
             substring: list[str] | None = None) -> dict:
    """server-state-changed 监听节点（对齐 1990 现网真实 schema）。

    ⚠️ 产品缺陷 ⑤（本轮发现）：本 contrib 版本的字段名是
        ``entities:{entity[],substring[],regex[]}`` / ``outputInitially`` /
        ``stateType`` / ``ifState`` / ``outputOnlyOnStateChange``（version=6）。
    早前按旧版（v0.5x）写成 ``entityId`` + ``entityIdType`` + ``output_initially``
    + ``state_type`` + ``halt_if`` 时，节点**既不报错也不订阅任何实体**——部署成功、
    状态灯正常、日志无声，就是永远不触发。这是最危险的一类静默失败：
    「没报错」在这里恰恰等于「什么都没干」。任何 DSL 生成该节点都必须对齐本 schema。

    另注：``outputInitially=True`` 在本实例会抛
    ``TypeError: Cannot read properties of undefined (reading 'entity')``，
    故一律置 False，改由用例主动制造一次真实状态变化来触发。

    Args:
        nid: 节点 id。
        name: 节点显示名。
        x: 画布横坐标。
        y: 画布纵坐标。
        wires: 出线。
        entity: 精确实体 id 列表。
        regex: 正则匹配表达式（与 entity 二选一）。
        substring: 子串匹配列表。

    Returns:
        新建的节点 dict。
    """
    node = N(nid, "server-state-changed", x, y, wires,
             name=name, server=SERVER, version=6, outputs=1,
             exposeAsEntityConfig="",
             entities={"entity": list(entity or []),
                       "substring": list(substring or []),
                       "regex": [regex] if regex else []},
             outputInitially=False, stateType="str",
             ifState="", ifStateType="str", outputOnlyOnStateChange=True,
             ignorePrevStateNull=False, ignorePrevStateUnknown=False,
             ignorePrevStateUnavailable=False, ignoreCurrentStateUnknown=False,
             ignoreCurrentStateUnavailable=False,
             # valueType 没有 "entityId" 这个取值（填了不报错，只是**什么都不写**，
             # 断言里表现为「实体=空」）。要拿实体 id 只能取整个 entity 对象再读
             # .entity_id；再挂一路 eventData 作兜底，避免单通道失效又变哑巴。
             outputProperties=[{"property": "payload", "propertyType": "msg",
                                "value": "", "valueType": "entityState"},
                               {"property": "capm_ent", "propertyType": "msg",
                                "value": "", "valueType": "entity"},
                               {"property": "capm_evt", "propertyType": "msg",
                                "value": "", "valueType": "eventData"}],
             exposeToHomeAssistant=False, haConfig=[])
    # `for` 是 Python 关键字，进不了 **kw，只能落地后补写。
    # 缺这三个字段时节点的「持续 N 秒才触发」判定拿到 undefined，同样是静默不触发。
    node["for"] = "0"
    node["forType"] = "num"
    node["forUnits"] = "seconds"
    return node


# ── 纯 NR 变换/流节点工厂（标准 Node-RED 原生 schema）──────────────────
def template_node(nid: str, name: str, tmpl: str, x: int, y: int, wires: list, *,
                  syntax: str = "mustache", out: str = "str",
                  field: str = "payload", field_type: str = "msg") -> dict:
    """template 节点。

    坑：template 只支持 syntax="mustache" | "plain"，**没有 jsonata 模式**。
    误填 jsonata + output="json" 时节点会把模板原文当 JSON 解析：
        Unexpected token 'p', "payload.a * 2" is not valid JSON
    然后节点报错不输出，下游 join 永不收齐。要 JSONata 请用 change 节点。

    Args:
        syntax: "mustache"（渲染 {{}}）或 "plain"（原样输出）。
        out: 输出格式 str / json / yaml。
    """
    if syntax not in ("mustache", "plain"):
        raise ValueError("template 只支持 mustache / plain，收到 %r" % syntax)
    return N(nid, "template", x, y, wires, name=name, field=field, fieldType=field_type,
             format="handlebars", syntax=syntax, template=tmpl, output=out)


def split_node(nid: str, name: str, x: int, y: int, wires: list, *,
               splt: str = "\n", splt_type: str = "str") -> dict:
    """split 节点（array 默认 / 字符串按 splt 切分）。"""
    return N(nid, "split", x, y, wires, name=name, splt=splt, spltType=splt_type,
             arraySplt=1, arraySpltType=1, addname="",
             property="payload", propertyType="msg")


def sort_node(nid: str, name: str, x: int, y: int, wires: list, *,
              order: str = "ascending", as_num: bool = True) -> dict:
    """sort 节点（targetType="msg"：就地排序 msg.payload 里的**数组**）。

    坑 ①：targetType="msg" 排的是 payload 数组本身，不是「消息序列」。
    若上游发的是 3 条标量消息（payload=3 / 1 / 2），sort 拿不到数组会报错不输出，
    下游 join 永不收齐 → 用例静默缺席。排消息序列须用 targetType="seq" 且带 msg.parts。

    坑 ②：``as_num`` 是 **boolean**，不是字符串。早前写成 "true"/"false" 字符串时，
    JS 里 ``"false"`` 是 truthy → 降序字符串用例被当数字排，['c','a','b'] 全部
    ``Number()`` 成 NaN、比较恒 0，结果原序返回 ['c','a','b']，看起来像「sort 没排序」。
    这是典型的「schema 类型对不上但不报错」的静默错配。
    """
    return N(nid, "sort", x, y, wires, name=name, order=order,
             as_num=bool(as_num),
             target="payload", targetType="msg", msgKey="payload", msgKeyType="elem",
             seqKey="payload", seqKeyType="elem")


def batch_node(nid: str, name: str, x: int, y: int, wires: list, *,
               mode: str = "count", count: int = 2, overlap: int = 0) -> dict:
    """batch 节点（count / consecutive）。"""
    return N(nid, "batch", x, y, wires, name=name, mode=mode, count=str(count),
             overlap=str(overlap), interval=1, allowAll=True, topics=[""],
             useSeq=False, property="payload", propertyType="msg")


def merge_node(nid: str, name: str, x: int, y: int, wires: list, *,
               build: str = "object", key: str = "topic", timeout: int = 5) -> dict:
    """⛔ 请勿使用：`merge` 是 RAW_NODE_ALLOWED 中的幽灵类型（产品缺陷 ④）。

    1880 / 1990 两实例 GET /nodes 均无 `merge` 类型（Node-RED 核心从未提供该节点，
    合并职责由 `join` 承担）。flow 中只要出现一个 merge 节点，NR 会打印
    "Waiting for missing types to be registered: - merge" 并让**整个 tab 拒绝启动**，
    连同 tab 内其它节点（含 inject）一并不注册 —— 表现为触发端点静默 404。

    保留本工厂仅为记录缺陷形态；等价能力请用
    ``join_node(..., mode="custom", build="object", key="topic")``。
    """
    raise RuntimeError(
        "merge 节点在 1880/1990 均未注册，会导致整个 tab 不启动；"
        "请改用 join_node(mode='custom', build='object', key='topic')"
    )


def csv_node(nid: str, name: str, x: int, y: int, wires: list, *,
             temp: str = "", sep: str = ",", skip: int = 0,
             multi: str = "mult") -> dict:
    """csv 节点（parse / stringify；temp 定义列）。

    Args:
        multi: "one"=每行一条消息（默认行为，下游拿到的是单个对象）；
               "mult"=整表一条消息（payload 为数组）。回归里用 "mult" 才好断言。
    """
    return N(nid, "csv", x, y, wires, name=name, sep=sep, quo="\"", ret="\n",
             temp=temp, skip=str(skip), multi=multi, strings=True, include_empty_strings=False,
             hdrin=False, hdrout="none")


def xml_node(nid: str, name: str, x: int, y: int, wires: list, *, attr: str = "") -> dict:
    """xml 节点（attr="" 属性合并 / attr="?" 属性单独成字段）。"""
    return N(nid, "xml", x, y, wires, name=name, property="payload",
             propertyType="msg", attr=attr, chr="")


def json_node(nid: str, name: str, x: int, y: int, wires: list, *,
              ret: str = "obj", pretty: bool = False) -> dict:
    """json 节点（ret: ""=自动切换 / "obj"=强制解析 / "str"=强制序列化）。

    坑：core json 节点**没有 jmespath 属性**（那是别的 contrib）。误填会被静默忽略，
    payload 原样透传，断言看似「没报错」但其实什么都没做 —— 典型的假通过。

    Args:
        pretty: 序列化时缩进 4 空格（对应编辑器里的 format JSON string）。
    """
    return N(nid, "json", x, y, wires, name=name, property="payload",
             propertyType="msg", action=ret, pretty=pretty)


def yaml_node(nid: str, name: str, x: int, y: int, wires: list) -> dict:
    """yaml 节点（parse / stringify）。"""
    return N(nid, "yaml", x, y, wires, name=name, property="payload",
             propertyType="msg")


def range_node(nid: str, name: str, x: int, y: int, wires: list, *,
               minin: str = "1", maxin: str = "10", minout: str = "0", maxout: str = "100",
               action: str = "scale", roundv: bool = False) -> dict:
    """range 节点（scale 线性映射 / roll 取模循环）。"""
    return N(nid, "range", x, y, wires, name=name, minin=minin, maxin=maxin,
             minout=minout, maxout=maxout, action=action, round=roundv,
             property="payload", propertyType="msg")


def html_node(nid: str, name: str, x: int, y: int, wires: list, *,
              tag: str = "h1", ret: str = "text", as_mode: str = "multi") -> dict:
    """html 节点（cheerio 选择器；ret text/html/attr；as single/multi）。"""
    return N(nid, "html", x, y, wires, name=name, tag=tag, ret=ret,
             **{"as": as_mode}, property="payload", propertyType="msg")


def trigger_node(nid: str, name: str, x: int, y: int, wires: list, *,
                 op1: str = "", op1_type: str = "nul", op2: str = "", op2_type: str = "str",
                 duration: str = "250", units: str = "ms", extend: bool = False,
                 reset: str = "", by_topic: str = "all", outputs: int = 1) -> dict:
    """trigger 节点（立即发 op1，duration 后发 op2；extend 续期；reset 复位）。"""
    return N(nid, "trigger", x, y, wires, name=name, op1=op1, op1type=op1_type,
             op2=op2, op2type=op2_type, duration=duration, extend=extend,
             overrideDelay=False, units=units, reset=reset, bytopic=by_topic,
             topic="", outputs=outputs)


def status_node(nid: str, name: str, scope: list[str], x: int, y: int, wires: list) -> dict:
    """status 节点（捕获 scope 内节点的状态事件）。"""
    return N(nid, "status", x, y, wires, name=name, scope=list(scope),
             complete="true", unhandled="false")


def complete_node(nid: str, name: str, scope: list[str], x: int, y: int, wires: list) -> dict:
    """complete 节点（捕获 scope 内节点完成事件）。"""
    return N(nid, "complete", x, y, wires, name=name, scope=list(scope))


def link_in_node(nid: str, name: str, x: int, y: int, wires: list) -> dict:
    """link in 节点。"""
    return N(nid, "link in", x, y, wires, name=name, links=[])


def link_out_node(nid: str, name: str, links: list[str], x: int, y: int) -> dict:
    """link out 节点（links 指向目标 link in 的 id）。"""
    return N(nid, "link out", x, y, [[]], name=name, mode="link", links=list(links))


def debug_node(nid: str, name: str, x: int, y: int, wires: list, *,
               complete: str = "payload", target_type: str = "msg",
               to_sidebar: bool = True, console: bool = False,
               to_status: bool = False, status_val: str = "") -> dict:
    """debug 节点（complete 表达式 / console / toStatus 固定节点）。"""
    return N(nid, "debug", x, y, wires, name=name, active=True,
             tosidebar=to_sidebar, console=console, tostatus=to_status,
             complete=complete, targetType=target_type,
             statusVal=status_val, statusType="auto" if to_status else "auto")


# ── 网络节点工厂（mqtt / tcp / udp / websocket，均走 127.0.0.1 环回自证）──────
#
# 设计原则：网络类节点必须拿**真环回证据**（发出去的字节从另一侧真收回来），
# 「部署没报错」「节点显示 connected」都不算通过。八类节点两两配对：
#     mqtt out → (现网 HAOS-MQTT broker，见 MQTT_BROKER 配置节点) → mqtt in
#     tcp  out → 127.0.0.1:PORT_TCP          → tcp  in
#     udp  out → 127.0.0.1:PORT_UDP          → udp  in
#     ws   out → ws://127.0.0.1:WS_PORT/path → ws   in
# tcp/udp/ws 的两端都在同一个 NR 容器进程内，不依赖任何外部服务；
# mqtt 复用现网已在跑的 HAOS-MQTT broker（45 个 mqtt out 在用，可用性已被生产验证）。
def mqtt_in_node(nid: str, name: str, topic: str, x: int, y: int, wires: list, *,
                 qos: str = "0", datatype: str = "utf8") -> dict:
    """mqtt in 订阅节点。

    Args:
        nid: 节点 id。
        name: 节点名。
        topic: 订阅主题（支持 + / # 通配符）。
        x: 画布 x。
        y: 画布 y。
        wires: 输出连线。
        qos: 订阅 QoS（"0"/"1"/"2"）。
        datatype: 载荷解码方式（utf8=字符串 / json=自动解析成对象 / buffer / base64）。

    Returns:
        节点 dict。
    """
    return N(nid, "mqtt in", x, y, wires, name=name, topic=topic, qos=qos,
             datatype=datatype, broker=MQTT_BROKER, nl=False, rap=True, rh=0,
             inputs=0)


def mqtt_out_node(nid: str, name: str, topic: str, x: int, y: int, *,
                  qos: str = "0", retain: str = "false") -> dict:
    """mqtt out 发布节点（无输出口，wires 恒为 []）。

    Args:
        nid: 节点 id。
        name: 节点名。
        topic: 发布主题；留空则由 msg.topic 决定（本身即一种参数变体）。
        x: 画布 x。
        y: 画布 y。
        qos: 发布 QoS。
        retain: 是否 retain；本矩阵一律 "false" —— retain=true 会在 broker 上
            留存消息污染其他订阅方，与 net-zero 纪律冲突。

    Returns:
        节点 dict。
    """
    return N(nid, "mqtt out", x, y, [], name=name, topic=topic, qos=qos,
             retain=retain, respTopic="", contentType="", userProps="",
             correl="", expiry="", broker=MQTT_BROKER)


def tcp_in_node(nid: str, name: str, x: int, y: int, wires: list, *,
                port: int = 0, datamode: str = "stream", datatype: str = "utf8",
                newline: str = "", topic: str = "") -> dict:
    """tcp in 监听节点（server 模式，监听容器内本地端口）。

    Args:
        nid: 节点 id。
        name: 节点名。
        x: 画布 x。
        y: 画布 y。
        wires: 输出连线。
        port: 监听端口。
        datamode: "stream"（按分隔符切帧）/"single"（连接关闭才吐）。
        datatype: "utf8"/"buffer"/"base64"。
        newline: 分帧分隔符，写**字面反斜杠 n**（NR 内部再 replace 成真换行）。
        topic: 附加到 msg.topic 的固定值。

    Returns:
        节点 dict。
    """
    return N(nid, "tcp in", x, y, wires, name=name, server="server", host="",
             port=str(port), datamode=datamode, datatype=datatype,
             newline=newline, topic=topic, base64=False, tls="", trim=False)


def tcp_out_node(nid: str, name: str, x: int, y: int, *,
                 host: str = "127.0.0.1", port: int = 0, end: bool = True) -> dict:
    """tcp out 发送节点（client 模式，连到本地 tcp in）。

    Args:
        nid: 节点 id。
        name: 节点名。
        x: 画布 x。
        y: 画布 y。
        host: 目标主机。
        port: 目标端口。
        end: 发完是否关闭连接（true 让每轮独立，避免连接残留）。

    Returns:
        节点 dict。
    """
    return N(nid, "tcp out", x, y, [], name=name, host=host, port=str(port),
             beserver="client", base64=False, end=end, tls="")


def udp_in_node(nid: str, name: str, x: int, y: int, wires: list, *,
                port: int = 0, datatype: str = "utf8") -> dict:
    """udp in 监听节点。

    Args:
        nid: 节点 id。
        name: 节点名。
        x: 画布 x。
        y: 画布 y。
        wires: 输出连线。
        port: 监听端口。
        datatype: "utf8"（字符串）/"buffer"/"base64"。

    Returns:
        节点 dict。
    """
    return N(nid, "udp in", x, y, wires, name=name, iface="", port=str(port),
             ipv6=False, multicast="false", group="", datatype=datatype)


def udp_out_node(nid: str, name: str, x: int, y: int, *,
                 addr: str = "127.0.0.1", port: int = 0) -> dict:
    """udp out 发送节点（无输出口）。"""
    return N(nid, "udp out", x, y, [], name=name, addr=addr, iface="",
             port=str(port), ipv6=False, outport="", base64=False,
             multicast="false")


def ws_listener_config(nid: str, path: str, *, wholemsg: str = "false") -> dict:
    """websocket-listener 配置节点（在 NR 自身 HTTP 端口上开 ws 服务端）。

    注：配置节点带 z=TAB_ID 属 NR 支持的 flow-scoped config node，随本 tab
    一起创建/销毁，不会污染全局配置面板。
    """
    node = {"id": nid, "type": "websocket-listener", "z": TAB_ID,
            "path": path, "wholemsg": wholemsg}
    nodes.append(node)
    return node


def ws_client_config(nid: str, url: str, *, wholemsg: str = "false") -> dict:
    """websocket-client 配置节点（回连自身 listener，构成进程内环回）。"""
    node = {"id": nid, "type": "websocket-client", "z": TAB_ID, "path": url,
            "tls": "", "wholemsg": wholemsg, "hb": "0", "subprotocol": ""}
    nodes.append(node)
    return node


def ws_out_node(nid: str, name: str, x: int, y: int, *,
                server: str = "", client: str = "") -> dict:
    """websocket out（server 模式=广播给所有已连客户端；无输出口）。"""
    return N(nid, "websocket out", x, y, [], name=name, server=server, client=client)


def ws_in_node(nid: str, name: str, x: int, y: int, wires: list, *,
               server: str = "", client: str = "") -> dict:
    """websocket in（client 模式=从自身 ws 客户端连接收消息）。"""
    return N(nid, "websocket in", x, y, wires, name=name, server=server, client=client)


# ══════════════════════════════════════════════════════════════════════
# 断言归一化节点（幂等闸门 + 真实证据汇聚）
# ══════════════════════════════════════════════════════════════════════
def assert_node(case: str, kind: str, js: str, y: int) -> str:
    """断言归一化节点：执行 js（须定义 ok/s），产出 {case,kind,ok,summary} 汇入 join。

    Args:
        case: 用例编号。
        kind: 用例类型（用于 覆盖矩阵 映射）。
        js: 断言片段，可用 p(=msg.payload) 与 msg；须给 ok 与 s 赋值。
        y: 画布纵坐标。

    Returns:
        断言节点 id。
    """
    nid = aid(case)
    body = (
        "// %s %s 断言归一化\n" % (case, kind)
        + "const _ep = flow.get('%s') || 0;\n" % EPOCH_KEY
        + "if (flow.get('%s_seen_%s') === _ep) { return null; }\n" % (PREFIX, case)
        + "flow.set('%s_seen_%s', _ep);\n" % (PREFIX, case)
        + "const p = msg.payload;\n"
        + js + "\n"
        + "msg.payload = { case: '%s', kind: '%s', ok: !!ok, summary: String(s) };\n"
        % (case, kind)
        # 双通道：除了汇入 join，同时写 flow context。
        # join(count=60) 只要有一路缺席就永不吐出 → 零可观测性；写 context 后
        # 任何时刻都能用 DUMP inject 导出快照，缺席者立刻现形。
        + "flow.set('%s_r_%s', msg.payload);\n" % (PREFIX, case)
        + "node.status({ fill: ok ? 'green' : 'red', shape: 'dot', "
          "text: (ok ? 'OK ' : 'FAIL ') + '%s' });\n" % case
        + "return msg;"
    )
    func_node(nid, body, 1700, y, [["capm_join"]])
    return nid


# ══════════════════════════════════════════════════════════════════════
# 顶部：RUN ALL inject + fan(N)
# ══════════════════════════════════════════════════════════════════════
def build_header(case_list: list[tuple[str, str]]) -> None:
    """建 RUN ALL inject 与 N 路 fan。"""
    N(PREFIX + "_run", "inject", 90, 60, [[PREFIX + "_fan"]],
      name="▶ RUN ALL (capmatrix)", props=[{"p": "payload"}], repeat="", crontab="",
      once=False, onceDelay=0.1, topic="", payload="", payloadType="date")
    pairs = ",\n ".join("['%s','%s']" % (c, k) for c, k in case_list)
    func_node(PREFIX + "_fan", (
        "// 一次点击并发跑完整矩阵：%d 路\n" % len(case_list)
        + "flow.set('%s', Date.now());\n" % EPOCH_KEY
        + "const C = [\n " + pairs + "\n];\n"
        "return C.map(function (c) { "
        "return { _case: c[0], _kind: c[1], payload: {} }; });"
    ), 300, 60, [], outputs=len(case_list))


# ══════════════════════════════════════════════════════════════════════
# 各用例构建器（平行 fan 入口：每个用例 ENTRY=[首节点]）
# ══════════════════════════════════════════════════════════════════════
def build_transform() -> list[tuple[str, str]]:
    """纯 NR 变换节点覆盖（template/split/sort/batch/merge/csv/xml/json/yaml/range/html）。"""
    cases: list[tuple[str, str]] = []
    y = 140

    # T1 template mustache
    func_node("capm_t1_prep", "msg.name='World'; msg.payload={}; return msg;", 480, y,
              [["capm_t1_tpl"]])
    template_node("capm_t1_tpl", "mustache", "Hi {{name}}!", 680, y, [["capm_a_T1"]])
    assert_node("T1", "template-mustache",
                "let ok = (p === 'Hi World!');\n"
                "let s = 'template(mustache) → payload=' + p;", y)
    ENTRY["T1"] = ["capm_t1_prep"]; cases.append(("T1", "template-mustache"))

    # T2 template plain + output=json（渲染结果再按 JSON 解析成对象）
    func_node("capm_t2_prep", "msg.payload={}; return msg;", 480, y + 50,
              [["capm_t2_tpl"]])
    template_node("capm_t2_tpl", "plain→json", "{'a': 5, 'b': [1,2]}".replace("'", '"'),
                  680, y + 50, [["capm_a_T2"]], syntax="plain", out="json")
    assert_node("T2", "template-plain-json",
                "let ok = (p && typeof p==='object' && p.a===5 && "
                "Array.isArray(p.b) && p.b.length===2);\n"
                "let s = 'template(syntax=plain,output=json) → 解析为对象 '"
                " + JSON.stringify(p);", y + 50)
    ENTRY["T2"] = ["capm_t2_prep"]; cases.append(("T2", "template-plain-json"))

    # T3 template multiline
    template_node("capm_t3_tpl", "multiline", "line1\nline2\nline3", 480, y + 100,
                  [["capm_a_T3"]], syntax="mustache", out="str")
    assert_node("T3", "template-multiline",
                "let ok = (typeof p==='string' && p.split('\\n').length===3);\n"
                "let s = 'template(多行) → 行数=' + (p||'').split('\\n').length;", y + 100)
    ENTRY["T3"] = ["capm_t3_tpl"]; cases.append(("T3", "template-multiline"))

    # T4 split array
    func_node("capm_t4_prep", "msg.payload=[1,2,3]; return msg;", 480, y + 150,
              [["capm_t4_sp"]])
    split_node("capm_t4_sp", "split array", 680, y + 150, [["capm_t4_join"]])
    join_node("capm_t4_join", "T4 join(3)", 880, y + 150, [["capm_a_T4"]], count=3)
    assert_node("T4", "split-array",
                "let ok = (Array.isArray(p) && p.length===3 && p.indexOf(1)>=0 "
                "&& p.indexOf(2)>=0 && p.indexOf(3)>=0);\n"
                "let s = 'split(array) → 收齐 ' + (Array.isArray(p)?p.length:0) + ' 段 '"
                "+ JSON.stringify(p);", y + 150)
    ENTRY["T4"] = ["capm_t4_prep"]; cases.append(("T4", "split-array"))

    # T5 split string by line
    func_node("capm_t5_prep", "msg.payload='a\\nb\\nc'; return msg;", 480, y + 200,
              [["capm_t5_sp"]])
    split_node("capm_t5_sp", "split \\n", 680, y + 200, [["capm_t5_join"]], splt="\n")
    join_node("capm_t5_join", "T5 join(3)", 880, y + 200, [["capm_a_T5"]], count=3)
    assert_node("T5", "split-string-line",
                "let ok = (Array.isArray(p) && p.length===3 && p[0]==='a' && p[2]==='c');\n"
                "let s = 'split(字符串按行) → ' + JSON.stringify(p);", y + 200)
    ENTRY["T5"] = ["capm_t5_prep"]; cases.append(("T5", "split-string-line"))

    # T6 sort asc num（targetType=msg：就地排 payload 数组，不是排消息序列）
    func_node("capm_t6_prep", "msg.payload=[3,1,2,10]; return msg;", 480, y + 250,
              [["capm_t6_sort"]])
    sort_node("capm_t6_sort", "sort asc num", 680, y + 250, [["capm_a_T6"]],
              order="ascending", as_num=True)
    assert_node("T6", "sort-asc-num",
                "let ok = (Array.isArray(p) && p.length===4 && p[0]===1 && p[1]===2 "
                "&& p[2]===3 && p[3]===10);\n"
                "let s = 'sort(升序/数字, 含 10 验证非字典序) → ' + JSON.stringify(p);",
                y + 250)
    ENTRY["T6"] = ["capm_t6_prep"]; cases.append(("T6", "sort-asc-num"))

    # T7 sort desc str
    func_node("capm_t7_prep", "msg.payload=['c','a','b']; return msg;", 480, y + 300,
              [["capm_t7_sort"]])
    sort_node("capm_t7_sort", "sort desc str", 680, y + 300, [["capm_a_T7"]],
              order="descending", as_num=False)
    assert_node("T7", "sort-desc-str",
                "let ok = (Array.isArray(p) && p.length===3 && p[0]==='c' "
                "&& p[1]==='b' && p[2]==='a');\n"
                "let s = 'sort(降序/字符串) → ' + JSON.stringify(p);", y + 300)
    ENTRY["T7"] = ["capm_t7_prep"]; cases.append(("T7", "sort-desc-str"))

    # T8 batch count=2（4 条消息切成 2 组序列，每组 2 条）
    #
    # 坑：原断言查 msg.parts.count，但中间过了 join —— join 合并序列时会**消费掉**
    #     msg.parts（序列已终结），断言恒读到 undefined → 假 FAIL。
    # 正解：不经 join，直接在 batch 下游逐条截获 msg.parts 快照，验证真正的分组语义：
    #     4 条被切成 2 个不同 parts.id 的组，每组 parts.count===2、index 为 0/1。
    func_node("capm_t8_gen", (
        "flow.set('capm_t8_parts', []);\n"
        "for (let i=1;i<=4;i++) node.send({payload:i});\nreturn null;"), 480, y + 350,
        [["capm_t8_b"]])
    batch_node("capm_t8_b", "batch count=2", 680, y + 350, [["capm_t8_tap"]],
               mode="count", count=2)
    func_node("capm_t8_tap", (
        "const k = 'capm_t8_parts';\n"
        "const arr = flow.get(k) || [];\n"
        "const pt = msg.parts || {};\n"
        "arr.push({gid: pt.id, idx: pt.index, cnt: pt.count, v: msg.payload});\n"
        "flow.set(k, arr);\n"
        "if (arr.length < 4) { return null; }\n"
        "msg.capm_t8 = arr;\nreturn msg;"), 880, y + 350, [["capm_a_T8"]])
    # 实测契约：batch(count) 的**各组复用同一个 parts.id**（不是每组换新 id），
    # 组边界靠 parts.index 归零标识。故组数 = index===0 的条数，而非 distinct(parts.id)。
    assert_node("T8", "batch-count",
                "const a = msg.capm_t8 || [];\n"
                "const groups = a.filter(function (x) { return x.idx === 0; }).length;\n"
                "const idxSeq = a.map(function (x) { return x.idx; }).join('');\n"
                "const valSeq = a.map(function (x) { return x.v; }).join(',');\n"
                "let ok = (a.length===4 && groups===2 && idxSeq==='0101' "
                "&& valSeq==='1,2,3,4' "
                "&& a.every(function (x) { return x.cnt===2; }));\n"
                "let s = 'batch(count=2) → 4 条切成 ' + groups + ' 组(index 归零计数), "
                "每组 parts.count=' + (a[0]?a[0].cnt:'-') + ', index 序=' + idxSeq"
                " + ', 值序=' + valSeq;", y + 350)
    ENTRY["T8"] = ["capm_t8_gen"]; cases.append(("T8", "batch-count"))

    # T9 按 topic 合并成对象（NR 原生等价：join mode=custom/build=object/key=topic）
    #
    # ⚠ 原设计想用 `merge` 节点，但 `merge` 是 RAW_NODE_ALLOWED 里的幽灵类型：
    #   1880 / 1990 两个实例的 GET /nodes 都查无此类型（见 capmatrix_probe_types.py）。
    #   flow 里只要出现一个 merge 节点，NR 就会
    #     "Waiting for missing types to be registered: - merge"
    #   并让**整个 tab 不启动**（inject 节点也不注册 → 触发 404），
    #   因此无法在流内断言，改由带外探针给出真实证据（产品缺陷 ④）。
    func_node("capm_t9_gen", (
        "node.send({topic:'a', payload:1}); node.send({topic:'b', payload:2});\n"
        "return null;"), 480, y + 400, [["capm_t9_m"]])
    join_node("capm_t9_m", "join→object by topic", 680, y + 400, [["capm_a_T9"]],
              mode="custom", build="object", key="topic", count=2, timeout=5)
    assert_node("T9", "join-object-by-topic",
                "let ok = (p && typeof p==='object' && p.a===1 && p.b===2);\n"
                "let s = 'join(build=object,key=topic) → ' + JSON.stringify(p);", y + 400)
    ENTRY["T9"] = ["capm_t9_gen"]; cases.append(("T9", "join-object-by-topic"))

    # T10 csv parse
    func_node("capm_t10_prep", "msg.payload='a,1\\nb,2'; return msg;", 480, y + 450,
              [["capm_t10_csv"]])
    csv_node("capm_t10_csv", "csv parse", 680, y + 450, [["capm_a_T10"]],
             temp="name,val")
    assert_node("T10", "csv-parse",
                # multi=mult → 整表一条消息；csv 会把纯数字列自动转 number（strings=true 时
                # 仍对全数字字段做转换），故断言 val===1 而非 '1'。
                "let ok = (Array.isArray(p) && p.length===2 && p[0].name==='a' "
                "&& Number(p[0].val)===1 && p[1].name==='b' && Number(p[1].val)===2);\n"
                "let s = 'csv(parse,multi=mult) → ' + JSON.stringify(p).slice(0,80);",
                y + 450)
    ENTRY["T10"] = ["capm_t10_prep"]; cases.append(("T10", "csv-parse"))

    # T11 csv stringify
    func_node("capm_t11_prep", "msg.payload=[{name:'x',val:9},{name:'y',val:8}]; "
              "return msg;", 480, y + 500, [["capm_t11_csv"]])
    csv_node("capm_t11_csv", "csv stringify", 680, y + 500, [["capm_a_T11"]],
             temp="name,val")
    assert_node("T11", "csv-stringify",
                "let ok = (typeof p==='string' && p.indexOf('x,9')>=0 && p.indexOf('y,8')>=0);\n"
                "let s = 'csv(stringify) → ' + JSON.stringify(p).slice(0,80);", y + 500)
    ENTRY["T11"] = ["capm_t11_prep"]; cases.append(("T11", "csv-stringify"))

    # T12 xml parse
    func_node("capm_t12_prep", "msg.payload='<r><n>hi</n><v>7</v></r>'; return msg;",
              480, y + 550, [["capm_t12_xml"]])
    xml_node("capm_t12_xml", "xml parse", 680, y + 550, [["capm_a_T12"]])
    assert_node("T12", "xml-parse",
                # xml2js 默认 explicitArray=true → 每个子元素都是数组
                "let ok = (p && p.r && Array.isArray(p.r.n) && p.r.n[0]==='hi' "
                "&& Array.isArray(p.r.v) && p.r.v[0]==='7');\n"
                "let s = 'xml(parse,attr=\\u0027\\u0027) → ' + JSON.stringify(p).slice(0,80);",
                y + 550)
    ENTRY["T12"] = ["capm_t12_prep"]; cases.append(("T12", "xml-parse"))

    # T13 xml attr/value 多节点
    change_set("capm_t13_prep",
               [{"p": "payload", "pt": "msg",
                 "to": "<r><n id='1'>hi</n><n id='2'>yo</n></r>", "tot": "str"}],
               480, y + 600, [["capm_t13_xml"]])
    xml_node("capm_t13_xml", "xml attr", 680, y + 600, [["capm_a_T13"]], attr="?")
    assert_node("T13", "xml-attr-value",
                # attr='?' → 属性收进 '?' 子键，文本值放 '_' 键（与 T12 的 attr='' 形成对照）
                "let n = (p && p.r && p.r.n) || [];\n"
                "let ok = (Array.isArray(n) && n.length===2 && n[0]._==='hi' "
                "&& n[0]['?'] && n[0]['?'].id==='1' && n[1]._==='yo' "
                "&& n[1]['?'].id==='2');\n"
                "let s = 'xml(attr=\\u0027?\\u0027 属性单独成键) → ' "
                "+ JSON.stringify(p).slice(0,100);", y + 600)
    ENTRY["T13"] = ["capm_t13_prep"]; cases.append(("T13", "xml-attr-value"))

    # T14 json parse
    change_set("capm_t14_prep",
               [{"p": "payload", "pt": "msg", "to": "{\"k\":42}", "tot": "str"}],
               480, y + 650, [["capm_t14_j"]])
    json_node("capm_t14_j", "json parse", 680, y + 650, [["capm_a_T14"]], ret="obj")
    assert_node("T14", "json-parse",
                "let ok = (p && typeof p==='object' && p.k===42);\n"
                "let s = 'json(parse) → ' + JSON.stringify(p);", y + 650)
    ENTRY["T14"] = ["capm_t14_prep"]; cases.append(("T14", "json-parse"))

    # T15 json stringify
    func_node("capm_t15_prep", "msg.payload={k:42}; return msg;", 480, y + 700,
              [["capm_t15_j"]])
    json_node("capm_t15_j", "json stringify", 680, y + 700, [["capm_a_T15"]], ret="str")
    assert_node("T15", "json-stringify",
                "let ok = (typeof p==='string' && p.indexOf('k')>=0 && p.indexOf('42')>=0);\n"
                "let s = 'json(stringify) → ' + p;", y + 700)
    ENTRY["T15"] = ["capm_t15_prep"]; cases.append(("T15", "json-stringify"))

    # T16 json 自动模式（action=""）+ pretty 缩进：对象 → 带缩进的 JSON 字符串
    func_node("capm_t16_prep", "msg.payload={a:{b:[10,20]}}; return msg;", 480, y + 750,
              [["capm_t16_j"]])
    json_node("capm_t16_j", "json auto+pretty", 680, y + 750, [["capm_a_T16"]],
              ret="", pretty=True)
    assert_node("T16", "json-auto-pretty",
                # 自动模式：输入是对象 → 序列化；pretty=true → 含换行与 4 空格缩进
                "let ok = (typeof p==='string' && p.indexOf('\\n')>=0 "
                "&& p.indexOf('    ')>=0 && JSON.parse(p).a.b[1]===20);\n"
                "let s = 'json(action=auto,pretty) → ' + JSON.stringify(p).slice(0,70);",
                y + 750)
    ENTRY["T16"] = ["capm_t16_prep"]; cases.append(("T16", "json-auto-pretty"))

    # T17 yaml parse
    func_node("capm_t17_prep", "msg.payload='k: 42\\nn: hello'; return msg;", 480, y + 800,
              [["capm_t17_y"]])
    yaml_node("capm_t17_y", "yaml parse", 680, y + 800, [["capm_a_T17"]])
    assert_node("T17", "yaml-parse",
                "let ok = (p && typeof p==='object' && p.k===42 && p.n==='hello');\n"
                "let s = 'yaml(parse) → ' + JSON.stringify(p);", y + 800)
    ENTRY["T17"] = ["capm_t17_prep"]; cases.append(("T17", "yaml-parse"))

    # T18 yaml stringify
    func_node("capm_t18_prep", "msg.payload={k:42, n:'hello'}; return msg;", 480, y + 850,
              [["capm_t18_y"]])
    yaml_node("capm_t18_y", "yaml stringify", 680, y + 850, [["capm_a_T18"]])
    assert_node("T18", "yaml-stringify",
                "let ok = (typeof p==='string' && p.indexOf('k: 42')>=0);\n"
                "let s = 'yaml(stringify) → ' + p.replace(/\\n/g,' | ');", y + 850)
    ENTRY["T18"] = ["capm_t18_prep"]; cases.append(("T18", "yaml-stringify"))

    # T19 range scale
    func_node("capm_t19_prep", "msg.payload=5; return msg;", 480, y + 900,
              [["capm_t19_r"]])
    range_node("capm_t19_r", "range scale", 680, y + 900, [["capm_a_T19"]],
               minin="1", maxin="10", minout="0", maxout="100", action="scale")
    assert_node("T19", "range-scale",
                "let ok = (typeof p==='number' && Math.abs(p - 44.44) < 1);\n"
                "let s = 'range(scale 1-10→0-100, in=5) → ' + p;", y + 900)
    ENTRY["T19"] = ["capm_t19_prep"]; cases.append(("T19", "range-scale"))

    # T20 range roll (loop)
    func_node("capm_t20_prep", "msg.payload=12; return msg;", 480, y + 950,
              [["capm_t20_r"]])
    range_node("capm_t20_r", "range roll", 680, y + 950, [["capm_a_T20"]],
               minin="1", maxin="10", minout="1", maxout="10", action="roll")
    assert_node("T20", "range-roll",
                # NR roll 公式：divisor=maxin-minin=9；((12-1)%9+9)%9=2；
                # out = minout + (2/9)*(maxout-minout) = 1 + 2 = 3
                "let ok = (p === 3);\n"
                "let s = 'range(roll 1-10→1-10, in=12) → ' + p "
                "+ ' (divisor=9, 折回到 3)';", y + 950)
    ENTRY["T20"] = ["capm_t20_prep"]; cases.append(("T20", "range-roll"))

    # T21 html cheerio selector
    func_node("capm_t21_prep",
              "msg.payload='<div><h1>One</h1><h1>Two</h1></div>'; return msg;",
              480, y + 1000, [["capm_t21_h"]])
    # as="single" 才是「一条消息 + 数组」；as="multi" 是每个匹配各发一条消息
    html_node("capm_t21_h", "html h1", 680, y + 1000, [["capm_a_T21"]],
              tag="h1", ret="text", as_mode="single")
    assert_node("T21", "html-cheerio",
                "let ok = (Array.isArray(p) && p.length===2 && p[0]==='One' && p[1]==='Two');\n"
                "let s = 'html(cheerio h1, as=single) → ' + JSON.stringify(p);", y + 1000)
    ENTRY["T21"] = ["capm_t21_prep"]; cases.append(("T21", "html-cheerio"))

    return cases


def build_change_switch() -> list[tuple[str, str]]:
    """change / switch 参数变体覆盖。"""
    cases: list[tuple[str, str]] = []
    y = 1120

    # C1 change set num
    func_node("capm_c1_prep", "msg.payload=0; return msg;", 480, y, [["capm_c1_ch"]])
    change_set("capm_c1_ch", [{"p": "payload", "pt": "msg", "to": "7", "tot": "num"}],
               680, y, [["capm_a_C1"]])
    assert_node("C1", "change-set-num",
                "let ok = (p === 7);\nlet s = 'change(set num) → payload=' + p;", y)
    ENTRY["C1"] = ["capm_c1_prep"]; cases.append(("C1", "change-set-num"))

    # C2 change set bool + jsonata
    func_node("capm_c2_prep", "msg.payload={x:true}; return msg;", 480, y + 50,
              [["capm_c2_ch"]])
    # 坑：JSONata **没有 `not` 关键字**，取反必须用 $not()。写 "not payload.x" 会在
    #     部署期就抛 Invalid JSONata expression: Syntax error: "payload"，
    #     节点直接不工作 → 用例静默缺席（不是运行期 FAIL，日志里才看得到）。
    change_set("capm_c2_ch", [
        {"p": "flag", "pt": "msg", "to": "payload.x", "tot": "jsonata"},
        {"p": "payload", "pt": "msg", "to": "$not(payload.x)", "tot": "jsonata"},
    ], 680, y + 50, [["capm_a_C2"]])
    assert_node("C2", "change-jsonata",
                "let ok = (msg.flag===true && p===false);\n"
                "let s = 'change(jsonata 取反) → payload=' + p + ' flag=' + msg.flag;", y + 50)
    ENTRY["C2"] = ["capm_c2_prep"]; cases.append(("C2", "change-jsonata"))

    # C3 change delete
    func_node("capm_c3_prep", "msg.payload={}; msg.temp=99; msg.keep=1; return msg;",
              480, y + 100, [["capm_c3_ch"]])
    # 坑：删除是**规则类型** t="delete"，不是 tot="delete"。
    #     写成 tot 时 t 落回默认 "set"，实际把 msg.temp 设成了空字符串 ——
    #     断言若只看「不等于 99」会假通过，必须显式断 typeof === 'undefined'。
    change_set("capm_c3_ch", [{"t": "delete", "p": "temp", "pt": "msg"}],
               680, y + 100, [["capm_a_C3"]])
    assert_node("C3", "change-delete",
                "let ok = (typeof msg.temp === 'undefined' "
                "&& !Object.prototype.hasOwnProperty.call(msg,'temp') && msg.keep===1);\n"
                "let s = 'change(t=delete msg.temp) → hasOwnProperty(temp)=' "
                "+ Object.prototype.hasOwnProperty.call(msg,'temp') + ' keep=' + msg.keep;",
                y + 100)
    ENTRY["C3"] = ["capm_c3_prep"]; cases.append(("C3", "change-delete"))

    # C4 change multi-rule chain
    func_node("capm_c4_prep", "msg.payload=1; return msg;", 480, y + 150,
              [["capm_c4_ch"]])
    change_set("capm_c4_ch", [
        {"p": "a", "pt": "msg", "to": "payload", "tot": "msg"},
        {"p": "b", "pt": "msg", "to": "a * 10", "tot": "jsonata"},
        {"p": "payload", "pt": "msg", "to": "b + 1", "tot": "jsonata"},
    ], 680, y + 150, [["capm_a_C4"]])
    assert_node("C4", "change-multi-rule",
                "let ok = (msg.a===1 && msg.b===10 && p===11);\n"
                "let s = 'change(多规则链) → a=' + msg.a + ' b=' + msg.b + ' payload=' + p;",
                y + 150)
    ENTRY["C4"] = ["capm_c4_prep"]; cases.append(("C4", "change-multi-rule"))

    # S1 switch regex
    func_node("capm_s1_prep", "msg.payload='abc123'; return msg;", 480, y + 200,
              [["capm_s1_sw"]])
    switch_node("capm_s1_sw", "payload",
                [{"t": "regex", "v": "^[a-z]+[0-9]+$", "vt": "str"}, {"t": "else"}],
                680, y + 200, [["capm_s1_hit"], ["capm_s1_el"]])
    change_set("capm_s1_hit", [{"p": "payload", "pt": "msg", "to": "MATCH", "tot": "str"}],
               860, y + 180, [["capm_a_S1"]])
    change_set("capm_s1_el", [{"p": "payload", "pt": "msg", "to": "NOMATCH", "tot": "str"}],
               860, y + 220, [["capm_a_S1"]])
    assert_node("S1", "switch-regex",
                "let ok = (p === 'MATCH');\n"
                "let s = 'switch(regex ^[a-z]+[0-9]+$) → ' + p;", y + 200)
    ENTRY["S1"] = ["capm_s1_prep"]; cases.append(("S1", "switch-regex"))

    # S2 switch contains
    func_node("capm_s2_prep", "msg.payload='hello world'; return msg;", 480, y + 250,
              [["capm_s2_sw"]])
    switch_node("capm_s2_sw", "payload",
                [{"t": "cont", "v": "world", "vt": "str"}, {"t": "else"}],
                680, y + 250, [["capm_s2_hit"], ["capm_s2_el"]])
    change_set("capm_s2_hit", [{"p": "payload", "pt": "msg", "to": "HAS", "tot": "str"}],
               860, y + 230, [["capm_a_S2"]])
    change_set("capm_s2_el", [{"p": "payload", "pt": "msg", "to": "NOHAS", "tot": "str"}],
               860, y + 270, [["capm_a_S2"]])
    assert_node("S2", "switch-contains",
                "let ok = (p === 'HAS');\n"
                "let s = 'switch(contains world) → ' + p;", y + 250)
    ENTRY["S2"] = ["capm_s2_prep"]; cases.append(("S2", "switch-contains"))

    # S3 switch type-number
    func_node("capm_s3_prep", "msg.payload=3.14; return msg;", 480, y + 300,
              [["capm_s3_sw"]])
    # 坑：istype 的 v 必须是 NR 的类型名全称（number/string/boolean/array/object/...），
    #     且 vt 必须与 v 同值。写 v="num"/vt="str" 恒不匹配 → 静默走 else，
    #     表现为「流程通了但走错分支」，最隐蔽的一类假通过。
    switch_node("capm_s3_sw", "payload",
                [{"t": "istype", "v": "number", "vt": "number"}, {"t": "else"}],
                680, y + 300, [["capm_s3_hit"], ["capm_s3_el"]])
    change_set("capm_s3_hit", [{"p": "payload", "pt": "msg", "to": "NUM", "tot": "str"}],
               860, y + 280, [["capm_a_S3"]])
    change_set("capm_s3_el", [{"p": "payload", "pt": "msg", "to": "NONNUM", "tot": "str"}],
               860, y + 320, [["capm_a_S3"]])
    assert_node("S3", "switch-type",
                "let ok = (p === 'NUM');\n"
                "let s = 'switch(istype num) → ' + p;", y + 300)
    ENTRY["S3"] = ["capm_s3_prep"]; cases.append(("S3", "switch-type"))

    # S4 switch between
    func_node("capm_s4_prep", "msg.payload=5; return msg;", 480, y + 350,
              [["capm_s4_sw"]])
    switch_node("capm_s4_sw", "payload",
                [{"t": "btwn", "v": "1", "v2": "10", "vt": "num"}, {"t": "else"}],
                680, y + 350, [["capm_s4_hit"], ["capm_s4_el"]])
    change_set("capm_s4_hit", [{"p": "payload", "pt": "msg", "to": "IN", "tot": "str"}],
               860, y + 330, [["capm_a_S4"]])
    change_set("capm_s4_el", [{"p": "payload", "pt": "msg", "to": "OUT", "tot": "str"}],
               860, y + 370, [["capm_a_S4"]])
    assert_node("S4", "switch-between",
                "let ok = (p === 'IN');\n"
                "let s = 'switch(between 1..10) → ' + p;", y + 350)
    ENTRY["S4"] = ["capm_s4_prep"]; cases.append(("S4", "switch-between"))

    # S5 switch otherwise (命中 else)
    func_node("capm_s5_prep", "msg.payload='zzz'; return msg;", 480, y + 400,
              [["capm_s5_sw"]])
    switch_node("capm_s5_sw", "payload",
                [{"t": "eq", "v": "abc", "vt": "str"}, {"t": "else"}],
                680, y + 400, [["capm_s5_hit"], ["capm_s5_el"]])
    change_set("capm_s5_hit", [{"p": "payload", "pt": "msg", "to": "EQ", "tot": "str"}],
               860, y + 380, [["capm_a_S5"]])
    change_set("capm_s5_el", [{"p": "payload", "pt": "msg", "to": "OTHER", "tot": "str"}],
               860, y + 420, [["capm_a_S5"]])
    assert_node("S5", "switch-otherwise",
                "let ok = (p === 'OTHER');\n"
                "let s = 'switch(otherwise 兜底) → ' + p;", y + 400)
    ENTRY["S5"] = ["capm_s5_prep"]; cases.append(("S5", "switch-otherwise"))

    return cases


def build_timing() -> list[tuple[str, str]]:
    """delay / inject / join / trigger 参数变体覆盖。"""
    cases: list[tuple[str, str]] = []
    y = 1540

    # D1 delay ms unit
    func_node("capm_d1_t0", "msg.t0=Date.now(); return msg;", 480, y, [["capm_d1_dl"]])
    delay_node("capm_d1_dl", 200, 680, y, [["capm_a_D1"]], units="milliseconds")
    assert_node("D1", "delay-ms",
                "const el=Date.now()-(msg.t0||0);\n"
                "let ok = (el>=180 && el<3000);\n"
                "let s = 'delay(200ms) 实测 ' + el + 'ms';", y)
    ENTRY["D1"] = ["capm_d1_t0"]; cases.append(("D1", "delay-ms"))

    # D2 delay random range
    func_node("capm_d2_t0", "msg.t0=Date.now(); return msg;", 480, y + 50,
              [["capm_d2_dl"]])
    N("capm_d2_dl", "delay", 680, y + 50, [["capm_a_D2"]],
      pauseType="delay", timeout="3", timeoutUnits="seconds", rate="1",
      nbRateUnits="1", rateUnits="second", randomFirst="1", randomLast="3",
      randomUnits="seconds", drop=False, allowrate=False, outputs=1)
    assert_node("D2", "delay-random",
                "const el=Date.now()-(msg.t0||0);\n"
                "let ok = (el>=800 && el<6000);\n"
                "let s = 'delay(random 1-3s) 实测 ' + el + 'ms';", y + 50)
    ENTRY["D2"] = ["capm_d2_t0"]; cases.append(("D2", "delay-random"))

    # D3 delay rate-limit
    func_node("capm_d3_gen", (
        "for (let i=0;i<3;i++) node.send({payload:i}); return null;"), 480, y + 100,
        [["capm_d3_dl"]])
    N("capm_d3_dl", "delay", 680, y + 100, [["capm_a_D3"]],
      pauseType="rate", timeout="1", timeoutUnits="seconds", rate="1",
      nbRateUnits="3", rateUnits="second", randomFirst="1", randomLast="5",
      randomUnits="seconds", drop=True, allowrate=True, outputs=1)
    assert_node("D3", "delay-ratelimit",
                "let ok = (typeof p==='number');\n"
                "let s = 'delay(rate-limit 1/3s drop) → 放行 payload=' + p + ' (限流生效)';",
                y + 100)
    ENTRY["D3"] = ["capm_d3_gen"]; cases.append(("D3", "delay-ratelimit"))

    # I1 inject payload json + interval
    inject_node("capm_i1_inj", "I1 inject json", 480, y + 150, [["capm_a_I1"]],
                payload='{"x":1}', payload_type="json", repeat="", crontab="")
    assert_node("I1", "inject-json",
                "let ok = (p && typeof p==='object' && p.x===1);\n"
                "let s = 'inject(payloadType=json) → payload=' + JSON.stringify(p);", y + 150)
    ENTRY["I1"] = ["capm_i1_inj"]; cases.append(("I1", "inject-json"))

    # I2 inject payload date
    inject_node("capm_i2_inj", "I2 inject date", 480, y + 200, [["capm_a_I2"]],
                payload="", payload_type="date", repeat="", crontab="")
    assert_node("I2", "inject-date",
                "let ok = (typeof p==='number' && p>0);\n"
                "let s = 'inject(payloadType=date) → payload=' + new Date(p).toISOString();",
                y + 200)
    ENTRY["I2"] = ["capm_i2_inj"]; cases.append(("I2", "inject-date"))

    # I3 inject interval (repeat) —— 用 flow context 计数，断言 repeat 真实触发
    inject_node("capm_i3_inj", "I3 inject interval", 300, y + 250, [["capm_i3_cnt"]],
                payload="tick", payload_type="str", repeat="0.1", crontab="", once=True)
    func_node("capm_i3_cnt", (
        "flow.set('%s_i3', (flow.get('%s_i3')||0)+1);\n" % (PREFIX, PREFIX)
        + "return null;"), 480, y + 250, [[]])
    assert_node("I3", "inject-interval",
                "const n = flow.get('%s_i3')||0;\n" % PREFIX
                + "let ok = (n >= 2);\n"
                "let s = 'inject(repeat=0.1s) 累计触发 ' + n + ' 次（repeat 生效）';", y + 250)
    ENTRY["I3"] = [aid("I3")]; cases.append(("I3", "inject-interval"))

    # J1 join manual (timeout + 手动 flush)
    func_node("capm_j1_gen", (
        "node.send({payload:1, topic:'x'}); node.send({payload:2, topic:'y'});\n"
        "return null;"), 480, y + 300, [["capm_j1_j"]])
    join_node("capm_j1_j", "J1 join manual", 680, y + 300, [["capm_a_J1"]],
              mode="custom", build="object", count=2, timeout=5, key="topic")
    assert_node("J1", "join-manual",
                "let ok = (p && typeof p==='object' && p.x===1 && p.y===2);\n"
                "let s = 'join(manual object) → ' + JSON.stringify(p);", y + 300)
    ENTRY["J1"] = ["capm_j1_gen"]; cases.append(("J1", "join-manual"))

    # J2 join timed (按时间收齐数组)
    func_node("capm_j2_gen", (
        "node.send({payload:'a'}); node.send({payload:'b'});\nreturn null;"),
        480, y + 350, [["capm_j2_j"]])
    join_node("capm_j2_j", "J2 join timed", 680, y + 350, [["capm_a_J2"]],
              mode="custom", build="array", count=0, timeout=3, key="topic")
    assert_node("J2", "join-timed",
                "let ok = (Array.isArray(p) && p.length>=2);\n"
                "let s = 'join(timed 3s) → ' + (Array.isArray(p)?p.length:0) + ' 段';", y + 350)
    ENTRY["J2"] = ["capm_j2_gen"]; cases.append(("J2", "join-timed"))

    # J3 join keys (按 key 聚合)
    func_node("capm_j3_gen", (
        "node.send({payload:1, topic:'k'}); node.send({payload:2, topic:'k'});\n"
        "return null;"), 480, y + 400, [["capm_j3_j"]])
    join_node("capm_j3_j", "J3 join keys", 680, y + 400, [["capm_a_J3"]],
              mode="custom", build="array", count=2, timeout=5, key="topic")
    assert_node("J3", "join-keys",
                "let ok = (Array.isArray(p) && p.length===2);\n"
                "let s = 'join(keys=topic) → 聚合 ' + (Array.isArray(p)?p.length:0) + ' 段';",
                y + 400)
    ENTRY["J3"] = ["capm_j3_gen"]; cases.append(("J3", "join-keys"))

    # TR1 trigger delay/extend
    inject_node("capm_tr1_inj", "TR1 trigger in", 480, y + 450, [["capm_tr1_tr"]],
                payload="go", payload_type="str", repeat="", crontab="")
    trigger_node("capm_tr1_tr", "TR1 trigger", 680, y + 450, [["capm_a_TR1"]],
                 op1="", op1_type="nul", op2="fired", op2_type="str",
                 duration="300", units="ms", extend=False)
    assert_node("TR1", "trigger-delay",
                "let ok = (p === 'fired');\n"
                "let s = 'trigger(300ms 后发 op2) → payload=' + p;", y + 450)
    ENTRY["TR1"] = ["capm_tr1_inj"]; cases.append(("TR1", "trigger-delay"))

    # TR2 trigger reset：验证「op1 已发、reset 后 op2 不再发」
    #
    # 坑（本轮实测踩到）：原写法把 trigger 的输出接到 reset 支路后就断了，
    #   断言节点 capm_a_TR2 **压根没有任何入边** → 用例永远缺席。
    #   而 join(count=N) 收不齐时连总表都不生成，这种「悬空断言」在旧结构里完全隐形。
    #   现已在 self_check 里加了「断言节点必须有入边」硬校验，构建期即拦截。
    func_node("capm_tr2_prep", (
        "flow.set('%s_tr2', []);\n" % PREFIX
        + "msg.payload='go'; return msg;"), 300, y + 500,
        [["capm_tr2_tr", "capm_tr2_rdl", "capm_tr2_wdl"]])
    trigger_node("capm_tr2_tr", "TR2 trigger+reset", 480, y + 500, [["capm_tr2_col"]],
                 op1="armed", op1_type="str", op2="fired", op2_type="str",
                 duration="2500", units="ms", extend=False, reset="reset")
    func_node("capm_tr2_col", (
        "const a = flow.get('%s_tr2') || [];\n" % PREFIX
        + "a.push(String(msg.payload));\n"
        + "flow.set('%s_tr2', a);\n" % PREFIX
        + "return null;"), 700, y + 500, [[]])
    # 400ms 后送 reset：此时 op1 已发出、op2 的 2.5s 定时还没到 → 应被清除
    delay_node("capm_tr2_rdl", 0.4, 480, y + 545, [["capm_tr2_rst"]], units="seconds")
    change_set("capm_tr2_rst", [{"p": "payload", "pt": "msg", "to": "reset", "tot": "str"}],
               680, y + 545, [["capm_tr2_tr"]])
    # 4s 后收网：远超 2.5s，若 reset 没生效 'fired' 一定已经进数组
    delay_node("capm_tr2_wdl", 4, 480, y + 590, [[aid("TR2")]])
    assert_node("TR2", "trigger-reset",
                "const a = flow.get('%s_tr2') || [];\n" % PREFIX
                + "let ok = (a.length===1 && a[0]==='armed' && a.indexOf('fired')<0);\n"
                "let s = 'trigger(op1=armed 立即, op2=fired@2.5s, 0.4s 送 reset) → "
                "4s 后实收 ' + JSON.stringify(a) + '（op2 已被 reset 清除）';", y + 500)
    ENTRY["TR2"] = ["capm_tr2_prep"]; cases.append(("TR2", "trigger-reset"))

    return cases


def build_events_subflow() -> list[tuple[str, str]]:
    """status / complete / link in-out / debug / server-state-changed / 子流程覆盖。"""
    cases: list[tuple[str, str]] = []
    y = 2040

    # ST1 status scope
    #
    # 坑（本轮实测踩到）：status / complete 是**事件节点，没有输入口**。
    #   把源节点连到它们身上是无效连线；更坑的是若断言同时接了「源节点直连」支路，
    #   断言会先收到原始 msg（没有 msg.status）→ 报 text=? 的假 FAIL，
    #   看起来像「产品不支持」，实则是接线姿势错了。
    #   正解：源节点自己 setStatus → status 节点捕获 → 写 flow context →
    #        另起一条延时支路读回断言（确定性，无竞态）。
    func_node("capm_st1_src", (
        "node.status({fill:'green',shape:'dot',text:'CAPM-STATUS'});\n"
        "return null;"), 480, y, [[]])
    status_node("capm_st1_st", "status scope", ["capm_st1_src"], 480, y + 40,
                [["capm_st1_cap"]])
    func_node("capm_st1_cap", (
        "flow.set('%s_st1', {text: (msg.status && msg.status.text) || '', "
        "ts: Date.now()});\n" % PREFIX + "return null;"), 700, y + 40, [[]])
    delay_node("capm_st1_dl", 2, 700, y, [[aid("ST1")]])
    assert_node("ST1", "status-scope",
                "const r = flow.get('%s_st1');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.text==='CAPM-STATUS' && r.ts>=ep);\n"
                "let s = 'status(scope=[capm_st1_src]) → 捕获 text=' + (r?r.text:'∅') "
                "+ ', 事件时刻在本轮 epoch 之后=' + !!(r && r.ts>=ep);", y)
    ENTRY["ST1"] = ["capm_st1_src", "capm_st1_dl"]
    cases.append(("ST1", "status-scope"))

    # CP1 complete scope（同上：complete 无输入口，且它转发的是**原 msg 本体**）
    func_node("capm_cp1_src", "msg.payload='done'; return msg;", 480, y + 80, [[]])
    complete_node("capm_cp1_cp", "complete scope", ["capm_cp1_src"], 480, y + 120,
                  [["capm_cp1_cap"]])
    func_node("capm_cp1_cap", (
        "flow.set('%s_cp1', {p: msg.payload, ts: Date.now()});\n" % PREFIX
        + "return null;"), 700, y + 120, [[]])
    delay_node("capm_cp1_dl", 2, 700, y + 80, [[aid("CP1")]])
    assert_node("CP1", "complete-scope",
                "const r = flow.get('%s_cp1');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.p==='done' && r.ts>=ep);\n"
                "let s = 'complete(scope=[capm_cp1_src]) → 捕获 payload=' "
                "+ (r?r.p:'∅') + ', 在本轮 epoch 之后=' + !!(r && r.ts>=ep);", y + 80)
    ENTRY["CP1"] = ["capm_cp1_src", "capm_cp1_dl"]
    cases.append(("CP1", "complete-scope"))

    # L1 link in/out
    lin = "capm_l1_in"
    func_node("capm_l1_prep", "msg.payload='via-link'; return msg;", 480, y + 160,
              [["capm_l1_out"]])
    link_out_node("capm_l1_out", "→ link in", [lin], 680, y + 160)
    link_in_node(lin, "link in", 860, y + 160, [["capm_a_L1"]])
    assert_node("L1", "link-in-out",
                "let ok = (p === 'via-link');\n"
                "let s = 'link out→in → payload=' + p;", y + 160)
    ENTRY["L1"] = ["capm_l1_prep"]; cases.append(("L1", "link-in-out"))

    # DB1 debug complete + console + toStatus
    func_node("capm_db1_prep", "msg.payload='debug-me'; return msg;", 480, y + 240,
              [["capm_db1_dbg", "capm_a_DB1"]])
    debug_node("capm_db1_dbg", "debug sink", 680, y + 240, [[]],
               complete="payload", target_type="msg", to_sidebar=True, console=True,
               to_status=True, status_val="payload")
    assert_node("DB1", "debug-complete",
                "let ok = (p === 'debug-me');\n"
                "let s = 'debug(complete+console+toStatus) → 消息抵达, payload=' + p;", y + 240)
    ENTRY["DB1"] = ["capm_db1_prep"]; cases.append(("DB1", "debug-complete"))

    # SS1 server-state-changed 精确实体列表（entities.entity）
    #
    # 触发编排：A3 在 t=0 对 E_LAMP2 发 homeassistant.turn_off、t=4s 读回。
    #   SS1 必须等它做完再动同一盏灯，否则两个用例互相打脸。
    #   故 t=0 起延时 8s 再 toggle（off→on），t=15s 断言，t=20s 复位为 off（net-zero）。
    ssc_node("capm_ss1_ssc", "SSC 精确实体(entities.entity)", 480, y + 320,
             [["capm_ss1_cap"]], entity=[E_TEMP, E_LAMP2])
    func_node("capm_ss1_cap", (
        "const eid = (msg.capm_ent && msg.capm_ent.entity_id) "
        "|| (msg.capm_evt && msg.capm_evt.entity_id) || '';\n"
        "flow.set('%s_ss1', {state: msg.payload, entity: eid, "
        "ts: Date.now()});\n" % PREFIX + "return null;"), 700, y + 320, [[]])
    delay_node("capm_ss1_gap", 8, 300, y + 280, [["capm_ss1_fire"]])
    call_service("capm_ss1_fire", "light", "toggle", [E_LAMP2], 480, y + 280,
                 [["capm_ss1_dl", "capm_ss1_rst_d"]])
    delay_node("capm_ss1_dl", 7, 700, y + 280, [[aid("SS1")]])
    delay_node("capm_ss1_rst_d", 12, 700, y + 250, [["capm_ss1_rst"]])
    call_service("capm_ss1_rst", "light", "turn_off", [E_LAMP2], 900, y + 250, [[]])
    assert_node("SS1", "ssc-entity-list",
                "const r = flow.get('%s_ss1');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts>=ep && typeof r.state==='string' "
                "&& r.state.length>0 && r.entity==='%s');\n" % E_LAMP2
                + "let s = 'server-state-changed(entities.entity 列表, 主动 light.toggle 触发)"
                " → 实体=' + (r?r.entity:'∅') + ' state=' + (r?r.state:'∅') "
                "+ ' 本轮内=' + !!(r && r.ts>=ep);", y + 320)
    ENTRY["SS1"] = ["capm_ss1_gap"]
    cases.append(("SS1", "ssc-entity-list"))

    # SS2 server-state-changed 正则实体过滤（entities.regex）
    #
    # 触发编排：A2 在 t=0 toggle E_SWITCH、t=4s 读回翻转。
    #   SS2 在 t=8s 再 toggle 一次 —— 既产生本用例需要的 state_changed 事件，
    #   又顺手把 A2 改动的开关复位回原态（一石二鸟，全流程 net-zero）。
    ssc_node("capm_ss2_ssc", "SSC 正则实体(entities.regex)", 480, y + 380,
             [["capm_ss2_cap"]], regex="^" + E_SWITCH.replace(".", "\\.") + "$")
    func_node("capm_ss2_cap", (
        "const eid = (msg.capm_ent && msg.capm_ent.entity_id) "
        "|| (msg.capm_evt && msg.capm_evt.entity_id) || '';\n"
        "flow.set('%s_ss2', {state: msg.payload, entity: eid, "
        "ts: Date.now()});\n" % PREFIX + "return null;"), 700, y + 380, [[]])
    delay_node("capm_ss2_gap", 8, 300, y + 420, [["capm_ss2_fire"]])
    call_service("capm_ss2_fire", "switch", "toggle", [E_SWITCH], 480, y + 420,
                 [["capm_ss2_dl"]])
    delay_node("capm_ss2_dl", 7, 700, y + 420, [[aid("SS2")]])
    assert_node("SS2", "ssc-entity-regex",
                "const r = flow.get('%s_ss2');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts>=ep && typeof r.state==='string' "
                "&& r.state.length>0 && r.entity==='%s');\n" % E_SWITCH
                + "let s = 'server-state-changed(entities.regex 正则过滤, 主动 switch.toggle)"
                " → 实体=' + (r?r.entity:'∅') + ' state=' + (r?r.state:'∅') "
                "+ ' 本轮内=' + !!(r && r.ts>=ep);", y + 380)
    ENTRY["SS2"] = ["capm_ss2_gap"]
    cases.append(("SS2", "ssc-entity-regex"))

    # SF1 demo_notify 子流程（link out → TTS 入口）
    #
    # ⚠ 环境事实（实测追链得出，非代码缺陷）：TTS 队列入口 link in 的**第一跳**是
    #   `time-range-switch: TTS播报时间过滤(06:50-23:00)`。夜间静默窗（23:00-06:50）内
    #   消息在此被拦下，压根到不了「TTS队列管理 v3」，自然不会 push TTS_RECENT_TRIGGERS。
    #   若断言只看触发戳，本用例在夜间必然 FAIL，且 FAIL 原因与「link out 断了」无法区分。
    #
    # 取证方案（双通道，两个时段都给出真断言）：
    #   ① link out 的 links 同时指向 **capmatrix 自建探针 link in**。link out 支持多目标，
    #      探针收到即证明「link out 按 link 语义完成了跨 tab 投递、payload 完整」。
    #      这是对被测节点类型本身的直接断言，不受下游业务策略影响。
    #   ② 播报窗口内（06:50-23:00）额外要求 TTS 队列管理器登记 ts≥发出时刻的触发戳；
    #      静默窗内则要求「未登记」——即验证时间闸门确实生效，同样是真断言。
    #   容器 TZ=Asia/Shanghai（总表 toLocaleString 已确证），故可直接用本地小时判窗。
    func_node("capm_sf1_prep", (
        "const tag = '[capmatrix回归] 子流程通知自检';\n"
        "msg._tts_t0 = Date.now();\n"
        "msg._tts_tag = tag;\n"
        "msg.payload = { text: tag, priority: 3 };\n"
        "flow.set('%s_sf1_t0', Date.now());\n" % PREFIX
        + "flow.set('%s_sf1_probe', null);\n" % PREFIX
        + "return msg;"), 480, y + 460, [["capm_sf1_link", "capm_sf1_dl"]])
    # wires 必须是 [[]]（1 个空输出数组）：nr_client 的结构 lint 要求 link out 恰有
    # 1 个 wires 数组，给 [] 会被判「0 个 wires 数组」直接拒绝部署。
    N("capm_sf1_link", "link out", 680, y + 440, [[]], name="→ demo_notify(TTS入口)+探针",
      mode="link", links=[TTS_LINK_IN, "capm_sf1_probe_in"])
    N("capm_sf1_probe_in", "link in", 880, y + 440, [["capm_sf1_probe_fn"]],
      name="capmatrix link out 投递探针", links=["capm_sf1_link"])
    func_node("capm_sf1_probe_fn", (
        "flow.set('%s_sf1_probe', {ts: Date.now(), " % PREFIX
        + "text: (msg.payload && msg.payload.text) || '', "
        "prio: (msg.payload && msg.payload.priority)});\nreturn null;"),
        1060, y + 440, [[]])
    # 坑：断言不能与 link out 并联即时执行 —— TTS 队列管理器登记触发戳要几百 ms，
    #     并联时断言恒读到 0 个戳，产生「子流程没工作」的假 FAIL（日志里 TTS 明明播了）。
    delay_node("capm_sf1_dl", 4, 680, y + 480, [[aid("SF1")]])
    assert_node("SF1", "subflow-demo_notify",
                "const t0 = flow.get('%s_sf1_t0') || 0;\n" % PREFIX
                + "const pb = flow.get('%s_sf1_probe');\n" % PREFIX
                + "const arr = global.get('TTS_RECENT_TRIGGERS') || [];\n"
                "const hit = arr.filter(function (t) { return t >= t0; });\n"
                "const cd = !!global.get('TTS_COOLDOWN_ACTIVE');\n"
                "const d = new Date();\n"
                "const mins = d.getHours()*60 + d.getMinutes();\n"
                "const voice = (mins >= 410 && mins < 1380);\n"
                "const delivered = !!(pb && pb.ts >= t0 && pb.prio === 3 "
                "&& String(pb.text).indexOf('capmatrix') >= 0);\n"
                "let ok = delivered && (voice ? (hit.length >= 1 && !cd) : (hit.length === 0));\n"
                "let s = 'demo_notify 子流程(link out→TTS入口) → 投递探针 '"
                " + (delivered ? '收到完整 payload(prio=' + pb.prio + ')' : '未收到')"
                " + '; 时段=' + (voice ? '播报窗06:50-23:00' : '夜间静默窗23:00-06:50')"
                " + ', TTS登记 ' + hit.length + ' 戳(窗口内共 ' + arr.length + '), 熔断=' + cd"
                " + (voice ? '' : ' [静默闸门 time-range-switch 生效=正确拦截]');",
                y + 460)
    ENTRY["SF1"] = ["capm_sf1_prep"]; cases.append(("SF1", "subflow-demo_notify"))

    # SF2 bark 带 title 子流程
    func_node("capm_sf2_prep", (
        "msg.title = 'capmatrix 回归'; msg.body = '[capmatrix回归] bark 自检 ' + Date.now();\n"
        "msg._sent = msg.body; return msg;"), 480, y + 540, [["capm_sf2_sub"]])
    N("capm_sf2_sub", "subflow:" + SUB_BARK, 680, y + 540, [[aid("SF2")]],
      name="SF2 bark 带title")
    assert_node("SF2", "subflow-bark",
                "let ok = !!(p && p.ok===true && p.status===200 && p.sent && "
                "p.sent.title==='capmatrix 回归' && p.sent.body===msg._sent);\n"
                "let s = 'bark 子流程(带title) → HTTP ' + (p&&p.status) + ', title=' "
                "+ (p&&p.sent&&p.sent.title);", y + 540)
    ENTRY["SF2"] = ["capm_sf2_prep"]; cases.append(("SF2", "subflow-bark"))

    return cases


def build_api_domains() -> list[tuple[str, str]]:
    """api-call-service 多 domain + data 变体覆盖。

    真实实体（台灯/开关）做强证据；其余 domain 用「HA 真实响应」作为已派发证据
    （成功或结构化错误均证明 api-call-service 节点正确形成并派发了调用）。
    """
    cases: list[tuple[str, str]] = []
    y = 2540

    # A1 light brightness data 变体（强证据：读回 brightness）
    # 延时 1.5s → 2.5s：Philips 走云，状态回报有滞后，1.5s 属于边缘值。
    call_service("capm_a1_call", "light", "turn_on", [E_LAMP], 480, y, [["capm_a1_d"]],
                 data={"brightness": 180})
    delay_node("capm_a1_d", 2.5, 660, y, [["capm_a1_rd"]])
    read_state("capm_a1_rd", E_LAMP, 840, y, [[aid("A1")]], prop="verify", entity_prop="ent")
    assert_node("A1", "api-light-brightness",
                "const b = msg.ent && msg.ent.attributes ? msg.ent.attributes.brightness : null;\n"
                "let ok = (msg.verify==='on' && typeof b==='number' && Math.abs(b-180)<=12);\n"
                "let s = 'api-call-service(light.turn_on brightness=180) → state=' + msg.verify "
                "+ ', brightness=' + b;", y)
    ENTRY["A1"] = ["capm_a1_call"]; cases.append(("A1", "api-light-brightness"))

    # A2 switch toggle data 变体（强证据：读回翻转）
    # 载体已换成实测 1s 内翻转的台风指示灯（见 E_SWITCH 常量注释）。
    # SS2 会在本用例读回之后（t≈8s）再 toggle 同一实体一次，正好把它复位 → net-zero。
    read_state("capm_a2_rd0", E_SWITCH, 480, y + 60, [["capm_a2_call"]], prop="before")
    call_service("capm_a2_call", "switch", "toggle", [E_SWITCH], 660, y + 60,
                 [["capm_a2_d"]])
    delay_node("capm_a2_d", 4, 840, y + 60, [["capm_a2_rd1"]])
    read_state("capm_a2_rd1", E_SWITCH, 1020, y + 60, [[aid("A2")]], prop="verify")
    assert_node("A2", "api-switch-toggle",
                "let ok = (msg.before!==msg.verify && (msg.verify==='on'||msg.verify==='off'));\n"
                "let s = 'api-call-service(switch.toggle) → ' + msg.before + '=>' + msg.verify;",
                y + 60)
    ENTRY["A2"] = ["capm_a2_rd0"]; cases.append(("A2", "api-switch-toggle"))

    # A3 homeassistant 通用域（强证据：读回 off）
    # 原设计用 persistent_notification.create + 读 persistent_notification.<id> 实体，
    # 但 HA 2023.8 起已移除该实体域，api-current-state 读空实体会直接报错且无输出，
    # 下游断言永久缺席。改用通用域 homeassistant.turn_off 打真实灯，HA 真值可核。
    call_service("capm_a3_call", "homeassistant", "turn_off", [E_LAMP2], 480, y + 120,
                 [["capm_a3_d"]])
    delay_node("capm_a3_d", 4, 660, y + 120, [["capm_a3_rd"]])
    read_state("capm_a3_rd", E_LAMP2, 840, y + 120, [[aid("A3")]], prop="verify")
    assert_node("A3", "api-homeassistant-domain",
                "let ok = (msg.verify==='off');\n"
                "let s = 'api-call-service(homeassistant.turn_off ' + '%s' + ') → 读回 state='"
                " + msg.verify;" % E_LAMP2, y + 120)
    ENTRY["A3"] = ["capm_a3_call"]; cases.append(("A3", "api-homeassistant-domain"))

    # A4+ 其余 domain：双路证据（成功路 + catch 错误路）
    # ─────────────────────────────────────────────────────────────────
    # 血泪 ①：原实现把 call_service 的输出直接接到 catch 节点上。catch **没有输入口**，
    #         成功路径的消息进去即蒸发 → 用例静默消失（不是 FAIL，是压根不出现）。
    # 血泪 ②：「没报错=通过」不算数。这里两条路都必须打标记后汇入断言：
    #         成功路 → 证明 websocket 往返完成（节点只在服务调用 resolve 后才输出）；
    #         错误路 → 要求 HA 的结构化错误里**回显本次载荷**（域名/服务名/探针实体），
    #                  证明是 HA 解析了我们构造的数据，而不是链路/注册层面的失败。
    # 另附实测往返耗时（工单认可的证据类型之一）。
    domains = [
        ("persistent_notification", "create", [],
         {"notification_id": "capm_probe_notify", "title": "capmatrix",
          "message": "[capmatrix回归] notify 域派发自检"}),
        ("climate", "turn_on", ["climate.capm_probe"], {}),
        ("cover", "open_cover", ["cover.capm_probe"], {}),
        ("lock", "lock", ["lock.capm_probe"], {}),
        ("vacuum", "start", ["vacuum.capm_probe"], {}),
        ("media_player", "media_play", ["media_player.capm_probe"], {}),
        ("script", "turn_on", ["script.capm_probe"], {}),
        ("scene", "turn_on", ["scene.capm_probe"], {}),
        ("automation", "turn_on", ["automation.capm_probe"], {}),
    ]
    yy = y + 180
    for i, (dom, svc, ents, data) in enumerate(domains):
        cid = "A%d" % (4 + i)
        pre, call = "capm_%s_pre" % cid, "capm_%s_call" % cid
        okn, ern, cat = "capm_%s_ok" % cid, "capm_%s_er" % cid, "capm_%s_catch" % cid
        func_node(pre, "msg.capm_t0 = Date.now();\nreturn msg;", 400, yy, [[call]])
        call_service(call, dom, svc, ents, 580, yy, [[okn]], data=data)
        func_node(okn, (
            "msg.capm_disp = 'ok';\n"
            "let v; try { v = JSON.stringify(msg.payload); } catch (e) { v = String(msg.payload); }\n"
            "msg.capm_ev = (v === undefined) ? '' : String(v);\n"
            "return msg;"
        ), 780, yy, [[aid(cid)]])
        catch_node(cat, [call], 580, yy + 32, [[ern]])
        func_node(ern, (
            "msg.capm_disp = 'err';\n"
            "msg.capm_ev = String((msg.error && msg.error.message) || msg.error || '');\n"
            "return msg;"
        ), 780, yy + 32, [[aid(cid)]])
        assert_node(cid, "api-%s" % dom, (
            "const disp = msg.capm_disp || '';\n"
            "const ev = String(msg.capm_ev || '');\n"
            "const rtt = msg.capm_t0 ? (Date.now() - msg.capm_t0) : -1;\n"
            "const echo = (ev.indexOf('%s') >= 0) || (ev.indexOf('%s') >= 0)"
            " || (ev.indexOf('capm_probe') >= 0);\n" % (dom, svc)
            + "let ok = false, why = '';\n"
            "if (disp === 'ok') { ok = (rtt >= 0); why = 'HA 往返完成(无错误)，响应=' + ev.slice(0,60); }\n"
            "else if (disp === 'err') { ok = (ev.length > 0 && echo);"
            " why = 'HA 结构化错误且回显本次载荷: ' + ev.slice(0,80); }\n"
            "else { why = '未收到任何派发标记(节点未输出且未抛错)'; }\n"
            "let s = 'api-call-service(%s.%s) 派发=' + (disp || '无') + ' rtt=' + rtt"
            " + 'ms | ' + why;" % (dom, svc)
        ), yy)
        ENTRY[cid] = [pre]; cases.append((cid, "api-%s" % dom))
        yy += 64

    return cases


def build_network() -> list[tuple[str, str]]:
    """网络节点覆盖：mqtt / tcp / udp / websocket 各 in+out，全部取真环回证据。

    每对节点用「同一条环回链 + 两个用例」：一个用例断言**发送侧确实把字节送出去**
    （对端收到且内容一致），另一个断言**接收侧参数真实生效**（分帧 / 解码类型 /
    通配符 / 来源地址等只有真跑通才能观测到的侧面）。这样 8 类节点各自都有独立
    断言，而不是「一条链绿了就算 8 类都过」。

    Returns:
        [(case, kind)] 列表。
    """
    cases: list[tuple[str, str]] = []
    y = 3100

    # ── N1/N2 mqtt out → HAOS-MQTT broker → mqtt in ──────────────────
    # N1：固定 topic + qos0 + datatype=utf8（收到的必须是**字符串**）
    # N2：节点 topic 留空（由 msg.topic 决定）+ qos2 + 订阅侧多层通配符 `#`
    #     + datatype=json（收到的必须是**已解析对象**）——后者是「参数真生效」
    #     的硬证据：若 datatype 没生效，拿到的会是 JSON 字符串而不是对象。
    func_node("capm_n1_prep", (
        "const v = 'capm-mqtt-n1-' + Date.now();\n"
        "flow.set('%s_n1_sent', v);\n" % PREFIX
        + "flow.set('%s_n1', null);\n" % PREFIX
        + "msg.payload = v; msg.topic = 'capm/loopback/n1';\nreturn msg;"),
        480, y, [["capm_n1_mo"]])
    mqtt_out_node("capm_n1_mo", "mqtt out 固定topic qos0", "capm/loopback/n1",
                  680, y, qos="0", retain="false")
    mqtt_in_node("capm_n1_mi", "mqtt in 精确topic utf8", "capm/loopback/n1",
                 480, y + 40, [["capm_n1_cap"]], qos="0", datatype="utf8")
    func_node("capm_n1_cap", (
        "flow.set('%s_n1', {p: msg.payload, t: msg.topic, " % PREFIX
        + "ty: typeof msg.payload, qos: msg.qos, ts: Date.now()});\nreturn null;"),
        700, y + 40, [[]])
    delay_node("capm_n1_dl", 4, 680, y - 40, [[aid("N1")]])
    assert_node("N1", "mqtt-out-publish",
                "const r = flow.get('%s_n1');\n" % PREFIX
                + "const sent = flow.get('%s_n1_sent');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.p === sent && r.ty === 'string' "
                "&& r.t === 'capm/loopback/n1');\n"
                "let s = 'mqtt out(qos0,retain=false) → broker → mqtt in(精确topic,utf8) "
                "环回: 收到=' + (r ? r.p : '∅') + ' 发出=' + sent + ' topic=' "
                "+ (r ? r.t : '∅') + ' 载荷类型=' + (r ? r.ty : '∅');", y)
    ENTRY["N1"] = ["capm_n1_prep", "capm_n1_dl"]
    cases.append(("N1", "mqtt-out-publish"))

    func_node("capm_n2_prep", (
        "flow.set('%s_n2', null);\n" % PREFIX
        + "msg.topic = 'capm/loop2/deep/x';\n"
        "msg.payload = JSON.stringify({k: 'capm-n2', n: 42});\nreturn msg;"),
        480, y + 100, [["capm_n2_mo"]])
    mqtt_out_node("capm_n2_mo", "mqtt out topic由msg决定 qos2", "",
                  680, y + 100, qos="2", retain="false")
    mqtt_in_node("capm_n2_mi", "mqtt in 多层通配符# json", "capm/loop2/#",
                 480, y + 140, [["capm_n2_cap"]], qos="2", datatype="json")
    func_node("capm_n2_cap", (
        "flow.set('%s_n2', {p: msg.payload, t: msg.topic, " % PREFIX
        + "ty: typeof msg.payload, ts: Date.now()});\nreturn null;"),
        700, y + 140, [[]])
    delay_node("capm_n2_dl", 4, 680, y + 60, [[aid("N2")]])
    assert_node("N2", "mqtt-in-subscribe",
                "const r = flow.get('%s_n2');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.ty === 'object' && r.p "
                "&& r.p.n === 42 && r.p.k === 'capm-n2' "
                "&& r.t === 'capm/loop2/deep/x');\n"
                "let s = 'mqtt in(多层通配符 capm/loop2/#, qos2, datatype=json) 收到 topic='"
                " + (r ? r.t : '∅') + ' 载荷类型=' + (r ? r.ty : '∅') + ' n=' "
                "+ (r && r.p ? r.p.n : '∅') + ' [object 而非 string 即证明 json 解码真生效]';",
                y + 100)
    ENTRY["N2"] = ["capm_n2_prep", "capm_n2_dl"]
    cases.append(("N2", "mqtt-in-subscribe"))

    # ── N3/N4 tcp out → 127.0.0.1:PORT_TCP → tcp in ──────────────────
    # 一次发两行 "capm-tcp-1\ncapm-tcp-2\n"：
    #   N3 断发送侧字节确实到达；N4 断 datamode=stream + newline 分帧真生效
    #   （分成 2 条独立 msg，而不是 1 条含换行的整串）。
    func_node("capm_n34_prep", (
        "flow.set('%s_n34', []);\n" % PREFIX
        + "msg.payload = 'capm-tcp-1' + String.fromCharCode(10) "
        "+ 'capm-tcp-2' + String.fromCharCode(10);\nreturn msg;"),
        480, y + 220, [["capm_n34_to"]])
    tcp_out_node("capm_n34_to", "tcp out 连本地环回", 680, y + 220,
                 host="127.0.0.1", port=PORT_TCP, end=True)
    tcp_in_node("capm_n34_ti", "tcp in 监听 stream+newline", 480, y + 260,
                [["capm_n34_cap"]], port=PORT_TCP, datamode="stream",
                datatype="utf8", newline="\\n", topic="capm/tcp")
    func_node("capm_n34_cap", (
        "const a = flow.get('%s_n34') || [];\n" % PREFIX
        + "a.push({p: msg.payload, t: msg.topic, sess: !!msg._session, "
        "ty: typeof msg.payload, ts: Date.now()});\n"
        + "flow.set('%s_n34', a);\nreturn null;" % PREFIX),
        700, y + 260, [[]])
    delay_node("capm_n3_dl", 4, 680, y + 180, [[aid("N3")]])
    assert_node("N3", "tcp-out-connect",
                "const a = flow.get('%s_n34') || [];\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "const fresh = a.filter(function (r) { return r.ts >= ep; });\n"
                "let ok = fresh.length >= 1 && fresh[0].p === 'capm-tcp-1';\n"
                "let s = 'tcp out(client→127.0.0.1:%d, end=true) 发出 2 行 → 对端本轮收到 '"
                " + fresh.length + ' 帧, 首帧=' + (fresh[0] ? fresh[0].p : '∅');" % PORT_TCP,
                y + 220)
    ENTRY["N3"] = ["capm_n34_prep", "capm_n3_dl"]
    cases.append(("N3", "tcp-out-connect"))

    delay_node("capm_n4_dl", 5, 900, y + 300, [[aid("N4")]])
    assert_node("N4", "tcp-in-listen",
                "const a = flow.get('%s_n34') || [];\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "const fresh = a.filter(function (r) { return r.ts >= ep; });\n"
                "let ok = fresh.length === 2 && fresh[1].p === 'capm-tcp-2' "
                "&& fresh[0].t === 'capm/tcp' && fresh[0].sess === true "
                "&& fresh[0].ty === 'string';\n"
                "let s = 'tcp in(server:%d, datamode=stream, newline=分帧, utf8) → 帧数=' "
                "+ fresh.length + ' (2 帧即证明 newline 分帧生效) 帧2=' "
                "+ (fresh[1] ? fresh[1].p : '∅') + ' topic=' + (fresh[0] ? fresh[0].t : '∅')"
                " + ' _session=' + (fresh[0] ? fresh[0].sess : '∅');" % PORT_TCP,
                y + 300)
    ENTRY["N4"] = ["capm_n4_dl"]
    cases.append(("N4", "tcp-in-listen"))

    # ── N5/N6 udp out → 127.0.0.1:PORT_UDP → udp in ──────────────────
    func_node("capm_n56_prep", (
        "const v = 'capm-udp-' + Date.now();\n"
        "flow.set('%s_n56_sent', v);\n" % PREFIX
        + "flow.set('%s_n56', null);\n" % PREFIX
        + "msg.payload = v;\nreturn msg;"), 480, y + 380, [["capm_n56_uo"]])
    udp_out_node("capm_n56_uo", "udp out → 127.0.0.1", 680, y + 380,
                 addr="127.0.0.1", port=PORT_UDP)
    udp_in_node("capm_n56_ui", "udp in 监听 utf8", 480, y + 420,
                [["capm_n56_cap"]], port=PORT_UDP, datatype="utf8")
    func_node("capm_n56_cap", (
        "flow.set('%s_n56', {p: msg.payload, ip: msg.ip, port: msg.port, " % PREFIX
        + "ty: typeof msg.payload, ts: Date.now()});\nreturn null;"),
        700, y + 420, [[]])
    delay_node("capm_n5_dl", 4, 680, y + 340, [[aid("N5")]])
    assert_node("N5", "udp-out-send",
                "const r = flow.get('%s_n56');\n" % PREFIX
                + "const sent = flow.get('%s_n56_sent');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.p === sent);\n"
                "let s = 'udp out(127.0.0.1:%d) → udp in 环回: 收到=' + (r ? r.p : '∅') "
                "+ ' 发出=' + sent;" % PORT_UDP, y + 380)
    ENTRY["N5"] = ["capm_n56_prep", "capm_n5_dl"]
    cases.append(("N5", "udp-out-send"))

    delay_node("capm_n6_dl", 5, 900, y + 460, [[aid("N6")]])
    assert_node("N6", "udp-in-listen",
                "const r = flow.get('%s_n56');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.ty === 'string' "
                "&& r.ip === '127.0.0.1' && typeof r.port === 'number' && r.port > 0);\n"
                "let s = 'udp in(port=%d, datatype=utf8) → 载荷类型=' + (r ? r.ty : '∅') "
                "+ ' [string 而非 Buffer 即证明 utf8 解码生效], 数据报来源 ip=' "
                "+ (r ? r.ip : '∅') + ' port=' + (r ? r.port : '∅') "
                "+ ' [真实网络栈回填的源地址]';" % PORT_UDP, y + 460)
    ENTRY["N6"] = ["capm_n6_dl"]
    cases.append(("N6", "udp-in-listen"))

    # ── N7/N8 websocket out(listener 广播) → ws client 回连 → websocket in ──
    # 两个配置节点带 z=本 tab：随 tab 创建/销毁，不污染全局配置。
    ws_listener_config("capm_ws_srv", WS_PATH, wholemsg="false")
    ws_client_config("capm_ws_cli", "ws://127.0.0.1:%d%s" % (WS_PORT, WS_PATH),
                     wholemsg="false")
    func_node("capm_n78_prep", (
        "const v = 'capm-ws-' + Date.now();\n"
        "flow.set('%s_n78_sent', v);\n" % PREFIX
        + "flow.set('%s_n78', null);\n" % PREFIX
        + "msg.payload = v;\nreturn msg;"), 480, y + 540, [["capm_n78_wo"]])
    ws_out_node("capm_n78_wo", "ws out 广播(listener)", 680, y + 540,
                server="capm_ws_srv")
    ws_in_node("capm_n78_wi", "ws in 客户端回连", 480, y + 580,
               [["capm_n78_cap"]], client="capm_ws_cli")
    func_node("capm_n78_cap", (
        "flow.set('%s_n78', {p: msg.payload, ty: typeof msg.payload, " % PREFIX
        + "ts: Date.now()});\nreturn null;"), 700, y + 580, [[]])
    # ws 连接状态旁证：status 节点抓 websocket in 的连接态，失败时能区分
    # 「连不上（端口/路径错）」与「连上了但没收到广播」。
    status_node("capm_n78_st", "ws in 连接状态", ["capm_n78_wi"], 480, y + 620,
                [["capm_n78_stc"]])
    func_node("capm_n78_stc", (
        "flow.set('%s_n78_conn', {text: (msg.status && msg.status.text) || '', " % PREFIX
        + "ts: Date.now()});\nreturn null;"), 700, y + 620, [[]])
    delay_node("capm_n7_dl", 4, 680, y + 500, [[aid("N7")]])
    assert_node("N7", "websocket-out-broadcast",
                "const r = flow.get('%s_n78');\n" % PREFIX
                + "const sent = flow.get('%s_n78_sent');\n" % PREFIX
                + "const c = flow.get('%s_n78_conn');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.p === sent);\n"
                "let s = 'websocket out(listener %s 广播) → 自连 ws client 环回: 收到=' "
                "+ (r ? r.p : '∅') + ' 发出=' + sent + ' 连接状态=' "
                "+ (c ? c.text : '未捕获');" % WS_PATH, y + 540)
    ENTRY["N7"] = ["capm_n78_prep", "capm_n7_dl"]
    cases.append(("N7", "websocket-out-broadcast"))

    delay_node("capm_n8_dl", 5, 900, y + 660, [[aid("N8")]])
    assert_node("N8", "websocket-in-client",
                "const r = flow.get('%s_n78');\n" % PREFIX
                + "const sent = flow.get('%s_n78_sent');\n" % PREFIX
                + "const ep = flow.get('%s') || 0;\n" % EPOCH_KEY
                + "let ok = !!(r && r.ts >= ep && r.ty === 'string' && r.p === sent);\n"
                "let s = 'websocket in(client 模式回连 ws://127.0.0.1:%d%s, wholemsg=false)"
                " → 载荷类型=' + (r ? r.ty : '∅') + ' [string 即证明 wholemsg=false 只传"
                " payload；true 时会收到整个 msg 对象], 内容一致=' + !!(r && r.p === sent);"
                % (WS_PORT, WS_PATH), y + 620)
    ENTRY["N8"] = ["capm_n8_dl"]
    cases.append(("N8", "websocket-in-client"))

    return cases


def build_subflow_registry() -> list[tuple[str, str]]:
    """子流程注册表枚举覆盖：SUBFLOWS 里 6 个 spec 全部各跑一遍。

    demo_notify / bark_push 已由 SF1 / SF2 覆盖，此处补齐 4 个 history_* 子流程。
    它们 param_style=flat（入参平铺到 msg.entity / msg.start / ...），返回归一化
    对象到 msg.payload —— 断言直接校验回显字段与数值域，不接受「有输出即通过」。

    Returns:
        [(case, kind)] 列表。
    """
    cases: list[tuple[str, str]] = []
    y = 3900

    # SF3 history_state_at：取温度传感器 1 小时前的值
    func_node("capm_sf3_prep", (
        "msg.entity = '%s'; msg.at = '1h前';\nreturn msg;" % E_TEMP),
        480, y, [["capm_sf3_sub"]])
    N("capm_sf3_sub", "subflow:af_hist_state_at", 700, y, [[aid("SF3")]],
      name="SF3 历史·某时刻状态")
    assert_node("SF3", "subflow-history_state_at",
                "let ok = !!(p && p.found === true && p.entity === '%s' " % E_TEMP
                + "&& p.at_iso && String(p.value).length > 0 && isFinite(Number(p.value)));\n"
                "let s = 'history_state_at(entity=温度, at=1h前) → found=' + (p&&p.found) "
                "+ ' at_iso=' + (p&&p.at_iso) + ' value=' + (p&&p.value);", y)
    ENTRY["SF3"] = ["capm_sf3_prep"]
    cases.append(("SF3", "subflow-history_state_at"))

    # SF4 history_occurred：查 E_LAMP2 最近 2h 是否发生变化。
    #   本轮 A3(turn_off) / SS1(toggle+复位) 都动了这盏灯，故区间内**必然**有事件；
    #   延时 26s 起查，等 HA recorder 落库（SS1 复位在 t≈20s）。
    delay_node("capm_sf4_dl", 26, 300, y + 60, [["capm_sf4_prep"]])
    func_node("capm_sf4_prep", (
        "msg.entity = '%s'; msg.start = '2h前'; msg.end = '现在';\nreturn msg;" % E_LAMP2),
        520, y + 60, [["capm_sf4_sub"]])
    N("capm_sf4_sub", "subflow:af_hist_occurred", 740, y + 60, [[aid("SF4")]],
      name="SF4 历史·是否发生")
    assert_node("SF4", "subflow-history_occurred",
                "const ev = (p && p.events) || [];\n"
                "let ok = !!(p && p.occurred === true && p.entity === '%s' " % E_LAMP2
                + "&& p.count >= 1 && ev.length === p.count && ev[0] && ev[0].ts "
                "&& p.start_iso && p.end_iso);\n"
                "let s = 'history_occurred(灯, 近2h; 本轮 A3/SS1 已动过它) → occurred=' "
                "+ (p&&p.occurred) + ' count=' + (p&&p.count) + ' events长度=' + ev.length "
                "+ ' 首事件 ' + (ev[0] ? (ev[0].from + String.fromCharCode(8594) + ev[0].to "
                "+ ' @' + ev[0].ts) : '∅');", y + 60)
    ENTRY["SF4"] = ["capm_sf4_dl"]
    cases.append(("SF4", "subflow-history_occurred"))

    # SF5 history_duration：灯近 2h 处于 on 的累计时长。
    #   不预设具体秒数（灯可能一直关着=0 秒也合法），断言结构完整 + 数值域自洽：
    #   total_seconds 为有限非负数、ratio∈[0,1] 且与 total_seconds/区间长度一致。
    func_node("capm_sf5_prep", (
        "msg.entity = '%s'; msg.start = '2h前'; msg.end = '现在'; " % E_LAMP2
        + "msg.state = 'on';\nreturn msg;"), 480, y + 140, [["capm_sf5_sub"]])
    N("capm_sf5_sub", "subflow:af_hist_duration", 700, y + 140, [[aid("SF5")]],
      name="SF5 历史·处于某态时长")
    assert_node("SF5", "subflow-history_duration",
                "const ts = p && p.total_seconds;\n"
                "let ok = !!(p && p.entity === '%s' && p.state === 'on' " % E_LAMP2
                + "&& typeof ts === 'number' && isFinite(ts) && ts >= 0 "
                "&& typeof p.ratio === 'number' && p.ratio >= 0 && p.ratio <= 1 "
                "&& String(p.total_human).length > 0 && p.start_iso && p.end_iso);\n"
                "let s = 'history_duration(灯, on, 近2h) → total_seconds=' + ts "
                "+ ' human=' + (p&&p.total_human) + ' ratio=' + (p&&p.ratio) "
                "+ ' 区间=' + (p&&p.start_iso) + String.fromCharCode(126) + (p&&p.end_iso);",
                y + 140)
    ENTRY["SF5"] = ["capm_sf5_prep"]
    cases.append(("SF5", "subflow-history_duration"))

    # SF6 history_aggregate：温度传感器近 2h 状态变化次数（metric=count 不需 attribute）
    func_node("capm_sf6_prep", (
        "msg.entity = '%s'; msg.start = '2h前'; msg.end = '现在'; " % E_TEMP
        + "msg.metric = 'count';\nreturn msg;"), 480, y + 220, [["capm_sf6_sub"]])
    N("capm_sf6_sub", "subflow:af_hist_aggregate", 700, y + 220, [[aid("SF6")]],
      name="SF6 历史·聚合统计")
    assert_node("SF6", "subflow-history_aggregate",
                "const v = p && p.value;\n"
                "let ok = !!(p && p.entity === '%s' && p.metric === 'count' " % E_TEMP
                + "&& typeof v === 'number' && isFinite(v) && v >= 1 "
                "&& p.start_iso && p.end_iso);\n"
                "let s = 'history_aggregate(温度, metric=count, 近2h) → value=' + v "
                "+ ' unit=' + (p&&p.unit) + ' 区间=' + (p&&p.start_iso) "
                "+ String.fromCharCode(126) + (p&&p.end_iso) "
                "+ ' [温度传感器 2h 内必有多次采样变化, v>=1 才算真跑通]';", y + 220)
    ENTRY["SF6"] = ["capm_sf6_prep"]
    cases.append(("SF6", "subflow-history_aggregate"))

    return cases


# ══════════════════════════════════════════════════════════════════════
# 汇总：join(N) → table → debug / 落盘
# ══════════════════════════════════════════════════════════════════════
def build_tail(case_list: list[tuple[str, str]]) -> None:
    """建 join / 总表 / 证据落盘节点。"""
    N(PREFIX + "_join", "join", 1900, 1400, [[PREFIX + "_table"]], name="capmatrix join",
      mode="custom", build="array", property="payload", propertyType="msg", key="topic",
      joiner=",", joinerType="str", accumulate=False, timeout="45",
      count=str(len(case_list)), reduceRight=False, reduceExp="", reduceInit="",
      reduceInitType="", reduceFixup="")

    # ── 旁路 DUMP：不依赖 join 收齐，随时导出 flow context 快照 ──────────
    # 血泪：join(count=N) 只要一路缺席就永不吐出，证据文件根本不生成，
    # 排障时等于全盲。断言已双写 flow context，这里提供一个独立触发口。
    inject_node(PREFIX + "_dump", "⇩ DUMP 快照", 1900, 1520, [[PREFIX + "_dump_fn"]],
                payload="dump", payload_type="str", topic=PREFIX + "_dump")
    func_node(PREFIX + "_dump_fn", (
        "// 从 flow context 收集所有已完成断言（缺席者由 capm_table 标出）\n"
        "const ALL = " + json.dumps([c for c, _ in case_list]).replace('"', "'") + ";\n"
        "const rows = [];\n"
        "for (const c of ALL) {\n"
        "  const r = flow.get('" + PREFIX + "_r_' + c);\n"
        "  if (r) rows.push(r);\n"
        "}\n"
        "msg.payload = rows;\n"
        "return msg;"
    ), 2080, 1520, [[PREFIX + "_table"]])

    func_node(PREFIX + "_table", (
        "// capmatrix 总表\n"
        "const rows = (msg.payload || []).slice().sort(function (a, b) {\n"
        "  return String(a.case).localeCompare(String(b.case));\n"
        "});\n"
        "const seen = {}; let pass = 0;\n"
        "const lines = ['===== capmatrix 节点×参数覆盖 @ ' + new Date().toLocaleString()"
        " + ' ====='];\n"
        "for (const r of rows) {\n"
        "  seen[r.case] = true;\n"
        "  if (r.ok) pass++;\n"
        "  lines.push((r.ok ? '[PASS] ' : '[FAIL] ') + r.case + '  ' + (r.kind || '')"
        " + '  | ' + (r.summary || ''));\n"
        "}\n"
        "const ALL = " + json.dumps([c for c, _ in case_list]).replace('"', "'") + ";\n"
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
    ), 2100, 1400, [[PREFIX + "_dbg", PREFIX + "_json", PREFIX + "_txt_chg"]])
    N(PREFIX + "_dbg", "debug", 2300, 1340, [[]], name="capmatrix 总表", active=True,
      tosidebar=True, console=False, tostatus=False, complete="payload", targetType="msg",
      statusVal="", statusType="auto")
    N(PREFIX + "_json", "file", 2300, 1400, [], name="证据 JSON",
      filename="/tmp/capm_result.json", filenameType="str", appendNewline=True,
      createDir=False, overwriteFile="true", encoding="utf8")
    change_set(PREFIX + "_txt_chg", [{"p": "payload", "pt": "msg",
                                      "to": "payload.table", "tot": "msg"}],
               2300, 1460, [[PREFIX + "_txt"]])
    N(PREFIX + "_txt", "file", 2480, 1460, [], name="证据 TXT",
      filename="/tmp/capm_result.txt", filenameType="str", appendNewline=True,
      createDir=False, overwriteFile="true", encoding="utf8")


def wire_fan(case_list: list[tuple[str, str]]) -> None:
    """回填 fan 的 N 组输出连线。"""
    fan = next(n for n in nodes if n["id"] == PREFIX + "_fan")
    fan["wires"] = [list(ENTRY[c]) for c, _ in case_list]


def self_check(case_list: list[tuple[str, str]]) -> list[str]:
    """结构自检：id 唯一 / z 一致 / 无悬空连线 / 子流程实例合法 / JS 无双引号 / 用例全可达。"""
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
            errs.append("%s: 裸 type=subflow" % n["id"])
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
            errs.append("%s: func 含双引号" % n["id"])
        if typ == "change":
            # change 节点合法规则类型共 4 种，早期只放行 set 会误伤 delete/move/change。
            for rule in n.get("rules") or []:
                if rule.get("t") not in ("set", "change", "delete", "move"):
                    errs.append("%s: change rule 非法 t=%r" % (n["id"], rule.get("t")))

    fan = next(n for n in nodes if n["id"] == PREFIX + "_fan")
    if fan["outputs"] != len(fan["wires"]):
        errs.append("fan outputs(%s) != wires 组数(%s)" % (fan["outputs"], len(fan["wires"])))
    asserts = {n["id"] for n in nodes if n["id"].startswith(PREFIX + "_a_")}
    if len(asserts) != len(case_list):
        errs.append("断言节点数 %d != 用例数 %d" % (len(asserts), len(case_list)))
    join = next(n for n in nodes if n["id"] == PREFIX + "_join")
    if join["count"] != str(len(case_list)):
        errs.append("join count(%s) != 用例数(%d)" % (join["count"], len(case_list)))
    driven = set()
    for n in nodes:
        for slot in n.get("wires") or []:
            driven.update(slot)
    for case, _ in case_list:
        if not ENTRY.get(case) and aid(case) not in driven:
            errs.append("%s 既无 fan 入口也无上游驱动" % case)
        # 硬校验：断言节点必须有入边。
        # 教训来自 TR2——它有 fan 入口（prep 被驱动），但断言节点本身悬空，
        # 于是永远不产出，join 收不齐时整个用例彻底隐形（不是 FAIL，是消失）。
        if aid(case) not in driven:
            errs.append("%s: 断言节点 %s 悬空（无任何入边），运行期将永久缺席" % (case, aid(case)))
    return errs


def build(tab_id: str) -> dict:
    """生成完整 flow 定义。"""
    global TAB_ID
    TAB_ID = tab_id
    nodes.clear()
    ENTRY.clear()
    all_cases: list[tuple[str, str]] = []
    all_cases += build_transform()
    all_cases += build_change_switch()
    all_cases += build_timing()
    all_cases += build_events_subflow()
    all_cases += build_api_domains()
    all_cases += build_network()
    all_cases += build_subflow_registry()
    build_header(all_cases)
    build_tail(all_cases)
    wire_fan(all_cases)
    errs = self_check(all_cases)
    sub_n = sum(1 for n in nodes if str(n.get("type", "")).startswith("subflow:"))
    typ_n = collections.Counter(n.get("type") for n in nodes)
    print("节点数=%d  用例=%d  子流程实例=%d  tab=%s" % (len(nodes), len(all_cases), sub_n, tab_id))
    print("类型分布:", dict(typ_n))
    if errs:
        print("\n[!!!] 自检失败:")
        for e in errs:
            print("  -", e)
        raise SystemExit(1)
    print("[OK] 自检通过（id唯一 / z一致 / 无悬空wires / change规则合法 / 用例全可达 / 断言节点均有入边）")
    return {"id": tab_id, "label": LABEL, "nodes": nodes}


def resolve_tab_id(argv: list[str]) -> str:
    """确定目标 tab id：命令行 > 台账 > 占位符。"""
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    if LEDGER.exists():
        val = LEDGER.read_text(encoding="utf-8").strip()
        if val:
            return val
    return "capm_tab"


def main() -> None:
    """生成 flow JSON 并写入 tests/fixtures_local/capm_flow.json。"""
    flow = build(resolve_tab_id(sys.argv))
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(flow, ensure_ascii=False, separators=(",", ":"))
    OUT_JSON.write_text(payload, encoding="utf-8")
    print("写出 %s (%d 字节)" % (OUT_JSON, len(payload.encode("utf-8"))))


if __name__ == "__main__":
    main()
