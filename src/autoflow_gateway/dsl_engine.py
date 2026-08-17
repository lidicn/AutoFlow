"""AutoFlow DSL 引擎（P3 第一代码模块）。

职责：把 agent 输出的**语义 DSL**（见 docs/dsl_design.md）翻译成合法 Node-RED 导出 JSON。
- parse(text)        : DSL 文本 → 场景 AST（行式、零依赖解析器）
- validate(scene)    : 静态校验（子流程存在 / 必填入参 / 枚举合法 / 无 Function）
- compile(scene)     : AST → NR flow 导出结构（自动分配 id、排版坐标、引用子流程）

设计铁律（§18.3）：引擎**绝不生成 Function 节点**。任何逻辑（队列/角色识别/分支）要么落到
专用节点，要么落到预建子流程（subflows.py）。本引擎只生成：inject / api-call-service /
switch / delay / change / link out / subflow 实例。

输出形状与 NR `/flow/:id` 接口兼容（与 data/tts_queue_flow.json 实测结构一致）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .subflows import get_subflow, SubflowSpec, HISTORY_SUBFLOW_IDS
from .flow_linter import lint_flow, _check_jsonata, _strip_strings_and_comments


# ── 实体解析钩子（友好名/语义标签 → entity_id）─────────────────────────────
# DSL 设计 §2：实体用语义标签或原始 entity_id 均可，引擎经 Tier2 实体映射解析。
# 解析器由网关注入（gateway.state.resolve），引擎本身零依赖、不持有映射。
_entity_resolver = None


def set_entity_resolver(fn):
    """注入 友好名/标签 → entity_id 解析器（网关在 compile 前调用）。"""
    global _entity_resolver
    _entity_resolver = fn


def _resolve_entity(target: str) -> str:
    if _entity_resolver is None:
        return target
    try:
        r = _entity_resolver(target)
    except Exception:
        r = None
    return r or target


# ── 实体属性解析钩子（entity_id → 已知属性名集合）──────────────────────────
# 用于编译期属性名校验（WB24 NEW-F1 defense-in-depth）：子流程/取值引用某实体的
# 属性时，若解析器可用且能拿到该实体的属性集合，则校验属性名是否合法。
# 解析器返回 None 表示「无法判定」（未知实体/取属性失败）→ 调用方跳过校验（fail-open）。
_entity_attributes_resolver = None


def set_entity_attributes_resolver(fn):
    """注入 entity_id → 已知属性名集合 解析器（网关在 compile 前调用）。

    fn(entity_id) -> set[str] | None：返回该实体已知属性名集合；返回 None 或抛异常
    均表示「无法判定」，调用方据此跳过校验（fail-open），绝不因属性查询失败而阻断编译。
    """
    global _entity_attributes_resolver
    _entity_attributes_resolver = fn


def _is_template_entity(s: str) -> bool:
    """判断是否为模板占位实体（<TEMP> / {{entity}} 等）—— 这类不进入 catalog，跳过属性/存在性校验。"""
    if not s:
        return True
    return ("{{" in s) or (s.startswith("<") and s.endswith(">"))


def _resolve_entity_attributes(entity: str) -> Optional[set]:
    """返回实体已知属性名集合；无法判定（无解析器/模板占位/未知实体/失败）返回 None。

    fail-open：任何异常都返回 None，调用方据此跳过校验，绝不因属性查询失败而阻断编译。
    """
    if _entity_attributes_resolver is None:
        return None
    if _is_template_entity(entity):
        return None
    eid = _resolve_entity(entity)  # 先尽量解析友好名 → entity_id
    if not eid or _is_template_entity(eid):
        return None
    try:
        attrs = _entity_attributes_resolver(eid)
    except Exception:
        return None
    return attrs


# ── 原生节点逃逸白名单（Phase 4）────────────────────────────────
# 逃生舱：DSL 表达不了的 20%（复合 AND/OR、特殊 contrib 节点等）由 agent 直接
# 嵌手写 NR 节点 JSON 兜。但必须白名单约束节点类型，永久禁止 function（铁律）
# 与 exec（RCE 风险）。增删类型需同步更新 gateway.dsl_help + docs/dsl_design.md。
RAW_NODE_ALLOWED = {
    # 核心流控 / 变换
    "switch", "change", "template", "delay", "debug", "inject",
    # ⚠ 此处曾有 "merge"：Node-RED 核心从未提供该节点类型（1880/1990 两实例
    #   GET /nodes 均查无，合并职责由 join 承担）。放行它等于给 agent 埋雷——
    #   flow 里只要出现一个 merge 节点，NR 会打印 "Waiting for missing types to
    #   be registered: - merge" 并让**整个 tab 拒绝启动**（inject 触发 404），
    #   且部署接口返回 200，属静默失败。已移除；回归见
    #   tests/regression/reg_m/capmatrix_probe_types.py（白名单 × 运行时注册表比对）。
    "join", "split", "sort", "batch",
    "csv", "html", "xml", "json", "yaml", "range",
    # 事件 / 状态 / 观测
    "status", "catch", "complete", "trigger",
    "link in", "link out",
    # 网络 / 消息
    "mqtt in", "mqtt out", "websocket in", "websocket out",
    "tcp in", "tcp out", "udp in", "udp out",
    # HA 原生
    "api-call-service", "server-state-changed",
}
# 永久禁止：function（铁律：绝不生成 Function 节点）/ exec（任意命令执行，RCE 风险）
RAW_NODE_FORBIDDEN = {"function", "exec"}


# 编译错误码表（前缀 C_），与 flow_linter 的 R* 规则码对齐，便于 agent 机读自修正。
# 每个码对应一类 agent 可据以自我修正的语法/语义错误；未显式标注的 raise 落 C_PARSE 兜底。
C_DEPRECATED = "C_DEPRECATED"            # 使用了已废弃原语
C_INDENT_TOPLEVEL = "C_INDENT_TOPLEVEL"  # 顶层指令被缩进写在块体内
C_INDENT_BLOCK = "C_INDENT_BLOCK"        # 缩进位置/归属错误（否则 位置错）
C_INDENT_BODY = "C_INDENT_BODY"          # 缩进体内出现未知指令
C_UNKNOWN_TOPLEVEL = "C_UNKNOWN_TOPLEVEL"  # 无法识别的顶层指令
C_MISSING_TRIGGER = "C_MISSING_TRIGGER"  # 缺少 触发 指令
C_RAW_JSON_NEED = "C_RAW_JSON_NEED"      # 原生节点需跟 JSON 对象
C_RAW_JSON_INVALID = "C_RAW_JSON_INVALID"  # 原生节点 JSON 非法
C_RAW_JSON_TYPE = "C_RAW_JSON_TYPE"      # 原生节点需是 JSON 对象（非数组/标量）
C_RAW_NO_TYPE = "C_RAW_NO_TYPE"          # 原生节点缺 type 字段
C_RAW_FORBIDDEN = "C_RAW_FORBIDDEN"      # 原生节点禁用类型（function/exec）
C_RAW_UNKNOWN_TYPE = "C_RAW_UNKNOWN_TYPE"  # 原生节点类型不在白名单
C_TRIGGER_FORMAT = "C_TRIGGER_FORMAT"    # 触发格式无法解析
C_ACTION_FORMAT = "C_ACTION_FORMAT"      # 动作格式错（应为 domain.service(target,k=v)）
C_ACTION_NO_DOMAIN = "C_ACTION_NO_DOMAIN"  # 动作缺 domain.service
C_ACTION_NO_TARGET = "C_ACTION_NO_TARGET"  # 动作缺目标实体
C_ACTION_ARRAY_NOBRACKET = "C_ACTION_ARRAY_NOBRACKET"  # 目标数组缺右括号 ]
C_ACTION_ARRAY_EMPTY = "C_ACTION_ARRAY_EMPTY"  # 目标数组为空
C_SUBFLOW_FORMAT = "C_SUBFLOW_FORMAT"    # 子流程调用格式错（应为 name(k=v)）
C_SUBFLOW_UNKNOWN = "C_SUBFLOW_UNKNOWN"  # 未知子流程（未在 subflows.py 注册）
C_SUBFLOW_ARG = "C_SUBFLOW_ARG"        # 子流程调用参数错误（必填缺失/枚举非法/未知参数）
C_SUBFLOW_ATTR_UNKNOWN = "C_SUBFLOW_ATTR_UNKNOWN"  # 子流程调用的 attribute 参数非实体已知属性
C_DELAY_FORMAT = "C_DELAY_FORMAT"        # 延时格式错
C_QUERY_FORMAT = "C_QUERY_FORMAT"        # 查询格式错
C_READ_FORMAT = "C_READ_FORMAT"          # 取值格式错
C_READ_RESERVED = "C_READ_RESERVED"      # 取值读取了保留字（payload/msg 等）
C_TIMERANGE_FORMAT = "C_TIMERANGE_FORMAT"  # 时间段格式错
C_REQUEST_FORMAT = "C_REQUEST_FORMAT"    # 请求格式错
C_REQUEST_PARAM = "C_REQUEST_PARAM"      # HTTP 参数解析错
C_EXTRACT_FORMAT = "C_EXTRACT_FORMAT"    # 提取格式错
C_EXTRACT_EMPTY = "C_EXTRACT_EMPTY"      # 提取字段名/表达式为空
C_BUILD_FORMAT = "C_BUILD_FORMAT"        # 构建格式错
C_MULTI_ERROR = "C_MULTI_ERROR"          # 多个解析错误聚合
C_UNKNOWN_STEP = "C_UNKNOWN_STEP"        # 暂不支持的步骤类型
C_SEMANTIC_GAP = "C_SEMANTIC_GAP"        # 语义缺口（高声拒绝，避免静默降级）
C_SELFCHECK = "C_SELFCHECK"              # 编译自检发现错误级问题
C_JSONATA_SYNTAX = "C_JSONATA_SYNTAX"    # 分支/条件 JSONata 语法断裂（WB24-N5 收口：编译期预检，防静默恒假）
C_HISTORY_CLOBBER = "C_HISTORY_CLOBBER"  # Defect B（iss_60e4d57ce8）：串行调用多个 history_* 子流程互相覆盖 msg.payload → 下游分支永假、静默失败
C_COMPARE_TYPE_WARN = "C_COMPARE_TYPE_WARN"  # 数值比较未包 $number()（state 为字符串，比较可能恒假）
C_ENTITY_UNRESOLVED = "C_ENTITY_UNRESOLVED"  # 中文/友好实体名未解析为 entity_id（原样写入 NR 必然找不到实体）
C_PARSE = "C_PARSE"                      # 兜底（未归类解析错误）


class DSLError(Exception):
    """DSL 解析/校验失败，携带行号 + 错误码 + 自修正提示，便于 agent 机读自修正。

    code：C_* 错误码（见上方常量），与 lint 的 R* 码同级可机读。
    hint：一句话「怎么改」；若未显式传入，自动从 message 的「（建议：…）」后缀抽取，
          绝大多数既有 raise 已内嵌建议文本，故 hint 免费可用。
    """

    def __init__(self, message: str, line: Optional[int] = None,
                 code: str = C_PARSE, hint: Optional[str] = None):
        self.line = line
        self.code = code
        self.hint = hint if hint is not None else _extract_hint(message)
        super().__init__(f"第 {line} 行: {message}" if line else message)


def _extract_hint(message: str) -> str:
    """从 message 的「（建议：…）」后缀抽取自修正提示（无则空串）。"""
    if "（建议：" in message:
        return message[message.index("（建议："):]
    return ""


# ── AST ───────────────────────────────────────────────────────────────────
@dataclass
class Trigger:
    kind: str            # "state" | "time" | "inject"
    entity: Optional[str] = None
    state: Optional[str] = None   # 具体状态值；"*" 表示任意变化
    cron: Optional[str] = None
    raw: str = ""
    first: bool = False   # 「首次」修饰：上升沿触发（outputOnlyOnStateChange）
    for_minutes: Optional[float] = None  # 「持续 N 分钟」持久等待：折算成分钟；None 表示无延时
    inject_props: dict[str, str] = field(default_factory=dict)  # inject 自定义 props（如 _src、payload）


@dataclass
class Action:
    domain: str
    service: str
    target: str                       # 主目标（多实体时取 targets[0]，兼容旧消费方）
    targets: list = field(default_factory=list)  # 多实体：[light.a, light.b] 展开（B4 修复）
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class SubflowCall:
    name: str
    raw_args: dict[str, str] = field(default_factory=dict)
    jsonata_args: set[str] = field(default_factory=set)  # 取值为 JSONata 表达式的参数名
    line: int = 0  # 所在 DSL 行号，供编译错误定位


@dataclass
class Extract:
    """从上游（通常是 http 响应 msg.payload）抽取字段到 msg。<name> = <jsonata表达式>。"""
    name: str
    expr: str   # JSONata 表达式，相对 msg 求值（如 payload.result.realtime.temperature）


@dataclass
class Delay:
    seconds: int


@dataclass
class ReadState:
    """读取实体状态数值并写入 msg.<field>（或 msg.payload）。
    DSL: 取值: <entity> [<field>]
    复用 api-current-state（仅透传+输出 state），不生成 Function 节点。
    field 缺省→写 msg.payload；否则写 msg.<field>（多传感器互不覆盖）。"""
    entity: str
    field: Optional[str] = None


@dataclass
class CurrentState:
    """查询实体当前状态：编译为 api-current-state（2 输出 pass/fail）。
    DSL: 查询: <entity> <state>  → pass 输出继续主链(body)，fail 输出走 else_body。
    body 为「主链/通过分支」（缩进写在门体下），else_body 为「否则分支」。"""
    entity: str
    state: str
    body: list = field(default_factory=list)        # pass 分支（主链）
    else_body: list = field(default_factory=list)      # fail 分支（否则）


@dataclass
class TimeRange:
    """时间段条件门：编译为 time-range-switch 节点（2 输出：窗口内 out0 继续主链，窗口外 out1 空=停止）。
    DSL: 时间段: 07:00-23:00  → 在时间段内继续主链(body)，不在则停止。
    可加星期前缀/后缀限定星期：时间段: 工作日 20:00-23:00 / 时间段: 20:00-23:00 周末。
    body 为「主链/通过分支」（缩进写在门体下），else_body 为「窗口外分支」（否则体）。

    ★FEEDBACK #9 修复：解析层（_parse 顶层块栈）对所有门型步骤都会把 `否则:` 的
    步骤 append 到 `gate.else_body`，但本 dataclass 此前**没有该字段** →
    `时间段:` 后跟 `否则:` 直接 AttributeError 编译期崩溃（且抛的是 Python 内部
    异常，用户看不懂）。time-range-switch 本身就有 out1=窗口外，语义天然对应
    「否则」，故补齐字段并在 emit 层接到 out1（而非禁用该语法）。"""
    start: str
    end: str
    weekday: Optional[str] = None   # 映射 time-range-switch 的 only 属性：weekdays/weekends/all/monday..sunday
    body: list = field(default_factory=list)        # 通过分支（主链，窗口内 out0）
    else_body: list = field(default_factory=list)   # 窗口外分支（否则体，out1）


@dataclass
class Branch:
    condition: str
    body: list = field(default_factory=list)


@dataclass
class Switch:
    branches: list[Branch] = field(default_factory=list)
    else_body: list = field(default_factory=list)


@dataclass
class Parallel:
    children: list = field(default_factory=list)


@dataclass
class Debug:
    name: str = "观测"


@dataclass
class Comment:
    text: str = ""


@dataclass
class HttpRequest:
    method: str
    url: str
    body: Optional[str] = None   # JSON 字符串（POST 时）
    headers: list[dict] = field(default_factory=list)  # [{"key":"...","value":"...","type":"text"}]
    payload: Optional[str] = None  # 覆盖 msg.payload 的表达式（JSONata/字面量）


@dataclass
class Build:
    """构建请求体：把 msg.payload 设置为一个 JSON 对象（字面量）或 JSONata 表达式求值结果。
    下游『请求』节点在不带字面 body 时会自动把 msg.payload 作为请求体发送——
    从而让黑箱能等价产出白箱的 function 拼装 body 类 flow，且不破『不生成 Function 节点』铁律。"""
    kind: str = "json"          # "json"（字面量）| "jsonata"（表达式）
    literal: Any = None         # kind=="json"：原生 dict/list
    expr: str = ""              # kind=="jsonata"：JSONata 表达式（相对 msg 求值）


@dataclass
class RawNode:
    """原生节点逃逸（Phase 4）：嵌手写 NR 节点 JSON，兜 DSL 表达不了的 20%。

    - node_type：白名单内节点类型（function/exec 被铁律永久禁止，见 RAW_NODE_FORBIDDEN）。
    - config：节点配置（除 id/z/x/y/wires/type 外的字段，这些由引擎托管）。
    接线约定：逃生节点只接**输出 0**（单 output 默认连线；多 output 如 switch 仅 out0 生效，
    其余输出口不连——复杂多分支请用 DSL 原生 分支:/否则:）。"""
    node_type: str
    config: dict = field(default_factory=dict)


@dataclass
class _SegmentBreak:
    """哨兵：标记多入口场景的新链路起点。出现在此后方的触发器属于独立链路，
    不再 fan-in 到前一段的 HTTP/首节点。trigger_count_before 记录此断点之前
    已收集的触发器数量，供发射器分段分配源节点。"""
    trigger_count_before: int = 0


Step = Any  # Action | SubflowCall | Delay | ReadState | Switch | Parallel | Debug | Comment | HttpRequest | Extract | Build | _SegmentBreak


@dataclass
class Scene:
    name: str
    triggers: list[Trigger] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    body: list[Step] = field(default_factory=list)
    expected: list[dict] = field(default_factory=list)  # 后置条件：[{entity_id,state}] 或 [{subflow}]
    _first_break_count: int = 0  # 第一个 _SegmentBreak 之前已收集的触发器数（0=无断点=单段）

    @property
    def trigger(self) -> Optional[Trigger]:
        """向后兼容：取首个触发（单触发场景）。"""
        return self.triggers[0] if self.triggers else None


# ── 关键字表（中英双语归一）───────────────────────────────────────────────
KW = {
    "场景": "scene", "scene": "scene",
    "触发": "trigger", "trigger": "trigger",
    "条件": "condition", "if": "condition", "when": "condition",
    "变量": "let", "let": "let",
    "动作": "action", "do": "action", "action": "action",
    "调用子流程": "subflow", "subflow": "subflow",
    "分支": "branch", "case": "branch", "switch": "branch",
    "否则如果": "elif", "否则若": "elif", "elif": "elif",
    "否则": "else", "else": "else",
    "延时": "delay", "delay": "delay",
    "查询": "current_state", "check": "current_state",
    "时间段": "time_range", "time_range": "time_range",
    "并行": "parallel", "parallel": "parallel",
    "观测": "debug", "debug": "debug",
    "注释": "comment", "comment": "comment",
    "请求": "http", "http": "http",
    "构建": "build", "build": "build", "组装": "build", "拼装": "build",
    "提取": "extract", "解析": "extract", "extract": "extract",
    "取值": "read_state", "读取": "read_state", "read": "read_state",
    "原生节点": "raw_node", "raw_node": "raw_node",
    "预期": "expected", "expect": "expected", "后置条件": "expected",
    "postcondition": "expected", "expected": "expected",
}

# ── 状态别名映射（DSL 人类可读 → HA 真实 state 值）──────────────────────
# binary_sensor 的真实状态是 on/off，不是「有人/无人」；light 也是 on/off 不是「开/关」。
# 编译 _emit_trigger 时用此表归一化，避免 ifState 写了中文导致 NR 永远不匹配。
_STATE_ALIAS = {
    # binary_sensor / motion / occupancy / door（on = 有事件）
    "有人": "on", "检测到": "on", "motion": "on", "occupied": "on",
    "打开": "on", "open": "on", "激活": "on", "active": "on",
    "开": "on", "亮": "on",
    # off = 无事件 / 复位
    "无人": "off", "未检测到": "off", "clear": "off", "unoccupied": "off",
    "关闭": "off", "close": "off", "灭": "off", "关": "off",
    "inactive": "off", "deactivated": "off",
}


def _norm_kw(line: str) -> Optional[str]:
    for cn, en in KW.items():
        if line.startswith(cn):
            return en
    return None


def _after_colon(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


# ── 已废弃原语拦截（2026-07-20：历史查询改走 调用子流程: history_*）────────
# 旧『历史:』原语只能拉原始历史数组、无法做时间点取值/区间判定/时长统计/聚合电量，
# 且常静默降级成读当前态（语义全反）。现以 4 个请求/响应子流程替代，故彻底移除该原语，
# 命中即高声报错并指向正确替代方案，避免 agent 撞『无法识别的顶层指令』硬墙。
_DEPRECATED_PRIM_MSG = (
    "『历史:』原语已废弃（改用请求/响应子流程查询历史）。它只能拉原始历史数组，"
    "无法做时间点取值/区间判定/时长统计/聚合电量，且常静默降级成读当前态。请改用：\n"
    "  调用子流程: history_state_at(entity=<实体>, at=<时刻>)        # 某时刻的值\n"
    "  调用子流程: history_occurred(entity=<实体>, start=<起>, end=<止>[, state=目标态])  # 区间是否发生\n"
    "  调用子流程: history_duration(entity=<实体>, start=<起>, end=<止>, state=<态>)     # 处于某态时长\n"
    "  调用子流程: history_aggregate(entity=<实体>, start=<起>, end=<止>, metric=energy|mean|...)  # 聚合\n"
    "返回值在 msg.payload，下游用『提取: <字段> = payload.x』或『分支:』读取"
    "（详见 autoflow_dsl_help 的『历史查询』示例）。"
)


def _check_deprecated(stripped: str, line: int):
    """命中已废弃原语即抛 DSLError 指向替代方案。"""
    low = stripped.lower()
    if low.startswith("历史:") or low.startswith("history:") \
       or low.startswith("历史 ") or low.startswith("history "):
        raise DSLError(_DEPRECATED_PRIM_MSG, line, code=C_DEPRECATED)


# ── 解析器 ─────────────────────────────────────────────────────────────────
def _parse_action_or_subflow(s: str, line: int) -> Step:
    """动作: 行容错分发。

    agent 常把子流程调用误写在 动作: 行（如 动作: 调用子流程.bark_push(...)）。
    若内容以「调用子流程.」或「调用子流程:」开头，路由到 _parse_subflow 正确编译成
    子流程节点；否则按原 HA 服务调用解析。避免被当成 domain=调用子流程 的非法服务调用，
    静默降级成坏掉的 api-call-service 节点（entityId 被塞入字面量、部署后静默失效）。
    """
    s = s.strip()
    if s.startswith("调用子流程.") or s.startswith("调用子流程:"):
        sub = s[len("调用子流程"):].lstrip(".:").strip()
        return _parse_subflow(sub, line)
    return _parse_action(s, line)


def parse(text: str) -> Scene:
    lines = text.splitlines()
    scene = Scene(name="未命名场景")
    expecting_expected: bool = False
    _TOP_KW = {"scene", "trigger", "condition", "let", "branch", "elif", "else",
               "parallel", "action", "subflow", "delay", "expected", "extract", "build",
               "current_state", "time_range", "read_state", "history"}
    _BLOCK_KW = ("branch", "elif", "else")

    # ── 上下文栈：支持任意层级嵌套（分支体内再分支 / 门体 / 并行体）──
    # 每帧描述「当前缩进块」的 attach 目标与 dedent 边界：
    #   kind = "top" | "switch" | "gate" | "parallel"
    #   indent = 本块 opener 行的缩进；parent_indent = 父块体缩进（dedent 到此即弹出本帧）
    # 叶子步骤（动作/调用子流程/…）在 indent == parent_indent 边界会弹出本帧回到父块；
    # 块关键字（分支/否则如果/否则）在边界不弹出，作为父块内的同级块（多路 if/elif/else）。
    class _Frame:
        __slots__ = ("kind", "indent", "parent_indent", "switch", "branch",
                     "is_else", "gate", "parallel")
        def __init__(self, kind, indent, parent_indent, switch=None, branch=None,
                     is_else=False, gate=None, parallel=None):
            self.kind = kind
            self.indent = indent
            self.parent_indent = parent_indent
            self.switch = switch
            self.branch = branch
            self.is_else = is_else
            self.gate = gate
            self.parallel = parallel
    _TOP = _Frame("top", -1, -1)
    _ctx: list = [_TOP]

    def attach(step: Step):
        top = _ctx[-1]
        if top.kind == "top":
            scene.body.append(step)
        elif top.kind == "parallel":
            top.parallel.children.append(step)
        elif top.kind == "switch":
            if top.is_else:
                top.switch.else_body.append(step)
            elif top.branch is not None:
                top.branch.body.append(step)
            else:
                scene.body.append(step)
        elif top.kind == "gate":
            if top.is_else:
                top.gate.else_body.append(step)
            else:
                top.gate.body.append(step)

    def _push_switch(indent: int, cond: str):
        sw = Switch()
        attach(sw)
        br = Branch(condition=cond)
        sw.branches.append(br)
        parent_indent = -1 if _ctx[-1].kind == "top" else indent
        _ctx.append(_Frame("switch", indent, parent_indent, switch=sw, branch=br))

    def _append_branch(cond: str):
        top = _ctx[-1]
        br = Branch(condition=cond)
        top.switch.branches.append(br)
        top.branch = br
        top.is_else = False

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        kw = _norm_kw(stripped)

        # ── 预期后置条件块（indent>0 或 非顶层关键字行都算后置条件）──
        if expecting_expected and (indent > 0 or kw not in _TOP_KW):
            scene.expected.append(_parse_expected_condition(stripped))
            i += 1
            continue
        # 顶层专属指令不允许缩进写在块体内
        if indent > 0 and kw in ("scene", "trigger", "condition", "let"):
            raise DSLError(f"『{stripped}』是顶层指令，不能缩进写在块体内"
                           f"（建议：场景/触发/条件/变量/预期 写在第 0 列）", i + 1, code=C_INDENT_TOPLEVEL)

        # ── 结构化 dedent：按缩进弹出已结束的块帧 ──
        if indent == 0:
            if kw in _BLOCK_KW:
                # 块关键字（分支/否则如果/否则）：关闭所有【嵌套】块帧（indent > 0），
                # 但保留顶层 block 帧（indent == 0）供同级 分支/否则 续接多路分支。
                # 否则嵌套场景下「外层 否则/分支」仍会把内层 switch 帧当 top ——
                # 外层 否则 被误挂到内层 switch 上（双层 否则 误接线 bug：
                # 外层 switch 缺 else 输出、外层 false 路径不可达）。
                while _ctx[-1] is not _TOP and _ctx[-1].indent > 0:
                    _ctx.pop()
            else:
                # 非块关键字：回到最外层，关闭所有已开块（原行为，避免叶子步骤
                # 误挂到仍压栈的块帧 else_body，如 门+否则 后的顶层 动作）。
                while _ctx[-1] is not _TOP:
                    _ctx.pop()
        else:
            while _ctx[-1] is not _TOP and indent < _ctx[-1].parent_indent:
                _ctx.pop()
            if (_ctx[-1] is not _TOP and indent == _ctx[-1].parent_indent
                    and kw not in _BLOCK_KW):
                _ctx.pop()

        # ── 统一指令分发（块关键字与叶子步骤均按当前帧上下文处理）──
        if kw == "scene":
            scene.name = _after_colon(stripped)
        elif kw == "expected":
            expecting_expected = True
            inline = _after_colon(stripped)
            if inline.strip():
                scene.expected.append(_parse_expected_condition(inline))
        elif kw == "trigger":
            # 多段链路：中途出现的触发器标记新链路起点
            if scene.body:
                if not scene._first_break_count:
                    scene._first_break_count = len(scene.triggers)
                scene.body.append(_SegmentBreak(trigger_count_before=len(scene.triggers)))
            scene.triggers.append(_parse_trigger(_after_colon(stripped), i + 1))
        elif kw == "condition":
            scene.conditions.append(_after_colon(stripped))
        elif kw == "let":
            kv = _after_colon(stripped)
            if "=" in kv:
                k, v = kv.split("=", 1)
                scene.variables[k.strip()] = v.strip()
        elif kw == "branch":
            cond = _extract_branch_cond(stripped)
            top = _ctx[-1]
            if top.kind == "switch" and indent == top.indent:
                # 同级 分支 → 追加到当前 switch 的新分支（多路 if/elif/else）
                _append_branch(cond)
            else:
                # 顶层 / 嵌套 / 门或并行体内 → 新建 switch 并压栈
                _push_switch(indent, cond)
        elif kw == "elif":
            cond = _extract_branch_cond(stripped)
            top = _ctx[-1]
            if top.kind == "switch":
                _append_branch(cond)
            else:
                _push_switch(indent, cond)
        elif kw == "else":
            top = _ctx[-1]
            if top.kind == "switch":
                top.is_else = True
                top.branch = None
            elif top.kind == "gate":
                top.is_else = True
                top.branch = None
            else:
                if scene.conditions:
                    raise DSLError(
                        "否则 必须出现在 分支 或 查询/时间段 之后"
                        "（建议：把『否则』放在一个 分支 块体后，或 查询/时间段 门体后）。"
                        "注意：你已使用『条件:』作为场景级前置条件，它本身没有『否则』分支；"
                        "需要『条件成立 / 否则』二选一逻辑，请改用『分支: ... 否则:』语法。",
                        i + 1, code=C_INDENT_BLOCK)
                raise DSLError("否则 必须出现在 分支 或 查询/时间段 之后"
                               "（建议：把『否则』放在一个 分支 块体后，或 查询/时间段 门体后）", i + 1, code=C_INDENT_BLOCK)
        elif kw == "parallel":
            par = Parallel()
            attach(par)
            parent_indent = -1 if _ctx[-1].kind == "top" else indent
            _ctx.append(_Frame("parallel", indent, parent_indent, parallel=par))
        elif kw == "action":
            attach(_parse_action_or_subflow(_after_colon(stripped), i + 1))
        elif kw == "subflow":
            attach(_parse_subflow(_after_colon(stripped), i + 1))
        elif kw == "delay":
            attach(_parse_delay(_after_colon(stripped), i + 1))
        elif kw == "current_state":
            st = _parse_current_state(_after_colon(stripped), i + 1)
            attach(st)
            parent_indent = -1 if _ctx[-1].kind == "top" else indent
            _ctx.append(_Frame("gate", indent, parent_indent, gate=st))
        elif kw == "time_range":
            st = _parse_time_range(_after_colon(stripped), i + 1)
            attach(st)
            parent_indent = -1 if _ctx[-1].kind == "top" else indent
            _ctx.append(_Frame("gate", indent, parent_indent, gate=st))
        elif kw == "read_state":
            # 取值是叶子读取（非门）：仅 attach 到当前上下文
            attach(_parse_read_state(_after_colon(stripped), i + 1))
        elif kw == "debug":
            attach(Debug(name=_after_colon(stripped) or "观测"))
        elif kw == "comment":
            attach(Comment(text=_after_colon(stripped)))
        elif kw == "http":
            attach(_parse_http(_after_colon(stripped), i + 1))
        elif kw == "extract":
            attach(_parse_extract(stripped, i + 1))
        elif kw == "build":
            attach(_parse_build(_after_colon(stripped), i + 1))
        elif kw == "原生节点" or kw == "raw_node":
            attach(_parse_raw_node(_after_colon(stripped), i + 1))
        else:
            _check_deprecated(stripped, i + 1)
            if indent > 0:
                raise DSLError(f"缩进体只支持 动作/调用子流程/延时/查询/时间段/分支/否则如果/否则/取值/"
                               f"观测/注释/请求/提取/构建/原生节点：{stripped}"
                               f"（建议：缩进步骤用 动作/调用子流程/延时/查询/时间段/分支/否则如果/否则/"
                               f"取值/观测/注释/请求/提取/构建/原生节点；历史查询用 调用子流程: history_*）", i + 1, code=C_INDENT_BODY)
            raise DSLError(f"无法识别的顶层指令：{stripped}（建议：顶层指令应为 "
                           f"场景/触发/条件/变量/动作/调用子流程/分支/否则如果/否则/延时/查询/时间段/"
                           f"取值/请求/提取/构建/原生节点/观测/注释/预期；历史查询请用 调用子流程: history_*）", i + 1, code=C_UNKNOWN_TOPLEVEL)
        i += 1

    if not scene.triggers:
        raise DSLError("缺少 触发 指令（建议：每个场景至少需要一行『触发: <实体> <状态>』"
                       "或『触发: 每天 HH:MM』或『触发: inject』）", code=C_MISSING_TRIGGER)
    return scene


def _parse_raw_node(s: str, line: int) -> RawNode:
    """解析 原生节点: <JSON 对象>。校验白名单（禁 function/exec），剥离引擎托管字段。"""
    s = s.strip()
    if not s:
        raise DSLError("原生节点 必须跟一个 JSON 对象（如 {\"type\":\"switch\",\"rules\":[...]}）", line, code=C_RAW_JSON_NEED)
    try:
        obj = json.loads(s)
    except Exception as e:
        raise DSLError(f"原生节点 必须是合法 JSON 对象：{e}", line, code=C_RAW_JSON_INVALID)
    if not isinstance(obj, dict):
        raise DSLError("原生节点 必须是 JSON 对象（如 {\"type\":\"switch\",\"rules\":[...]}）", line, code=C_RAW_JSON_TYPE)
    ntype = obj.get("type")
    if not isinstance(ntype, str) or not ntype:
        raise DSLError("原生节点 必须含 type 字段（如 \"type\":\"switch\"）", line, code=C_RAW_NO_TYPE)
    if ntype in RAW_NODE_FORBIDDEN:
        raise DSLError(
            f"原生节点 禁止嵌入 {ntype} 节点（{'违反『不生成 Function 节点』铁律' if ntype == 'function' else 'RCE 风险'}）",
            line, code=C_RAW_FORBIDDEN)
    if ntype not in RAW_NODE_ALLOWED:
        raise DSLError(
            f"原生节点 类型 {ntype} 不在白名单（允许：{', '.join(sorted(RAW_NODE_ALLOWED))}）",
            line, code=C_RAW_UNKNOWN_TYPE)
    # 剥离由引擎托管的字段，避免与编译产物冲突（id/z/x/y/wires 自动生成）
    cfg = {k: v for k, v in obj.items()
           if k not in ("id", "z", "x", "y", "wires", "type")}
    return RawNode(node_type=ntype, config=cfg)


def _extract_branch_cond(stripped: str) -> str:
    body = (_after_colon(stripped) if stripped.startswith("分支:")
            else stripped[len("分支"):].strip())
    return body.rstrip(":").strip()


# 时长词 → 折算分钟的系数（省略单位默认按分钟）
_DURATION_UNIT_TO_MIN = {
    "小时": 60.0, "时": 60.0, "h": 60.0,
    "分钟": 1.0, "分": 1.0, "min": 1.0, "mins": 1.0,
    "秒": 1.0 / 60.0, "s": 1.0 / 60.0,
}


def _parse_duration(s: str):
    """从触发 state 串中提取『持续 N 单位』持久等待时长。

    返回 (clean_state, minutes_or_None)：
      - clean_state：剔除时长词后的剩余状态串（已 strip）
      - minutes_or_None：折算成分钟的数值；无时长词时返回 None
    例：『on 持续5分钟』→ ('on', 5.0)；『on 持续2小时』→ ('on', 120.0)。
    """
    m = re.search(r"持续\s*(\d+(?:\.\d+)?)\s*(小时|时|分钟|分|秒|min|mins|s|h)?", s)
    if not m:
        return s, None
    num = float(m.group(1))
    unit = m.group(2)  # 省略单位 → 默认按分钟
    factor = _DURATION_UNIT_TO_MIN.get(unit, 1.0)
    clean = (s[:m.start()] + s[m.end():]).strip()
    return clean, num * factor


def _parse_trigger(s: str, line: int) -> Trigger:
    s = s.strip()
    if s in ("inject", "注入"):
        return Trigger(kind="inject", raw=s)
    # inject 自定义 props：触发: inject(_src=handler_a) / inject(payload={"cmd":"开灯"})
    m = re.match(r"^(inject|注入)\s*\((.*)\)$", s, re.IGNORECASE)
    if m:
        raw_args = m.group(2).strip()
        props: dict[str, str] = {}
        for part in raw_args.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                props[k.strip()] = v.strip()
        return Trigger(kind="inject", raw=s, inject_props=props)
    # 可选「状态/当」前缀：触发: [状态] <entity> <state>
    m = re.match(r"^(状态|state|当)\s+(.+)$", s)
    if m:
        s = m.group(2).strip()
    # 英文/参数化 time 触发写法：time.is_between(hour=7, minute=0) / time(07:00) 等
    # （LLM 常从 HA 文档学来，编译器须识别为 time 触发，避免误当 state 实体被闸门拦截）
    if s.lower().startswith("time") or "is_between" in s or ("hour=" in s and "minute=" in s):
        hh = re.search(r"hour\s*=\s*(\d{1,2})", s)
        mm_val = re.search(r"minute\s*=\s*(\d{1,2})", s)
        eh = re.search(r"end_hour\s*=\s*(\d{1,2})", s)
        em = re.search(r"end_minute\s*=\s*(\d{1,2})", s)
        wd = re.search(r"weekday\s*=\s*([0-6](?:\s*,\s*[0-6])*)", s)
        if hh:
            h = int(hh.group(1)); m_ = int(mm_val.group(1)) if mm_val else 0
            if eh:
                eh_v = int(eh.group(1)); em_v = int(em.group(1)) if em else 0
                hr = f"{h}-{eh_v}" if eh_v != h else f"{h}"
                mn = f"{m_}-{em_v}" if (em and em_v != m_) else f"{m_}"
            else:
                hr, mn = f"{h}", f"{m_}"
            dow = "*"
            if wd:
                # HA weekday: 0=周一..6=周日 → cron DOW: 1=周一..7=周日(0)
                nums = [int(x) for x in re.split(r"[,\s]+", wd.group(1)) if x.strip() != ""]
                dow = ",".join(str((n + 1) % 7) for n in nums)
            return Trigger(kind="time", cron=f"{mn} {hr} * * {dow}", raw=s)
        return Trigger(kind="time", cron=_parse_cron_zh(s), raw=s)
    if ("每天" in s or "每周" in s or "周末" in s) or re.search(r"\d{1,2}:\d{2}", s):
        return Trigger(kind="time", cron=_parse_cron_zh(s), raw=s)
    # event 实体触发（门锁开门/门窗打开/按钮点击等）：单 token、无状态部分。
    # HA 中 event 实体每次事件 state 更新为时间戳，server-state-changed 监听任意变化即触发。
    if re.match(r"^event\.\S+$", s):
        return Trigger(kind="state", entity=s, state="*", raw=s)
    # —— 时长词 → 折算分钟的系数（省略单位默认按分钟）——
    m = re.match(r"^(\S+)\s+(.+)$", s)
    if not m:
        raise DSLError(f"触发格式无法解析：{s}（建议：状态触发用『触发: <实体> <状态>』；"
                       f"定时用『触发: 每天 HH:MM』；手动用『触发: inject』）", line, code=C_TRIGGER_FORMAT)
    entity, state = m.group(1), m.group(2).strip()
    if state in ("变化", "变更", "changed"):
        state = "*"
    # 「首次/第一次/头一次」修饰 → 上升沿触发（每次状态变到目标值触发一次）
    first = bool(re.search(r"首次|第一次|头一次", entity + " " + state))
    # 去掉修饰词，避免污染 entity/state 值
    entity = re.sub(r"(首次|第一次|头一次)", "", entity).strip()
    state = re.sub(r"(首次|第一次|头一次)", "", state).strip()
    # 「持续 N 分钟/小时/秒」修饰 → 持久等待（编译为 server-state-changed 的 for 等待）。
    # 从 state 里剥离时长词，干净 state 回填，时长折算成分钟存入 for_minutes。
    state, for_minutes = _parse_duration(state)
    return Trigger(kind="state", entity=entity, state=state, raw=s, first=first,
                   for_minutes=for_minutes)


def _parse_cron_zh(s: str) -> str:
    hhmm = re.search(r"(\d{1,2}):(\d{2})", s)
    if not hhmm:
        return "0 0 * * *"
    h, m = int(hhmm.group(1)), int(hhmm.group(2))
    if "周一至周五" in s or "工作日" in s:
        return f"{m} {h} * * 1-5"
    if "周末" in s:
        return f"{m} {h} * * 6,0"
    return f"{m} {h} * * *"


def _split_top_level(s: str, sep: str = ",") -> list:
    """按顶层分隔符切分，忽略方括号内的分隔符（如 [a, b] 不被切开）。

    B4 根因补强：动作目标 [light.a, light.b] 或 entity_id=[a, b] 中的逗号位于
    方括号内，普通 split(',') 会误切导致实体错位/被吞；按括号深度切分可正确保留。"""
    parts, buf, depth = [], [], 0
    for ch in s:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _action_param_value(v: str) -> str:
    """动作 参数值归一：反引号包裹的值视为【动态引用】，转为 HA 服务调用的 mustache 模板。

    WB25-NEW-3：DSL help 在「构建/请求」里用反引号示范动态值，但 动作 参数里反引号此前
    不被识别，被当死字面量残留（与子流程参数 kwargs 的反引号→JSONata 处理不一致），
    造成「我以为注入成功了其实塞了个字符串」的静默失败。此处统一：
      - 裸名 `current_temperature`          →  {{payload.current_temperature}}
        （与 取值 落点 msg.payload.<field> 对齐，下游 取值 X 后 动作 用 `X` 即引用其值）
      - 已带前缀 `payload.x`/`msg.x`/`flow.x` → {{前缀.x}}（保持原路径，不重复加 payload.）
      - 已是模板 `{{...}}`                  → 原样保留（不二次包裹）
      - 以 `$` 开头（JSONata 表达式）        → 原样保留（动作 data 非 JSONata 上下文，交给用户自担）
    其余（数字 / 普通字符串）原样返回，交给 _coerce_params 做数值归一（#506 行为不变）。"""
    v = v.strip()
    if len(v) >= 2 and v.startswith("`") and v.endswith("`"):
        inner = v[1:-1].strip()
        if not inner:
            return v
        if inner.startswith("{{") and inner.endswith("}}"):
            return inner
        if inner.startswith("$"):
            return inner
        if inner.startswith(("payload.", "msg.", "flow.")):
            return "{{" + inner + "}}"
        return "{{payload." + inner + "}}"
    return v


