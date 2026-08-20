"""AutoFlow 预建子流程注册表（P3 子流程库 #1/#2）。

来源：直接取自 NR 1990 真实 flow（见 docs/dsl_design.md §9）。
- demo_notify   = 示例通知（link_out 教学示例，参数形状继承历史 TTS 播报规格）。

设计铁律（§18.2/§9.3）：状态ful 基础设施（队列/互斥/计时循环/全局状态）**不属于 DSL 表达对象**，
由这里 hand-build 为预建子流程，agent 只"按名调用"不"重建"。

引擎编译子流程调用时，按 `call` 字段生成：
- type="link_out"  → 生成一个 `change`(设 msg.payload) + `link out`(指向 entry_link_id) 节点对。
  这是与真实 weigh flow 完全一致的方式（它也是 link out 到 TTS 入口 b595563939283231）。
- type="subflow"   → 生成一个 `subflow` 实例节点，引用已部署的 subflow type。
  当前范例是 bark_push；历史查询 history_* 4 个能力同样走此模式（请求/响应，
  子流程经输出口把答案透传回 msg.payload 供下游分支）。与 link_out 的
  fire-and-forget 单向模型本质不同（天气/AnySearch/demo_notify 不返回值）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Param:
    name: str
    required: bool = False
    default: Any = None
    type: str = "str"  # str | int | float | bool
    enum: Optional[list[str]] = None
    desc: str = ""


@dataclass
class SubflowSpec:
    name: str
    title: str
    call: dict  # {"type":"link_out","entry_link_id": "..."} | {"type":"subflow","subflow_id":"..."}
    params: dict[str, Param] = field(default_factory=dict)
    description: str = ""
    notes: str = ""
    # "payload" = 入参整体塞进 msg.payload(dict) 或 payload.<k>（默认，适配 link_out/weigh 入口）
    # "flat"    = 入参平铺到 msg.<k>（适配直接读 msg.title/msg.body 的子流程，如 Bark）
    param_style: str = "payload"
    # 位置参数顺序：声明后，调用方可用 无 key 的位置参数（如 bark_push(标题, 正文)），
    # 引擎按此顺序填入对应命名参数，容错 agent 漏写 key= 的常见写法。None 表示不支持位置参数。
    positional: Optional[list[str]] = None
    # 来源：managed（网关预置，参数契约权威，调用方传参严格校验）
    #       imported（用户从 NR 自省导入，input_schema 为 best-effort 推断，传参宽松）
    source: str = "managed"
    # 是否随网关启动 seed 进用户注册表（WebUI 默认面板）。
    # preload=False 的子流程仍注册在 SUBFLOWS（编译器/dsl_help 可见、可作测试基线、
    # 用户可从示例主动导入），但不在全新/重置用户的默认预置面板出现。
    # 例：demo_notify 是 link_out 编译路径的教学示例，注册但不预载。
    preload: bool = True
    # V-NEW-3：子流程的【已声明输出字段】名清单（msg.payload 返回结构的顶层键）。
    # 供 flow_linter._compute_reliable_fields 建模——否则下游 switch 读子流程输出字段
    # 会被误判「未定义」→ 恒假分支 → 子流程流被过度拦截。空 = 不建模（unknown 子流程
    # 默认保持过度拦截，安全侧）。link_out 型（fire-and-forget 无返回值）留空。
    outputs: list = field(default_factory=list)

    def resolve_args(self, raw: dict[str, str],
                     dynamic: Optional[set] = None) -> dict[str, Any]:
        """合并默认值、类型转换、枚举校验，返回规范化入参。

        缺必填或枚举非法抛 ValueError（供引擎静态校验捕获）。

        `dynamic`：值为【运行期表达式】的参数名集合（DSL 里用反引号包裹的
        JSONata 入参，或 `${}` 插值出的运行期引用）。这类值编译期根本不知道，
        对其做 int()/枚举校验只会产生假错误（`bark_badge=\\`payload.n\\`` 被判
        「不是数字」），故整体跳过类型/枚举校验、原样透传给发射器。
        """
        dyn = set(dynamic or ())
        out: dict[str, Any] = {}
        for pname, p in self.params.items():
            # WB24 NEW-F3：必填参数给了「空串」视为缺失（与 validate_args 一致），
            # 避免 anysearch_batch(keywords=) 这类「必填但空值」被当成已填而静默逃逸。
            if pname in raw and str(raw[pname]).strip() != "":
                val = raw[pname]
                if pname in dyn:
                    out[pname] = val
                    continue
                if p.enum and val not in p.enum:
                    raise ValueError(
                        f"子流程 {self.name} 参数 {pname}='{val}' 非法，应为 {p.enum}"
                    )
                try:
                    out[pname] = _coerce(val, p.type)
                except (ValueError, TypeError):
                    # R1(#round4)：int/float 解析失败（如 `bark_badge=abc` / `=7.5`）
                    # 必须给出带【参数名+合法示例+类型】的可读错误，而非裸
                    # `int('abc')` 的 ValueError 栈（无 DSL 行号、无上下文）。
                    # 该 ValueError 会被 _emit_subflow 捕获并转译为 C_SUBFLOW_ARG。
                    raise ValueError(
                        f"子流程 {self.name} 参数 {pname} 期望类型 {p.type}，"
                        f"但收到 '{val}'（合法示例：{_coerce_example(p)}）。"
                        f"请检查 {pname} 的值。"
                    )
            elif p.required:
                raise ValueError(f"子流程 {self.name} 缺少必填参数：{pname}")
            elif p.default is not None:
                out[pname] = p.default
        return out

    def validate_args(self, raw: dict[str, str], strict: bool = False,
                      dynamic: Optional[set] = None) -> None:
        """编译期校验调用方入参。strict=True 时未知参数也报错（managed 子流程用，
        用于捕获拼写错误）；imported 子流程 schema 为 best-effort 推断，宽松不报未知参数。
        缺必填 / 枚举非法 /（strict 时）未知参数 → 抛 ValueError（msg 含可读原因）。

        WB24 NEW-F3：必填参数若给了空串（如 `anysearch_batch(keywords=)`），与缺失等价 ——
        此前「值存在即算填了」导致空必填静默放行，与 history 系列（缺参被拦）校验覆盖不一致。
        现统一：必填 + 空串 → 报「缺少必填参数」。
        """
        dyn = set(dynamic or ())
        for pname, p in self.params.items():
            if pname in raw and str(raw[pname]).strip() != "":
                # 运行期表达式（反引号 JSONata）/ 尚未展开的 `${}` 模板：
                # 编译期无从判定其最终值，跳过枚举校验（否则必然假报错）。
                if pname in dyn or "${" in str(raw[pname]):
                    continue
                if p.enum and raw[pname] not in p.enum:
                    raise ValueError(
                        f"子流程 {self.name} 参数 {pname}='{raw[pname]}' 非法，应为 {p.enum}")
            elif p.required:
                raise ValueError(f"子流程 {self.name} 缺少必填参数：{pname}")
        if strict:
            # 空串未知参数视为未提供，不计入未知（容忍 agent 多写空位）
            unknown = [k for k in raw if k not in self.params and str(raw[k]).strip() != ""]
            if unknown:
                raise ValueError(
                    f"子流程 {self.name} 收到未知参数：{unknown}（已声明：{list(self.params)})")


def _coerce(val: str, t: str) -> Any:
    if t == "int":
        return int(val)
    if t == "float":
        return float(val)
    if t == "bool":
        return val.strip().lower() in ("1", "true", "yes", "是")
    return val


def _coerce_example(p: "Param") -> str:
    """R1(#round4)：给类型解析失败一个可读的「合法示例」提示。"""
    if p.enum:
        return " / ".join(p.enum)
    if p.type == "int":
        return "42"
    if p.type == "float":
        return "3.14"
    if p.type == "bool":
        return "true / false"
    return "文本"


# ── 子流程库 ──────────────────────────────────────────────────────────────
# 真实 TTS 队列入口 link-in 节点 id（来自 tts_queue_flow.json）
TTS_ENTRY_LINK_ID = "b595563939283231"
# 兼容 #107 已提交 test_dsl_engine.py 的 demo_notify 链接断言（ID 与 TTS 入口相同）
DEMO_NOTIFY_ENTRY_LINK_ID = "b595563939283231"

# Bark 推送子流程：NR1990 上手动创建（API 推定义不显示在侧栏，故只引用已建 id）。
# 其 env 内置 BARK_SERVER / BARK_KEY / BARK_CIPHER_KEY / BARK_CIPHER_IV。
# 调用方传 msg.title + msg.body，可选 level/sound/url 等。
BARK_SUBFLOW_ID = "b0bbc86abb2172a5"

# 声明式 Bark 子流程定义（单一真相源）。
# ⚠️ 活体是【明文 JSON POST】，非早期记忆所述的 AES 加密；BARK_CIPHER_KEY/IV 为死变量（未使用）。
# 结构对齐 NR1990 活体 b0bbc86（经只读探活提取，2026-07-25）：
#   in → b0bb_enc01(function 构造 {title,body,device_key:env.BARK_KEY,+bark_*扩展})
#      → 49ed308a4efdbade(http request POST env.BARK_SERVER+/push)
#      → 86d4245ed4ade0a2(change 结果透传) → out。
# env 仅声明 name/type，value 不写死（密钥不进 git）；生成时由 ensure_bark_subflow 从 os.environ 注入。
# #683：build 期预置 3 个 debug 探针（n_dbg_enc/n_dbg_http/n_dbg_result），供 #644 debug 回读桥
#       抓 bark 内部真实帧；按 G1 约定「debug 节点须在 build 期预置，绝不插热路径」。子流程输出路径
#       （out_ports→86d4245ed4ade0a2）未动，探针仅 fan-out 旁路。
BARK_SUBFLOW_SPEC: Dict[str, Any] = {
    "id": BARK_SUBFLOW_ID,
    "name": "Bark",
    "info": "",
    "category": "subflows",
    "in_ports": [{"x": 380, "y": 220, "wires": [{"id": "b0bb_enc01"}]}],
    "out_ports": [{"x": 1660, "y": 220, "wires": [{"id": "86d4245ed4ade0a2", "port": 0}]}],
    "nodes": [
        {
            "id": "b0bb_enc01", "type": "function", "z": BARK_SUBFLOW_ID,
            "name": "构造 Bark 明文 JSON",
            "func": (
                "const title = (typeof msg.title === 'string' && msg.title.length) ? msg.title : 'AutoFlow';\n"
                "const body  = (typeof msg.body  === 'string') ? msg.body : '';\n"
                "const p = { title: title, body: body, device_key: env.get('BARK_KEY') };\n"
                "// 透传 bark_* 扩展参数(原 AES 版支持的全部字段)\n"
                "const map = {bark_level:'level',bark_sound:'sound',bark_url:'url',bark_group:'group',\n"
                "             bark_copy:'copy',bark_isArchive:'isArchive',bark_icon:'icon',bark_badge:'badge',\n"
                "             bark_volume:'volume',bark_autoCopy:'autoCopy'};\n"
                "for (const k in map) { if (msg[k] != null && msg[k] !== false) p[map[k]] = msg[k]; }\n"
                "msg.payload = p;\n"
                "msg.headers = { 'Content-Type': 'application/json' };\n"
                "msg.url = env.get('BARK_SERVER') + '/push';\n"
                "return msg;"
            ),
            "outputs": 1, "noerr": 0, "initialize": "", "finalize": "", "libs": [],
            "x": 620, "y": 220, "wires": [["49ed308a4efdbade", "n_dbg_enc"]],
        },
        {
            "id": "49ed308a4efdbade", "type": "http request", "z": BARK_SUBFLOW_ID,
            "name": "POST→本地 Bark", "method": "POST", "ret": "txt", "paytoqs": "ignore",
            "url": "", "tls": "", "persist": False, "proxy": "", "insecureHTTPParser": False,
            "authType": "", "senderr": False, "headers": [],
            "x": 1290, "y": 220, "wires": [["86d4245ed4ade0a2", "n_dbg_http"]],
        },
        {
            "id": "86d4245ed4ade0a2", "type": "change", "z": BARK_SUBFLOW_ID,
            "name": "结果透传",
            "rules": [{"t": "set", "p": "payload", "pt": "msg",
                       "to": "{\"ok\":statusCode=200,\"status\":statusCode,\"sent\":{\"title\":title,\"body\":body},\"raw\":payload}",
                       "tot": "jsonata"}],
            "x": 1520, "y": 220, "wires": [["n_dbg_result"]],
        },
        # ── #683：build 期预置 debug 探针，供 debug_bridge 回读桥抓 bark 内部真实帧 ──
        {
            "id": "n_dbg_enc", "type": "debug", "z": BARK_SUBFLOW_ID,
            "name": "DBG 构造请求", "active": True,
            "tosidebar": True, "console": False,
            "complete": "true", "targetType": "full",
            "statusType": "auto", "x": 620, "y": 380, "wires": [[]],
        },
        {
            "id": "n_dbg_http", "type": "debug", "z": BARK_SUBFLOW_ID,
            "name": "DBG HTTP响应", "active": True,
            "tosidebar": True, "console": False,
            "complete": "true", "targetType": "full",
            "statusType": "auto", "x": 1290, "y": 380, "wires": [[]],
        },
        {
            "id": "n_dbg_result", "type": "debug", "z": BARK_SUBFLOW_ID,
            "name": "DBG 最终结果", "active": True,
            "tosidebar": True, "console": False,
            "complete": "true", "targetType": "full",
            "statusType": "auto", "x": 1520, "y": 380, "wires": [[]],
        },
    ],
    # env 仅声明 name/type；value 不写死（避免密钥进 git）。生成时由 ensure 注入 os.environ。
    "env": [
        {"name": "BARK_SERVER", "type": "str"},
        {"name": "BARK_KEY", "type": "str"},
        {"name": "BARK_CIPHER_KEY", "type": "str"},
        {"name": "BARK_CIPHER_IV", "type": "str"},
    ],
}


def ensure_bark_subflow(nr, allow_prod: bool = False) -> Dict[str, Any]:
    """幂等确保 bark_push 子流程(b0bbc86)存在于目标 NR 实例。

    - 已存在（list_flows 查到 type=subflow 且 id==BARK_SUBFLOW_ID）→ 直接返回，零改动（no-op）。
    - 缺失 → 从 BARK_SUBFLOW_SPEC 生成（env 值从 os.environ 的 BARK_* 注入，缺失则空串）。
    仅 1990 调用（allow_prod=False），绝不碰 1880。
    """
    try:
        flows = nr.list_flows()
    except Exception:
        flows = []
    for f in flows:
        if f.get("type") == "subflow" and f.get("id") == BARK_SUBFLOW_ID:
            return {"id": BARK_SUBFLOW_ID, "created": False, "exists": True}
    # 缺失 → 从声明式规格生成（env 值从运行环境注入，不硬编码密钥）
    spec = dict(BARK_SUBFLOW_SPEC)
    spec["env"] = [
        {"name": e["name"], "type": e.get("type", "str"),
         "value": os.environ.get(e["name"], "")}
        for e in BARK_SUBFLOW_SPEC.get("env", [])
    ]
    return nr.generate_subflow_from_spec(spec, allow_prod=allow_prod)


def flow_uses_bark_subflow(nodes) -> bool:
    """判断原始 flow 节点里是否引用了 bark_push 子流程实例。

    兼容两种写法：
      - NR5 前缀型 type="subflow:<id>"（dsl_engine 产出，当前标准写法）
      - 裸型 type="subflow" + c:"<id>"（个别手搓/旧导出）
    """
    for n in nodes or []:
        nt = n.get("type", "")
        if nt == f"subflow:{BARK_SUBFLOW_ID}":
            return True
        if nt == "subflow" and n.get("c") == BARK_SUBFLOW_ID:
            return True
    return False

# 历史查询 4 个子流程：请求/响应语义（agent 需拿返回值做分支），仿 bark_push 注册为
# type="subflow"，由 NR 子流程实例真正干活（link in → 时间解析 → api-get-history / statistics
# → 计算 → link out），网关只引用 subflow_id。
# 以下 4 个 id 已于 Task #272（2026-07-21）部署到 NR 1990，回填真实子流程 id
# （与 nr_subflows/history/build_subflows.py 的 HIST_IDS 对齐）。
HISTORY_STATE_AT_SUBFLOW_ID = "af_hist_state_at"
HISTORY_OCCURRED_SUBFLOW_ID = "af_hist_occurred"
HISTORY_DURATION_SUBFLOW_ID = "af_hist_duration"
HISTORY_AGGREGATE_SUBFLOW_ID = "af_hist_aggregate"


SUBFLOWS: dict[str, SubflowSpec] = {
    # ── demo_notify：link_out 编译路径的教学示例（#181 注册进 SUBFLOWS，#183 标记 preload=False）──
    # 参数形状与历史 TTS 播报规格完全一致（既有测试/golden fixture 经机械重命名即可继续作回归基线）。
    # 注册但不预载：编译器 / dsl_help 可见、可作测试基线、用户可从示例主动导入；
    # 但全新/重置用户的默认预置面板不出现（个人 TTS 队列 link 不应作为默认功能）。
    # entry_link_id 指向 TTS 队列入口（与历史 TTS 播报同一真实下游 b595563939283231）。
    "demo_notify": SubflowSpec(
        name="demo_notify",
        title="示例通知（link_out 演示）",
        call={"type": "link_out", "entry_link_id": DEMO_NOTIFY_ENTRY_LINK_ID},
        description="【示例子流程，非产品功能】仅用于演示『调用子流程 → 生成 change(设参) + link out』"
                    "的 link_out 编译路径。entry_link_id 指向 TTS 队列入口 b595563939283231。"
                    "生产请改用 imported 子流程或教学导入（bark_push / anysearch 等）。",
        params={
            "text": Param("text", required=True, desc="通知文本（必填）"),
            "room": Param(
                "room", required=False, default="default",
                desc="通知房间；default=书房+客厅。取值见 notes。",
            ),
            "level": Param(
                "level", required=False, default="一般", type="str",
                enum=["一般", "重要", "警告"],
                desc="音量/重要档位：一般30%/重要50%/警告80%（与 volume 二选一）",
            ),
            "volume": Param("volume", required=False, default=None, type="int",
                            desc="手动音量 0-100，优先级高于 level"),
            "priority": Param("priority", required=False, default=3, type="int",
                             desc="优先级 1 最高，强制全屋并跳过夜间/过期"),
            "mode": Param("mode", required=False, default="announce", type="str",
                          enum=["announce", "command"],
                          desc="announce=播报文本；command=执行文本指令"),
            "message": Param("message", required=False, default=None,
                             desc="command 模式的指令文本"),
        },
        notes=(
            "【示例】参数形状与历史 TTS 播报规格完全一致，以便既有测试/golden fixture 经机械重命名即可继续作为"
            "link_out 编译路径的回归基线。生产请改用 imported 子流程或教学导入。"
            "room 取值：default / 客厅 / 房间 / 卧室 / 书房 / 卫生间 / 主卧室 / 主卧室浴室 / 全屋。"
        ),
        param_style="payload",
        positional=None,
        source="managed",
        preload=False,
    ),

    "bark_push": SubflowSpec(
        name="bark_push",
        title="Bark 推送(iPhone 通知)",
        call={"type": "subflow", "subflow_id": BARK_SUBFLOW_ID},
        param_style="flat",
        positional=["title", "body"],
        description="经 NAS 自建 bark-server 推送 iPhone 通知（破微信会话窗口死局，不需科学上网、不受 Tailscale 本地推送判定影响）。"
                    "调用方传 title+body，可选 level/sound/url/group 等。",
        params={
            "title": Param("title", required=True, desc="通知标题"),
            "body": Param("body", required=True, desc="通知正文"),
            "bark_level": Param("bark_level", required=False, default=None,
                                enum=["active", "timeSensitive", "critical"],
                                desc="重要性：critical 可绕过 iOS 专注/摘要"),
            "bark_sound": Param("bark_sound", required=False, default=None, desc="提示音名，如 minuet"),
            "bark_url": Param("bark_url", required=False, default=None, desc="点击通知打开的 URL"),
            "bark_group": Param("bark_group", required=False, default=None, desc="分组（同组通知互相覆盖）"),
            "bark_copy": Param("bark_copy", required=False, default=None, desc="复制按钮文本"),
            "bark_isArchive": Param("bark_isArchive", required=False, default=None, type="bool",
                                    desc="是否存档（不自动清除）"),
            "bark_icon": Param("bark_icon", required=False, default=None, desc="图标 URL"),
            "bark_badge": Param("bark_badge", required=False, default=None, type="int", desc="角标数字"),
            "bark_volume": Param("bark_volume", required=False, default=None, type="int", desc="音量 0-100"),
            "bark_autoCopy": Param("bark_autoCopy", required=False, default=None, type="bool", desc="自动复制"),
        },
        notes=(
            f"subflow_id={BARK_SUBFLOW_ID} 指向 NR1990 上手动创建的 Bark 子流程，其 env 内置 BARK_SERVER/BARK_KEY/BARK_CIPHER_KEY/BARK_CIPHER_IV。"
            "⚠️ 该子流程需在【目标实例】手动创建后回填 id；黑箱不靠 API 推子流程定义（侧栏不显示）。"
            "param_style=flat → 入参平铺到 msg.title / msg.body / msg.bark_* 供子流程读取。"
        ),
    ),

    # ── 历史查询 4 能力（请求/响应，仿 bark_push 的 type=subflow 模式）──────────
    # 与天气/AnySearch 的 link_out 单向 fire-and-forget 不同：历史查询 agent 要拿返回值
    # 做分支，故注册为 subflow，由 NR 子流程实例真正干活。参数走 param_style=flat，
    # 入参平铺到 msg.<k> 供子流程读取；答案对象统一写回 msg.payload 供下游『提取/分支』。
    # subflow_id 已部署于 NR 1990（Task #272，2026-07-21）。
    "history_state_at": SubflowSpec(
        name="history_state_at",
        title="历史状态查询（某时刻的值）",
        call={"type": "subflow", "subflow_id": HISTORY_STATE_AT_SUBFLOW_ID},
        param_style="flat",
        positional=["entity"],
        description="查询实体在『某个过去时刻』的状态/属性值。例：空调昨晚23:12设定几度？"
                    "门昨天11:30是什么状态？返回归一化答案对象到 msg.payload，"
                    "下游用『提取: <字段> = payload.x』或『分支:』读取。",
        params={
            "entity": Param("entity", required=True,
                            desc="实体id（建议先用 resolve_entity 把友好名解析成 entity_id）"),
            "at": Param("at", required=True,
                        desc="时刻，自然语言：昨晚23:12 / 昨天11:30 / 今天08:00 / 8h前 / 2026-07-19T23:12 / ISO"),
            "attribute": Param("attribute", required=False, default=None,
                               desc="取哪个属性（如 temperature/设定温度/亮度）；缺省取 state"),
        },
        notes=(
            "返回 msg.payload = {found:bool, entity, at_iso, value, attribute, unit?, nearest_ts?}。"
            "value 为最近邻采样值；found=false 表示窗口内无任何记录。"
            "param_style=flat → 入参平铺到 msg.entity / msg.at / msg.attribute。"
            f"subflow_id={HISTORY_STATE_AT_SUBFLOW_ID}（已部署于 NR 1990，Task #272）。"
        ),
    ),

    "history_occurred": SubflowSpec(
        name="history_occurred",
        title="历史是否发生（区间内状态变化/达到某态）",
        call={"type": "subflow", "subflow_id": HISTORY_OCCURRED_SUBFLOW_ID},
        param_style="flat",
        positional=["entity"],
        description="查询实体在『时间区间』内是否发生过某状态变化或达到某状态。例：门昨天11-12点开过没？"
                    "昨晚空调有没有被调到26度以上？返回归一化答案到 msg.payload。",
        params={
            "entity": Param("entity", required=True, desc="实体id"),
            "start": Param("start", required=True, desc="区间起点，自然语言：昨天11:00 / 2026-07-19T11:00 / 8h前"),
            "end": Param("end", required=True, desc="区间终点，自然语言：昨天12:00 / 今天08:00 / 现在"),
            "state": Param("state", required=False, default=None,
                           desc="目标状态（如 on/off/开/26）；缺省=发生过任意变化即算"),
            "attribute": Param("attribute", required=False, default=None,
                               desc="针对某属性（如 temperature）的变化；缺省看 state"),
        },
        notes=(
            "返回 msg.payload = {occurred:bool, entity, start_iso, end_iso, count, state?, "
            "events:[{ts,from,to}], first_ts?, last_ts?}。"
            "state 缺省：区间内有任意状态变化即 occurred=true；给定 state：出现过该状态才 true。"
            f"subflow_id={HISTORY_OCCURRED_SUBFLOW_ID}（已部署于 NR 1990，Task #272）。"
        ),
    ),

    "history_duration": SubflowSpec(
        name="history_duration",
        title="历史处于某状态的总时长",
        call={"type": "subflow", "subflow_id": HISTORY_DURATION_SUBFLOW_ID},
        param_style="flat",
        positional=["entity"],
        description="统计实体在『时间区间』内处于某状态的累计时长。例：昨天电视机开启时长？"
                    "昨晚空调运行了几小时？返回归一化答案到 msg.payload。",
        params={
            "entity": Param("entity", required=True, desc="实体id"),
            "start": Param("start", required=True, desc="区间起点，自然语言"),
            "end": Param("end", required=True, desc="区间终点，自然语言"),
            "state": Param("state", required=True,
                           desc="目标状态（如 on/播放中/开）；统计处在该状态的累计时长"),
        },
        notes=(
            "返回 msg.payload = {total_seconds, total_human(如 '2小时13分'), entity, "
            "start_iso, end_iso, state, ratio(占比0-1)}。"
            "按状态段首尾时间差累计；状态在窗口边界处用边界裁剪。"
            f"subflow_id={HISTORY_DURATION_SUBFLOW_ID}（已部署于 NR 1990，Task #272）。"
        ),
    ),

    "history_aggregate": SubflowSpec(
        name="history_aggregate",
        title="历史聚合统计（能量/计数/均值/最值）",
        call={"type": "subflow", "subflow_id": HISTORY_AGGREGATE_SUBFLOW_ID},
        param_style="flat",
        positional=["entity"],
        description="对实体在『时间区间』做聚合统计。例：昨晚23:00-今早7:00空调耗几度电？"
                    "本周睡眠时长？过去24h平均温度？返回归一化答案到 msg.payload。",
        params={
            "entity": Param("entity", required=True, desc="实体id"),
            "start": Param("start", required=True, desc="区间起点，自然语言"),
            "end": Param("end", required=True, desc="区间终点，自然语言"),
            "metric": Param("metric", required=True, type="str",
                            enum=["energy", "count", "mean", "min", "max", "sum"],
                            desc="energy=耗电(kWh，走 statistics)；count=状态变化次数；"
                                 "mean/min/max/sum=数值属性统计"),
            "attribute": Param("attribute", required=False, default=None,
                               desc="数值属性名（energy 默认 kWh；mean/min/max/sum 必填数值属性）"),
        },
        notes=(
            "返回 msg.payload = {value, unit, entity, start_iso, end_iso, metric, attribute}。"
            "⚠️ energy 必须走 statistics_during_period（history 取不到 kWh 累计）；其余走 history period + 计算。"
            "count 不要求 attribute（统计 state 变化次数）。"
            f"subflow_id={HISTORY_AGGREGATE_SUBFLOW_ID}（已部署于 NR 1990，Task #272）。"
        ),
    ),

}

# ── 历史查询子流程：幂等 ensure（仿 bark_push 的 A3 模式）──────────────
# 4 个历史子流程（af_hist_*）的【原生节点图】存于 nr_subflows/history/subflows_built.json
# （每个子流程 = [def 节点 + n_parse + n_hist + n_catch + n_err + n_calc] 扁平条目数组）。
# Task #272（2026-07-21）已手动部署进 NR1990 并回填 id；但此前无等价 ensure 函数——
# 一旦 NR1990 被清空/重置即永久丢失（这是 agent「历史查询子流程无法使用」的三重根因之一）。
# ensure_history_subflow 幂等重建：list_flows 命中即 no-op；缺失则从 built.json 加载、
# 把硬编码 server 替换为 nr.get_default_server_id()（保证可移植），经安全 append 路径部署。
# 仅 1990 调用（allow_prod=False），绝不碰 1880。
HISTORY_SUBFLOW_IDS = {
    HISTORY_STATE_AT_SUBFLOW_ID, HISTORY_OCCURRED_SUBFLOW_ID,
    HISTORY_DURATION_SUBFLOW_ID, HISTORY_AGGREGATE_SUBFLOW_ID,
}

# ⚠️ 注册表 key（history_*）≠ NR 子流程 id（af_hist_*），两者绝不可互换：
# WebUI 的「安装到 NR」/「注销」端点拿到的是 path_params 里的**注册表 key**，
# 故判定必须用本集合；误用 HISTORY_SUBFLOW_IDS 会让 `key not in` 恒真 → 稳定 400。
# #711 只在 webui.py 引用了本名字却漏了定义 → ImportError 在函数体首行抛出、
# 位于所有 try 之外 → Starlette 裸 500（「安装历史子流程」按钮全线不可用）。
# 从 SUBFLOWS 单一真源派生，避免再出现两处硬编码漂移。
HISTORY_REGISTRY_KEYS = frozenset(
    key for key, spec in SUBFLOWS.items()
    if isinstance(getattr(spec, "call", None), dict)
    and spec.call.get("subflow_id") in HISTORY_SUBFLOW_IDS
)

_HISTORY_BUILT_PATH = os.path.join(
    os.path.dirname(__file__), "nr_subflows", "history", "subflows_built.json")


def _load_history_subflows_built() -> list:
    """读取 subflows_built.json → 4 个子流程的原生扁平条目数组列表。"""
    with open(_HISTORY_BUILT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _flatten_nodes(flows) -> List[Dict]:
    """把 list_flows 的返回摊平成节点数组。双兼容扁平（list）/嵌套（dict）两种形态。"""
    nodes: List[Dict] = []
    if isinstance(flows, list):
        nodes = [n for n in flows if isinstance(n, dict)]
    elif isinstance(flows, dict):
        for key in ("flows", "nodes", "subflows"):
            v = flows.get(key)
            if isinstance(v, list):
                nodes.extend(n for n in v if isinstance(n, dict))
        if not nodes and isinstance(flows.get("flow"), dict):
            nodes = [flows["flow"]]
    return nodes


def _count_internal_nodes(flows, subflow_ids) -> Dict[str, int]:
    """统计每个历史子流程的内部节点数（z==sid 且 type!=subflow）。双兼容扁平/嵌套。"""
    counts = {sid: 0 for sid in subflow_ids}
    for n in _flatten_nodes(flows):
        z = n.get("z")
        if z in subflow_ids and n.get("type") != "subflow":
            counts[z] += 1
    return counts


# ── 内容指纹：判「线上是不是当前代码版本」──────────────────────────────
# #107 踩坑：旧 ensure 只看「内部节点数 > 0」，线上残留的旧版子流程永远判为「已存在」→
# no-op → 修好的新版永远装不上去（升级网关代码毫无效果，极难察觉）。
# 故加内容指纹：只对决定【运行时行为】的字段取 hash，忽略坐标/线序/server 等环境相关值。
_FP_HIST_FIELDS = ("entityId", "entityIdType", "startDateType", "endDateType",
                   "useRelativeTime", "relativeTime", "flatten", "outputType",
                   "outputLocationType", "outputLocation")


def _behavior_fingerprint(nodes: List[Dict]) -> str:
    """对一组子流程内部节点算行为指纹。

    纳入：function 节点的 func 源码、api-get-history 的关键配置字段。
    排除：x/y 坐标、wires 线序、server（部署时按目标实例替换，非行为差异）、
          NR 回写的 _users 等运行时字段。
    """
    parts: List[str] = []
    for n in sorted(nodes, key=lambda x: str(x.get("id", ""))):
        t = n.get("type")
        nid = str(n.get("id", ""))
        if t == "function":
            parts.append("fn\x00%s\x00%s" % (nid, n.get("func") or ""))
        elif t == "api-get-history":
            vals = "|".join(str(n.get(f, "")) for f in _FP_HIST_FIELDS)
            parts.append("hist\x00%s\x00%s" % (nid, vals))
    return hashlib.md5("\x01".join(parts).encode("utf-8")).hexdigest()


def _local_history_fingerprints(built: Optional[list] = None) -> Dict[str, str]:
    """本地 built.json 中各历史子流程的行为指纹（sid → md5）。"""
    entries = built if built is not None else _load_history_subflows_built()
    out: Dict[str, str] = {}
    for arr in entries:
        if not arr or arr[0].get("type") != "subflow":
            continue
        out[arr[0]["id"]] = _behavior_fingerprint(list(arr[1:]))
    return out


def _live_history_fingerprints(flows, subflow_ids) -> Dict[str, str]:
    """线上 NR 各历史子流程的行为指纹（sid → md5）。无内部节点者不产出条目。"""
    grouped: Dict[str, List[Dict]] = {sid: [] for sid in subflow_ids}
    for n in _flatten_nodes(flows):
        z = n.get("z")
        if z in subflow_ids and n.get("type") != "subflow":
            grouped[z].append(n)
    return {sid: _behavior_fingerprint(ns) for sid, ns in grouped.items() if ns}


def ensure_history_subflow(nr, allow_prod: bool = False) -> Dict[str, Any]:
    """幂等确保 4 个历史查询子流程存在于目标 NR 实例。

    - 已存在、内部节点>0、且行为指纹与本地 built.json 一致 → no-op。
    - 缺失 / 退化成空壳（内部节点=0，#607 复发态）/ 内容陈旧（指纹不符，#107）
      → 从 subflows_built.json 重建。
      重建前先从线上剔除该 sid 的全部条目（def + 内部节点），避免 deploy_all 复用旧空壳
      def 不补内部节点（#607 空壳复用陷阱）；用 force+allow_partial 仅替换命中子流程。
    仅 1990 调用（allow_prod=False），绝不碰 1880。
    """
    try:
        flows = nr.list_flows()
    except Exception:
        flows = []
    internal = _count_internal_nodes(flows, HISTORY_SUBFLOW_IDS)
    # 存在且内部节点>0 才算 OK；空壳(=0)视为需重建（#607 复发根因：
    # 原 ensure 只看 id 是否存在，空壳 id 在就 no-op，导致 recurring 退化）
    present_ok = {sid for sid in HISTORY_SUBFLOW_IDS if internal.get(sid, 0) > 0}
    missing = [sid for sid in HISTORY_SUBFLOW_IDS if sid not in present_ok]

    # 加载 built.json：每个数组首元素即 subflow def（含 id/name/in/out/info/env），
    # 其余为内部节点（z 已指向 subflow_id）。
    built = _load_history_subflows_built()

    # #107：内容漂移检测——线上虽在场，但代码不是当前版本（如残留含废弃 RED 查找路径的
    # 旧版）→ 必须一并重建，否则网关代码升级后线上行为永远停在旧版且无任何提示。
    stale: list = []
    try:
        local_fp = _local_history_fingerprints(built)
        live_fp = _live_history_fingerprints(flows, HISTORY_SUBFLOW_IDS)
        stale = sorted(sid for sid in present_ok
                       if sid in local_fp and live_fp.get(sid) != local_fp[sid])
    except Exception:
        # 指纹算不出来（built.json 异常等）不应阻断原有的缺失/空壳重建能力
        stale = []
    if stale:
        missing = sorted(set(missing) | set(stale))

    if not missing:
        return {"created": False, "exists": True, "missing": [], "rebuilt": [],
                "shells_rebuilt": [], "stale_rebuilt": []}
    by_id = {arr[0]["id"]: arr for arr in built if arr and arr[0].get("type") == "subflow"}
    server = nr.get_default_server_id()

    all_entries: list = []
    rebuilt: list = []
    for sid in missing:
        arr = by_id.get(sid)
        if not arr:
            continue
        # 替换硬编码 HA server（built.json 里写死 e93e1ad9c034e866，不可移植）
        for e in arr:
            if e.get("type") == "api-get-history":
                e["server"] = server
        all_entries.extend(arr)
        rebuilt.append(sid)

    if all_entries:
        live_nodes = _flatten_nodes(nr.list_flows())
        # 剔除 missing 子流程的全部线上条目（def + 内部节点），其余保留，
        # 仅替换命中子流程（避免清场其余 tab）。再 force+allow_partial 部署完整 def+内部。
        kept = [n for n in live_nodes
                if n.get("id") not in missing and n.get("z") not in missing]
        combined = kept + all_entries
        nr.deploy_all(combined, force=True, allow_partial=True, allow_prod=allow_prod)
    shells = [sid for sid in missing if internal.get(sid, 0) == 0]
    return {"created": bool(rebuilt), "exists": not missing, "missing": missing,
            "rebuilt": rebuilt, "shells_rebuilt": shells,
            "stale_rebuilt": stale}


def flow_uses_history_subflow(nodes) -> bool:
    """判断原始 flow 节点里是否引用了任一历史查询子流程实例。

    兼容两种写法（同 flow_uses_bark_subflow）：
      - NR5 前缀型 type="subflow:<id>"（dsl_engine 产出，当前标准写法）
      - 裸型 type="subflow" + c:"<id>"（个别手搓/旧导出）
    """
    for n in nodes or []:
        nt = n.get("type", "")
        for sid in HISTORY_SUBFLOW_IDS:
            if nt == f"subflow:{sid}":
                return True
            if nt == "subflow" and n.get("c") == sid:
                return True
    return False


# ── API 能力从 api_specs 单一真相源派生（见 api_specs.py）──────────────
# 所有 API 能力（如 llm_caiyun_weather / anysearch_batch）只在此处经
# api_specs.API_SPECS 一处定义、两处派生（网关 SubflowSpec + NR tab flow），
# 不再在此手搓，避免"改一处漏一处"的 split。
from .api_specs import API_SPECS  # noqa: E402  (循环导入：subflows 先定义 Param/SubflowSpec/SUBFLOWS)
for _api_spec in API_SPECS:
    SUBFLOWS[_api_spec.name] = _api_spec.to_subflow_spec()


# ── V-NEW-3：声明 history 子流程的输出字段（msg.payload 返回结构顶层键）──
# 这些字段名取自各 subflow 的 notes 文档（data/subflows/subflows.json）。
# 让 flow_linter._compute_reliable_fields 能建模「子流程输出 → 下游 switch 读它」，
# 消除子流程流被误判未定义字段而过度拦截。只声明有文档依据的字段，未知子流程留空（安全侧）。
_SUBFLOW_OUTPUTS: dict[str, list] = {
    "history_state_at": ["found", "entity", "at_iso", "value", "attribute", "unit", "nearest_ts", "source"],
    "history_occurred": ["occurred", "entity", "start_iso", "end_iso", "count", "state",
                          "events", "first_ts", "last_ts"],
    "history_duration": ["total_seconds", "total_human", "entity", "start_iso", "end_iso",
                          "state", "ratio"],
    "history_aggregate": ["value", "unit", "entity", "start_iso", "end_iso", "metric", "attribute", "samples", "error"],
}
for _n, _o in _SUBFLOW_OUTPUTS.items():
    if _n in SUBFLOWS:
        SUBFLOWS[_n].outputs = _o


# 注册表 store 解析（get_subflow 统一入口）────────────────────────────
# 设计取舍（D23 修复）：get_subflow 此前只依赖模块级单例 _registry_store，
# 而该单例仅在 Gateway.__init__ set_registry_store 时注入。任何『未实例化
# Gateway』的编译上下文（独立进程 / import-time 编译 / verify_flow /
# simulate_flow / 单测 / 模块被重新 import 重置全局）下，单例为 None，
# 注册表分支被静默跳过 → 已注册的 imported 子流程查不到 → 间歇性
# C_SUBFLOW_UNKNOWN（实验室已实证：DB 行 status=active 且 nr_subflow_id 有效，
# 但 _registry_store=None 时 get_subflow 仍返回 None）。
# 自愈：单例缺失时按需构造并缓存一个 TaskStore(get_config())，读同一份
# autoflow.db，保证注册表必被查到。仅当构造也失败（config/DB 不可用）时
# 才退回 None（与旧行为一致，fail-safe）。
_FALLBACK_STORE_UNSET = "_unset"   # 哨兵：尚未尝试构造
_fallback_store = _FALLBACK_STORE_UNSET


def _resolve_registry_store(registry_store=None):
    """解析 get_subflow 实际要查的注册表 store。
    - 调用方显式传入 registry_store 时优先；
    - 否则用网关启动时注入的模块级单例 _registry_store；
    - 若两者皆空 → 自愈：按需构造并缓存 TaskStore(get_config())（D23）。"""
    if registry_store is not None:
        return registry_store
    global _registry_store
    if _registry_store is not None:
        return _registry_store
    global _fallback_store
    if _fallback_store == _FALLBACK_STORE_UNSET:
        try:
            from .config import get_config
            from .task_store import TaskStore
            _fallback_store = TaskStore(get_config())
        except Exception:
            _fallback_store = False   # 构造失败：标记，避免每次调用都重试
    return _fallback_store if _fallback_store is not False else None


def get_subflow(name: str, registry_store=None) -> Optional[SubflowSpec]:
    # 1) 网关预置（SUBFLOWS 硬编码清单）优先
    spec = SUBFLOWS.get(name)
    if spec is not None:
        return spec
    # 2) 查注册表（用户从 NR 自省导入的 imported 且 active 子流程）
    #    registry_store 可由调用方传入；否则用网关注入单例；单例缺失时
    #    _resolve_registry_store 会自愈构造 TaskStore 查同一份 DB（D23）。
    store = _resolve_registry_store(registry_store)
    if store is not None:
        meta = store.get_subflow_meta(name)
        if meta and meta.get("status") == "active":
            kind = meta.get("kind", "subflow")
            if kind == "link_out":
                # 注册表也可登记 link_out 型能力（如导入的 fire-and-forget 能力）：
                # 网关发 link out 到 entry_link_id，无 NR 子流程实例。
                entry = meta.get("entry_link_id")
                if entry:
                    return SubflowSpec(
                        name=name,
                        title=meta.get("title") or name,
                        call={"type": "link_out", "entry_link_id": entry},
                        params=_params_from_schema(meta.get("input_schema") or []),
                        description=meta.get("spec_ref") or "",
                        param_style="payload",
                        source="imported",
                    )
            else:
                nr_id = meta.get("nr_subflow_id")
                if nr_id:
                    return SubflowSpec(
                        name=name,
                        title=meta.get("title") or name,
                        # 调用走子流程实例：type=subflow:<nr_subflow_id>
                        call={"type": "subflow", "subflow_id": nr_id},
                        params=_params_from_schema(meta.get("input_schema") or []),
                        description=meta.get("spec_ref") or "",
                    # 与 introspect 推断的 msg.<x> 读取对齐：入参平铺到 msg.<k>
                    param_style="flat",
                    source="imported",
                )
    return None


# 注册表 store 注入点（模块级单例）。网关启动时调用 set_registry_store(gateway.task_store)
# 注入；离线/测试可手动注入。get_subflow 查注册表优先用此单例，但即便单例为 None
# （未实例化 Gateway 的编译上下文），get_subflow 也会经 _resolve_registry_store 自愈
# 构造 TaskStore 查同一份 autoflow.db，故注册表始终可达（D23 修复）。
_registry_store = None


def set_registry_store(store) -> None:
    """注入 TaskStore 实例，使 get_subflow 能查 subflow_registry 表。
    注入为可选加速/覆盖手段；即便不注入，get_subflow 也会自愈读取注册表。"""
    global _registry_store
    _registry_store = store


def _params_from_schema(schema) -> dict:
    """把注册表的 input_schema（[{name,required,type,default,enum,desc}]）
    转成 SubflowSpec.params（{name: Param}），供 resolve_args 复用默认值/类型/枚举校验。"""
    out: dict = {}
    for p in schema or []:
        name = p.get("name")
        if not name:
            continue
        out[name] = Param(
            name=name,
            required=bool(p.get("required", False)),
            default=p.get("default"),
            type=p.get("type", "str") or "str",
            enum=p.get("enum"),
            desc=p.get("desc", ""),
        )
    return out


def _schema_from_params(params: dict) -> list:
    """_params_from_schema 的逆操作：SubflowSpec.params（{name: Param}）→ 注册表 input_schema 列表。"""
    out = []
    for name, p in (params or {}).items():
        out.append({
            "name": name,
            "required": bool(getattr(p, "required", False)),
            "type": getattr(p, "type", "str") or "str",
            "default": getattr(p, "default", None),
            "enum": getattr(p, "enum", None),
            "desc": getattr(p, "desc", ""),
        })
    return out


# ── 注册校验门（#575 Full 阶段）：注册/导入前的统一校验 ──────────────────
# 校验 key 合法性 + 不与预置撞名 + input_schema/env_requirements 结构，
# 返回 {ok, error, cleaned}。cleaned 为规范化后的字段，供 register_subflow 直接使用。
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_subflow_registration(key, nr_subflow_id, source_type="imported",
                                  title="", input_schema=None,
                                  env_requirements=None) -> dict:
    """注册校验门：返回 {ok, error, cleaned}。

    - key 必须是 DSL 安全标识符（[A-Za-z_][A-Za-z0-9_]*），且不得与网关预置子流程撞名
    - imported 必须带 nr_subflow_id
    - input_schema 必须是 [{name,required?,type?,default?,enum?,desc?}] 列表，每项 name 为字符串
    - env_requirements 必须是字符串列表（或 {name} 字典列表）
    """
    if not key or not str(key).strip():
        return {"ok": False, "error": "key（DSL 调用名）必填"}
    key = str(key).strip()
    if not _KEY_RE.match(key):
        return {"ok": False, "error": f"key 须为字母/下划线开头的标识符（[A-Za-z_][A-Za-z0-9_]*），当前：{key}"}
    if key in SUBFLOWS:
        return {"ok": False, "error": f"key 与网关预置子流程撞名：{key}（请换一个名字）"}
    st = (source_type or "imported").lower()
    if st not in ("managed", "imported"):
        return {"ok": False, "error": f"source_type 须为 managed/imported，当前：{st}"}
    if st == "imported" and not nr_subflow_id:
        return {"ok": False, "error": "imported 子流程必须提供 nr_subflow_id"}
    # input_schema 规范化
    cleaned_schema = []
    if input_schema:
        if not isinstance(input_schema, list):
            return {"ok": False, "error": "input_schema 必须是列表"}
        for i, p in enumerate(input_schema):
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("name"):
                return {"ok": False, "error": f"input_schema[{i}] 缺 name 或 name 非字符串"}
            cleaned_schema.append({
                "name": p["name"],
                "required": bool(p.get("required", False)),
                "type": p.get("type", "str") or "str",
                "default": p.get("default"),
                "enum": p.get("enum"),
                "desc": p.get("desc", ""),
            })
    # env_requirements 规范化 → 字符串列表
    cleaned_env = []
    if env_requirements:
        if not isinstance(env_requirements, list):
            return {"ok": False, "error": "env_requirements 必须是列表"}
        for e in env_requirements:
            if isinstance(e, str):
                cleaned_env.append(e)
            elif isinstance(e, dict) and isinstance(e.get("name"), str):
                cleaned_env.append(e["name"])
            else:
                return {"ok": False, "error": f"env_requirements 项须为字符串或 {{name}} 字典，当前：{e!r}"}
    return {"ok": True, "cleaned": {
        "key": key,
        "title": (title or key).strip(),
        "nr_subflow_id": (nr_subflow_id or None),
        "source_type": st,
        "input_schema": cleaned_schema,
        "env_requirements": cleaned_env,
    }}


# 网关预置、需在注册表登记的「subflow 实例型」子流程（NR 子流程实例，需 nr_subflow_id）。
# link_out 型能力（demo_notify / apisay / weather / anysearch）不再排除，
# 见 seed_managed_subflows：它们以 kind=link_out 纳入治理（fire-and-forget，网关发 link out
# 到 entry_link_id，无 NR 子流程实例，但仍可在 WebUI 查看入参与状态）。
_MANAGED_SUBFLOW_KEYS = (
    "bark_push", "history_state_at", "history_occurred",
    "history_duration", "history_aggregate",
)


def _env_requirements_for_managed(key: str) -> list:
    """预置子流程的 env 配置变量需求（如 bark 需要 BARK_SERVER/BARK_KEY）。"""
    if key == "bark_push":
        return [e["name"] for e in BARK_SUBFLOW_SPEC.get("env", [])]
    return []


def seed_managed_subflows(store) -> dict:
    """把网关预置子流程写入 subflow_registry（#578/#587，幂等）。

    覆盖两类：
      - subflow 实例型（_MANAGED_SUBFLOW_KEYS：bark_push / history_* 等），需 nr_subflow_id；
      - link_out 型（SUBFLOWS 中 call.type=="link_out" 的能力：demo_notify /
        apisay / weather / anysearch），网关只发 link out 到 entry_link_id，无 NR 子流程实例。
    仅 seed 未在表中的 key（已存在则跳过，保护用户可能手动改过的 status / input_schema /
    title）。返回 {ok, seeded, skipped}。运行时机：网关启动时（Gateway.__init__ 注入 store 后）。
    """
    seeded, skipped = 0, 0
    seen = set()
    # 1) subflow 实例型（_MANAGED_SUBFLOW_KEYS）
    for key in _MANAGED_SUBFLOW_KEYS:
        spec = SUBFLOWS.get(key)
        if spec is None:
            continue
        # preload=False 的示例（如 demo_notify）注册但不随启动预载到用户面板
        if spec.preload is False:
            continue
        seen.add(key)
        if store.get_subflow_meta(key):
            skipped += 1
            continue
        call = spec.call
        nr_id = call.get("subflow_id") if call.get("type") == "subflow" else None
        r = store.register_subflow(
            key, title=spec.title, nr_subflow_id=nr_id,
            source_type="managed",
            input_schema=_schema_from_params(spec.params),
            env_requirements=_env_requirements_for_managed(key),
            owner="system", status="active", spec_ref=key,
            kind="subflow",
        )
        if r["ok"]:
            seeded += 1
    # 2) link_out 型能力（fire-and-forget，网关发 link out 到 entry_link_id）
    for key, spec in SUBFLOWS.items():
        call = spec.call or {}
        if call.get("type") != "link_out":
            continue
        # preload=False 的示例（如 demo_notify）注册但不随启动预载到用户面板
        if spec.preload is False:
            continue
        if key in seen:
            continue
        if store.get_subflow_meta(key):
            skipped += 1
            continue
        r = store.register_subflow(
            key, title=spec.title, nr_subflow_id=None,
            source_type="managed",
            input_schema=_schema_from_params(spec.params),
            env_requirements=[],
            owner="system", status="active", spec_ref=key,
            kind="link_out", entry_link_id=call.get("entry_link_id"),
        )
        if r["ok"]:
            seeded += 1
    return {"ok": True, "seeded": seeded, "skipped": skipped}


# ── NR 子流程自省（#576）：导入用户既有子流程时免手填前置参数 ──────────────
# NR 子流程 def 不声明形式入参；真实调用方入参藏在内部节点读 msg.<x> 里，
# 子流程级配置变量则在 def.env 显式声明。自省二者，供 WebUI 注册时自动填充。
_MSG_READ_RE = re.compile(r"msg\.(?:payload\.)?([A-Za-z_][A-Za-z0-9_]*)")
# 消息信封字段：非业务入参，过滤掉避免噪声
_RESERVED_MSG = {"payload", "topic", "headers", "url", "req", "res",
                 "error", "_session", "statusCode", "responseUrl"}


def _introspect_nr_subflow_from_flows(flows, nr_subflow_id: str) -> dict:
    """从 NR flows 列表（list_flows 返回）自省抽取某子流程的『前置参数』。

    返回 {ok, nr_subflow_id, title, in_ports, out_ports, env_requirements,
          input_schema, internal_node_count} 或 {ok:False, error}。
      - env_requirements：def.env → 子流程级配置变量（owner 需提供），[{name,type}]
      - input_schema    ：扫描内部节点 function/change 的 msg.<x> 读取，best-effort
                          推断调用方入参（NR 不声明形式入参，真实入参藏在函数读 msg 中）
    纯函数、无副作用，便于离线 mock 测试。
    """
    def_entry = None
    for f in flows or []:
        if f.get("type") == "subflow" and f.get("id") == nr_subflow_id:
            def_entry = f
            break
    if def_entry is None:
        return {"ok": False, "error": f"NR 中未找到 type=subflow 且 id={nr_subflow_id}"}

    z = nr_subflow_id
    internal = [n for n in flows if n.get("z") == z]
    text = []
    for n in internal:
        if n.get("type") == "function":
            text.append(n.get("func", "") or "")
        elif n.get("type") == "change":
            for r in n.get("rules", []) or []:
                text.append(f"{r.get('to', '')} {r.get('p', '')}")
        elif n.get("type") == "template":
            text.append(n.get("format", "") or n.get("field", ""))
    reads = set()
    for t in text:
        for m in _MSG_READ_RE.finditer(t or ""):
            reads.add(m.group(1))
    input_names = sorted(reads - _RESERVED_MSG)
    input_schema = [{
        "name": nm, "required": False, "type": "str",
        "default": None, "enum": None,
        "desc": f"（自省推断）来自子流程内部 msg.{nm} 读取",
    } for nm in input_names]

    env_reqs = [{"name": e.get("name"), "type": e.get("type", "str")}
                for e in def_entry.get("env", []) or []]

    return {
        "ok": True,
        "nr_subflow_id": nr_subflow_id,
        "title": def_entry.get("name", "") or "",
        "in_ports": len(def_entry.get("in", []) or []),
        "out_ports": len(def_entry.get("out", []) or []),
        "env_requirements": env_reqs,
        "input_schema": input_schema,
        "internal_node_count": len(internal),
    }


def introspect_nr_subflow(nr, nr_subflow_id: str) -> dict:
    """生产路径：经 nr 客户端读 /flows 后自省（见 _introspect_nr_subflow_from_flows）。"""
    try:
        flows = nr.list_flows()
    except Exception as e:
        return {"ok": False, "error": f"读取 NR flows 失败: {type(e).__name__}: {e}"}
    return _introspect_nr_subflow_from_flows(flows, nr_subflow_id)