def _parse_action(s: str, line: int) -> Action:
    m = re.match(r"^([\w.]+)\((.*)\)$", s.strip())
    if not m:
        raise DSLError(f"动作格式应为 domain.service(target, k=v)：{s}"
                       f"（建议：写成 light.turn_on(light.xxx, brightness=80)，目标用位置参数，勿写 entity_id=）", line, code=C_ACTION_FORMAT)
    dom_svc = m.group(1)
    if "." not in dom_svc:
        raise DSLError(f"动作缺少 domain.service：{s}（建议：动作须含『域.服务』，如 light.turn_on / switch.turn_off）", line, code=C_ACTION_NO_DOMAIN)
    domain, service = dom_svc.split(".", 1)
    inner = m.group(2).strip()
    if not inner:
        raise DSLError(f"动作缺少目标实体：{s}（建议：在括号内给出目标，如 switch.turn_on(switch.xxx)；"
                       f"若写成 entity_id=... 本引擎会自动提取其值作为目标）", line, code=C_ACTION_NO_TARGET)
    # 目标支持两种写法：
    #  - 位置单实体：light.turn_on(light.x)
    #  - 实体数组：  light.turn_on([light.a, light.b], brightness_pct=80)  → 多实体（B4 修复）
    # 用 _split_top_level 切分，方括号内逗号不被误切（否则 [light.a, light.b] 会错位）。
    tokens = _split_top_level(inner)
    first = tokens[0].strip() if tokens else ""
    targets: list = []
    params: dict[str, str] = {}
    if first.startswith("["):
        # 位置数组目标：[e1, e2, ...]
        close = first.find("]")
        if close == -1:
            raise DSLError(f"动作目标数组缺少右括号 ]：{s}（建议：(light.a, light.b) 或 [light.a, light.b]）", line, code=C_ACTION_ARRAY_NOBRACKET)
        arr = first[1:close]
        targets = [t.strip() for t in arr.split(",") if t.strip()]
        if not targets:
            raise DSLError(f"动作目标数组为空：{s}（建议：在括号内给出至少一个实体，如 [light.a, light.b]）", line, code=C_ACTION_ARRAY_EMPTY)
        kv_tokens = tokens[1:]
    elif "=" in first:
        k0, v0 = first.split("=", 1)
        if k0.strip() in ("entity_id", "target", "entity"):
            # entity_id/target/entity kwarg（单值或数组 [a,b]）定义目标
            if v0.strip().startswith("["):
                close = v0.find("]")
                arr = v0[1:close] if close != -1 else v0[1:]
                targets = [t.strip() for t in arr.split(",") if t.strip()]
            else:
                targets = [v0.strip()]
            kv_tokens = tokens[1:]
        else:
            # 首位即普通参数（缺位置目标）：全部当参数，稍后从 entity_id 兜底
            kv_tokens = tokens
    else:
        targets = [first]
        kv_tokens = tokens[1:]
    # 解析 k=v 参数；entity_id/target/entity 的数组值 [a,b] 也展开为多目标
    for p in kv_tokens:
        p = p.strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in ("entity_id", "target", "entity"):
            # 目标实体（单值或数组）始终定义/覆盖 targets，绝不进 service data（避免 HA 校验报错）
            if v.startswith("["):
                close = v.find("]")
                arr = v[1:close] if close != -1 else v[1:]
                targets = [t.strip() for t in arr.split(",") if t.strip()]
            else:
                targets = [v]
        else:
            params[k] = _action_param_value(v)
    if not targets:
        if domain == "notify":
            targets = []  # notify 服务无实体目标（目标在 params 里，如 text/title）
        else:
            raise DSLError(f"动作缺少目标实体：{s}（建议：在括号内给出目标，如 switch.turn_on(switch.xxx)；"
                           f"若写成 entity_id=... 本引擎会自动提取其值作为目标）", line, code=C_ACTION_NO_TARGET)
    target = targets[0] if targets else ""
    return Action(domain=domain, service=service, target=target, targets=targets, params=params)


def _parse_subflow(s: str, line: int) -> SubflowCall:
    m = re.match(r"^(\w+)\((.*)\)$", s.strip())
    if not m:
        raise DSLError(f"子流程调用格式应为 name(k=v)：{s}"
                       f"（建议：写成 demo_notify(text=欢迎, room=客厅)；可用子流程见 autoflow_dsl_help 的 subflows 列表）", line, code=C_SUBFLOW_FORMAT)
    name = m.group(1)
    inner = m.group(2).strip()
    spec = get_subflow(name)
    args: dict[str, str] = {}
    jsonata_args: set[str] = set()
    positionals: list[str] = []
    if inner:
        for p in inner.split(","):
            p = p.strip()
            if not p:
                continue
            if "=" in p:
                k, v = p.split("=", 1)
                k, v = k.strip(), v.strip()
                # 反引号包裹的值视为 JSONata 表达式（动态取值，而非字面量）
                if len(v) >= 2 and v.startswith("`") and v.endswith("`"):
                    args[k] = v[1:-1]
                    jsonata_args.add(k)
                else:
                    args[k] = v
            else:
                positionals.append(p)
    if not spec:
        raise DSLError(f"未知子流程：{name}（未在 subflows.py 注册）"
                       f"（建议：用 autoflow_dsl_help 确认已注册子流程名，勿拼错）", line, code=C_SUBFLOW_UNKNOWN)
    # 位置参数容错：子流程声明了 positional 顺序（如 bark_push 的 [title, body]）时，
    # 无 key 的位置参数按声明顺序填入，使 bark_push(标题, 正文) 也能正确映射。
    if positionals and getattr(spec, "positional", None):
        for idx, pname in enumerate(spec.positional):
            if idx < len(positionals) and pname not in args:
                args[pname] = positionals[idx]
    # 参数契约校验（编译期）：managed 严格（未知参数也报错，捕获拼写错误），imported 宽松。
    try:
        spec.validate_args(args, strict=(getattr(spec, "source", "managed") == "managed"),
                           dynamic=jsonata_args)
    except ValueError as e:
        raise DSLError(f"子流程 {name} 调用参数错误：{e}",
                       line, code=C_SUBFLOW_ARG)
    # WB24 NEW-F1 defense-in-depth：子流程引用实体属性（如 history_* 的 attribute=）时，
    # 编译期校验属性是否存在于实体的已知 attributes，避免「属性名拼错却零提示」静默逃逸。
    _validate_subflow_attribute(name, args, line)
    return SubflowCall(name=name, raw_args=args, jsonata_args=jsonata_args, line=line)


def _validate_subflow_attribute(name: str, args: dict, line: int) -> None:
    """校验子流程调用里引用实体属性的参数（如 history_* 的 attribute=）是否真实存在。

    WB24 NEW-F1：history_state_at/period/last_changed/aggregate 等通过 attribute= 引用某实体的
    具体属性；若该属性名拼写错误，运行期会静默读到空/错误值，且编译器与 gate 此前均不校验属性名，
    形成与"坏实体被 gate 拦、坏属性放行"不对称。此处用实体属性解析器做编译期校验。

    fail-open：无属性解析器 / 实体未解析 / 属性集合未知（None 或空集）→ 跳过（不阻断）。
    仅当「能确定实体属性集合」且 attribute 不在其中时才报错（明确的属性名错误）。
    """
    if "attribute" not in args or not (args.get("attribute") or "").strip():
        return
    if "entity" not in args or not (args.get("entity") or "").strip():
        return
    attrs = _resolve_entity_attributes(args["entity"])
    if not attrs:  # None 或空集 → 无法判定 → 跳过
        return
    attr = args["attribute"].strip()
    if attr not in attrs:
        raise DSLError(
            f"子流程 {name} 的 attribute='{attr}' 不是实体 {args['entity']} 的已知属性"
            f"（已知属性：{sorted(attrs)}）。可能是拼写错误；"
            f"若确为该实体的动态属性且未进目录，请改用白箱 autoflow_deploy_raw 直接写节点。",
            line, code=C_SUBFLOW_ATTR_UNKNOWN)


def _parse_delay(s: str, line: int) -> Delay:
    m = re.match(r"^(\d+)\s*(秒|s|分钟|min)?", s.strip())
    if not m:
        raise DSLError(f"延时格式应为 '<数字> 秒'：{s}（建议：写成 5 秒 / 2 分钟）", line, code=C_DELAY_FORMAT)
    val = int(m.group(1))
    if m.group(2) in ("分钟", "min"):
        val *= 60
    return Delay(seconds=val)


def _parse_current_state(s: str, line: int) -> CurrentState:
    """解析状态查询：查询: <entity> <state>
    如：查询: light.living_room on  →  当前是 on 则继续，否则停止。"""
    s = s.strip()
    m = re.match(r"^(\S+)\s+(.+)$", s)
    if not m:
        raise DSLError(f"查询格式应为 '<entity> <state>'：{s}（建议：写成 查询: light.xxx on）", line, code=C_QUERY_FORMAT)
    entity = m.group(1)
    state = m.group(2).strip()
    state = _STATE_ALIAS.get(state, state)
    return CurrentState(entity=entity, state=state)


def _parse_read_state(s: str, line: int) -> ReadState:
    """取值: <entity> [<field>]。field 缺省→写 msg.payload，否则写 msg.<field>。"""
    s = s.strip()
    m = re.match(r"^(\S+)(?:\s+(\S+))?$", s)
    if not m:
        raise DSLError(f"取值格式应为 '<entity> [<field>]'：{s}（建议：写成 取值: sensor.xxx 温度，读到的状态写入 msg.温度）", line, code=C_READ_FORMAT)
    entity = m.group(1)
    RESERVED = {"payload", "msg", "message", "消息", "上下文", "context"}
    if entity.lower() in RESERVED:
        raise DSLError(
            f"取值 不能读取 '{entity}'：'取值' 读取的是 Home Assistant 实体状态，不是消息负载。"
            f"天气/AnySearch 是 fire-and-forget 子流程、不返回值；如需读取上一节点的消息字段，"
            f"请用 '提取: <字段> = <表达式>'。",
            line, code=C_READ_RESERVED)
    return ReadState(entity=entity, field=m.group(2))


# 星期 → time-range-switch 的 only 属性值（node-red-contrib-time-range-switch 标准）。
# 长短语排在短前缀前，避免「周一至周五」被「周一」误截。
_WEEKDAY_ONLY = (
    ("工作日", "weekdays"), ("周一至周五", "weekdays"), ("周一到周五", "weekdays"),
    ("周末", "weekends"), ("周六周日", "weekends"),
    ("每天", "all"), ("每日", "all"), ("全天", "all"),
    ("星期一", "monday"), ("星期二", "tuesday"), ("星期三", "wednesday"),
    ("星期四", "thursday"), ("星期五", "friday"), ("星期六", "saturday"),
    ("星期日", "sunday"), ("星期天", "sunday"),
    ("周一", "monday"), ("周二", "tuesday"), ("周三", "wednesday"),
    ("周四", "thursday"), ("周五", "friday"), ("周六", "saturday"), ("周日", "sunday"),
)


def _parse_time_range_weekday(s: str):
    """从时间段字符串剥离可选星期限定词，返回 (only值 或 None, 剩余串)。"""
    for kw, val in _WEEKDAY_ONLY:
        if s.startswith(kw):
            return val, s[len(kw):].strip()
        if s.endswith(kw):
            return val, s[: -len(kw)].strip()
    return None, s


def _parse_time_range(s: str, line: int) -> TimeRange:
    """解析时间段：时间段: 07:00-23:00 / 时间段: 工作日 20:00-23:00
    编译为 time-range-switch（2 输出 + only 星期限定），在时间段内继续主链。"""
    s = s.strip()
    weekday, rest = _parse_time_range_weekday(s)
    m = re.match(r"^(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})$", rest)
    if not m:
        raise DSLError(f"时间段格式应为 'HH:MM-HH:MM'（可加星期前缀如 工作日）：{rest}"
                       f"（建议：写成 时间段: 07:00-23:00，星期前缀放前面如 工作日 20:00-23:00）", line, code=C_TIMERANGE_FORMAT)
    return TimeRange(start=m.group(1), end=m.group(2), weekday=weekday)


def _parse_http(s: str, line: int) -> HttpRequest:
    """解析 HTTP 请求。格式：
      请求: GET <url>
      请求: POST <url> <json_body>  [K=V ...]
      请求: POST <url>  K1=V1  K2=V2
    尾部 JSON 自动识别为 body；k=V 对为 headers。
    """
    s = s.strip()
    m = re.match(r"^(GET|POST|PUT|DELETE|HEAD|PATCH)\s+(\S+)(?:\s+(.*))?$", s, re.IGNORECASE)
    if m:
        method = m.group(1).upper()
        url = m.group(2)
        rest = (m.group(3) or "").strip()
    else:
        # 省略方法默认 GET
        m2 = re.match(r"^(\S+)(?:\s+(.*))?$", s)
        if not m2:
            raise DSLError(f"请求格式应为 'GET <url>' 或 '<url>'：{s}（建议：写成 请求: GET https://... 或 请求: POST https://... {{...body}}）", line, code=C_REQUEST_FORMAT)
        method = "GET"
        url = m2.group(1)
        rest = (m2.group(2) or "").strip()

    body = None
    headers: list[dict] = []
    if not rest:
        return HttpRequest(method=method, url=url)

    # 尝试提取尾部 JSON body：找到匹配的 {}/[] 块，之后的部分为 headers
    json_body, remaining = _extract_trailing_json(rest)
    if json_body:
        body = json.loads(json_body)   # 解析为原生 dict/list，避免双层转义
        rest = remaining.strip()

    # 剩余的 k=v 对作为 headers（值可含空格，贪婪匹配到下一个 k= 或行尾）
    if rest:
        # 用正则提取 K=V 对：key= 后面贪婪取到下一个 " key=" 或行尾
        hdr_pattern = re.compile(r'(\S+?)=(.+?)(?=\s+\S+=|$)', re.DOTALL)
        for m in hdr_pattern.finditer(rest):
            headers.append({"key": m.group(1).strip(), "value": m.group(2).strip(), "type": "text"})
        if not headers:
            raise DSLError(f"无法解析 HTTP 参数 '{rest}'（期望 K=V header）：{s}（建议：尾部 K=V 成对写 header，如 Content-Type=application/json）", line, code=C_REQUEST_PARAM)

    return HttpRequest(method=method, url=url, body=body, headers=headers)


def _extract_trailing_json(text: str) -> tuple[Optional[str], str]:
    """从文本中提取一个完整的 JSON 值（从 { 或 [ 开始到匹配结束）。
    返回 (json_string, remaining_text)。如果找不到合法 JSON，返回 (None, text)。
    """
    i = 0
    while i < len(text) and text[i] in " \t":
        i += 1
    if i >= len(text) or text[i] not in ("{", "["):
        return None, text

    depth = 0
    in_str = False
    escape = False
    for j in range(i, len(text)):
        ch = text[j]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not in_str:
            in_str = True
            continue
        if ch == '"' and in_str:
            in_str = False
            continue
        if in_str:
            continue
        if ch in ("{", "["):
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0:
                candidate = text[i:j+1]
                try:
                    json.loads(candidate)   # 验证合法
                    return candidate, text[j+1:]
                except json.JSONDecodeError:
                    return None, text
    return None, text


def _parse_extract(s: str, line: int) -> Extract:
    """提取 <字段名> = <JSONata表达式>。表达式相对 msg 求值（如 payload.result.x）。"""
    body = s.split(" ", 1)[1].strip() if " " in s else s.strip()
    if "=" not in body:
        raise DSLError(f"提取格式应为 '提取 <字段名> = <表达式>'：{s}（建议：写成 提取 温度 = payload.result.temperature）", line, code=C_EXTRACT_FORMAT)
    name, expr = body.split("=", 1)
    name, expr = name.strip(), expr.strip()
    if not name or not expr:
        raise DSLError(f"提取的字段名与表达式均不可为空：{s}（建议：至少给出一个 字段名 = 表达式）", line, code=C_EXTRACT_EMPTY)
    return Extract(name=name, expr=expr)


def _parse_build(s: str, line: int) -> Build:
    """构建请求体：<JSON 字面量> 或 <JSONata 表达式>（动态值用反引号包裹）。
    - 字面量（{...}/[...]）：直接 json.loads 存为原生对象（tot=json）。
    - 含反引号：去掉反引号后整体作为 JSONata 表达式（如 `payload` 引用 msg.payload）。
    语义：把结果写入 msg.payload，供下游『请求』节点作为请求体发送。"""
    s = s.strip()
    if not s:
        raise DSLError("构建 需要一个 JSON 对象或 JSONata 表达式"
                       "（建议：写成 构建 {\"key\":\"value\"} 或 构建 `payload.x`）", line, code=C_BUILD_FORMAT)
    if "`" in s:
        # JSONata 模式：反引号内为表达式（jsonata 变量/运算），去掉反引号恢复原意
        return Build(kind="jsonata", expr=s.replace("`", ""))
    try:
        val = json.loads(s)
        return Build(kind="json", literal=val)
    except json.JSONDecodeError:
        # 退化：整行当作 JSONata 字符串表达式
        return Build(kind="jsonata", expr=s)


def _parse_expected_condition(s: str) -> dict:
    """把一行后置条件解析为 {entity_id,state} 或 {subflow:name}。

    支持写法：
      light.xxx = on              实体变为某状态
      light.xxx：off              中文冒号
      light.xxx on                空格分隔（无标点）
      subflow: demo_notify          子流程被调用
      调用子流程: demo_notify
      demo_notify 被调用
    """
    s = s.strip()
    if not s:
        return {}
    low = s.lower()
    if "被调用" in s or low.startswith("subflow:") or low.startswith("调用子流程:"):
        name = s
        for p in ("被调用", "subflow:", "调用子流程:"):
            name = name.replace(p, "")
        name = name.strip().strip(":").strip()
        return {"subflow": name}
    if "=" in s:
        eid, st = s.split("=", 1)
    elif "：" in s:
        eid, st = s.split("：", 1)
    else:
        parts = s.split(None, 1)
        eid, st = (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")
    return {"entity_id": eid.strip(), "state": st.strip()}


# ── 校验 ───────────────────────────────────────────────────────────────────
@dataclass
class Issue:
    level: str   # "error" | "warning"
    message: str
    line: Optional[int] = None
    # R1(#round4)：错误码。validate() 聚合出的错误此前一律被 compile() 打成
    # C_MULTI_ERROR（丢失具体码与行号），调用方（MCP 工具/WebUI）无法按码分流。
    # 单条带码错误现在会原样抛出该码 + 行号。
    code: Optional[str] = None


# ── WB24 静默放行收口：编译期条件/实体预检助手 ─────────────────────────────
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 以比较/逻辑运算符结尾 = 缺右操作数（N5：`$number(state) >` 编译被接受、NR 恒判假）。
# 不含 * / + - 等（`payload.*` 是合法 JSONata 通配），只拦比较与逻辑连接词。
_TRAILING_OP_RE = re.compile(r"(?:[<>]=?|!=|==?|\band\b|\bor\b|\bin\b)\s*$")
# 与数字字面量比较（< > <= >= 以及 == !=），用于 $number() 缺失提示。
# 三类：① 大小比较 < > <= >= 后接数字字面量；② 等值比较 ==/!= 后接数字字面量；
# ③ 数字字面量在左（1 == x / 1 != x）。字符串（字段或字面量）与数字比较未包
# $number()/$millis() 时恒 false，需提示；含 $ 一律豁免（见 _compare_type_hint）。
_NUM_CMP_RE = re.compile(
    r"[<>]=?\s*-?\d+(?:\.\d+)?\s*(?:$|\)|\band\b|\bor\b)"      # ① 大小比较后接数字
    r"|(?:==|!=)\s*-?\d+(?:\.\d+)?\s*(?:$|\)|\band\b|\bor\b)"  # ② 等值比较 ==/!= 后接数字字面量
    r"|-?\d+(?:\.\d+)?\s*(?:==|!=)"                            # ③ 数字字面量在左：1 == x
)


def _jsonata_precheck(expr: str) -> Optional[str]:
    """编译期 JSONata 语法预检（C_JSONATA_SYNTAX）。返回错误说明；None=通过。

    复用 flow_linter._check_jsonata（括号/引号平衡、双点），另拦「以运算符结尾」
    的断裂表达式——此前这类表达式被编译接受，落到 NR 后 switch 静默恒假，
    只能靠 gate 兜底（WB24-N5）。"""
    s = _sanitize_jsonata((expr or "").strip())
    if not s:
        return "表达式为空"
    ok, why = _check_jsonata(s)
    if not ok:
        return why
    if _TRAILING_OP_RE.search(s):
        return "以比较/逻辑运算符结尾，缺少右操作数"
    return None


def _compare_type_hint(expr: str) -> Optional[str]:
    """数值比较类型提示（C_COMPARE_TYPE_WARN，warning 不阻断）。

    HA 实体 state / 取值 结果是字符串；JSONata 中字符串与数字比较恒 false，
    分支会静默恒走 else。仅当表达式完全不含 $（无任何函数转换）且出现
    「与数字字面量比较（含 ==/!= 等值）」时提示——含 $number()/$millis() 等一律豁免，
    零误伤合法写法。size 比较（< > <= >=）与等值比较（== !=）同等处理。"""
    s = _sanitize_jsonata((expr or "").strip())
    if not s or "$" in s:
        return None
    if not _NUM_CMP_RE.search(s):
        return None
    return ("与数字比较但未包 $number()：HA 实体 state/取值结果是字符串，"
            "字符串与数字在 JSONata 中比较恒为 false，分支会静默恒走 else。"
            "（建议：显式转数值，如 $number(payload.state) > 25）")


def _unresolved_name_issue(name: str, where: str) -> Optional[Issue]:
    """中文/友好实体名未解析检查（C_ENTITY_UNRESOLVED，warning 不阻断）。

    仅查含中文的名字：中文名绝不可能是合法 entity_id，若 Tier2 实体映射也查无此名，
    照编译只会静默产出永远找不到实体的节点（此前完全静默直通 NR）。
    定为 warning 而非 error：实体目录可能为空/未同步（离线网关、测试环境），
    fail-open 哲学下不因目录状态阻断编译——但不再静默，agent/gate 可据码自查。
    ASCII entity_id 的存在性校验仍归 gate/linter；无解析器（离线编译）时跳过。"""
    if not name or _entity_resolver is None or _is_template_entity(name):
        return None
    if not _CJK_RE.search(name):
        return None
    try:
        r = _entity_resolver(name)
    except Exception:
        r = None
    if r:
        return None
    return Issue("warning",
                 f"{where}实体『{name}』无法解析为 entity_id（Tier2 实体映射查无此名）。"
                 f"中文名不会被 Node-RED/HA 识别，照编译会产出找不到实体的节点 "
                 f"[C_ENTITY_UNRESOLVED]（建议：改用真实 entity_id（如 light.study_desk_lamp），"
                 f"或先在实体映射登记该友好名后重试）")


def _lint_extract(st: "Extract", prev_api_subflow: Optional[str],
                  prev_ff_subflow: Optional[str] = None) -> list:
    """R8(#round4) iss_6748217860：`提取:` 的编译期体检。

    两类静默失败：
      1. 自赋值 `提取: payload.reply = payload.reply` —— 编译出一个不改变任何数据的
         空转 change 规则（A28 同族），作者以为「提取了」其实什么也没发生；
      2. 字段名写错 `提取: x = payload.resp`（实际是 `.reply`）—— JSONata 取不到就是
         undefined，静默写入空值，lint/闸门全过。
    没有实体返回值 schema 可查，所以只在**能确定上游落点**时提示（http_api 子流程
    的返回值规范落在 msg.payload.reply），其余情况不猜、不报。均为 warning，不阻断。"""
    out: list = []
    name = (st.name or "").strip()
    expr = (st.expr or "").strip()
    norm_expr = expr[4:] if expr.startswith("msg.") else expr
    if name and name == norm_expr:
        out.append(Issue(
            "warning",
            f"提取: 『{name} = {expr}』是自赋值——目标字段与来源表达式是同一路径，"
            f"编译出的 change 规则不改变任何数据（空转节点）。"
            f"若本意是改名，请把右侧改成真正的来源路径（如 {name} = payload.result.{name}）；"
            f"若只是想让字段「存在」，请删掉这行 [C_EXTRACT_SELF_ASSIGN]",
            line=getattr(st, "line", None)))
    if prev_api_subflow:
        m = re.fullmatch(r"payload\.(\w+)", norm_expr)
        if m and m.group(1) != "reply":
            out.append(Issue(
                "warning",
                f"提取: 『{name} = {expr}』紧跟在子流程 {prev_api_subflow} 之后，"
                f"但该子流程的返回值按网关约定落在 **msg.payload.reply**，"
                f"payload.{m.group(1)} 很可能取不到（JSONata 读不到就是 undefined，"
                f"会静默写入空值）。请确认字段名，或改成 payload.reply "
                f"[C_EXTRACT_FIELD_SUSPECT]",
                line=getattr(st, "line", None)))
    # R7(#round4) iss_516bc5d816（报告 A16）：紧跟在 link-out 型子流程后面的 `提取:`。
    # link_out 是 fire-and-forget —— 网关只把入参写进 msg.payload 再发一个 link out
    # 到目标 tab 的 link in，Node-RED 侧**不存在**把结果送回调用点的回执通道
    # （回送需要 link call + link out(mode=return)，本项目的 link_out 能力都不是）。
    # 所以这里的「提取」永远取不到子流程返回值，属静默空转，必须显式告警。
    if prev_ff_subflow:
        out.append(Issue(
            "warning",
            f"提取: 『{name} = {expr}』紧跟在子流程 {prev_ff_subflow} 之后，但 "
            f"{prev_ff_subflow} 是 **link-out 型 fire-and-forget 调用**：网关只把入参"
            f"发到下游入口，Node-RED 没有任何把结果送回调用点的回执通道，"
            f"这里取不到它的返回值。编译器已把该调用编成【副链】（不污染主链 msg），"
            f"因此本次提取读到的是**调用前的上下文**而非子流程结果。"
            f"若确实需要返回值，请改用返回值型能力（http_api 或子流程实例，"
            f"返回值按约定落在 msg.payload.reply）[C_SUBFLOW_NO_REPLY]",
            line=getattr(st, "line", None)))
    return out


def validate(scene: Scene) -> list[Issue]:
    issues: list[Issue] = []
    if not scene.triggers:
        issues.append(Issue("error", "缺少触发"))

    # WB24-N5/N6 收口：条件/分支表达式编译期预检（语法断裂拦截 + 类型提示）
    var_names = set(scene.variables.keys()) if scene.variables else set()

    def _check_cond_expr(expr: str, where: str):
        err = _jsonata_precheck(expr)
        if err:
            issues.append(Issue(
                "error",
                f"{where}『{expr}』JSONata 语法断裂（{err}）——该表达式落到 NR 后会被静默判恒假，"
                f"分支/条件永不命中 [C_JSONATA_SYNTAX]"
                f"（建议：补全表达式，如 $number(payload.state) > 25）"))
            return
        # 场景变量按原生类型存 flow 上下文（#505），与数字比较合法，豁免提示
        if var_names and any(v in expr for v in var_names):
            return
        hint = _compare_type_hint(expr)
        if hint:
            issues.append(Issue("warning", f"{where}『{expr}』{hint} [C_COMPARE_TYPE_WARN]"))

    # 触发实体的未解析中文名检查
    for t in scene.triggers:
        ent = getattr(t, "entity", None)
        if ent:
            iss = _unresolved_name_issue(ent, "触发: ")
            if iss:
                issues.append(iss)

    # 顶层条件：等式走 api-current-state（查实体名）；复杂表达式走 jsonata（查语法）
    for cond in scene.conditions:
        parsed = _parse_state_condition(cond)
        if parsed:
            iss = _unresolved_name_issue(parsed[0], "条件: ")
            if iss:
                issues.append(iss)
        else:
            _check_cond_expr(cond, "条件: ")

    # WB25-NEW-1：跨实体同名字段碰撞检测。两个 取值 读同名 <field> 时都写
    # msg.payload.<field>，后执行的静默覆盖前一个 → 下游无法区分来源（数据损坏）。
    # 仅当同一 field 被 ≥2 个不同实体使用时报错（同实体同字段只是冗余，不损坏）。
    read_fields_seen: dict[str, list[str]] = {}
    def walk(steps: list):
        prev_api_subflow: Optional[str] = None  # 上一步是 http_api 子流程时记其名
        # R7(#round4)：上一步是 link_out（fire-and-forget，无回执）子流程时记其名
        prev_ff_subflow: Optional[str] = None
        for st in steps:
            if isinstance(st, Extract):
                issues.extend(_lint_extract(st, prev_api_subflow, prev_ff_subflow))
                continue  # 提取 不改变「上一步是否 http_api 子流程」的判定
            prev_api_subflow = None
            prev_ff_subflow = None
            if isinstance(st, SubflowCall):
                _sp = get_subflow(st.name)
                _ctype = (getattr(_sp, "call", {}) or {}).get("type") if _sp else None
                if _ctype == "http_api":
                    prev_api_subflow = st.name
                elif _ctype == "link_out":
                    prev_ff_subflow = st.name
            if isinstance(st, SubflowCall):
                spec = get_subflow(st.name)
                if not spec:
                    issues.append(Issue("error", f"未注册子流程：{st.name}（建议：用 autoflow_dsl_help 查看已注册子流程）"))
                    continue
                try:
                    spec.resolve_args(st.raw_args, dynamic=getattr(st, "jsonata_args", None))
                except ValueError as e:
                    # R1(#round4)：带上行号 + C_SUBFLOW_ARG 码，让 `bark_badge=abc`
                    # 这类类型错误在编译期给出可定位、可分流的错误（而非 C_MULTI_ERROR）。
                    issues.append(Issue("error", str(e),
                                        line=getattr(st, "line", None),
                                        code=C_SUBFLOW_ARG))
            elif isinstance(st, Switch):
                for b in st.branches:
                    # WB24-N5 收口：仅对走 jsonata 兜底的分支条件做语法预检
                    # （简单等式走 eq/ne 规则，无 JSONata 语法问题）
                    rule = _parse_switch_rule(b.condition)
                    if rule.get("vt") == "jsonata" and rule.get("t") != "else":
                        _check_cond_expr(b.condition, "分支: ")
                    elif rule.get("t") in ("eq", "ne") and rule.get("vt") == "num":
                        # WB44/R2 N679 收口：简单等式 ==/!= 与数字字面量比较，
                        # 字段（HA 实体 state 为字符串）未包 $number() 时可能恒 false。
                        prop = (rule.get("property") or "").strip()
                        # 豁免：已显式转数值（$number()/$millis()）或场景变量（原生类型，#505）
                        if "$" in prop:
                            pass
                        elif var_names and any(prop == v or prop.endswith("." + v)
                                               for v in var_names):
                            pass
                        else:
                            issues.append(Issue(
                                "warning",
                                f"分支: 『{b.condition}』与数字比较但未包 $number()："
                                f"HA 实体 state/取值结果是字符串，字符串与数字比较可能恒为 false，"
                                f"分支会静默恒走 else [C_COMPARE_TYPE_WARN]"
                                f"（建议：显式转数值，如 $number({prop}) "
                                f"{'!=' if rule.get('t') == 'ne' else '=='} {rule.get('v')}）"))
                    walk(b.body)
                walk(st.else_body)
            elif isinstance(st, (CurrentState, TimeRange)):
                if isinstance(st, CurrentState):
                    iss = _unresolved_name_issue(st.entity, "当前状态查询: ")
                    if iss:
                        issues.append(iss)
                # 门的主链(pass 分支)与否则(fail)分支都需校验子流程入参
                walk(getattr(st, "body", []))
                walk(getattr(st, "else_body", []))
            elif isinstance(st, Action):
                # WB24 收口：动作目标的未解析中文名检查
                for tgt in (st.targets or [st.target]):
                    iss = _unresolved_name_issue(tgt, "动作: ")
                    if iss:
                        issues.append(iss)
            elif isinstance(st, ReadState):
                iss = _unresolved_name_issue(st.entity, "取值: ")
                if iss:
                    issues.append(iss)
                # WB25-NEW-1：记录具名字段及其来源实体
                if st.field:
                    read_fields_seen.setdefault(st.field, []).append(st.entity)
            elif isinstance(st, RawNode):
                pass  # 逃生节点无子步骤，白名单校验已在 parse 阶段完成
            elif isinstance(st, Parallel):
                if not st.children:
                    # WB24 NEW-F2：空并行块会被静默丢弃（不产生任何节点），属静默降级。
                    # 编译期直接报错，逼 agent 补上子步骤，而非产出悬空 inject。
                    issues.append(Issue(
                        "error",
                        "并行块为空：『并行:』之后必须至少包含一个子步骤"
                        "（动作/取值/调用子流程/分支/延时等）。空并行块会被静默丢弃（不产生任何节点），"
                        "请删除该空块或在其中填入要并行执行的步骤。"))
                else:
                    walk(st.children)
    walk(scene.body)
    # WB25-NEW-1：同字段跨不同实体 → 落点撞车，编译期强拦
    for field, entities in read_fields_seen.items():
        if len(set(entities)) >= 2:
            issues.append(Issue(
                "error",
                f"取值 字段名冲突：『{field}』被 {len(entities)} 个不同实体的 取值 步骤重复使用"
                f"（实体：{', '.join(entities)}）。它们都会写入同一个 msg.payload.{field}，"
                f"后执行的会静默覆盖前一个，下游无法区分来源 → 数据损坏（WB25-NEW-1）。"
                f"请给每个 取值 起不同字段名（如 室内温度 / 室外温度），或用实体短名区分；"
                f"若确实只读同一实体同一字段，删掉其中一个重复 取值 即可。"))
    return issues


# ── 编译（AST → NR 导出）──────────────────────────────────────────────────
HA_SERVER_ID = "REPLACE_WITH_HA_SERVER"  # 部署时由 deploy 步骤替换为真实 server id


def _slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", name).strip("_")
    return "af_scene_" + (s or "unnamed")


# ── 节点默认字段字典（A4：根治红三角 / 脏节点观感）──────────────────────
# 源于对真实 NR 导出（1069 节点）的字段频率统计：编译器已正确发射每种节点
# 100% 必带字段，但部分「普遍存在、可安全缺省」的可选字段未补（尤其是
# server-state-changed 还误用了 output_properties 蛇形键而非 NR 的 outputProperties
# 驼峰键）。此表在 _Emitter.add 中以 setdefault 补齐——显式字段优先，只填空缺，
# 绝不覆盖既有值、不改变节点行为。
# 后续新增节点类型时，在此登记其安全默认字段即可获得同等保护。
NODE_DEFAULT_FIELDS = {
    "server-state-changed": {
        "stateType": "str",
        "outputProperties": [],   # 修正：编译器此前误用 output_properties(snake)，NR 实际字段名为 outputProperties
    },
    "api-current-state": {
        "state_location": "payload",
        "override_topic": False,
        # B4 修复（WB21 诊断报告）：override_payload 必须 True，否则节点不把实体状态
        # 写入 msg.payload，而是透传上游 inject 时间戳（实测回传 1785256256350），
        # 导致下游取值恒拿到时间戳而非真实状态。历史/查询子流程取值均依赖此。
        "override_payload": True,
    },
}


class _Emitter:
    def __init__(self, flow_id: str):
        self.flow_id = flow_id
        self.nodes: list[dict] = []
        self._n = 0
        self._y = 120
        # 场景变量名集合（由 compile 注入）：分支 LHS 命中时改读 flow 上下文，
        # 与 `变量:` 的 `pt:"flow"` 写入对齐，修复 变量↔分支 作用域错配(C2)。
        self.flow_vars: set = set()
        # WB25-NEW-2：取值 字段名集合（落点 msg.payload.<field>）。分支 JSONata / eq 规则
        # 引用裸字段名时据此对齐到 payload.<field>，避免读空（见 _emit_switch）。
        self.read_fields: set = set()

    def add(self, ntype: str, **fields) -> str:
        self._n += 1
        # 节点 id 必须以 flow_id 为前缀，保证全 NR 实例内全局唯一：
        # 否则 934 个既有 flow 极易已占用 af_001，POST /flow 会报 "duplicate id"。
        nid = f"{self.flow_id}_{self._n:03d}"
        node = {"id": nid, "type": ntype, "z": self.flow_id, "x": 200, "y": self._y}
        self._y += 100
        node.update(fields)
        # A4：补齐本类型普遍携带的"安全默认"字段（setdefault：显式优先，只填空缺）
        for _dk, _dv in NODE_DEFAULT_FIELDS.get(ntype, {}).items():
            node.setdefault(_dk, _dv)
        node.setdefault("wires", [[]])
        self.nodes.append(node)
        return nid

    def connect(self, src: str, dst: str):
        """单 output 节点的连线（一对多 fan-out / 多源 fan-in）。

        NR 单 output 节点的 wires 必须是「一个数组、数组内多个目标」：
        `[['a','b','c']]`。旧实现会「填第一个空数组，否则新建数组」，
        导致单 output 节点被写成 `[['a'],['b']]`（2 个数组）——第二个数组里的
        目标在 NR 中永不触发（经典「只开灯不播报」坑，见 linter R10）。
        这里统一把目标追加进 wires[0]，保证单 output 一对多/多对一都收敛到一个数组。
        多 output 节点的定向连线请用 connect_out(out_idx, ...)，不走本方法。"""
        nd = self._find(src)
        if nd is None:
            return
        # comment 节点不参与连线（仅作可视化说明）
        if nd.get("type") == "comment":
            return
        wires = nd.setdefault("wires", [[]])
        if not wires or not isinstance(wires[0], list):
            wires.insert(0, [])
        wires[0].append(dst)

    def connect_out(self, src: str, out_idx: int, dst: str):
        nd = self._find(src)
        if nd is None:
            return
        # comment 节点不参与连线
        if nd.get("type") == "comment":
            return
        wires = nd.setdefault("wires", [[]])
        while len(wires) <= out_idx:
            wires.append([])
        wires[out_idx].append(dst)

    def _find(self, nid: str):
        for nd in self.nodes:
            if nd["id"] == nid:
                return nd
        return None


# ── R2/R5(#round4)：`${...}` 模板插值管线 ────────────────────────────────
# 背景 iss_e8768ca640(A8) / iss_18c853bbce(A10)：文档示范
# ``调用子流程: xxx(提示=`阈值 ${阈值}`)``，但编译器完全不认 `${}`：
#   - 反引号值被整体当 JSONata → `阈值 ${阈值}` 是非法 JSONata，运行态报错；
#   - 裸值被当死字面量 → 编译产物里残留字面 `${阈值}`。
# 两条路径都是「编译通过、lint 全过、值是错的」的静默失败。
#
# 统一规则（JS 模板字符串直觉，反引号 + ${} 本来就是 JS 模板字面量语法）：
#   * 值中出现 `${` → 整个值按【文本模板】处理，反引号只是包裹符（不再当 JSONata 表达式）；
#   * 值中没有 `${` → 保持原行为（反引号=JSONata 表达式，裸值=字面量），零回归。
# `${name}` 的解析顺序（越早解析越好，能常量折叠就折叠）：
#   1. name 带 payload./msg./flow. 前缀 → 运行期引用，原样；
#   2. name 是场景变量（`变量:` 声明，编译期常量）→ 直接字面替换其值；
#   3. name 是 取值 字段 → 运行期引用 payload.<name>；
#   4. 都不是 → 编译期 C_SUBFLOW_ARG 报错（禁止静默塞空串）。
_INTERP_RE = re.compile(r"\$\{\s*([^{}]*?)\s*\}")


def _jsonata_str_literal(s: str) -> str:
    """纯文本片段 → JSONata 字符串字面量。

    用**单引号**：Node-RED 的 JSONata 约定字符串字面量单引号（flow_linter R9
    对双引号报警），且双引号在 change 节点 `to` 的 JSON 序列化里还要再转义一层。"""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _resolve_interp_ref(name: str, flow_vars: dict, read_fields: set,
                        *, where: str, line: Optional[int]) -> tuple:
    """解析 `${name}`，返回 ("const", 字面值) 或 ("expr", 运行期引用路径)。"""
    if not name:
        raise DSLError(f"{where} 中出现空插值 `${{}}`（建议：写成 ${{变量名}}）",
                       line, code=C_SUBFLOW_ARG)
    if name.startswith(("payload.", "msg.", "flow.", "global.")):
        return ("expr", name)
    if name in flow_vars:
        # 场景变量是编译期常量（只有 `设置变量` 节点写一次，运行期无人改写），
        # 直接折叠成字面值：产物更简单，也不依赖 flow 上下文时序。
        lit, _tot = _flow_var_to_tot(str(flow_vars[name]))
        return ("const", lit)
    if name in read_fields:
        # 取值 字段落点是 msg.payload.<field>（见 _emit_read_state / WB25-NEW-2）
        return ("expr", "payload." + name)
    known_vars = "、".join(sorted(flow_vars)) or "（无）"
    known_fields = "、".join(sorted(read_fields)) or "（无）"
    raise DSLError(
        f"{where} 引用了未定义的插值变量 `${{{name}}}`。"
        f"已声明的场景变量：{known_vars}；已声明的 取值 字段：{known_fields}。"
        f"（建议：先用『变量: {name}=<值>』声明，或用『取值: {name} = <实体>.<字段>』读取；"
        f"若要引用消息字段请写全路径，如 ${{payload.{name}}}）",
        line, code=C_SUBFLOW_ARG)


def _interp_value(raw: str, flow_vars: dict, read_fields: set,
                  *, where: str, line: Optional[int]) -> tuple:
    """把含 `${}` 的模板值编译掉。返回 (新值, 是否运行期动态)。

    - 全部片段可编译期折叠 → 返回普通字符串，dynamic=False；
    - 含运行期引用 → 返回 JSONata 拼接表达式，dynamic=True；
    - 整个值就是单个运行期引用（如 `${payload.n}`）→ 返回裸引用（不拼接），
      这样 int/num 型参数才不会被 `&` 强制转成字符串。
    """
    parts: list = []  # [(kind, text)]，kind ∈ {"lit", "expr"}
    pos = 0
    dynamic = False
    for m in _INTERP_RE.finditer(raw):
        if m.start() > pos:
            parts.append(("lit", raw[pos:m.start()]))
        kind, val = _resolve_interp_ref(m.group(1), flow_vars, read_fields,
                                        where=where, line=line)
        parts.append(("lit", val) if kind == "const" else ("expr", val))
        dynamic = dynamic or (kind == "expr")
        pos = m.end()
    if pos < len(raw):
        parts.append(("lit", raw[pos:]))
    if not dynamic:
        return "".join(t for _k, t in parts), False
    # 合并相邻字面片段（常量折叠后常出现 lit+lit），产物更短更可读
    merged: list = []
    for k, t in parts:
        if k == "lit" and merged and merged[-1][0] == "lit":
            merged[-1] = ("lit", merged[-1][1] + t)
        else:
            merged.append((k, t))
    merged = [(k, t) for k, t in merged if not (k == "lit" and t == "")]
    if len(merged) == 1 and merged[0][0] == "expr":
        return merged[0][1], True
    return " & ".join(_jsonata_str_literal(t) if k == "lit" else f"({t})"
                      for k, t in merged), True


def _apply_interpolation(scene: Scene) -> None:
    """在 validate 之前把所有子流程字符串入参里的 `${}` 编译掉（就地改写 scene）。

    必须早于 validate：`bark_badge=${阈值}`（阈值=7）若不先折叠，会被 int 类型
    校验判成「非数字」误报。幂等：折叠后值里已无 `${}`，重复调用无副作用。"""
    flow_vars = dict(scene.variables or {})
    read_fields = _collect_read_fields(scene)

    def _do(st: SubflowCall):
        for k, v in list(st.raw_args.items()):
            if not isinstance(v, str) or "${" not in v:
                continue
            new_v, dynamic = _interp_value(
                v, flow_vars, read_fields,
                where=f"子流程 {st.name} 参数 {k}", line=getattr(st, "line", None))
            st.raw_args[k] = new_v
            # 含 ${} 的值一律按文本模板处理：全常量→普通字符串（必须移出
            # jsonata_args，否则 "阈值 30" 会被当 JSONata 求值而报错）；
            # 含运行期引用→JSONata 拼接表达式。
            if dynamic:
                st.jsonata_args.add(k)
            else:
                st.jsonata_args.discard(k)

    def walk(steps):
        for st in steps:
            if isinstance(st, SubflowCall):
                _do(st)
            elif isinstance(st, Switch):
                for b in st.branches:
                    walk(b.body)
                walk(st.else_body)
            elif isinstance(st, (CurrentState, TimeRange)):
                walk(getattr(st, "body", []))
                walk(getattr(st, "else_body", []))
            elif isinstance(st, Parallel):
                walk(st.children)

    walk(scene.body)


def compile(scene: Scene, target: str = "staging") -> dict:
    # R2/R5(#round4)：`${var}` 插值必须在 validate 之前完成——否则
    # `bark_badge=${阈值}` 会先被类型校验判成「非数字」而误报。
    _apply_interpolation(scene)
    issues = validate(scene)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        # R1(#round4)：单条带码错误保留原码 + 行号（此前一律降级成 C_MULTI_ERROR，
        # 调用方拿不到 C_SUBFLOW_ARG 之类的具体码，也拿不到出错行）。
        if len(errors) == 1 and errors[0].code:
            raise DSLError(errors[0].message, errors[0].line, code=errors[0].code)
        raise DSLError("；".join(e.message for e in errors), code=C_MULTI_ERROR)

    flow_id = _slug(scene.name)
    em = _Emitter(flow_id)
    em.flow_vars = set(scene.variables.keys())
    # WB25-NEW-2：收集 取值 字段名，供 _emit_switch 对齐分支 JSONata / eq 路径到 msg.payload.<field>
    em.read_fields = _collect_read_fields(scene)

    # 多触发：先全部发射（拿到节点 id），再按段分配
    all_src_ids = [_emit_trigger(em, t, target) for t in scene.triggers]

    if scene.variables:
        # #505：变量按值语义存 flow 上下文原生类型（_flow_var_to_tot），
        # 而非一律字符串——下游 JSONata（flow.X）与 HA 服务调用才能拿到真数值/布尔。
        rules = []
        for k, v in scene.variables.items():
            to, tot = _flow_var_to_tot(v)
            rules.append({"t": "set", "p": k, "pt": "flow", "to": to, "tot": tot})
        vid = em.add("change", name="设置变量", rules=rules)
        for s in all_src_ids:
            em.connect(s, vid)
        all_src_ids = [vid]

    for cond in scene.conditions:
        parsed = _parse_state_condition(cond)
        if parsed:
            # 状态断言：编译为「读取 api-current-state（不门控）+ 后续 switch 按 payload 路由」
            # 读节点把实体态写入 msg.payload(outputProperties)，switch 据此判定
            # payload==expected → 继续主链(body)，否则 → 空(条件不符终止)。
            # 注意：NR 原生 api-current-state 的 halt_if 是"匹配即 halt 走 output1"，
            # 与"条件满足才执行"语义相反，故此处不门控、改由 switch 路由（FEEDBACK #8）。
            entity, expected, compare, vtype = parsed
            resolved = _resolve_entity(entity)
            op_label = "≠" if compare == "is_not" else "="
            rid = em.add("api-current-state", name=f"条件: {entity}{op_label}{expected}",
                         server=HA_SERVER_ID, version=7,
                         entityId=resolved,
                         state_type=vtype, blockInputOverrides=False,
                         outputProperties=[{"property": "payload", "propertyType": "msg",
                                            "value": "", "valueType": "entityState"}],
                         state_location="data", override_payload=False,
                         halt_if="", halt_if_type=vtype, halt_if_compare=compare,
                         outputs=1)
            _sw_t = "ne" if compare == "is_not" else "eq"
            sw = em.add("switch", name=f"分支: {entity}{op_label}{expected}",
                        property="payload", propertyType="msg",
                        rules=[{"t": _sw_t, "v": expected, "vt": vtype},
                               {"t": "else"}],
                        checkall="true", repair=False, outputs=2)
            em.connect(rid, sw)
            for s in all_src_ids:
                em.connect(s, rid)
            all_src_ids = [sw]
        else:
            # 非状态断言（其他复杂 JSONata 表达式）仍走 jsonata switch 兜底；
            # 经 _sanitize_jsonata 归一全角符号（（）＝），避免 R7 静默不求值。
            sid = em.add("switch", name=f"条件: {cond}",
                         rules=[{"t": "jsonata_exp", "v": _sanitize_jsonata(cond), "vt": "jsonata"}],
                         outputs=1)
            for s in all_src_ids:
                em.connect(s, sid)
            all_src_ids = [sid]

    # 按段拆分 body：_SegmentBreak 将步骤和触发器分组
    segments = _split_segments(scene.body)
    src_offset = 0
    for seg_idx, seg_steps in enumerate(segments):
        if seg_idx == 0:
            # 第一段：使用从头到第一个断点之间的触发器（或全部，若无断点）
            seg_sources = list(all_src_ids[:scene._first_break_count or len(all_src_ids)])
        else:
            seg_sources = list(all_src_ids[src_offset:])
        _emit_body(em, seg_steps, seg_sources)
        # 推进偏移：下一段从当前已用掉的源之后开始
        if seg_idx + 1 < len(segments):
            src_offset = scene._first_break_count or len(all_src_ids)
    layout_flow(em.nodes)  # BFS 分层自动布局（坐标系）
    return {
        "id": flow_id,
        "label": scene.name,
        "disabled": False,
        "info": f"compiled by dsl_engine (target={target})",
        "nodes": em.nodes,
        "lint": _self_lint(em.nodes),
    }


def _self_lint(nodes: list) -> list:
    """编译期自检：把生成的节点过一遍 flow_linter，返回 issue 列表。

    不阻塞编译（linter 是启发式），但把 R5/R7/R8 级反模式挂到 flow["lint"]，
    供网关部署前展示；网关可调用 compile_dsl_strict 强制拦截。
    """
    out = []
    try:
        out = list(lint_flow({"nodes": nodes}))
    except Exception:
        pass
    out.extend(_lint_layout(nodes))  # 布局级 lint：重叠 / 连线回退
    return out


# ── 自动布局（坐标系）────────────────────────────────
# 根治「全部 x=200 单列 / 顺序颠倒但连线正确」：编译后按图结构做分层布局，
# 节点坐标有体系、永不重叠；NR 侧零改动。配套 render_flow_svg 只读预览 + _lint_layout 部署前拦截。

LAYOUT_COL_W = 230   # 列间距（节点默认宽 ~200）
LAYOUT_ROW_H = 80    # 同列行距（节点默认高 ~60）
LAYOUT_X0 = 40
LAYOUT_Y0 = 120


def layout_flow(nodes: list, *, col_w: int = LAYOUT_COL_W, row_h: int = LAYOUT_ROW_H,
               x0: int = LAYOUT_X0, y0: int = LAYOUT_Y0) -> None:
    """BFS 最长路径分层自动布局（原地修改 nodes 的 x/y）。

    列(depth) = 最长入路径长度：无父节点为 0，否则 1 + max(父 depth)。
    同一列内按节点原始发射序竖直堆叠。坐标：
        x = x0 + depth * col_w
        y = y0 + row_in_column * row_h
    保证：不同列 x 不同；同列 row 不同 → 永不重叠；连线天然由左向右。
    坏数据兜底：环、缺失父节点、孤立节点均安全（视为 depth 0）。
    """
    if not nodes:
        return
    by_id = {n["id"]: n for n in nodes}
    parents: dict = {n["id"]: [] for n in nodes}
    for n in nodes:
        for w in (n.get("wires") or []):
            for dst in (w if isinstance(w, list) else [w]):
                if dst in by_id and dst != n["id"]:
                    parents[dst].append(n["id"])

    depth: dict = {}
    visiting = set()

    def dep(nid: str) -> int:
        if nid in depth:
            return depth[nid]
        if nid in visiting:       # 环保护
            return 0
        visiting.add(nid)
        ps = parents.get(nid, [])
        d = 0 if not ps else 1 + max(dep(p) for p in ps)
        visiting.discard(nid)
        depth[nid] = d
        return d

    for n in nodes:
        dep(n["id"])

    order = {n["id"]: i for i, n in enumerate(nodes)}
    cols: dict[int, list] = {}
    for n in nodes:
        cols.setdefault(depth[n["id"]], []).append(n["id"])
    for d, ids in cols.items():
        ids.sort(key=lambda i: order[i])
        for row, nid in enumerate(ids):
            by_id[nid]["x"] = x0 + d * col_w
            by_id[nid]["y"] = y0 + row * row_h


def _lint_layout(nodes: list) -> list:
    """布局级 lint（非阻塞，warn 级）：重叠检测 + 连线方向回退检测。

    重叠：两节点坐标完全相同 → 部署后在 NR 中叠在一起无法点击。
    回退：dst.x 比 src.x 还小（超过一列） → 「顺序颠倒」类结构，连线方向与布局冲突。
    """
    issues: list = []
    seen: dict = {}
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        key = (n.get("x"), n.get("y"))
        if key in seen:
            issues.append({
                "level": "warn", "rule": "LAYOUT_OVERLAP",
                "node_id": n["id"], "node_type": n.get("type"),
                "message": f"节点 {n['id']} 与 {seen[key]} 坐标重叠 {key}（部署后叠在一起）",
            })
        else:
            seen[key] = n["id"]
    for n in nodes:
        sx = n.get("x")
        for w in (n.get("wires") or []):
            for dst in (w if isinstance(w, list) else [w]):
                if dst in by_id:
                    dx = by_id[dst].get("x")
                    if isinstance(sx, int) and isinstance(dx, int) and dx < sx - LAYOUT_COL_W:
                        issues.append({
                            "level": "warn", "rule": "LAYOUT_BACKWARD",
                            "node_id": n["id"], "node_type": n.get("type"),
                            "message": f"连线由右向左回退超一列：{n['id']}(x={sx}) → {dst}(x={dx})，"
                                       f"疑似顺序颠倒/环",
                        })
    return issues


def render_flow_svg(nodes: list, *, title: str = "", path: str = None,
                     orientation: str = "vertical", width: int = 680) -> str:
    """渲染只读 SVG 预览（即时可视化，零外部依赖）。返回 SVG 字符串；path 非空则同时写盘。

    orientation="vertical"（默认）：纵向 top-down 渲染，深度→y、层内序→x。
        结构与原图一致（同连线、同分支/否则关系），仅旋转 90°，
        以便塞进 680px 宽面板、方框保持可读。
    orientation="horizontal"：按节点 x/y 自然比例横向渲染（与 NR 编辑器一致），
        图过宽时由展示容器横向滚动。
    用途：编译后即可看到布局是否有序、有无重叠/错乱，部署前拦截「顺序颠倒」。
    """
    if not nodes:
        return ""
    _C = {
        "inject": "#4caf50", "delay": "#9c27b0", "api-current-state": "#ff9800",
        "change": "#2196f3", "http request": "#00bcd4", "link out": "#9e9e9e",
        "switch": "#f44336", "api-call-service": "#795548", "debug": "#607d8b",
        "server-state-changed": "#3f51b5", "comment": "#999999",
    }

    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    by_id = {n["id"]: n for n in nodes}
    box_w, box_h = 230, 56

    if orientation == "horizontal":
        xs = [n.get("x", 0) for n in nodes]
        ys = [n.get("y", 0) for n in nodes]
        maxx = max(xs) + box_w + 30
        maxy = max(ys) + box_h + 30
        L = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{maxx}" height="{maxy}" '
              f'viewBox="0 0 {maxx} {maxy}" font-family="monospace" font-size="13" '
              f'role="img"><title>{_esc(title or "flow preview")}</title>']
        for n in nodes:
            sx, sy = n.get("x", 0), n.get("y", 0)
            for w in (n.get("wires") or []):
                for dst in (w if isinstance(w, list) else [w]):
                    if dst in by_id:
                        b = by_id[dst]
                        L.append(f'<line x1="{sx + box_w}" y1="{sy + box_h//2}" '
                                 f'x2="{b["x"]}" y2="{b["y"] + box_h//2}" '
                                 f'stroke="#999" stroke-width="2"/>')
        for n in nodes:
            x, y = n.get("x", 0), n.get("y", 0)
            t = n.get("type", "?")
            lab = f'{n.get("id", "?").split("_")[-1]} {t[:10]}'
            L.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="6" '
                     f'fill="{_C.get(t, "#607d8b")}" opacity="0.92"/>')
            L.append(f'<text x="{x + 8}" y="{y + 34}" fill="#fff" font-size="14" '
                     f'font-family="monospace">{_esc(lab)}</text>')
        svg = "\n".join(L)
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        return svg

    # ── vertical（默认）────────────────────────────
    # 由 x 反推深度层（x = x0 + depth*col_w），层内按 y 排序定 x。
    col_w = LAYOUT_COL_W
    x0 = LAYOUT_X0
    row_h = LAYOUT_ROW_H
    layers: dict = {}
    for n in nodes:
        depth = round((n.get("x", x0) - x0) / col_w) if col_w else 0
        layers.setdefault(depth, []).append(n)
    for d in layers:
        layers[d].sort(key=lambda n: n.get("y", 0))
    maxn = max((len(v) for v in layers.values()), default=1) or 1
    rw = min(box_w, int((width - 2 * x0) / maxn))  # 渲染列宽，保证塞进 width
    W = width
    H = (max((d for d in layers), default=0) + 1) * row_h + 40
    L = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
          f'viewBox="0 0 {W} {H}" font-family="monospace" font-size="13" '
          f'role="img"><title>{_esc(title or "flow preview")}</title>']
    pos = {}
    for d, ns in layers.items():
        for i, n in enumerate(ns):
            pos[n["id"]] = (x0 + i * rw, 40 + d * row_h)
    for n in nodes:
        ox, oy = pos[n["id"]]
        for w in (n.get("wires") or []):
            for dst in (w if isinstance(w, list) else [w]):
                if dst in pos:
                    dx, dy = pos[dst]
                    L.append(f'<line x1="{ox + rw//2}" y1="{oy + box_h}" '
                             f'x2="{dx + rw//2}" y2="{dy}" stroke="#999" stroke-width="2"/>')
    for n in nodes:
        ox, oy = pos[n["id"]]
        t = n.get("type", "?")
        lab = f'{n.get("id", "?").split("_")[-1]} {t[:9]}'
        L.append(f'<rect x="{ox}" y="{oy}" width="{rw}" height="{box_h}" rx="6" '
                 f'fill="{_C.get(t, "#607d8b")}" opacity="0.92"/>')
        L.append(f'<text x="{ox + 6}" y="{oy + 34}" fill="#fff" font-size="13" '
                 f'font-family="monospace">{_esc(lab)}</text>')
    svg = "\n".join(L)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
    return svg


def _split_segments(body: list[Step]) -> list[list[Step]]:
    """按 _SegmentBreak 哨兵将 body 拆分为多段。每段是独立链路，
    有自己的入口触发器源。哨兵本身不进入任何段。"""
    segments: list[list[Step]] = []
    current: list[Step] = []
    for st in body:
        if isinstance(st, _SegmentBreak):
            segments.append(current)
            current = []
        else:
            current.append(st)
    if current:
        segments.append(current)
    return [seg for seg in segments if seg]  # 过滤空段（防错）


def _emit_trigger(em: _Emitter, trg: Trigger, target: str) -> str:
    if trg.kind == "inject":
        props = [{"p": "payload"}]
        payload = ""
        payload_type = "date"
        # 自定义 props（如 _src / payload=...）
        if trg.inject_props:
            for k, v in trg.inject_props.items():
                if k == "payload":
                    payload = v
                    # 尝试识别 JSON（对象/数组）否则按字符串
                    try:
                        json.loads(v)
                        payload_type = "json"
                    except Exception:
                        payload_type = "str"
                else:
                    props.append({"p": k, "v": v, "vt": "str"})
        return em.add("inject", name="手动触发", props=props,
                      repeat="", crontab="", once=False,
                      payload=payload, payloadType=payload_type)
    if trg.kind == "time":
        return em.add("inject", name=f"定时 {trg.raw}", props=[{"p": "payload"}],
                      repeat="", crontab=trg.cron or "", once=False,
                      payload="", payloadType="date")
    # 所有 target（staging / e2e / prod）统一发射 server-state-changed + for 持久等待：
    # 状态触发器本质是「实体状态变化（持续 N 分钟）才触发」，与部署环境无关。
    # 历史实现仅 prod 发射、staging/e2e 降级为 inject，会静默丢失「持续 N 分钟」语义
    #（WB4 #2：编译绿灯、行为错误——staging 下 for 整段丢失）。e2e 路径通过
    # _e2e_entry_to_inject 把入口原地转合成 inject 来真实点燃（vhass 无 websocket），
    # 无副作用、无需在此区分 target。
    # 归一化状态值：DSL 中文语义词 → HA 真实 state（如「有人」→「on」）
    raw_state = (trg.state or "") if trg.state != "*" else "*"
    ha_state = _STATE_ALIAS.get(raw_state, raw_state)
    _extra = {
        # ★ 单输出：DSL 仅用「状态变到目标值」这一路（port0）。
        # 旧版写死 outputs:2，port1（未命中分支）永远空悬 → NR 编辑器显示
        # 悬空线头、视觉上像「线没连到」。DSL 不产出未命中分支，故收敛为 1。
        "outputs": 1,
        "outputInitially": False,
        "outputOnlyOnStateChange": True,
        # 持久等待：把 for_minutes（已折算为分钟）写进 for；无时长则 "0"（即时触发）。
        # forType="num" + forUnits="minutes" → NR 按分钟解释 for 值（如 5 = 持续 5 分钟）。
        "for": ("%g" % trg.for_minutes) if trg.for_minutes else "0",
        "forType": "num",
        "forUnits": "minutes",
        # 鲁棒性默认值：HA 启动/实体上线时防误触发（与 NR 默认新建节点行为一致）
        "ignorePrevStateNull": True,
        "ignorePrevStateUnknown": True,
        "ignorePrevStateUnavailable": True,
        "ignoreCurrentStateUnknown": True,
        "ignoreCurrentStateUnavailable": True,
    }
    if ha_state != "*" and ha_state:
        _extra["ifState"] = ha_state
        _extra["ifStateType"] = "str"
        _extra["ifStateOperator"] = "is"
    nid = em.add("server-state-changed", name=f"状态 {trg.raw}",
                  server=HA_SERVER_ID, version=6,
                  entities={"entity": [trg.entity], "substring": [], "regex": []},
                  outputProperties=[],
                  **_extra)
    if trg.first:
        # 文档节点：明确『首次』语义边界，避免误以为自动跨夜去重。
        # comment 节点不参与连线/E2E 插桩（已在 E2E_SINK_TYPES 内），仅作可视化说明。
        em.add("comment", name=(
            "首次触发（上升沿）：实体状态首次变到目标值时触发一次。"
            "注意：跨夜『每晚第一次』去重需额外 flow 变量/逻辑，本节点不自动实现。"))
    return nid


# ── Defect B 守卫（iss_60e4d57ce8 / high）──────────────────────────────────────
# history_* 子流程把答案写回 msg.payload，串行调用会互相覆盖；下游 switch 若引用被
# 覆盖的较早子流程字段 → 条件永假、动作永不执行（运行期不报错，黑箱静默 bug）。
# 编译期 fail-loud 拦截。映射仅收录各 history 子流程的【专属字段】
# （排除 entity/state/value 等跨域通用名），避免误伤普通 取值 字段（如 payload.state）。
# 与 subflows._SUBFLOW_OUTPUTS 的专属字段保持一致。
_HISTORY_FIELD_TO_SUBFLOW = {
    # history_state_at
    "found": "history_state_at", "nearest_ts": "history_state_at", "at_iso": "history_state_at",
    # history_occurred
    "occurred": "history_occurred", "events": "history_occurred",
    "first_ts": "history_occurred", "last_ts": "history_occurred",
    # history_duration
    "total_seconds": "history_duration", "total_human": "history_duration", "ratio": "history_duration",
    # history_aggregate
    "samples": "history_aggregate",
}


def _is_history_subflow_call(st) -> bool:
    """st 是否为 history_* 子流程调用（替换式写 msg.payload 的 subflow 实例）。"""
    if not isinstance(st, SubflowCall):
        return False
    try:
        spec = get_subflow(st.name)
    except Exception:
        return False
    call = getattr(spec, "call", None) or {}
    return call.get("type") == "subflow" and call.get("subflow_id") in HISTORY_SUBFLOW_IDS


def _jsonata_payload_fields(expr: str) -> set:
    """从 JSONata 表达式中抽取被引用的 payload.<field> 字段名集合。"""
    s = _strip_strings_and_comments(expr or "")
    return set(re.findall(r"payload\.([A-Za-z_]\w*)", s))


def _check_history_clobber_in_switch(st, live_history: Optional[str]) -> None:
    """Defect B：若本 switch 引用了 ≥2 个不同 history 子流程字段，或引用了
    已被后续 history 调用覆盖的较早子流程字段 → 编译期 fail-loud。

    live_history：当前线性序列中最近一次 history_* 调用的子流程名（其字段在 payload 中）。
    """
    referenced = set()
    for br in (getattr(st, "branches", None) or []):
        for f in _jsonata_payload_fields(getattr(br, "condition", "") or ""):
            owner = _HISTORY_FIELD_TO_SUBFLOW.get(f)
            if owner:
                referenced.add(owner)
    if not referenced or live_history is None:
        # 本序列无 history 调用 → 引用历史字段属未定义字段域（R31 另管），不在此拦截
        return
    if referenced != {live_history}:
        names = "、".join(sorted(referenced))
        raise DSLError(
            f"分支 引用了 history_* 子流程 {names} 的输出字段，但当前线性序列中最近一次 "
            f"history 查询是 {live_history}，前者结果已被后者串行覆盖"
            f"（history_* 子流程把答案写回 msg.payload，后调用者抹掉前调用者）。"
            f"运行态该字段恒为 undefined → 分支条件永假、动作永不执行（且不报错）。"
            f"请改为：① 嵌套——在第一个 history 查询命中后的『分支』内再调第二个；"
            f"或 ② 用『提取: 变量 = payload.<字段>』先把首个结果暂存到变量，再调第二个查询，"
            f"下游分支读该变量而非 payload.<字段>。",
            getattr(st, "line", None), code=C_HISTORY_CLOBBER)


def _emit_body(em: _Emitter, steps: list, sources: list, x: int = 200):
    """顺序编排步骤。sources 为上游节点 id 列表（fan-in）；首节点接收全部上游连线，
    之后顺次链接。遇 并行 块做 fan-out。连续的 提取 步骤合并进同一个 change 节点。

    返回 (head, tail)：
      - head = 本段第一个被 emit 的节点（分支/否则 入口，上游必须连到它）；
      - tail = 本段最后一个节点（线性链续接用）。
    此前只返回 tail，导致 分支/否则 体首节点永远收不到上游连线 → 孤儿节点
    （被 R13 抓出的 22 例真实提案皆是此因）。"""
    last = None
    head = None
    sources = list(sources)
    # R4(#round4)：记录「喂给 last 那一步的上游 id」。switch 之后的并行块要回挂到
    # 这里，而不是挂在 switch 的输出上（详见下方注释）。
    last_upstream: list = []
    pending_extract: Optional[str] = None  # 当前正在累积的「提取」change 节点 id
    live_history: Optional[str] = None      # 本线性序列最近一次 history_* 调用名（其字段在 payload）
    for st in steps:
        if isinstance(st, Parallel):
            pending_extract = None
            # 并行块：每个子节点都从【同一上游】扇出（并行执行），不串行链接。
            # 上游 = sources（本段首节点时）或 last（接在串行链之后时）。
            # 并行块本身不推进串行链：块后同级步骤仍从同一上游扇出（而非接并行块尾节点）。
            upstream = list(sources) if sources else ([last] if last else [])
            # R4(#round4) iss_a2ee55d8c8：switch 节点【没有直通输出】——它的 output0
            # 就是第一条分支。旧实现对 `分支:` 之后的同级 `并行:` 直接 em.connect(switch, 子节点)，
            # 而 connect() 一律写 wires[0] → 并行动作被编译进「第一个分支」的 wires，
            # 语义从「无论条件都并行」退化成「仅首分支命中才执行」（静默、lint 全过）。
            # 正解：回挂到喂给 switch 的同一上游，使并行路径与 switch 平级、独立起线。
            if (not sources) and last and last_upstream and _is_fanout_node(em, last):
                upstream = list(last_upstream)
            for child in st.children:
                child_head, _ = _emit_step(em, child, x=x)
                cid = child_head
                if head is None:
                    head = cid
                for s in upstream:
                    em.connect(s, cid)
            if last:
                # 接在串行链之后：后续同级步骤仍从同一前驱扇出；并行块不推进链
                sources = []
                # last 保持不变
            else:
                # 是首节点（由 sources 喂入）：后续同级步骤也从同一 sources 扇出
                sources = list(upstream)
                # last 保持 None
            continue
        if isinstance(st, Extract):
            if pending_extract is None:
                pending_extract = em.add("change", name="提取字段", rules=[])
                if head is None:
                    head = pending_extract
                if sources:
                    for s in sources:
                        em.connect(s, pending_extract)
                    last_upstream = list(sources)
                    sources = []
                elif last:
                    em.connect(last, pending_extract)
                    last_upstream = [last]
                last = pending_extract
            em._find(pending_extract)["rules"].append(
                {"t": "set", "p": st.name, "pt": "msg",
                 "to": _sanitize_jsonata(st.expr), "tot": "jsonata"})
            continue
        pending_extract = None
        # Defect B 守卫（iss_60e4d57ce8）：跟踪本线性序列 history_* 调用顺序，
        # 供后续 switch 引用检查捕获「后者覆盖前者」的静默失败。
        if isinstance(st, SubflowCall) and _is_history_subflow_call(st):
            live_history = st.name
        elif isinstance(st, Switch):
            _check_history_clobber_in_switch(st, live_history)
        head_id, tail_id = _emit_step(em, st, x=x)
        # 修复：Comment 不参与连线，仅作可视化说明
        if isinstance(st, Comment):
            continue
        nid = head_id
        if head is None:
            head = nid
        # R7(#round4) iss_516bc5d816（报告 A16）：link-out 型子流程是 fire-and-forget，
        # 编译产物是 `change(设 msg.payload=入参) → link out`，msg.payload 已被入参整体
        # 覆写，而 NR 没有任何把结果送回调用点的回执通道。旧实现把它当普通线性步骤
        # （tail=设参 change），于是后续 `提取:`/续接节点被串在**请求侧**：既等不到子流程
        # 结果，读到的还是自己刚写进去的入参 —— 静默取错值，lint/闸门全过。
        # 正解：按「副链」编排（复用 并行块 的模型）——请求链从当前上游分叉出去，主链
        # 不被它推进，后续步骤仍从**调用前的上游**起线，拿到未被污染的 msg。
        # 需要返回值请改用 http_api / 子流程实例（返回值落 payload.reply）；
        # 「link-out 之后紧跟 提取:」由 validate() 给出 C_SUBFLOW_NO_REPLY 警告。
        fire_and_forget = _is_fire_and_forget_call(st)
        if sources:
            for s in sources:
                em.connect(s, nid)
            last_upstream = list(sources)
            if not fire_and_forget:
                sources = []
        elif last:
            em.connect(last, nid)
            last_upstream = [last]
        else:
            last_upstream = []
        if not fire_and_forget:
            last = tail_id
    return head, last


def _is_fanout_node(em: "_Emitter", nid: str) -> bool:
    """R4(#round4)：判断节点是否是「条件分叉」节点（无直通输出）。

    switch 的 wires[0] 语义是「第一条规则命中」而非「直通」，所以任何需要
    *无条件* 执行的后继（并行块）都不能挂在它身上。"""
    nd = em._find(nid)
    return bool(nd) and nd.get("type") == "switch"


def _is_fire_and_forget_call(st) -> bool:
    """R7(#round4)：该步骤是否是「无回执」的异步子流程调用（link_out 型）。

    link_out 编译成 `change(设入参) → link out`：消息发到目标 tab 的 `link in`
    就结束了，Node-RED 侧**没有**把结果送回调用点的通道（回送要靠
    `link call` + `link out(mode=return)`，本项目 4 个 link_out 能力都不是——
    出口 link out 要么指向 TTS 下游、要么为空）。
    所以它在主链上等价于一次「副作用」，不产生可续接的返回值，必须编成副链，
    否则后续步骤会挂到被入参覆写的请求侧（详见 _emit_body 中的 R7 注释）。"""
    if not isinstance(st, SubflowCall):
        return False
    try:
        spec = get_subflow(st.name)
    except Exception:
        return False
    return bool(spec) and ((getattr(spec, "call", None) or {}).get("type") == "link_out")


def _emit_step(em: _Emitter, st: Step, x: int = 200):
    """返回 (head, tail)：head=本步骤首个节点（承接上游连线），tail=末节点（续接下游）。

    单节点步骤返回 (nid, nid)；多节点步骤（如 http_api 的 change→http）由对应
    emitter 直接返回 (head, tail)。"""
    if isinstance(st, Action):
        nid = _emit_action(em, st)
    elif isinstance(st, SubflowCall):
        nid = _emit_subflow(em, st)
    elif isinstance(st, Delay):
        # 补齐 NR delay 节点全部默认字段（rate/随机/丢弃开关），否则编辑器
        # 判定"字段集与类型 defaults 不一致"→ 节点显示红色警告三角。
        # 这些字段对 pauseType=delay 虽不参与运算，但 NR 调色板拖出的节点
        # 必带，缺失会让生成流在编辑器里观感"脏"。
        nid = em.add("delay", name=f"延时 {st.seconds}s",
                     pauseType="delay", timeout=str(st.seconds * 1000),
                     timeoutUnits="milliseconds",
                     rate="1", nbRateUnits="1", rateUnits="second",
                     randomFirst="1", randomLast="5", randomUnits="seconds",
                     drop=False, allowrate=False)
    elif isinstance(st, CurrentState):
        nid = _emit_current_state(em, st)
    elif isinstance(st, ReadState):
        nid = _emit_read_state(em, st)
    elif isinstance(st, TimeRange):
        nid = _emit_time_range(em, st)
    elif isinstance(st, Switch):
        nid = _emit_switch(em, st)
    elif isinstance(st, Debug):
        nid = _emit_debug(em, st)
    elif isinstance(st, Comment):
        nid = _emit_comment(em, st)
    elif isinstance(st, HttpRequest):
        nid = _emit_http(em, st)
    elif isinstance(st, Build):
        nid = _emit_build(em, st)
    elif isinstance(st, RawNode):
        nid = _emit_raw(em, st)
    else:
        raise DSLError(f"暂不支持的步骤类型：{type(st).__name__}", code=C_UNKNOWN_STEP)
    # 多节点步骤（如 http_api 的 change→http）由 _emit_subflow 直接返回 (head, tail)
    if isinstance(nid, tuple):
        return nid
    return (nid, nid)


def _emit_current_state(em: _Emitter, st: CurrentState) -> str:
    """编译为 api-current-state 节点（2 输出：pass/fail）。
    pass 输出(0)继续主链 body，fail 输出(1)走 else_body（若有）。
    halt_if 由查询状态驱动（关→off / 开→on），与 state_value 一致——修复硬编码 "off" 的反模式。"""
    entity = _resolve_entity(st.entity)
    nid = em.add("api-current-state", name=f"查询 {st.entity}",
                 server=HA_SERVER_ID, version=7,
                 entityId=entity,
                 state_type="str", blockInputOverrides=False,
                 outputProperties=[{"property": "payload", "propertyType": "msg",
                                    "value": "", "valueType": "entityState"}],
                 state_location="data", override_payload=False,
                 # state_type=str, comparator=is → 精确匹配 state 值
                 state_value=st.state,
                 # 不门控：仅把实体态输出到 msg.payload（node 原生状态改写 msg.data 避免冲突），
                 # 分支路由交由后续 switch 节点按 payload 判定（与 _emit_condition / 取值节点一致）
                 halt_if="", halt_if_type="str", halt_if_compare="is",
                 outputs=1)
    # 分支路由（替代原 halt_if 门控）：state==st.state → body（out0），否则 → else_body（out1）
    sw = em.add("switch", name=f"分支: {st.entity}={st.state}",
                property="payload", propertyType="msg",
                rules=[{"t": "eq", "v": st.state, "vt": "str"}, {"t": "else"}],
                checkall="true", repair=False, outputs=2)
    em.connect(nid, sw)
    main_last = None
    if st.body:
        _, tail = _emit_body(em, st.body, [sw], x=560)
        if tail:
            main_last = tail
    # fail 分支（否则体）：从 switch 的 out1 串接 —— 必须连到体首节点(head)，否则首节点孤儿
    if st.else_body:
        else_head, _ = _emit_body(em, st.else_body, [], x=560)
        if else_head:
            em.connect_out(sw, 1, else_head)
    # 关键修复：作为「门」被嵌入其它 body 时，父节点必须把连线接到本门【入口】(nid)，
    # 而非门体尾节点；否则本门自身会被留成孤儿（见 嵌套门孤儿接线 bug）。
    # 门体内 body/else_body 已在上方用 connect/connect_out 各自接到本门输出口，无需返回尾节点。
    return nid


def _emit_read_state(em: _Emitter, st: ReadState) -> str:
    """读实体状态数值，写到 msg.payload（或 msg.payload.<field>），同时别名到 msg.payload.state。
    复用 api-current-state（HA 贡献节点），不生成 Function。
    halt_if 置空 → 不做门控，仅透传并把 state 输出到 outputProperties 指定的字段。

    C3 修复（取值→提取 路径错位）：旧实现把状态写到扁平 msg.<field>，但 DSL 文档与黑箱 agent
    都用『提取: X = payload.<field>』/『提取: X = payload.state』读取（见 dsl_engine 头部文档），
    扁平字段与 payload 子路径错位 → 提取恒 undefined。现统一写到 msg.payload 下：
    - 具名字段：msg.payload.<field> + msg.payload.state（两种取用习惯都覆盖）
    - 无字段：整条 msg.payload = state（与文档"返回值在 msg.payload"一致）

    ★ WB72 缺陷#3 / #712（标量 payload 静默吞写）：
    NR 的 `RED.util.setObjectProperty` 在写多段路径 `payload.<field>` 时，逐段下钻中间
    路径；若中间值**既不是 object 也不是 undefined**（典型：inject 默认
    payloadType="date" → msg.payload 是时间戳数值），它既不下钻也不创建，直接
    `return false` —— **静默失败，不抛错、不告警**。于是 payload.<field> 从未写入，
    下游 switch 读到 undefined，链路在首节点后无声断裂（WB72 wb66–wb71 多轮修补
    始终打不通即此因，且黑箱/白箱静态校验全盲，只有真机 E2E 能暴露）。

    修法：在字段写入**之前**插一条 payload 保底归一（HA 节点按数组顺序逐条
    setMessageProperty 到同一 msg，故顺序即执行序）：
      · payload 已是对象 → 原样保留（多次「取值」可在同一 payload 上累积字段，
        不会互相覆盖 —— 这正是 WB72 lux + comp 双取值场景所需）；
      · 否则（标量/数组/缺失）→ 重置为 {}，让后续子字段写入有合法落点。
    只归一容器、不碰取值语义：实体态仍由 entityState 项独家写入。"""
    entity = _resolve_entity(st.entity)
    if st.field:
        output_properties = [
            {"property": "payload", "propertyType": "msg",
             "value": '$type(payload) = "object" ? payload : {}', "valueType": "jsonata"},
            {"property": f"payload.{st.field}", "propertyType": "msg",
             "value": "", "valueType": "entityState"},
            {"property": "payload.state", "propertyType": "msg",
             "value": "", "valueType": "entityState"},
        ]
    else:
        output_properties = [
            {"property": "payload", "propertyType": "msg",
             "value": "", "valueType": "entityState"},
        ]
    nid = em.add("api-current-state", name=f"取值 {st.entity}",
                 server=HA_SERVER_ID, version=7,
                 entityId=entity,
                 state_type="str", blockInputOverrides=False,
                 outputProperties=output_properties,
                 # WB23 #634 修复：NODE_DEFAULT_FIELDS 给 api-current-state 注入的
                 # state_location="payload" + override_payload=True 会与上面的 outputProperties
                 # 同写 msg.payload（或 payload.*），在 NR 节点上冲突 → 节点吐时间戳
                 # 而非数值（全实体类型通病，gate 全盲）。此处把节点原生状态输出改写到
                 # msg.data（避开 payload），由 outputProperties 独家负责把实体态写入
                 # msg.payload（含 payload.<field> / payload.state 别名），消除同写冲突。
                 state_location="data", override_payload=False,
                 # 空 halt_if → 不门控，仅透传
                 halt_if="", halt_if_type="str", halt_if_compare="is",
                 outputs=1)
    return nid


def _emit_time_range(em: _Emitter, st: TimeRange) -> str:
    """编译为 time-range-switch 节点（2 输出：在窗口内→out0 继续主链；
    否则→out1 空=停止）。

    关键：真实在 NR 上注册的节点类型是 `time-range-switch`（非 `time-range`）。
    旧版误发 `time-range` 导致部署即坏——该类型未注册，陌生节点静默丢 msg，
    整条下游断掉，且白箱/黑箱都查不出（仅真机 E2E 能抓）。"""
    nid = em.add("time-range-switch", name=f"时间段 {st.start}-{st.end}",
                  startTime=st.start, endTime=st.end,
                  startOffset=0, endOffset=0,
                  only=st.weekday or "all",
                  outputs=2)
    # 2 输出节点需显式双 wires 数组（em.add 默认只给 [[]]）
    nd = em._find(nid)
    nd["wires"] = [[], []]   # out0=窗口内(接主链)，out1=窗口外(否则体，无否则则空=停止)
    # 通过分支（主链 body）：从 out0 串接（_emit_body 默认接父节点输出0）
    if st.body:
        _emit_body(em, st.body, [nid], x=560)
    # 窗口外分支（否则体）：从 out1 串接 —— 必须连到体首节点(head)，否则首节点孤儿。
    # ★FEEDBACK #9：此前 TimeRange 无 else_body 字段，`时间段:`+`否则:` 编译期崩溃。
    if st.else_body:
        else_head, _ = _emit_body(em, st.else_body, [], x=560)
        if else_head:
            em.connect_out(nid, 1, else_head)
    # 关键修复：作为「门」被嵌入其它 body 时须返回本门【入口】(nid)，
    # 否则父节点会把连线接到门体尾节点、本门自身留成孤儿（嵌套门孤儿接线 bug）。
    # 门体已在上方从本门 out0 串接好，无需返回尾节点。
    return nid


def _emit_action(em: _Emitter, st: Action) -> str:
    # notify 域服务（如 notify.mobile_app）无实体目标，目标在 params 里；其余域解析 target
    # B4 修复：targets 多实体逐个解析，产出 [id1, id2] 列表（NR api-call-service entityId 数组形态）
    if st.domain == "notify":
        entity_id = []
    else:
        raw_targets = st.targets if getattr(st, "targets", None) else ([st.target] if st.target else [])
        entity_id = [_resolve_entity(t) for t in raw_targets]
    # #506：动作参数若引用【已声明场景变量】，改用 dataType=jsonata 读 flow 上下文
    # （flow.<变量名>，原生类型）。否则变量沦为死变量、且数值参数落到 HA 会被当字符串。
    # 未引用变量的动作保持原 json 形态（dataType=json，_coerce_params 数值归一）。
    params = dict(st.params)
    uses_var = em.flow_vars and any(
        isinstance(v, str) and v in em.flow_vars for v in params.values()
    )
    if uses_var:
        pairs = []
        for k, v in params.items():
            key = json.dumps(k, ensure_ascii=False)
            if isinstance(v, str) and v in em.flow_vars:
                val = f"flow.{v}"
            else:
                cv = _coerce_scalar(v)
                if isinstance(cv, bool):
                    val = "true" if cv else "false"
                elif isinstance(cv, (int, float)):
                    val = str(cv)
                else:
                    val = json.dumps(cv, ensure_ascii=False)
            pairs.append(f"{key}: {val}")
        data_field = "{" + ", ".join(pairs) + "}"
        data_type = "jsonata"
    else:
        data_field = json.dumps(_coerce_params(params), ensure_ascii=False)
        data_type = "json"
    return em.add("api-call-service", name=f"{st.domain}.{st.service}",
                  server=HA_SERVER_ID, version=7,
                  action=f"{st.domain}.{st.service}",
                  domain=st.domain, service=st.service,
                  entityId=entity_id,
                  data=data_field, dataType=data_type, mergeContext="", queue="none",
                  outputProperties=[])


def _emit_subflow(em: _Emitter, st: SubflowCall) -> str:
    spec: SubflowSpec = get_subflow(st.name)
    try:
        args = spec.resolve_args(st.raw_args, dynamic=getattr(st, "jsonata_args", None))
    except ValueError as e:
        raise DSLError(f"子流程 {st.name} 参数解析失败：{e}",
                       getattr(st, "line", None), code=C_SUBFLOW_ARG)
    flat = getattr(spec, "param_style", "payload") == "flat"

    def _to_tot(v):
        """把子流程入参值映射为 NR change 规则的 (to, tot)。
        - 数值(int/float) → tot=num, to=str(v)（保证 to 恒为字符串，避免 NR 把 int 写进 to 的反模式）
        - 布尔 → tot=bool, to=str(v).lower()
        - JSON 字符串 → tot=json
        - 其余 → tot=str, to=str(v)
        旧实现把 priority=3 这类 int 直接塞进 to（to: 3, tot: str），
        既不符合 NR change 节点 to 必为字符串的约定，又会让下游 `str(to)` 类断言崩溃。"""
        if isinstance(v, bool):
            return str(v).lower(), "bool"
        if isinstance(v, (int, float)):
            return str(v), "num"
        try:
            json.loads(v)
            return v, "json"
        except Exception:
            return str(v), "str"

    if st.jsonata_args:
        # 含动态（JSONata）参数：逐属性 set，动态参数用 jsonata 求值、静态参数按字面量
        rules = []
        for k, v in args.items():
            p = k if flat else f"payload.{k}"
            if k in st.jsonata_args:
                # #507：子流程 JSONata 入参里的裸变量名绑定到 flow 上下文
                rules.append({"t": "set", "p": p, "pt": "msg", "to": _bind_flow_vars(_sanitize_jsonata(v), em.flow_vars), "tot": "jsonata"})
            else:
                to, tot = _to_tot(v)
                rules.append({"t": "set", "p": p, "pt": "msg", "to": to, "tot": tot})
        cid = em.add("change", name=f"设置 {st.name} 入参", rules=rules)
    elif flat:
        # 平铺模式：每个入参直接 set 到 msg.<k>（适配读 msg.title/msg.body 的子流程）
        rules = []
        for k, v in args.items():
            to, tot = _to_tot(v)
            rules.append({"t": "set", "p": k, "pt": "msg", "to": to, "tot": tot})
        cid = em.add("change", name=f"设置 {st.name} 入参", rules=rules)
    else:
        cid = em.add("change", name=f"设置 {st.name} 入参",
                     rules=[{"t": "set", "p": "payload", "pt": "msg",
                             "to": json.dumps(args, ensure_ascii=False), "tot": "json"}])
    call = spec.call
    if call["type"] == "link_out":
        lid = em.add("link out", name=f"→ {st.name}", links=[call["entry_link_id"]])
        em.connect(cid, lid)
        return (cid, cid)
    if call["type"] == "http_api":
        # 网关内联「拼请求体 + http 调用 + 取返回值」为隐藏节点，agent 不碰 URL。
        # head=cid(设参) → http request → tail=extract(把 reply 规整到 msg.payload.reply)。
        http = em.add("http request", name=f"→ {st.name}",
                      method=call.get("method", "POST"),
                      ret="obj", paytoqs="ignore", url=call["url"], wires=[[]])
        em.connect(cid, http)
        extract = (call.get("extract") or "").strip()
        # R8(#round4) iss_6748217860 / A28：extract 恰好等于落点 `payload.reply` 时，
        # 生成的 change 规则是 `msg.payload.reply = msg.payload.reply` —— 纯自赋值空
        # 节点（http 节点 ret=obj 已把响应体放进 msg.payload）。它不改变任何数据，
        # 只是在每条 llm_* 链路中间插一个看着「有处理」实则空转的节点，既污染产物
        # 又误导排障。这里直接不发射。
        if extract and extract != "payload.reply":
            ext = em.add("change", name=f"取 {st.name} 返回值",
                         rules=[{"t": "set", "p": "payload.reply", "pt": "msg",
                                 "to": extract, "tot": "jsonata"}])
            em.connect(http, ext)
            return (cid, ext)
        return (cid, http)
    # 默认：subflow 实例（返回内部 subflow 节点，请求-响应透传返回值）
    # 注意：NR 5.x 子流程**实例**的 type 必须是 "subflow:<id>"（带前缀）。
    # 旧写法 type=<裸id> + flow=<裸id> 是 NR 1.0 之前的遗留格式，NR 5.x 不识别，
    # 会渲染成 "unknown: <id>"（编辑器红虚线、无法连线）。见 2026-07-20 书房专注模式场景。
    lid = em.add("subflow", name=st.name, type=f"subflow:{call['subflow_id']}",
                 wires=[[]])
    em.connect(cid, lid)
    # 关键修复：返回 (cid, lid) 而非 (cid, cid)。
    # 旧实现把 tail 设为 change 节点 cid，导致下游连线接到 cid、子流程实例 lid 的
    # 输出口 wires=[[]] 永远空 → 子流程的返回值（经其输出口透传的 msg）被丢弃。
    # 对 bark_push（fire-and-forget）无影响；对 history_*（请求/响应）则是硬伤：
    # agent 分支拿不到答案。修正后下游接到 lid 输出口，返回值正常透传。
    return (cid, lid)


def _emit_debug(em: _Emitter, st: Debug) -> str:
    return em.add("debug", name=st.name or "观测",
                  active=True, tosidebar=True, console=False, tostatus=False,
                  complete="payload", targetType="msg")


def _emit_comment(em: _Emitter, st: Comment) -> str:
    return em.add("comment", name=st.text or "注释")


def _emit_http(em: _Emitter, st: HttpRequest) -> str:
    nid = em.add("http request", name=f"{st.method} {st.url}",
                 method=st.method, ret="obj", paytoqs="ignore",
                 url=st.url, tls="", persist=False, proxy="",
                 insecureHTTPParser=False, authType="", senderr=False,
                 headers=st.headers or [])
    if st.body:
        nd = em._find(nid)
        nd["body"] = st.body
        nd["bodyType"] = "json"
    return nid


def _emit_build(em: _Emitter, st: Build) -> str:
    """发 change 节点，把 msg.payload 设为请求体（字面 JSON 或 JSONata 表达式）。
    下游 http request 节点在不带字面 body 时会自动发送这个 msg.payload。"""
    if st.kind == "json":
        to, tot = json.dumps(st.literal, ensure_ascii=False), "json"
    else:
        # #507：构建请求体的 JSONata 表达式里裸变量名绑定到 flow 上下文
        to, tot = _bind_flow_vars(_sanitize_jsonata(st.expr), em.flow_vars), "jsonata"
    return em.add("change", name="构建请求体",
                  rules=[{"t": "set", "p": "payload", "pt": "msg",
                          "to": to, "tot": tot}])


def _state_value_type(v: str) -> str:
    """推断 api-current-state 的 state_type / halt_if_type：数字→num，其余→str。"""
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return "num"
    return "str"


def _coerce_scalar(v):
    """把 DSL 参数值按语义归一：数值字符串 → int/float（HA 服务调用要求数值类型，
    而非字符串），其余保持原样。

    ★ 修复：旧实现把 Action 参数原样 json.dumps，于是 `brightness=80` / `temperature=22`
    编译成 `"80"` / `"22"`（字符串）。HA 的 climate.set_temperature 等服务的数值字段
    收到字符串会校验失败或静默降级，真实部署的服务调用失效。子流程参数早已用同款
    归一（_to_tot），此处对齐，保证 动作 调用的数值参数也是真正的 JSON number。

    布尔值保持 bool；非数值字符串（如 text=欢迎回家、room=客厅）原样保留。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    return v


def _coerce_params(params: dict) -> dict:
    """整参数字典做 _coerce_scalar（键不变，值按语义归一）。"""
    return {k: _coerce_scalar(val) for k, val in params.items()}


def _flow_var_to_tot(v: str):
    """把 `变量: X = v` 的字符串值按语义映射为 (to, tot)，使 flow 上下文存原生类型
    （而非一律字符串），避免变量被下游 JSONata / HA 服务调用误读成字符串。

    - 去掉用户常写的外层引号（变量: X = 'on' / "eco"）→ 引号是用户引用而非数据；
    - true/false → tot=bool；
    - 整数/小数 → tot=num（HA/JSONata 拿到真数值）；
    - JSON 对象/数组字面量（{...} / [...] 且可解析）→ tot=json；
    - 其余（含中文文本、带单位文本）→ tot=str。
    to 恒为字符串（NR change 节点 to 必为字符串的约定）。"""
    s = v.strip()
    if len(s) >= 2 and s[0] in "\"'\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    low = s.lower()
    if low in ("true", "false"):
        return low, "bool"
    if re.fullmatch(r"-?\d+", s):
        return s, "num"
    if re.fullmatch(r"-?\d+\.\d+", s):
        return s, "num"
    if s and s[0] in "{[":
        try:
            json.loads(s)
            return s, "json"
        except Exception:
            pass
    return s, "str"


# 标识符改写时必须整体跳过的片段：mustache 模板 与 引号字符串字面量。
# 字符串字面量里的中文是【正文】，把 '阈值 30' 改成 'flow.阈值 30' 会直接改烂用户文案
# （R2/R5#round4 引入模板插值后必现；此前无引号字面量所以没暴露）。
_PROTECTED_SEG_RE = re.compile(
    r"(\{\{.*?\}\}|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")")


def _split_protected(expr: str) -> list:
    """按「受保护片段」切分表达式，受保护片段原样保留在结果里。"""
    return _PROTECTED_SEG_RE.split(expr)


def _is_protected_seg(seg: str) -> bool:
    return bool(seg) and (seg.startswith("{{") or seg[0] in "'\"")


def _bind_flow_vars(expr: str, var_names):
    """把 JSONata 表达式里的【已声明场景变量名】绑定到 flow 上下文（flow.<name>）。

    作用：变量（变量: X=v 经 _flow_var_to_tot 写入 flow 上下文）此前在 JSONata 里是死变量——
    裸写 X 既不被识别为 flow 也不被识别为 msg，节点不认识 X → 表达式静默不求值。现改写裸变量名
    为 flow.X，使 JSONata 从 flow 上下文取到原生类型变量值（iss_185a55e085 根因修复 #507）。

    安全边界：
    - 只改写【声明过】的变量名，未声明标识符（如 msg 字段 s、$函数）不受影响；
    - 已带 flow./msg./$ 前缀的标识符不重复改写（负向后顾 `[^.\\w$一-鿿]`）；
    - 词尾边界 `[^一-鿿\\w]` 防止「阈值」误吞进「阈值XX」这类更长标识符；
    - mustache {{...}} 整体跳过（变量绑定只在 JSONata 语义层，不碰模板占位符）；
    - **引号字符串字面量整体跳过**（R2/R5#round4）：`'阈值 30'` 里的「阈值」是正文
      不是标识符，改写成 `'flow.阈值 30'` 会把用户文案改烂；
    - 按变量名长度降序处理，避免短名误吞长名前缀。幂等（flow.X 内 X 不被二次改写）。"""
    if not expr or not var_names:
        return expr
    parts = _split_protected(expr)
    names = sorted(var_names, key=len, reverse=True)
    out = []
    for seg in parts:
        if _is_protected_seg(seg):
            out.append(seg)
            continue
        s = seg
        for nm in names:
            rx = re.compile(r"(?<![.\w$一-鿿])" + re.escape(nm) + r"(?![一-鿿\w])")
            s = rx.sub(r"flow." + nm, s)
        out.append(s)
    return "".join(out)


def _bind_read_fields(expr: str, field_names) -> str:
    """把 switch JSONata 表达式里的【取值字段名】绑定到 msg.payload 上下文（payload.<field>）。

    取值 节点把实体状态写到 msg.payload.<field>（而非 msg.<field>）；而 NR switch 的 JSONata 规则
    以 msg 为求值上下文（裸 identifier 解析为 msg.<id>）。于是 取值 X 后用 分支: $number(X)>25 时，
    $number(X) 实际读 msg.X（undefined）→ 分支恒走 else（WB25-NEW-2 / #634 数据断裂根因之一）。

    此处把开关规则里的裸字段名改写为 payload.X，使其与 取值 的落点对齐。
    安全边界（同 _bind_flow_vars）：只改写【取值过】的字段名；已带 payload./msg./flow./$ 前缀或
    {{...}} 模板的标识符不重复改写（负向后顾 `[^.\\w$一-鿿]`）；词尾边界 `[^一-鿿\\w]` 防长标识符
    误吞；mustache {{...}} 与引号字符串字面量整体跳过；按字段名长度降序处理，避免短名误吞长名
    前缀；幂等。"""
    if not expr or not field_names:
        return expr
    parts = _split_protected(expr)
    names = sorted(field_names, key=len, reverse=True)
    out = []
    for seg in parts:
        if _is_protected_seg(seg):
            out.append(seg)
            continue
        s = seg
        for nm in names:
            rx = re.compile(r"(?<![.\w$一-鿿])" + re.escape(nm) + r"(?![一-鿿\w])")
            s = rx.sub(r"payload." + nm, s)
        out.append(s)
    return "".join(out)


def _collect_read_fields(scene: Scene) -> set:
    """收集场景里所有 取值 步骤的具名字段名（落点 msg.payload.<field>）。

    供 _emit_switch 对分支/条件里的裸字段名做 JSONata 路径对齐（WB25-NEW-2）。
    仅收 ReadState 的具名字段；提取(Extract) 写的是 msg.<name> 顶层字段，由 _parse_switch_rule
    的 eq 规则 property 直接读 msg.<name>，不需对齐，故不收。"""
    fields: set = set()

    def walk(steps):
        for st in steps:
            if isinstance(st, ReadState) and st.field:
                fields.add(st.field)
            elif isinstance(st, Switch):
                for b in st.branches:
                    walk(b.body)
                walk(st.else_body)
            elif isinstance(st, (CurrentState, TimeRange)):
                walk(getattr(st, "body", []))
                walk(getattr(st, "else_body", []))
            elif isinstance(st, Parallel):
                for c in st.children:
                    walk([c])
    walk(scene.body)
    return fields


def _sanitize_jsonata(expr: str) -> str:
    """编译期 JSONata 消毒：把 agent 常误输的全角符号转半角，避免 JSONata 静默不求值
    （全角括号 R7 根因）。仅做字符归一，不动语义。空串原样返回。"""
    if not expr:
        return expr
    repl = {
        "（": "(", "）": ")", "＝": "=", "≠": "!=",
        "＞": ">", "＜": "<", "≥": ">=", "≤": "<=",
    }
    out = expr
    for fw, hw in repl.items():
        out = out.replace(fw, hw)
    return out


def _parse_state_condition(cond: str) -> Optional[tuple]:
    """解析条件中的状态断言，返回 (entity_id, expected_value, compare, value_type) 或 None。

    支持多种写法（均编译为 api-current-state 节点，避免 $state 非函数 JSONata 崩溃）：
      1. $state('entity') = 'value'        （旧写法，compare=is）
      2. entity = value  /  entity == value （自然写法，最常用，compare=is）
      3. entity != value / entity <> value / entity ≠ value （否定，compare=is_not）
    兼容全角符号（＝ ≠ （ ））经 _sanitize_jsonata 归一后再判。
    其他复杂表达式（含 $ / 函数调用( / and / or / < / > 等）返回 None → 走 jsonata switch 兜底。
    """
    c = _sanitize_jsonata(cond.strip())
    c = c.rstrip(":")  # 剥离 DSL 块尾冒号（如「条件: X == on:」的冗余冒号）
    # 1. $state('entity') = 'value'
    m = re.match(r"^\s*\$state\(\s*'([^']+)'\s*\)\s*=\s*'([^']*)'\s*$", c)
    if m:
        ent, val = m.group(1), m.group(2)
        return (ent, val, "is", _state_value_type(val))
    # 2/3. entity = value  /  entity == value  /  entity != value  /  entity <> value（自然写法）
    # 排除明显是 JSONata 表达式的情况：含 $、函数调用(、逻辑词、比较符
    if any(ch in c for ch in "$(") or re.search(r"\b(and|or)\b", c) or "<" in c or ">" in c:
        return None
    # value 不含冒号（避免 DSL 块尾冒号 "on:" 被误吞进 value，导致 switch 判 "on:"≠"on"）
    m2 = re.match(r"^([\w.\-:]+)\s*(!=|==|<>|=)\s*([\w.\-]+)$", c)
    if m2:
        ent, op, val = m2.group(1), m2.group(2), m2.group(3)
        compare = "is_not" if op in ("!=", "<>") else "is"
        return (ent, val, compare, _state_value_type(val))
    return None


def _parse_switch_rule(cond: str) -> dict:
    """把分支条件翻译为 switch 规则。简单等式 → eq 规则（与 NR 原生一致）；
    否则 → jsonata 规则。返回含 t/v/vt/property 的 dict。

    ★ 修复：旧正则以 (.+?) 贪婪最小化匹配 lhs，遇到 `==` 会把第一个 `=` 吞进 lhs，
    导致 `msg.payload == "有人"` 解析出 property="payload ="（带尾随 `=`）。
    NR switch 的 property 变成非法表达式 `msg.payload =` → 语法错误、分支恒假、
    真实部署的分支彻底失效。现用 [^=!]+ 阻止 lhs 吞运算符，并支持 != / <> 翻译为 ne。"""
    # [^=!]+ 防止 lhs 吞掉等式/不等运算符；运算符组显式匹配 ==|!=|<>|=。
    # 注意分组：g1=lhs, g2=运算符, g3=可选引号, g4=值；\3 回引引号（勿写成 \2）。
    m = re.match(r'^([^=!]+?)\s*(==|!=|<>|=)\s*("?)([^"]*)\3$', cond.strip())
    if m:
        lhs = m.group(1).strip()
        if lhs.startswith("msg."):       # 去掉 msg. 前缀（property 相对 msg）
            lhs = lhs[4:]
        op = m.group(2)
        # ★ 同时剥离单/双引号：DSL 写 state='off'（单引号，中文习惯）时，
        # 旧正则 ("?)([^"]*)\2 只剥双引号，会把 'off' 连同单引号一起 capture →
        # 编译出 v="'off'"，真实 NR switch 与 gate 比较 msg.state('off')≠"'off'"
        # 永不成立，分支恒走 else（既让闸门误拦，也令真实部署的分支失效）。
        rhs = m.group(4).strip().strip('"\'').strip()
        t = "ne" if op in ("!=", "<>") else "eq"
        low = rhs.lower()
        if low in ("true", "false"):
            vt, v = "bool", low
        elif re.fullmatch(r"-?\d+(\.\d+)?", rhs):
            vt, v = "num", rhs
        else:
            vt, v = "str", rhs
        return {"t": t, "v": v, "vt": vt, "property": lhs}
    return {"t": "jsonata_exp", "v": _sanitize_jsonata(cond.strip()), "vt": "jsonata", "property": "payload"}


def _emit_raw(em: _Emitter, st: RawNode) -> str:
    """原生节点逃逸：把白名单内手写 NR 节点嵌主链（单输出连线）。

    引擎托管 id/z/x/y/wires，config 来自 agent（已剥离托管字段）。多 output 节点
    （如 switch）只接 out0，其余输出口不连——复杂多分支请改用 DSL 原生 分支:/否则:。"""
    nid = em.add(st.node_type, **st.config)
    return nid


def _emit_switch(em: _Emitter, sw: Switch) -> str:
    rule_info = [_parse_switch_rule(b.condition) for b in sw.branches]
    if sw.else_body:
        rule_info.append({"t": "else", "v": "true", "vt": "jsonata", "property": "payload"})
    # 节点级 property：取首个非 jsonata 规则的 property（同 switch 内通常一致），否则默认 payload
    node_prop = "payload"
    node_ptype = "msg"
    for ri in rule_info:
        if ri["t"] != "jsonata":
            node_prop = ri["property"]
            # WB25-NEW-2：取值 字段落点在 msg.payload.<field>，节点级 property 同步对齐，
            # 否则 NR 按 msg.<field> 读（undefined）。与 flow 变量作用域互斥判定并存。
            if node_prop in getattr(em, "read_fields", set()):
                node_prop = "payload." + node_prop
            # C2 修复：若分支 LHS 是场景变量（由 `变量:` 写入 flow 上下文），
            # 则 switch 改读 flow 上下文，否则读 msg（默认）。同一 switch 只能用一种作用域，
            # 按首个规则的 LHS 判定——变量与 msg 字段混用的分支极罕见，且变量场景应优先对齐 flow。
            if node_prop in getattr(em, "flow_vars", set()):
                node_ptype = "flow"
            break
    # #507：分支/条件 jsonata 规则里的裸变量名绑定到 flow 上下文（flow.<变量名>），
    # 让变量从死变量变为可被 switch 读到的真值（iss_185a55e085）。else 规则 v="true" 无变量名，
    # _bind_flow_vars 不改写。
    # WB25-NEW-2：取值字段落点 msg.payload.<field>，jsonata 里裸字段名须对齐到 payload.<field>，
    # 否则 $number(温度) 读 msg.温度（undefined）→ 分支恒走 else。
    _bound_rules = []
    for ri in rule_info:
        v = ri["v"]
        if ri["vt"] == "jsonata":
            if em.flow_vars:
                v = _bind_flow_vars(v, em.flow_vars)
            if getattr(em, "read_fields", set()):
                v = _bind_read_fields(v, em.read_fields)
        else:
            # eq/ne 规则的 property 同样须对齐取值落点（msg.payload.<field>）
            prop = ri.get("property", "payload")
            if prop in getattr(em, "read_fields", set()):
                prop = "payload." + prop
                ri = {**ri, "property": prop}
        _bound_rules.append({"t": ri["t"], "v": v, "vt": ri["vt"], **({"property": ri["property"]} if ri["vt"] != "jsonata" else {})})
    rules = _bound_rules
    sid = em.add("switch", name="分支", property=node_prop, propertyType=node_ptype,
                 checkall="true", repair=False, rules=rules, outputs=len(rules))
    for idx, b in enumerate(sw.branches):
        # 关键修复：分支体首节点(head)必须接到 switch 对应输出，否则首节点孤儿(R13)。
        head, _ = _emit_body(em, b.body, [], x=560)
        if head:
            em.connect_out(sid, idx, head)
    if sw.else_body:
        else_head, _ = _emit_body(em, sw.else_body, [], x=560)
        if else_head:
            em.connect_out(sid, len(sw.branches), else_head)
    return sid


# ── 语义缺口预检（B1：高声拒绝，避免静默降级）──────────────────────
# 编译器当前只能读『当前』状态（取值/查询）；需求里出现『昨晚/昨天/历史/首次/去重』
# 等意图却没用对应能力（history_* 子流程 / 触发…首次）时，若照常编译会静默降级成
# 读当前态 → 产出『看似满足、语义全反』的 flow。这里在编译期高声拦下。
# 注：历史查询原『历史:』原语已于 2026-07-20 废弃，统一改为 调用子流程: history_*。
_HIST_PHRASES = ("昨晚", "昨天", "前天", "历史", "上次", "过往", "过去", "前一次")
_FIRST_PHRASES = ("首次", "第一次", "头一次", "去重", "整晚", "当天", "当日")
_HISTORY_SUBS = ("history_state_at", "history_occurred", "history_duration", "history_aggregate")

# 缺口预检用的『合法 DSL 顶层关键字』白名单：命中这些前缀的行是正常 DSL 指令，
# 不参与『自然语言条件/直到…才』的误判（尤其 注释: 行常含自然语言说明）。
_GAP_TOP_KW = ("场景", "触发", "条件", "变量", "分支", "否则", "并行", "动作",
               "调用子流程", "延时", "预期", "提取", "构建", "查询", "时间段",
               "取值", "请求", "观测", "注释", "子流程", "switch", "scene")


def detect_semantic_gaps(text: str) -> list[str]:
    """扫描 DSL 原始文本，识别『编译器会静默降级 / 必然解析失败』的语义缺口意图。

    返回人类可读警告列表（空=无缺口）。调用方（compile_dsl / staging 闸门 /
    E2E 追踪）据以高声拒绝——而非产出语义错乱的 flow。

    覆盖两类缺口：
      (A) 静默降级类：意图存在但当前原语不表达，照编译会变成『看似满足语义全反』
          ——历史/时间窗（未用 history_* 子流程）、首次/去重（未用 触发…首次）。
      (B) 必然失败类：自然语言写法当前 DSL 无法解析（会落进『无法识别的顶层指令』硬错），
          提前给出『正确原语』建议，让 LLM 自我修正而非撞硬墙：
          ——间隔触发（每隔 N 分钟/秒，无对应原语）；
          ——自然语言条件（如果…否则 / 若…则…否则，未用 分支/否则）；
          ——直到…才（等待/持久意图，当前只能映射到 触发+动作）。
    所有 (B) 类检测只对本应不出现的 token（如果/直到/每隔）触发，合法 DSL 绝不命中，零误伤。
    """
    issues: list[str] = []
    has_history_call = any(c in text for c in _HISTORY_SUBS)
    has_first_trig = bool(re.search(r"触发:.*(首次|第一次|头一次)", text))
    # (B1) 间隔触发：每隔 N 分钟/秒/小时 —— 当前无间隔触发原语
    _INTERVAL_RE = re.compile(r"每\s*隔?\s*\d+\s*(分钟|秒|min|s|小时|h|个小时)")
    # (B2) 自然语言条件：如果/假如/要是 … 则/就/否则/那么/不然
    _NATURAL_COND_RE = re.compile(r"(如果|假如|要是).*(则|就|否则|那么|不然)")
    for ln_idx, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        is_kw_line = s.startswith(_GAP_TOP_KW)
        # —— (B1) 间隔触发（每隔 N 分钟）——
        if _INTERVAL_RE.search(s) and s.startswith(("触发", "trigger")):
            issues.append(
                f"[第{ln_idx}行] 『{s}』用了间隔触发（每隔 N 分钟/秒），但当前 DSL 仅支持 "
                f"『触发: 每天 HH:MM』(定点) 或 状态变化触发，无间隔触发原语。"
                f"建议：用 触发: 每天 HH:MM 近似，或申请新增间隔触发原语。")
            continue
        # —— (B2) 自然语言条件（如果…否则），未用 分支/否则 原语 ——
        if _NATURAL_COND_RE.search(s) and not is_kw_line:
            issues.append(
                f"[第{ln_idx}行] 『{s}』用了自然语言『如果…否则』，但 DSL 用 分支/否则 原语表达条件"
                f"（见 autoflow_dsl_help 的 分支 示例）。"
                f"建议：改成『分支: <条件>』…『否则:』结构，条件用 当前状态查询/数值条件。")
            continue
        # —— (B3) 直到…才：等待/持久意图 ——
        if "直到" in s and "才" in s and not is_kw_line:
            issues.append(
                f"[第{ln_idx}行] 『{s}』含『直到…才』等待/持久意图。当前 DSL 可用"
                f"『触发: <X> <state> … 动作: <Y>』表达『当 X 变到 state 时做 Y』；"
                f"『X 持续 N 分钟才 Y』的持久等待已支持：写成『触发: <X> <state> 持续N分钟』"
                f"（编译为 server-state-changed 的 for 等待，N 分钟/小时/秒均可）。")
            continue
        # —— (A) 既有：取值/查询 含历史/首次意图却未用对应原语 ——
        is_read = s.startswith(("取值", "读取", "read:"))
        is_query = s.startswith(("查询", "check:"))
        if not (is_read or is_query):
            continue
        body = s.split(":", 1)[1].strip() if ":" in s else s
        if not has_history_call and any(p in body for p in _HIST_PHRASES):
            issues.append(
                f"[第{ln_idx}行] 『{body}』含历史/时间窗意图（昨晚/昨天/历史…），但『取值/查询』只读当前状态"
                f"——会静默降级为读当前值，丢失时间语义。"
                f"请改用请求/响应子流程查询历史：调用子流程: history_state_at(entity=<实体>, at=<时刻>) / "
                f"history_occurred(entity=<实体>, start=<起>, end=<止>) / "
                f"history_duration(...) / history_aggregate(...)；返回值在 msg.payload，下游用『提取:』/『分支:』读取。")
        if not has_first_trig and any(p in body for p in _FIRST_PHRASES):
            issues.append(
                f"[第{ln_idx}行] 『{body}』含首次/去重意图，但当前原语无法表达『第一次』。"
                f"请在『触发:』后加『首次』修饰（上升沿触发）。")
    return issues


def compile_dsl(text: str, target: str = "staging") -> dict:
    """便捷入口：DSL 文本 → NR flow 导出。失败时抛 DSLError（含行号）。
    编译前先做语义缺口预检（B1）：若 DSL 含历史/首次意图却未用对应原语，
    高声拒绝而非静默降级。"""
    gaps = detect_semantic_gaps(text)
    if gaps:
        raise DSLError("语义缺口（高声拒绝，避免静默降级）：" + "；".join(gaps), code=C_SEMANTIC_GAP)
    return compile(parse(text), target=target)


def compile_dsl_strict(text: str, target: str = "staging") -> dict:
    """严格模式：编译后若 flow["lint"] 含 error 级问题（R5/R7/R8 等），
    直接抛 DSLError 拦截，避免黑箱产出带已知反模式的 flow。"""
    flow = compile_dsl(text, target=target)
    errors = [i for i in flow.get("lint", []) if i.get("level") == "error"]
    if errors:
        summary = "；".join(f"[{i['rule']}] {i['message']}" for i in errors)
        raise DSLError(f"编译自检发现 {len(errors)} 个错误级问题：{summary}", code=C_SELFCHECK)
    return flow
