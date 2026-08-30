#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 编排核心（把各层串起来）

agent 唯一接触面：所有操作都经过 共享态 → 防御层 → 确认闸。
读即时返回；写进待确认队列，批准后才落地。结构上不暴露 replace-all / delete-all。
"""
import os
import json
import re
import secrets
import time
import threading
import uuid
import urllib.request
import urllib.error
from collections import deque
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
import logging

from .config import get_config, is_raw_node_escape_enabled, get_deploy_policy, load_feature_flags
from .state import SharedState
from .defense import DefenseLayer, DefenseError
from .confirm import ConfirmationGate, PendingOp, ConfirmationError
from .proposals import ProposalStore
from .plan_store import PlanStore
from .command_store import CommandStore
from .decision_store import DecisionStore
from .task_store import TaskStore

# 决策看门狗单例守卫：网关多实例（webui / 各 mcp_server 调用）共享，只启一个后台线程，
# 避免对同一 pending 决策重复发 Bark 催办。
_watchdog_lock = threading.Lock()
_watchdog_started = False
from .ha_layer import HALayer
from .nr_layer import NRLayer
from .debug_bridge import DebugBridge

# debug_bridge 进程内单例（#649 修复关键）：
# 网关以单 uvicorn worker 运行，但 _gw() 每次返回【新】Gateway，WebUI 的 build_webui_asgi
# 也自建 Gateway；若每个 Gateway 各 new 一个 DebugBridge，后台线程只在第一实例启动（start()
# 有类级单例守卫），其余实例的 bridge 永远 disconnected / 空缓冲，导致 /api/debug 与
# autoflow_debug_read 读到的永远是「没连上」的孪生实例。故在此缓存唯一 bridge 实例，
# 所有 Gateway（MCP 面 / WebUI 面）共享同一份连接状态与本地缓冲。
_debug_bridge_singleton = None

def _get_debug_bridge(nr_client, nr_url: str, enabled: bool) -> "DebugBridge":
    global _debug_bridge_singleton
    if _debug_bridge_singleton is None:
        _debug_bridge_singleton = DebugBridge(
            nr_client=nr_client, nr_url=nr_url, enabled=enabled)
    elif enabled and not _debug_bridge_singleton.enabled:
        # 首建时未启用、后续调用要求启用：升级为启用并启动后台线程（幂等）
        _debug_bridge_singleton.enabled = True
        _debug_bridge_singleton.start()
    return _debug_bridge_singleton
from .schemas import SceneIntent, validate_intent
from .build_scene import build_scene
from .dsl_engine import parse as _dsl_parse, compile as _dsl_compile
from .flow_linter import lint_flow
from .flow_simulator import simulate_flow
from .flow_diff import diff_flow_dicts, _node_sig
from .lib.affordance import affordance_for
from .telemetry import tag_action as _tag_action

def _compile_error_envelope(e) -> dict:
    """把 DSLError 转成结构化 compile_error 信封，供 agent 机读自修正。

    字段：
      code   —— C_* 错误码（见 dsl_engine 常量）；非 DSLError 兜底 C_PARSE。
      line   —— 出错行号（无则 None）。
      message—— 人类可读消息（含『第 X 行:』前缀）。
      hint   —— 一句话『怎么改』，DSLError 自动从 message 的「（建议：…）」抽取。
    任何异常类型都安全（用 getattr 兜底）。"""
    code = getattr(e, "code", "C_PARSE")
    line = getattr(e, "line", None)
    hint = getattr(e, "hint", "") or ""
    return {"code": code, "line": line, "message": str(e), "hint": hint}

# 原生节点逃逸关键字（Phase 4）：DSL 含此关键字且开关关闭时，编译入口直接拒绝。
_RAW_NODE_KW_RE = re.compile(r"^\s*(原生节点|raw_node)\s*:", re.MULTILINE)

# ── D36 修复（WB83 P1 DoS 根因）：entity_id 形态判定 ──
# HA entity_id 形如 domain.object（小写字母/数字/下划线/连字符，无空格、无中文、无大写）。
# 这种字符串只可能是「真实实体 ID 或编造的实体 ID」，绝不该走自然语言模糊解析；
# 模糊解析（resolve_entity 全目录扫描打分）只服务于中文/友好名输入。
# 低于目录命中即判未知，直接返回 None，避免对 N 个未知实体触发 N 次 O(目录) 模糊扫描。
_ENTITY_ID_SHAPE_RE = re.compile(r"^[a-z0-9_][a-z0-9_.\-]*\.[a-z0-9_.\-]+$")

# D36 防御纵深：resolve_entity 全目录模糊扫描代价高（O(目录)）。同进程内对相同查询
# 结果缓存，使模糊成本仅首次发生、跨调用摊销；目录刷新后 stale 仅导致 fail-closed
# 误拒（安全），不导致误放行。容量上限防无限增长。
_RESOLVE_ENTITY_CACHE: dict = {}
_RESOLVE_ENTITY_CACHE_MAX = 1024

# D36 防御纵深：单次闸门校验引用的实体上限。家自动化流极少超过此数；超过即判异常复杂度，
# 廉价拒绝（不逐个模糊解析），杜绝『N 个未知实体 → N 次 O(目录) 扫描』的串行阻塞 DoS。
_MAX_ENTITY_REFS = 256


# ── R_branch_required 内容触发（修复 iss_ebfe742222）──
# DSL 自然语言里出现条件连词/阈值比较，但编译产物不含任何分支/条件门节点时，
# 动作会被无条件执行（黑箱模式易丢分支）。此处做「内容意图 → 编译产物」对账：
# 意图有分支、产物无门 → 硬拦（无论 strict 与否），逼 agent 补全 分支:/条件:/查询:/时间段:。
# 注意：分支:/条件:/查询:/时间段: 是 DSL 语法 token，编译器会产出对应门节点，
# _flow_has_branch_node 必为 True，不会误拦；本正则只命中「自然语言条件但没写门」的危险情形。
_BRANCH_CUE_RE = re.compile(
    r"(如果|若|一旦|除非"                      # 显式条件连词
    r"|当[^。\n]{0,10}则|当[^。\n]{0,10}就"    # 当…则 / 当…就
    r"|只有[^。\n]{0,12}才"                    # 只有…才
    r"|(?<!刚)才"                              # 才（排除「刚才」误判）
    r"|超过|高于|低于|大于|小于|不低于|不高于|不超过)"  # 阈值比较
)

def _dsl_implies_branch(dsl: str) -> bool:
    """DSL 文本是否含条件语义意图（自然语言连词/阈值比较）。"""
    if not dsl:
        return False
    return bool(_BRANCH_CUE_RE.search(dsl))

def _flow_has_branch_node(flow: Optional[Dict[str, Any]]) -> bool:
    """编译产物是否含真正的分支/条件门节点（与 verify_task_dsl 内 _has_branch_node 同义，
    提升为模块级供 propose_dsl 内容触发复用）。接受：
      · switch            —— 分支:/否则: 或复杂 JSONata 条件
      · api-current-state —— 条件:/查询: 门控（outputs>=2 才认，排除取值: 的只读节点）
      · time-range-switch —— 时间段: 门控（2 路输出）
    """
    for n in (flow or {}).get("nodes", []):
        t = n.get("type")
        if t == "switch":
            return True
        if t in ("api-current-state", "time-range-switch"):
            try:
                outs = int(n.get("outputs", 0) or 0)
            except (TypeError, ValueError):
                outs = 0
            if outs >= 2:
                return True
    return False

# ── A9 结构化日志（trace_id + 各阶段耗时）──
# 纯增量：网关此前无任何日志输出，加 logging 不影响既有行为。
# 每条请求生成 trace_id，关键入口(propose_dsl/deploy_raw/deploy_proposal/list_pending)
# 在 start / 各阶段 / done|error 输出一行 JSON，便于测试时按 trace_id 串联定位瓶颈。
_gw_logger = logging.getLogger("autoflow.gateway")
if not _gw_logger.handlers:
    _gw_h = logging.StreamHandler()
    _gw_h.setFormatter(logging.Formatter("%(message)s"))
    _gw_logger.addHandler(_gw_h)
    _gw_logger.setLevel(logging.INFO)
    _gw_logger.propagate = False

def _new_trace_id() -> str:
    return uuid.uuid4().hex[:12]

def _slog(trace_id: str, stage: str, **fields) -> None:
    """输出一行结构化日志（JSON）：ts + trace_id + stage + 自定义字段。

    同时写入进程内环形缓冲 _TRACE_RING（cap 200），供 WebUI 诊断查看器回显最近 trace。
    环形缓冲写与 _GOLDEN_JOBS 共用同一把锁，避免 _slog 高频写与 list_golden_jobs/get_recent_traces 并发竞争。
    """
    rec = {"ts": round(time.time(), 3), "trace_id": trace_id, "stage": stage}
    rec.update(fields)
    _gw_logger.info(json.dumps(rec, ensure_ascii=False))
    with _GOLDEN_JOBS_LOCK:
        _TRACE_RING.append(rec)

def _enrich_pending_op(op: Dict[str, Any]) -> Dict[str, Any]:
    """A10：给待确认项附加人类友好的预览字段（不改动原字段）。

    - create_flow / update_flow：算 node_count + 一句预览；
    - ha_call：附 domain.service 预览。
    便于在 WebUI / 黑箱回显里一眼看清「这条待确认要干啥、动多大」。
    """
    e = dict(op)
    payload = op.get("payload") or {}
    op_type = op.get("operation", "")
    if op_type in ("create_flow", "update_flow"):
        flow = payload.get("flow") or {}
        nodes = flow.get("nodes") or []
        e["node_count"] = len(nodes)
        label = flow.get("label") or payload.get("flow_id") or "未命名"
        e["preview"] = f"{'新建' if op_type == 'create_flow' else '更新'}流程「{label}」（{len(nodes)} 节点）"
    elif op_type == "ha_call":
        dom = payload.get("domain", "?")
        svc = payload.get("service", "?")
        e["preview"] = f"HA 写操作 {dom}.{svc}"
    elif op_type == "set_tab_state":
        e["preview"] = (f"切换 tab 状态：{payload.get('flow_id', '?')} → "
                        f"{'启用' if payload.get('enabled') else '禁用'}")
    else:
        e.setdefault("preview", op.get("summary", ""))
    return e

def _build_node_diff(live: Optional[Dict[str, Any]],
                     proposed: Dict[str, Any]) -> Dict[str, Any]:
    """A8：给 dry-run 生成「将增/删/改哪些节点」的节点级摘要。

    - live=None（新建）：全部节点都是 added。
    - live 存在（更新）：用 flow_diff 做签名配对（remap 后 id 与线上不同，故
      topology=False、strict=False，只看业务字段增删改）。
    返回 {added:[{type,sig}], removed:[{type,sig}], changed:[{node,type,field,from,to}], report}。
    """
    if live is None:
        added = [{"type": n.get("type"), "sig": _node_sig(n)}
                 for n in proposed.get("nodes", []) if n.get("type") != "tab"]
        return {"added": added, "removed": [], "changed": [],
                "report": f"[dry-run · 新建] 将新增 {len(added)} 个节点"}
    dr = diff_flow_dicts(live, proposed, strict=False, topology=False)
    added = [{"type": n.get("type"), "sig": _node_sig(n)} for n in dr.extra_nodes]
    removed = [{"type": n.get("type"), "sig": _node_sig(n)} for n in dr.missing_nodes]
    changed = [{"node": ident, "type": ntype, "field": fld, "from": rv, "to": cv}
               for (ident, ntype, fld, rv, cv) in dr.field_issues]
    return {"added": added, "removed": removed, "changed": changed,
            "report": dr.report()}

# P5 · E2E 插桩/比对共用的『不可插桩节点』集合。
# 这些类型要么是终点(debug/complete/status)、要么是入口触发器(inject)、
# 要么是错误捕获(catch)——_instrument_flow 不给它们加 tap，故它们【永远】不会
# 在 trace 里自报。_compare_trace 必须同步排除，否则会把它们冤枉成『断点』。
#
# D34 修复：移除 "link out" / "link in"。
#   - link out：保持 SINK 跳过（**绝不**插 tap、改 wires）——link out 无 wires 输出，
#     强行加 wires 会破坏其 link 广播语义并导致 NR 运行时 TypeError
#     (Cannot read properties of null (reading 'config'))。其到达由上游 tap 间接覆盖。
#   - link in：**改为可插桩**（它有正常 wires 输出，加 tap 分支安全）——这样 e2e
#     能真实记录『link 穿越成功』，link 链路下游节点也可被正确追踪与比对。
#   注：_derive_planned_path 的 link 隐式边(link out→link in)不依赖 wires，仍生效。
E2E_SINK_TYPES = {"debug", "link out", "complete", "status", "catch", "inject", "comment"}


def _link_ids(value) -> set:
    """把 link out / link in 节点的 `links` 字段归一为【目标 id 字符串集合】。

    Node-RED 不同版本 / 导出工具对 links 的序列化形态不一致：
      - 主流形态：字符串数组  ["li1", "li2"]
      - 部分导出 / 构造形态：对象数组  [{"id": "li1"}, ...]
    统一剥成 id 字符串集合，避免 `set([dict])` 直接抛
    `unhashable type: 'dict'`（D30 e2e trace 含 link 节点崩溃）。"""
    out: set = set()
    if not value:
        return out
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        i = value.get("id")
        if i:
            out.add(i)
        return out
    if isinstance(value, list):
        for x in value:
            out |= _link_ids(x)
    return out


# C1 · 可被「合成 inject」替代的事件入口节点类型（staging 无 HA websocket 推送时，
# 在插桩副本里把它们换成发出 faithful state-change msg 的 inject，从而真实点燃下游逻辑）。
E2E_STATE_ENTRY_TYPES = {
    "server-state-changed", "server-event", "trigger", "poll-state",
    "events: all", "events: state", "ha-entity-events",
    "api-current-state", "api-get-history",
}

# ── Golden 回归：进程内活动日志（模块级，跨请求持久；供 autoflow_golden_eval 读取判分）──
# 注意：_gw() 每次 MCP 调用都新建 Gateway 实例，故不能挂在实例上，必须模块级全局。
# 单进程内有效；若将来 uvicorn 多 worker 才需改文件/Redis 支撑。设计者不读此日志判分。
_PROPOSAL_LOG: List[Dict[str, Any]] = []
_PROPOSAL_LOG_LOCK = threading.Lock()
_PROPOSAL_LOG_CAP = 500

# ── Golden 回归：异步任务 store（非阻塞改造，根治『golden_eval 占死网关→deepseek++ 连不上』死锁）──
# autoflow_golden_eval 改为『发完 ds_bridge 立即返回 job_id』，真正的 300s 等待+判分跑在后台 daemon 线程，
# 网关事件循环立刻腾出，deepseek++ 的 autoflow_* 工具调用才能在同一窗口正常进来（否则互相饿死）。
_GOLDEN_JOBS: Dict[str, Dict[str, Any]] = {}
_GOLDEN_JOBS_LOCK = threading.Lock()

# P4-C：进程内结构化 trace 环形缓冲（cap 200），承接 _slog 输出，
# 供 WebUI 诊断查看器只读回显「最近发生了什么」。不落盘、重启即丢——
# 诊断本就是瞬时视角，无需持久化，也避免引入磁盘 IO 与隐私留存。
_TRACE_RING = deque(maxlen=200)

def _record_proposal_event(agent_id: str, kind: str, data: Dict[str, Any]) -> None:
    """把一次 agent 活动(提案/解析)追加到进程内活动日志，供 golden 评测读取。"""
    entry = {"ts": time.time(), "agent_id": agent_id, "kind": kind, "data": data}
    with _PROPOSAL_LOG_LOCK:
        _PROPOSAL_LOG.append(entry)
        if len(_PROPOSAL_LOG) > _PROPOSAL_LOG_CAP:
            del _PROPOSAL_LOG[: len(_PROPOSAL_LOG) - _PROPOSAL_LOG_CAP]

# Node-RED 核心内置节点（无需安装即存在于所有实例）。
# get_installed_node_types() 经 /nodes API 只返回已注册的贡献节点，漏报核心节点，
# 若只用它做闸门会误杀所有用标准节点(http in/split/join/file 等)的合法 flow。
_NR_CORE_NODE_TYPES = {
    "tab", "subflow", "group", "comment", "debug", "inject", "catch", "status",
    "complete", "function", "switch", "change", "range", "template",
    "delay", "trigger", "rbe", "smooth", "join", "split", "sort", "batch",
    "exec", "http in", "http request", "http response", "websocket in",
    "websocket out", "mqtt in", "mqtt out", "tcp in", "tcp out", "udp in",
    "udp out", "file", "file in", "watch", "tail", "link in", "link out",
    "link call", "ping", "tls-config", "websocket-listener", "flow",
    "subflow-instance", "unknown", "markdown", "http proxy", "status",
}

def check_unknown_node_types(flow: Dict[str, Any], installed: set) -> List[str]:
    """返回 flow 中在目标 NR 未注册的节点类型（空=全部已注册）。

    用于部署前注册表闸门：编译产物若含目标 NR 没安装的节点类型，
    部署即坏（陌生节点静默丢 msg，整条下游断掉，且白/黑箱都查不出）。
    flow 可以是 {nodes:[...]} 或扁平节点列表。
    核心节点(NR 内置)自动视为已知，规避 /nodes API 漏报核心节点的误杀。
    """
    nodes = flow.get("nodes") if isinstance(flow, dict) else flow
    if not isinstance(nodes, list):
        return []
    unknown: List[str] = []
    known = installed | _NR_CORE_NODE_TYPES
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        # 子流程实例的 type 形如 "subflow:<24位hex>"（带前缀，非纯 hex），
        # 不在 _NR_CORE_NODE_TYPES 里，其合法性取决于目标 NR 是否真有该子流程定义——
        # 由 _gate_node_types 把目标 NR 已装子流程的 "subflow:<id>" 合并进 installed 决定。
        # 保留对「纯 24 位 hex」节点的跳过（旧格式兜底）。
        if t and t not in known and not (len(t) == 24 and all(c in "0123456789abcdef" for c in t)):
            unknown.append(t)
    return unknown


# ── R9(#round4) iss_86d66844f7（报告 A19）：schema 级「致命错误」集 ──
# 症结：validate_flow_schema 一直只把问题**记进 validation 数组**，deploy_raw 里那句
# `if errors: pass  # 记录后继续` 让缺 server / switch wires≠rules 这种「部署即坏」的流
# 照样落 NR，自检工具还回 will_deploy_block=false、node_gate_ok=true——绿灯放行坏流。
# 这里给致命项打上稳定 rule 码，统一升格为 blocking：
#   S1 结构非法（flow 非对象 / nodes 非数组）—— NR 收到即 400/静默丢弃
#   S2 节点缺 type       —— NR 无法实例化该节点，整条链断
#   S3 HA 节点缺 server  —— 注意 _inject_ha_server 只替换 REPLACE_WITH_HA_SERVER 占位符，
#                          「压根没有 server 字段」的节点不会被补值，部署后永久未配置
#   S4 switch rules/wires 不匹配 —— 分支错位或整枝丢消息，静态合法运行必错
#   S5 空 flow（nodes 为空数组）—— 覆盖已有 tab 时等同静默清空，破坏性最强
# 非致命项（数字 id、负坐标、缺顶层 label、POST 无 body、http 缺 url 等）保持原级别，
# 仅报告不拦——避免误伤合法手搓流（白箱 escape hatch 原则）。
SCHEMA_BLOCK_RULES = {"S1", "S2", "S3", "S4", "S5"}


def schema_blocking_issues(issues: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从 validate_flow_schema 结果中挑出会硬拦部署的致命项（R9）。

    Args:
        issues: validate_flow_schema 返回的 [{"level","rule","node_id","message"}] 列表。

    Returns:
        致命项子集（level=error 且 rule ∈ SCHEMA_BLOCK_RULES）；输入为空时返回 []。
    """
    return [v for v in (issues or [])
            if isinstance(v, dict) and v.get("level") == "error"
            and v.get("rule") in SCHEMA_BLOCK_RULES]


# ── C4：staging 闸门「分支感知重放」辅助（vhass 真闭环）──
# 让闸门不再"跳过所有 switch 后代"，而是评估 switch/条件/时间段门控，只重放
# 命中分支内的 HA 意图，从而能断言多步/状态类场景（关闭"只验闸门没验真跑"缺口）。
def _vg_resolve_path(msg, path):
    cur = msg
    for part in (path or "payload").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _vg_set_path(msg, path, val):
    """按点路径写 msg，返回是否真正写入。

    刻意复刻 NR `RED.util.setObjectProperty` 的语义：中间段若**既非 dict 也非缺失**
    （典型：inject payloadType=date → msg.payload 是标量），既不下钻也不创建，
    直接放弃写入且不报错。返回 False 让调用方能把这次「静默丢写」显式记为告警，
    而不是像旧版那样连闸门自己都不知道字段没落地（A15-b）。
    """
    cur = msg
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            return False
    cur[parts[-1]] = val
    return True


# 编译器为规避「标量 payload 静默吞写」在取值节点注入的容器归一表达式，
# 形如 `$type(payload) = "object" ? payload : {}`（见 dsl_engine._emit_read_state）。
_VG_PAYLOAD_OBJ_RE = re.compile(
    r'^\s*\$type\(\s*(?P<p>[\w.]+)\s*\)\s*=\s*"object"\s*\?\s*(?P=p)\s*:\s*\{\s*\}\s*$')

def _vg_val_eq(val, expect, vt):
    # 剥离两侧单/双引号（与编译器分支值归一化保持一致），避免 'off' vs "off" 类引号不一致误判
    a = str(val).strip().strip('"\'')
    b = str(expect).strip().strip('"\'')
    if vt == "num":
        try:
            return float(a) == float(b)
        except Exception:
            return False
    if vt == "bool":
        return a.lower() == b.lower()
    return a == b

def _vg_lookup(msg, var):
    """从 msg 取变量（支持顶级键、msg. 前缀、payload、payload.xxx）。

    O1（2026-08-29，WB93）：F11 后编译器分支 jsonata 用 `msg.<field>` 引用取值标签
    （如 `$number(msg.亮度)`），本函数此前不认 `msg.` 前缀 → 恒 unknown → 闸门
    保守视为命中 → 取值-label 分支永远「未充分验证」。现剥离 `msg.` 前缀后按
    msg 根/payload 路径解析（`msg.payload.x` 等价 `payload.x`）。"""
    if not isinstance(msg, dict):
        return None
    if var.startswith("msg."):
        var = var[4:]
        if var.startswith("msg."):  # 防御：真实字段名就叫 "msg.x" 时不二次剥离
            var = "msg." + var[4:]
    if var in msg:
        return msg[var]
    if var == "payload":
        return msg.get("payload")
    if var.startswith("payload."):
        cur = msg.get("payload")
        for part in var.split(".")[1:]:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur
    return None

def _vg_split_outer(s, sep):
    """按 sep 拆分（顶层，忽略括号内）。无拆分点时返回 None。"""
    parts, depth, buf = [], 0, ""
    i, L, n = 0, len(s), len(sep)
    while i < L:
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and s[i:i + n] == sep:
            parts.append(buf.strip())
            buf = ""
            i += n
            continue
        buf += ch
        i += 1
    if buf.strip():
        parts.append(buf.strip())
    return parts if len(parts) > 1 else None

def _vg_jsonata_cmp(val, op, num):
    if op == "<":
        return val < num
    if op == ">":
        return val > num
    if op == "<=":
        return val <= num
    if op == ">=":
        return val >= num
    if op in ("=", "=="):
        return val == num
    if op in ("!=", "<>"):
        return val != num
    return False

def _vg_eval_jsonata_expr(expr, msg):
    """本地子集求值黑箱子常见 JSONata 分支。

    返回 (matched, known)：known=False 表示无法解析（调用方保守视为命中）。
    支持：$number(VAR) <op> NUM、VAR = "STR"/!= "STR"、裸变量布尔、
    及顶层 and/or 组合。足以覆盖 DSL 黑箱实际产出的分支写法。
    """
    if not expr:
        return (False, False)
    e = expr.strip()
    for sep in (" and ", " or "):
        parts = _vg_split_outer(e, sep)
        if parts:
            subs = [_vg_eval_jsonata_expr(p, msg) for p in parts]
            if not all(k for _, k in subs):
                return (False, False)  # 含无法解析 → 整体未知
            if sep == " and ":
                return (all(m for m, _ in subs), True)
            return (any(m for m, _ in subs), True)
    m = re.match(r"^\$number\(\s*([\w.]+)\s*\)\s*(<=|>=|!=|<>|<|>|=)\s*(-?\d+(?:\.\d+)?)$", e)
    if m:
        var, op, num = m.group(1), m.group(2), float(m.group(3))
        # O1（2026-08-29，WB93）：$number(<数值字面量>) 是纯常量比较，此前被当变量名
        # 查不到 → known=False → 保守视为命中 → 未充分验证。常量无需 msg 数据即可求值。
        if re.fullmatch(r"-?\d+(?:\.\d+)?", var):
            return (_vg_jsonata_cmp(float(var), op, num), True)
        val = _vg_lookup(msg, var)
        if val is None:
            return (False, False)
        try:
            val = float(val)
        except Exception:
            return (False, False)
        return (_vg_jsonata_cmp(val, op, num), True)
    m = re.match(r"^([\w.]+)\s*(!=|<>|=)\s*[\"']([^\"']*)[\"']$", e)
    if m:
        var, op, s = m.group(1), m.group(2), m.group(3)
        val = _vg_lookup(msg, var)
        if val is None:
            return (False, False)
        eq = str(val) == s
        return ((not eq) if op in ("!=", "<>") else eq, True)
    if re.fullmatch(r"[\w.]+", e):
        val = _vg_lookup(msg, e)
        return (val not in (None, False, 0, "0", "", "off", "unknown", "unavailable"), True)
    return (False, False)

def _vg_eval_switch(node, msg, warnings=None, dead_rules=None, report=None):
    """返回 switch 命中输出的索引列表（checkall=true 语义：可多输出）。

    dead_rules：`{switch_id: {规则下标: [未定义字段]}}`（来自 flow_linter.R31 静态判定）。
    命中其中的规则一律按**不命中**处理——见 G3 注释。
    report：可选 dict，回填 {"dead": [...], "conservative": [...], "unevaluable": [...]}
            供闸门归因。

    【W3·闸门诚实性】本求值器只实现了 eq/neq/jsonata(jsonata_exp)/else/otherwise 五类规则；
    NR switch 的 lt/lte/gt/gte/btwn/cont/regex/true/false/null/nnull/empty/nempty/istype/hask
    等类型化规则**没有任何 elif 接住** → 规则静默不命中 → 下游动作永不激活 → 0 重放。
    若此时 flow 又「声明了动作」，闸门实际什么都没验证，却可能因 _cause 为空而误报
    fully_verified=True（假绿）。同理，eq/neq 引用**未声明属性**时 val 解析不到值，也属
    「闸无法求值」。这两类一律回填 report["unevaluable"]，由 G2 重放归零检测按
    「无法验证」处置（fail_closed 下 block），绝不静默放过。
    """
    prop = node.get("property", "payload")
    rules = node.get("rules", []) or []
    val = _vg_resolve_path(msg, prop)
    node_dead = (dead_rules or {}).get(node.get("id") or "", {}) or {}
    nlabel = node.get("name") or node.get("id") or "switch"

    def _mark_unevaluable(i, kind):
        if report is None:
            return
        report.setdefault("unevaluable", []).append({
            "node_id": node.get("id"), "node_name": node.get("name"),
            "rule_index": i, "kind": kind, "expr": rule.get("v", ""),
        })
        if warnings is not None:
            warnings.append(
                f"switch「{nlabel}」第 {i + 1} 条分支闸门【无法本地求值】"
                f"（{kind}）：该分支行为不可验证，下游动作按「未激活」处理，"
                f"其 THEN 体不在重放/断言范围；依赖该分支的期望项记为 N/A（不算通过）。"
                f"请改用闸门支持的 eq/neq/JSONata 条件，或显式声明所引用的字段。")

    taken = []
    matched = False
    # 【V-F3】尊重节点的 checkall 标志：NR 默认 checkall=true（多输出，逐条判定）；
    # 显式 checkall=false 时命中即停（与真实 NR 语义对齐，避免对显式单命中流过度激活）。
    # 默认(缺省/"true")行为与旧实现一致，零回归。
    checkall = str(node.get("checkall", True)).lower() != "false"
    for i, rule in enumerate(rules):
        t = rule.get("t")
        # ── G3（报告 A30 闸门侧）：编译器已判「恒假/不可达」的分支，闸门不得保守视为命中 ──
        # 旧行为：`分支: 状态.光照 < night_start` 引用未声明字段 → 本地无法求值 →
        # 一律「保守视为命中」→ passed=true，与编译器 R31「条件恒假、动作永不执行」
        # 的结论**直接矛盾**，把恒假分支 bug 掩盖成闸门放行。
        # 新行为：尊重编译器静态判定——undefined 字段在运行态 JSONata 求值即 undefined，
        # NR switch 该规则本就不命中，闸门必须如实反映（有 else 则走 else）。
        # 只有「编译器未判恒假、仅运行期不可本地求值」才保留保守命中 + warn。
        if t not in ("else", "otherwise") and i in node_dead:
            toks = "、".join(node_dead[i])
            if report is not None:
                report.setdefault("dead", []).append(
                    {"node_id": node.get("id"), "node_name": node.get("name"),
                     "rule_index": i, "undefined_fields": list(node_dead[i]),
                     "expr": rule.get("v", "")})
            if warnings is not None:
                warnings.append(
                    f"switch「{nlabel}」第 {i + 1} 条分支引用未定义字段「{toks}」"
                    f"（编译器 R31 已判条件恒假）：闸门按【不命中】处理，其 THEN 体动作"
                    f"不在重放/断言范围；依赖该分支的期望项记为 N/A（不算通过）。"
                    f"请修正 取值:/变量: 声明与分支引用的字段名。")
            continue
        if t == "eq":
            if _vg_val_eq(val, rule.get("v"), rule.get("vt")):
                taken.append(i)
                matched = True
                if not checkall:
                    break
        elif t == "neq":
            if not _vg_val_eq(val, rule.get("v"), rule.get("vt")):
                taken.append(i)
                matched = True
                if not checkall:
                    break
        elif t in ("jsonata", "jsonata_exp") or (
                t not in ("else", "otherwise") and rule.get("vt") == "jsonata"):
            # A15 关键：NR switch 的 JSONata 规则类型是 **jsonata_exp**（编译器亦按此产出，
            # 见 dsl_engine._parse_switch_rule / _emit_switch）。旧代码只判 t == "jsonata"，
            # 对真实产物**永不命中** → 有 else 体时恒走 else（断言反向后置条件），
            # 无 else 体时一个分支都不走 → 重放归零（0 意图 → 闸门 skip → 假过）。
            # 这里同时兜住 vt=="jsonata" 的变体写法，并显式排除 else/otherwise
            # （编译器给 else 规则也带 vt="jsonata"，不可当条件求值）。
            matched_rule, known = _vg_eval_jsonata_expr(rule.get("v", ""), msg)
            if known:
                if matched_rule:
                    taken.append(i)
                    matched = True
                    if not checkall:
                        break
            else:
                # 无法本地求值的复杂 jsonata：保守视为命中（走 THEN 体），
                # 由调用方经 warnings 记录，避免误杀结构正确的 DSL。
                taken.append(i)
                matched = True
                if not checkall:
                    break
                if report is not None:
                    report.setdefault("conservative", []).append(
                        {"node_id": node.get("id"), "rule_index": i,
                         "expr": rule.get("v", "")})
                if warnings is not None:
                    warnings.append(
                        f"switch 规则含无法本地求值的 JSONata「{rule.get('v','')}」，"
                        f"保守视为命中（未按逻辑校验）")
        elif t not in ("else", "otherwise"):
            # 【W3】类型化规则（lt/lte/gt/gte/btwn/cont/regex/true/false/null/
            # nnull/empty/nempty/istype/hask 等）：闸门不支持 → 无法求值 → 记 unevaluable。
            # 不视为命中（避免把「未知规则」当 True 放行），由 G2 重放归零检测拦下。
            # （else/otherwise 不在此处理，留给函数末尾的「无命中则走 else」逻辑。）
            _mark_unevaluable(i, f"不支持的 switch 规则类型 `{t}`")
    if rules and rules[-1].get("t") in ("else", "otherwise") and not matched:
        taken.append(len(rules) - 1)
    return taken

def _vg_parse_hhmm(s):
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None

def _vg_to_minutes(vt):
    """虚拟时间 → 当天分钟数（支持 datetime / epoch / iso）。"""
    import datetime as _dt
    if isinstance(vt, _dt.datetime):
        return vt.hour * 60 + vt.minute
    if isinstance(vt, (int, float)):
        d = _dt.datetime.fromtimestamp(vt, tz=_dt.timezone.utc)
        return d.hour * 60 + d.minute
    if isinstance(vt, str):
        try:
            d = _dt.datetime.fromisoformat(vt.replace("Z", "+00:00"))
            return d.hour * 60 + d.minute
        except Exception:
            return None
    return None

def _vg_eval_time_range(node, virtual_time):
    if virtual_time is None:
        return True  # 未给定虚拟时间 → 默认窗口内（兼容旧单步）
    s = _vg_parse_hhmm(node.get("startTime"))
    e = _vg_parse_hhmm(node.get("endTime"))
    if s is None or e is None:
        return True
    tod = _vg_to_minutes(virtual_time)
    if tod is None:
        return True
    return s <= tod <= e

def _vg_apply_change(node, msg):
    """change 节点把变量写进 msg（供下游 switch 读取）。"""
    rules = node.get("rules") or []
    m = dict(msg) if isinstance(msg, dict) else {"payload": msg}
    for r in rules:
        if r.get("t") != "set":
            continue
        p = r.get("p") or "payload"
        to = r.get("to")
        tot = r.get("tot")
        if tot == "json":
            try:
                val = json.loads(to)
            except Exception:
                val = to
        elif tot == "num":
            try:
                val = float(to)
            except Exception:
                val = to
        elif tot == "bool":
            val = str(to).lower() == "true"
        elif tot == "msg":
            # O1（2026-08-29，WB93）：tot="msg" 表示 to 是 msg 路径引用（真实 NR 语义），
            # 旧代码落到 else 当字面量 → 绑定节点把 msg.亮度 写成字符串 "payload.state"。
            # 必须按点路径解析（如 payload.state），取不到值时与 NR 对齐置 None。
            val = _vg_resolve_path(m, to)
        else:
            val = to
        _vg_set_path(m, p, val)
    return m

def _vg_is_external_call(node_type) -> bool:
    """是否为「外部调用」节点：link out 或子流程实例。

    A12 关键：NR 5.x 的子流程**实例** type 是 `subflow:<subflow_id>`（带前缀），
    旧代码只判 `t in ("link out","subflow")`，对真实编译产物**永不命中** →
    external_calls 恒空 → 「期待调用某子流程」根本无从验证。
    """
    t = node_type or ""
    return t in ("link out", "subflow") or t.startswith("subflow:")


def _ha_node_call(nd) -> tuple:
    """从 api-call-service 节点解析 (domain, service, targets, data)。

    必须兼容两种写法，否则白箱路径重放不到任何意图：
      - 编译产物：domain/service/entityId 三字段；
      - agent 手写 / NR HA 2.x：action="light.turn_on" + data.entity_id 或 entities.entity。
    """
    raw = nd.get("data")
    try:
        if isinstance(raw, str):
            data = json.loads(raw or "{}")
        elif isinstance(raw, dict):
            data = dict(raw)
        else:
            data = {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    domain = nd.get("domain") or ""
    service = nd.get("service") or ""
    if not (domain and service):
        action = nd.get("action") or ""
        if isinstance(action, str) and "." in action:
            domain, service = action.split(".", 1)
    targets = nd.get("entityId") or []
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    if not targets:
        ent = (nd.get("entities") or {}).get("entity") or []
        if isinstance(ent, list):
            targets = [e for e in ent if e]
    if not targets:
        eid = data.get("entity_id") or data.get("entityId")
        if isinstance(eid, str):
            targets = [t.strip() for t in eid.split(",") if t.strip()]
        elif isinstance(eid, list):
            targets = [e for e in eid if e]
    return domain, service, list(targets), data


def _expected_state_for(domain, service, data):
    """按 vhass 的服务建模推导「调用后该实体应有的 state」。

    A18 之二：旧实现只认 turn_on/turn_off，导致 climate.set_hvac_mode /
    fan.set_percentage 之类**一个期望都提不出来** → 闸被 skip → 顶层假 pass。
    这里直接复用 vhass 的建模表作为唯一事实源，能推就推、推不出就说清为什么。

    返回 (expected_state | None, unverifiable_reason | None)。
    """
    from . import vhass as _vh
    key = (domain, service)
    if service in _vh._ON_OFF:
        return _vh._ON_OFF[service], None
    if service in _vh._COVER:
        return _vh._COVER[service], None
    if service in _vh._LOCK:
        return _vh._LOCK[service], None
    if key in _vh._FIXED_STATE:
        return _vh._FIXED_STATE[key], None
    if key in _vh._STATE_FROM_DATA:
        dkey = _vh._STATE_FROM_DATA[key]
        val = data.get(dkey)
        if val is not None:
            return str(val), None
        return None, f"{domain}.{service} 缺少参数 {dkey}，无法推导后置状态"
    if key in _vh._ATTR_FROM_DATA:
        return None, f"{domain}.{service} 只改属性不改 state，无法用 state 断言"
    if service == "toggle":
        return None, f"{domain}.toggle 终态取决于初始态，无法静态推导"
    return None, f"{domain}.{service} 未被 vhass 建模，后置状态无法验证"


def _auto_expected_from_nodes(nodes) -> tuple:
    """从 flow 的 api-call-service 节点自动推导 (expected 列表, 不可验证原因列表)。"""
    expected, unverifiable = [], []
    for n in nodes or []:
        if n.get("type") != "api-call-service":
            continue
        domain, service, targets, data = _ha_node_call(n)
        if not (domain and service):
            unverifiable.append(f"节点 {n.get('id')} 无法解析 domain.service")
            continue
        if not targets:
            unverifiable.append(f"{domain}.{service} 未指定 entity_id，无法断言")
            continue
        state, why = _expected_state_for(domain, service, data)
        if state is None:
            unverifiable.append(why)
            continue
        for t in targets:
            item = {"entity_id": t, "state": state}
            if item not in expected:
                expected.append(item)
    return expected, unverifiable


def _sub_name_norm(s) -> str:
    """子流程名归一化：去空白/下划线/连字符/箭头装饰，转小写。

    编译产物里 link out 节点名是 `→ bark_push`、子流程实例名是 `bark_push`，
    而 expected 里人写的是 `bark_push`，需要能对上。
    """
    return re.sub(r"[\s_\-→>]+", "", str(s or "")).lower()


def _sub_name_match(want, actual) -> bool:
    """期望的子流程名是否命中某个实际外部调用名（归一化后相等或被包含）。"""
    w, a = _sub_name_norm(want), _sub_name_norm(actual)
    if not w or not a:
        return False
    return w == a or w in a


def _replay_zero_policy() -> str:
    """「重放归零」处置策略（G2 / 报告 A15）。

    fail_closed（默认）：闸门无法本地判定条件（unevaluable JSONata）或分支被判恒假，
      导致本步 **0 个 HA 意图 + 0 个外部调用** 被重放时，不得报「验证通过」——
      0 重放意味着**什么都没验证**，静默 pass 就是假过。
    warn_only：只给显式告警、保留放行（可用性优先，env 逃生门）。

    **决议（2026-08-15 · c4_replay_semantics 终裁）**：按 `fail_closed` 定稿关闭。
    证据见 TEST_RESULT_001 §1.5——fail_closed 阻止「0 重放 + 断言绿」的假绿变 pass，
    `warn_only` 翻转面极窄（1/6）且 `fully_verified` 恒为 false、verdict 降级为
    「未充分验证」而非「放行」，可作 staging 调试期逃生门。代码已留 hook：仅调本函数
    默认值或 env `AUTOFLOW_REPLAY_ZERO_POLICY`，不动闸门主体。
    """
    v = (os.environ.get("AUTOFLOW_REPLAY_ZERO_POLICY") or "").strip().lower()
    return v if v in ("fail_closed", "warn_only") else "fail_closed"


def _vg_dead_switch_rules(flow):
    """从编译产物静态提取「恒假分支」索引：`{switch_id: {规则下标: [未定义字段]}}`。

    直接复用编译器 lint 的 R31 判定（flow_linter.collect_undefined_field_refs），
    保证**闸门结论与编译器结论一致**（G3）。取不到就返回空 dict（fail-open，
    退回旧的保守命中行为，不因 linter 异常把正常 flow 拦死）。
    """
    try:
        from .flow_linter import collect_undefined_field_refs
        return collect_undefined_field_refs(flow.get("nodes", []) or []) or {}
    except Exception:
        return {}


def _vg_dead_branch_reach(flow, dead_rules):
    """恒假分支下游可达的「本应执行却永不执行」的动作面。

    返回 (entity_ids, subflow_names)：供闸门把依赖这些动作的 expected 标 N/A，
    而不是笼统报「状态不对」——让 agent 一眼看出是分支写错、不是设备没响应。
    """
    ents, subs = set(), set()
    if not dead_rules:
        return ents, subs
    nodes = {n["id"]: n for n in flow.get("nodes", []) if n.get("id")}
    wires = {}
    for n in flow.get("nodes", []):
        w = n.get("wires") or []
        wires[n.get("id")] = [list(o) if isinstance(o, (list, tuple)) else [o] for o in w]

    def _walk(nid, seen):
        if nid in seen:
            return
        seen.add(nid)
        nd = nodes.get(nid)
        if nd is None:
            return
        if nd.get("type") == "api-call-service":
            try:
                _d, _s, targets, _data = _ha_node_call(nd)
                ents.update(t for t in targets if t)
            except Exception:
                pass
        elif _vg_is_external_call(nd.get("type")):
            subs.add(nd.get("name") or nd.get("type") or "subflow")
        for outs in wires.get(nid, []) or []:
            for tgt in outs:
                _walk(tgt, seen)

    for sid, rules in dead_rules.items():
        ow = wires.get(sid, []) or []
        for i in rules:
            for tgt in (ow[i] if i < len(ow) else []):
                _walk(tgt, set())
    return ents, subs


def _vg_evaluate_active_intents(flow, world, virtual_time=None, warnings=None,
                                dead_rules=None, report=None):
    """分支感知：返回当前世界态下应执行的 api-call-service 节点 id 集合。

    从每个触发的触发器出发，沿 wires 传播 msg；遇 switch 评估规则选定输出分支、
    遇 api-current-state 条件门控按世界态决定通行/走否则、遇 time-range-switch 按虚拟时间
    决定窗口内/外。最终 api-call-service 节点若从触发源经「通行路径」可达即视为激活。

    dead_rules 缺省自动从 flow 静态推导（G3），保证任何调用方都不会退回
    「undefined 字段分支被保守视为命中」的假过行为。
    """
    if dead_rules is None:
        dead_rules = _vg_dead_switch_rules(flow)
    nodes = {n["id"]: n for n in flow.get("nodes", [])}
    out_wires = {}
    for n in flow.get("nodes", []):
        w = n.get("wires") or []
        out_wires[n["id"]] = [list(o) if isinstance(o, (list, tuple)) else [o] for o in w]
    active = set()

    def _msg_from_trigger(node):
        if node["type"] == "inject":
            msg = {}
            for pr in node.get("props") or []:
                p = pr.get("p")
                if p and p != "payload":
                    msg[p] = pr.get("v")
            p = node.get("payload")
            pt = node.get("payloadType")
            try:
                msg["payload"] = json.loads(p) if pt == "json" and isinstance(p, str) else p
            except Exception:
                msg["payload"] = p
            return msg
        ent = (node.get("entities") or {}).get("entity", [None])[0]
        return {"payload": world(ent)}

    def _fires(node):
        if node["type"] == "inject":
            return True
        if node["type"] == "server-state-changed":
            ifs = node.get("ifState")
            if not ifs:
                return True
            ent = (node.get("entities") or {}).get("entity", [None])[0]
            return world(ent) == ifs
        return False

    def _trace(nid, msg, seen=None):
        if seen is None:
            seen = set()
        if nid in seen:
            return
        seen.add(nid)
        node = nodes.get(nid)
        if node is None:
            return
        t = node["type"]
        # 【W1 修复】节点 wires 可能为 []（白箱手写/非规范形状），out_wires[nid] 即存成
        # 空列表；若用 .get(nid, [[]]) 取默认则因 key 已存在返回 []，下方 outs[0]
        # 下标越界 IndexError → 被 verify_flow try/except 吞 → 闸 ran=false → fail-open
        # 降级 warn（幽灵实体反而拿到放行）。统一兜底成单空出边 [[]]（与 NR「1 输出口
        # 未连线」语义等价），任何 outs[0] 访问都安全、且对规范流零行为差异。
        outs = out_wires.get(nid) or [[]]
        if t == "api-call-service":
            active.add(nid)
            # 继续向下游传播：命中分支上的外部调用(link out/subflow)需标为可达
            for tgt in outs[0]:
                _trace(tgt, msg, seen)
            return
        if _vg_is_external_call(t):
            # 外部调用（子流程/link）同样按分支感知标记可达，供闸门只记录命中分支的调用
            active.add(nid)
            # 子流程实例可有下游（调用完继续往下走），沿 out0 继续传播
            for tgt in (outs[0] if outs else []):
                _trace(tgt, msg, seen)
            return
        if t == "switch":
            for oi in _vg_eval_switch(node, msg, warnings, dead_rules, report):
                for tgt in (outs[oi] if oi < len(outs) else []):
                    _trace(tgt, msg)
            return
        if t == "api-current-state":
            if node.get("halt_if") in (None, ""):
                # 读值节点：按 outputProperties **声明顺序**回放
                # （NR 对同一 msg 逐条 setMessageProperty，顺序即执行序）。
                m = dict(msg)
                _ent_state = world(node.get("entityId") or node.get("entity_id"))
                for op in (node.get("outputProperties") or []):
                    if op.get("propertyType") != "msg":
                        continue
                    prop = op.get("property") or "payload"
                    vtp = op.get("valueType")
                    if vtp == "entityState":
                        if not _vg_set_path(m, prop, _ent_state) and warnings is not None:
                            warnings.append(
                                f"取值节点写 msg.{prop} 被静默丢弃（中间路径不是对象），"
                                f"下游分支读不到该字段")
                    elif vtp == "jsonata" and _VG_PAYLOAD_OBJ_RE.match(str(op.get("value") or "")):
                        # A15-b：这条是编译器插的「payload 容器归一」。旧重放器只认
                        # entityState、直接跳过它 → 上游 inject 的标量 payload 没被重置成 {}
                        # → 紧随其后的 payload.<field> 写入静默失败 → 分支变量恒 undefined
                        # → JSONata 判定不出结果。必须一并回放才能还原真机行为。
                        if not isinstance(_vg_resolve_path(m, prop), dict):
                            _vg_set_path(m, prop, {})
                msg = m
                for tgt in outs[0]:
                    _trace(tgt, msg)
            else:
                # 条件门控：世界态满足 halt_if → 主链(out0)通行；否则走 out1(否则体)
                cond_met = (world(node.get("entityId") or node.get("entity_id")) == node.get("halt_if"))
                if cond_met:
                    for tgt in outs[0]:
                        _trace(tgt, msg)
                elif len(outs) > 1:
                    for tgt in outs[1]:
                        _trace(tgt, msg)
            return
        if t == "time-range-switch":
            oi = 0 if _vg_eval_time_range(node, virtual_time) else 1
            for tgt in (outs[oi] if oi < len(outs) else []):
                _trace(tgt, msg)
            return
        # 透传节点（change/debug/comment/link out/subflow/http/delay/api-get-history）
        msg = _vg_apply_change(node, msg)
        for tgt in outs[0]:
            _trace(tgt, msg)

    for trig in flow.get("nodes", []):
        if trig["type"] in ("inject", "server-state-changed") and _fires(trig):
            _trace(trig["id"], _msg_from_trigger(trig))
    return active

# apply 闭环 B 段胶水：状态归一白名单（fail-closed，写路径拒绝未知状态）
_ON_STATES = ("on", "true", "1", "open", "yes", "home", "playing", "unlocked")
_OFF_STATES = ("off", "false", "0", "closed", "no", "not_home", "idle", "standby", "locked")
# 设备掉线/未知：明确拒绝，绝不静默 turn_off
_UNCERTAIN_STATES = ("unavailable", "unknown", "none", "null", "")

class Gateway:
    def __init__(self, config=None, ha_layer=None, nr_layer=None):
        self.cfg = config or get_config()
        self.state = SharedState(self.cfg)
        self.defense = DefenseLayer(self.cfg)
        self.confirm = ConfirmationGate(self.cfg)
        self.plan = PlanStore(self.cfg)
        self.commands = CommandStore(self.cfg)
        self.decisions = DecisionStore(self.cfg)
        self.tasks = TaskStore(self.cfg)
        # 注入注册表 store，使 DSL 编译期的 get_subflow 能查 subflow_registry（#575/#577）：
        # 用户从 NR 自省导入的 imported 子流程经此进入编译器，与预置 SUBFLOWS 统一查表路径。
        from . import subflows as sf
        sf.set_registry_store(self.tasks)
        # 启动幂等 seed 预置子流程（bark_push + 4 history）进注册表（#578），供 WebUI 列出/查询前置参数。
        sf.seed_managed_subflows(self.tasks)
        self.ha = ha_layer or HALayer(self.cfg)
        self.nr = nr_layer or NRLayer(self.cfg)
        # debug 回读桥：后台线程旁路订阅 NR5.0.1 原生 ws://<nr>/comms debug 事件流，
        # 落本地内存环形缓冲；绝不往 flow 插采集节点（两条热路径都不碰，#644）。
        # 共享进程内唯一 bridge 单例（见文件顶部 _get_debug_bridge 注释，#649 修复）
        self.debug_bridge = _get_debug_bridge(
            nr_client=self.nr.client,
            nr_url=self.cfg.nr_url,
            enabled=self.cfg.debug_bridge_enabled,
        )
        self.debug_bridge.start()
        self._start_watchdog()

    @property
    def _telemetry_log(self) -> str:
        """遥测日志路径：data/telemetry.jsonl（append-only）。"""
        return os.path.join(self.cfg.data_dir, "telemetry.jsonl")

    # ───────────── 读：设备发现 ─────────────
    def discover(self, keyword: Optional[str] = None, domain: Optional[str] = None,
                 area: Optional[str] = None, limit: int = 30, offset: int = 0,
                 compact: bool = True, with_affordance: bool = True) -> Dict[str, Any]:
        """从 device_catalog 快速发现（不触真实 HA）。无 catalog 时提示刷新。
        area 接受中文房间词/别名/区域名，经 _resolve_area 解析。
        - compact=True（默认）：每条只回 entity_id/friendly_name/state/domain/area，体积压到 1/5，
          专治白箱列设备撑爆 64KB 上下文的问题（B2）。
        - 始终透明回报 matched_count / returned / truncated / next_offset（B3），杜绝静默截断。
        - with_affordance=True：每条附加该域的『状态契约+服务词汇』（B5c），写 flow 立刻知道能调什么。
        """
        cat = self.state.get_device_catalog()
        ents = cat.get("entities", {})
        if not ents:
            return {"entities": [], "hint": "device_catalog 为空，请先 refresh_catalog()。",
                    "returned": 0, "matched_count": 0, "truncated": False,
                    "offset": offset, "next_offset": None, "total": 0,
                    "freshness": "", "area_resolved": None, "area_hint": None}
        area_filter, area_hint = self._resolve_area(area)
        # A29：区域过滤透明化——area 传入却解析失败(area_filter=None)时实际未按
        # 区域过滤、返回全量；显式告警，避免 agent 被 area_resolved:null 误导以为过滤生效。
        area_warning = area_hint if (area and area_filter is None) else None
        area_index = self.state.get_area_index()
        # ── 先全量过滤出命中集合，再分页（这样 matched_count 才准确，B3）──
        matched = []
        for eid, meta in ents.items():
            if meta.get("gone"):
                continue
            if domain and meta.get("domain") != domain:
                continue
            if area_filter and not self._area_match(meta, area_filter, area, area_index):
                continue
            if keyword:
                kw = keyword.lower()
                fn = (meta.get("friendly_name") or "").lower()
                if kw not in eid.lower() and kw not in fn and kw not in (meta.get("area") or "").lower():
                    continue
            matched.append((eid, meta))
        matched_count = len(matched)
        page = matched[offset:offset + limit]
        out = []
        for eid, meta in page:
            if compact:
                item = {
                    "entity_id": eid,
                    "friendly_name": meta.get("friendly_name"),
                    "state": meta.get("state"),
                    "domain": meta.get("domain"),
                    "area": meta.get("area") or "",
                }
            else:
                item = {"entity_id": eid, **meta}
            if with_affordance:
                aff = affordance_for(meta.get("domain"))
                if aff:
                    item["affordance"] = {"states": aff["states"], "services": aff["services"]}
            out.append(item)
        truncated = (offset + limit) < matched_count
        return {
            "entities": out,
            "returned": len(out),
            "matched_count": matched_count,
            "truncated": truncated,
            "offset": offset,
            "next_offset": (offset + limit) if truncated else None,
            "total": len(ents),
            "freshness": cat.get("freshness", ""),
            "area_resolved": area_filter,
            "area_hint": area_hint,
            "area_warning": area_warning,
        }

    def list_entities(self, domain: Optional[str] = None, area: Optional[str] = None,
                      keyword: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """【全屋实体目录·过滤浏览】按 domain/area/keyword 过滤返回实体目录（读 device_catalog，不触真实 HA）。

        与 autoflow_resolve_entity（自然语言设备名→候选 entity_id，写 DSL 前必调）互补：
        resolve_entity 是你已知大概设备名、想拿到唯一真实 entity_id；本工具是你想要「按条件浏览目录」，
        例如「书房有哪些 light？」「全屋所有 cover 各在什么状态？」。每个实体直接带 possible_states，
        告诉它能在哪些状态间切换，省去猜域/猜状态。

        - domain：按域过滤（light/switch/cover/climate/media_player/binary_sensor…），None=不过滤。
        - area：中文房间词/别名/区域名（书房/主卧室…），经 _resolve_area 解析；找不到自动忽略区域过滤。
        - keyword：模糊匹配 entity_id / friendly_name / area（不区分大小写）。
        - limit/offset：分页（默认 50/页，上限 200），防止全屋 2976 实体一次撑爆上下文。
        - 每个实体返回 {entity_id, friendly_name, domain, area, state(当前状态),
          possible_states(该域可能状态)}。
        - 始终透明回报 matched_count / returned / truncated / next_offset，杜绝静默截断。
        """
        try:
            limit = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        cat = self.state.get_device_catalog()
        ents = cat.get("entities", {})
        if not ents:
            return {"entities": [], "hint": "device_catalog 为空，请先 refresh_catalog()。",
                    "returned": 0, "matched_count": 0, "truncated": False,
                    "offset": offset, "next_offset": None, "total": 0,
                    "freshness": "", "area_resolved": None, "area_hint": None}
        area_filter, area_hint = self._resolve_area(area)
        # A29：同 discover——area 传入却解析失败(area_filter=None)时显式告警。
        area_warning = area_hint if (area and area_filter is None) else None
        area_index = self.state.get_area_index()
        matched = []
        for eid, meta in ents.items():
            if meta.get("gone"):
                continue
            if domain and meta.get("domain") != domain:
                continue
            if area_filter and not self._area_match(meta, area_filter, area, area_index):
                continue
            if keyword:
                kw = keyword.lower()
                if kw not in eid.lower() and kw not in (meta.get("friendly_name") or "").lower() \
                        and kw not in (meta.get("area") or "").lower():
                    continue
            matched.append((eid, meta))
        matched_count = len(matched)
        page = matched[offset:offset + limit]
        out = []
        for eid, meta in page:
            out.append({
                "entity_id": eid,
                "friendly_name": meta.get("friendly_name"),
                "domain": meta.get("domain"),
                "area": meta.get("area") or "",
                "state": meta.get("state"),
                "possible_states": self._possible_states(meta.get("domain")),
            })
        truncated = (offset + limit) < matched_count
        return {
            "entities": out,
            "returned": len(out),
            "matched_count": matched_count,
            "truncated": truncated,
            "offset": offset,
            "next_offset": (offset + limit) if truncated else None,
            "total": len(ents),
            "freshness": cat.get("freshness", ""),
            "area_resolved": area_filter,
            "area_hint": area_hint,
            "area_warning": area_warning,
        }

    # 设备级总览里挑代表名 / 关键可控实体状态用的域
    _KEY_DOMAINS = ("climate", "light", "switch", "fan", "cover", "media_player",
                    "humidifier", "water_heater", "lock", "binary_sensor")

    def room_summary(self, area: str, compact: bool = True, view: str = "devices",
                     device: Optional[str] = None, limit: int = 30, offset: int = 0) -> Dict[str, Any]:
        """某房间的『设备归组』视图（B4/B6）。两级投递，默认给小、想细再钻。

        渐进式投递设计（专治大房间把客户端响应撑爆的问题，见 2026-07-11 压测）：
        - view="devices"（默认，<5KB）：只回**设备级总览**——每个物理设备的
          device_id / 代表名 / entity_count / 覆盖 domains / 关键可控实体当前 state。
          任何客户端（含低上限的 deepseek++ 浏览器插件）一把装得下。
        - device=<id>（下钻）：回该设备全部实体（小体积），拿真实 entity_id 写 flow 用。
        - view="full"（旧行为，向后兼容）：回全部实体按设备归组（大体积，仅能力强的客户端用）。
        - limit/offset：设备总览分页（默认 30/页）。
        area 接受中文房间词/别名，经 _resolve_area 解析。
        """
        area_filter, area_hint = self._resolve_area(area)
        if not area_filter:
            return {"ok": False, "error": f"未识别房间: {area}", "area_hint": area_hint}
        cat = self.state.get_device_catalog()
        ents = cat.get("entities", {})
        area_index = self.state.get_area_index()
        devices: Dict[str, List[Dict]] = {}
        ungrouped: List[Dict] = []
        matched = 0
        for eid, meta in ents.items():
            if meta.get("gone"):
                continue
            if not self._area_match(meta, area_filter, area, area_index):
                continue
            matched += 1
            entry = {
                "entity_id": eid,
                "domain": meta.get("domain"),
                "friendly_name": meta.get("friendly_name"),
                "state": meta.get("state"),
            }
            dev = meta.get("device_id")
            if dev:
                devices.setdefault(dev, []).append(entry)
            else:
                ungrouped.append(entry)

        # ── 下钻：单设备全量实体（小体积）──
        if device:
            if device in devices:
                return {"ok": True, "area": area_filter, "device_id": device,
                        "entity_count": len(devices[device]),
                        "entities": devices[device],
                        "freshness": cat.get("freshness", "")}
            return {"ok": False, "error": f"该房间无此 device_id: {device}",
                    "known_devices": list(devices.keys())[:50]}

        # ── 设备级总览（默认，小体积）──
        if view in ("devices", "overview"):
            dev_list = []
            for d, elist in devices.items():
                domains = sorted({e.get("domain") for e in elist})
                rep = next((e for e in elist if e.get("domain") in self._KEY_DOMAINS), None)
                if rep is None:
                    rep = min(elist, key=lambda e: len(e.get("friendly_name") or ""))
                key_states = {e["entity_id"]: e.get("state")
                              for e in elist if e.get("domain") in self._KEY_DOMAINS}
                dev_list.append({
                    "device_id": d,
                    "name": rep.get("friendly_name"),
                    "entity_count": len(elist),
                    "domains": domains,
                    "key_states": key_states,
                })
            dev_list.sort(key=lambda x: (x["name"] or ""))
            total_dev = len(dev_list)
            page = dev_list[offset:offset + limit]
            truncated = (offset + limit) < total_dev
            ug = [{"entity_id": e["entity_id"], "friendly_name": e["friendly_name"],
                   "state": e.get("state")} for e in ungrouped]
            return {
                "ok": True, "area": area_filter, "area_hint": area_hint,
                "view": "devices",
                "device_count": total_dev, "ungrouped_count": len(ungrouped),
                "total_entities": matched,
                "devices": page,
                "truncated": truncated, "offset": offset,
                "next_offset": (offset + limit) if truncated else None,
                "ungrouped": ug,
                "freshness": cat.get("freshness", ""),
                "hint": "默认返回设备级总览(小体积)。下钻某设备全量实体: room_summary(area, device=<device_id>)。"
                        "要完整分类 markdown 一次拿: autoflow_export_room(area)。",
            }

        # ── 全量（旧行为，向后兼容）──
        return {
            "ok": True, "area": area_filter, "area_hint": area_hint, "view": "full",
            "device_count": len(devices), "ungrouped_count": len(ungrouped),
            "total_entities": matched,
            "devices": [{"device_id": d, "entity_count": len(v), "entities": v}
                        for d, v in devices.items()],
            "ungrouped": ungrouped,
            "freshness": cat.get("freshness", ""),
        }

    # 域 → 中文类别名（用于 export_room_markdown 表头）
    _DOMAIN_CN = {
        "sensor": "传感器", "switch": "开关", "event": "事件", "select": "选择器",
        "button": "按钮", "number": "数值", "device_tracker": "设备追踪(网络)",
        "binary_sensor": "二元传感器", "text": "文本输入", "notify": "通知",
        "light": "灯/照明", "climate": "空调/温控", "media_player": "媒体播放器",
        "water_heater": "热水器", "humidifier": "加湿器", "fan": "风扇",
        "cover": "窗帘/覆盖面", "lock": "锁",
    }

    def export_room_markdown(self, area: str, domain: Optional[str] = None,
                              limit: Optional[int] = None, offset: int = 0,
                              compact: Optional[bool] = None) -> Dict[str, Any]:
        """服务端一次性渲染某房间分类 markdown（消灭多轮翻页，速度最快路径）。

        遍历 device_catalog 一次，按 domain 分组生成含
        entity_id / friendly_name / 当前状态 / 可能状态 的分类表格。
        返回 {ok, area, domain, entity_count, markdown, size_bytes, freshness,
              domains, per_domain, truncated, next_offset, note}。

        - 适合『获取某房间全部设备清单并分类输出』任务：客户端一次调用拿成品落地成文件。
        - **分域下钻（弱客户端救命绳）**：传 domain='light' 等只渲染单域，
          每域 markdown < 7KB，任何 MCP 客户端都不会截断。
          响应里的 `domains`/`per_domain` 列出全部可下钻域，agent 据此逐域补齐。
        - limit/offset：单域内分页安全垫（单域仍过大时），truncated/next_offset 明示。
        - 可能状态取自 affordance_for(domain)（B5c 状态契约）。
        """
        area_filter, area_hint = self._resolve_area(area)
        if compact is None:
            compact = (domain is None)  # 全域导出默认紧凑索引(entity_id/friendly_name/domain)，防弱客户端截断
        if not area_filter:
            return {"ok": False, "error": f"未识别房间: {area}", "area_hint": area_hint}
        cat = self.state.get_device_catalog()
        ents = cat.get("entities", {})
        matched = []
        for eid, meta in ents.items():
            if meta.get("gone"):
                continue
            if domain and meta.get("domain") != domain:
                continue
            if not self._area_match(meta, area_filter, area):
                continue
            matched.append((eid, meta))
        # 统计全域分布（无论是否过滤，都告诉 agent 可下钻哪些域）
        all_matched = []
        for eid, meta in ents.items():
            if meta.get("gone"):
                continue
            if not self._area_match(meta, area_filter, area):
                continue
            all_matched.append((eid, meta))
        per_domain: Dict[str, int] = {}
        for _, m in all_matched:
            per_domain[m.get("domain")] = per_domain.get(m.get("domain"), 0) + 1
        domains = sorted(per_domain, key=lambda x: -per_domain[x])

        if not matched:
            md = (f"# {area} 设备清单\n\n"
                  f"> 数据来源：autoflow MCP · `autoflow_export_room`\n\n（无匹配实体）\n")
            return {"ok": True, "area": area_filter, "domain": domain,
                    "entity_count": 0, "markdown": md,
                    "size_bytes": len(md.encode("utf-8")),
                    "freshness": cat.get("freshness", ""),
                    "domains": domains, "per_domain": per_domain,
                    "truncated": False, "next_offset": None, "note": None}

        by_domain: Dict[str, List] = {}
        for eid, meta in matched:
            by_domain.setdefault(meta.get("domain"), []).append((eid, meta))

        # 单域内分页（安全垫）
        truncated = False
        next_offset = None
        if domain and limit:
            elist = by_domain.get(domain, [])
            total = len(elist)
            elist = elist[offset:offset + limit]
            by_domain[domain] = elist
            if offset + limit < total:
                truncated = True
                next_offset = offset + limit

        lines = []
        lines.append(f"# {area} 设备清单（Home Assistant）\n")
        lines.append(f"> 数据来源：autoflow MCP · `autoflow_export_room(area={area})`")
        if domain:
            lines.append(f"> 仅域：`{domain}`")
        if truncated:
            lines.append(f"> 本页显示第 {offset+1}-{offset+len(by_domain.get(domain, []))} 条（共 {per_domain.get(domain, 0)} 条，下页 offset={next_offset}）")
        lines.append(f"> 实体总数：**{len(matched)}** ｜ 分类数（domain）：**{len(by_domain)}**\n")
        # 概览表（指定 domain 时只列该域，缩小体积）
        overview_domains = [domain] if domain else domains
        lines.append("## 分类概览\n")
        lines.append("| 分类(domain) | 中文类别 | 实体数 | 可能状态 |")
        lines.append("|---|---|---|---|")
        for d in overview_domains:
            aff = affordance_for(d)
            states = aff["states"] if aff else "（见实体明细）"
            states_txt = "/".join(states) if isinstance(states, list) else str(states)
            lines.append(f"| {d} | {self._DOMAIN_CN.get(d, d)} | {per_domain[d]} | {states_txt} |")
        lines.append("")
        # 明细：每域一张表
        for d in sorted(by_domain, key=lambda x: -len(by_domain[x])):
            elist = by_domain[d]
            cn = self._DOMAIN_CN.get(d, d)
            lines.append(f"### {cn}（`{d}`，{len(elist)} 个实体）\n")
            if compact:
                lines.append("| entity_id | friendly_name | domain |")
                lines.append("|---|---|---|")
            else:
                lines.append("| entity_id | friendly_name | 当前状态 | 可能状态 |")
                lines.append("|---|---|---|---|")
            aff = affordance_for(d)
            states = aff["states"] if aff else ""
            states_txt = "/".join(states) if isinstance(states, list) else str(states)
            for eid, meta in elist:
                fn = (meta.get("friendly_name") or "").replace("|", "/")
                if compact:
                    lines.append(f"| `{eid}` | {fn} | {meta.get('domain')} |")
                else:
                    st = str(meta.get("state")) if meta.get("state") is not None else "—"
                    lines.append(f"| `{eid}` | {fn} | {st} | {states_txt} |")
            lines.append("")
        md = "\n".join(lines)
        rendered_count = sum(len(v) for v in by_domain.values())
        total_count = len(matched)
        note = None
        if not domain and len(md.encode("utf-8")) > 30000:
            note = ("响应较大，弱客户端可能截断。可用 domain= 参数逐域获取（如 "
                    "autoflow_export_room(area='主卧室', domain='light')），"
                    "每域 <7KB。可下钻域见 domains 字段。")
        return {"ok": True, "area": area_filter, "domain": domain,
                "entity_count": rendered_count, "total": total_count if (domain and limit) else None,
                "markdown": md,
                "size_bytes": len(md.encode("utf-8")),
                "freshness": cat.get("freshness", ""),
                "domains": domains, "per_domain": per_domain,
                "truncated": truncated, "next_offset": next_offset, "note": note}

    def export_room_markdown_all(self, area: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
        """服务端一次性拼装某房间全部分类 markdown 并落盘，只返回极小摘要。

        **弱客户端救命路径（根因修复）**：把『逐域拉取 → 模型上下文拼装』的
        ~130KB 上下文压成 **1 次调用 + <400B 摘要**。所有实体在服务端遍历拼装，
        模型上下文只看到 {path, total, per_domain}，不再被大响应撑爆——彻底规避
        deepseek++ 等弱客户端因上下文累加过大而 abort 续写（表现为
        "BodyStreamBuffer was aborted" / "empty agent continuation"）。

        - 先取该房间全域分布（domains/per_domain），再对每个域调用
          export_room_markdown(domain=d) 取单域 markdown（每域 <14KB，纯服务端内部，不进模型上下文）。
        - 概览表 + 各域明细表拼成完整 markdown，落盘到 out_dir（默认 <项目>/data/room_exports/<area>.md）。
        - 返回 {ok, area, path, total, entity_per_domain, domains, size_bytes, freshness, note}——
          调用方直接读 path 指向的 .md 交付，无需把大内容塞进对话上下文。
        - 纯读操作（只遍历 device_catalog + 写报告文件），黑箱/白箱身份皆可用。
        """
        base = self.export_room_markdown(area)  # 拿 domains/per_domain；markdown 可能被截断，此处无关
        if not base.get("ok"):
            return base
        area_filter = base["area"]
        domains = base["domains"]
        per_domain = base["per_domain"]
        total = sum(per_domain.values())

        chunks = []
        chunks.append(f"# {area} 设备清单（Home Assistant）\n")
        chunks.append(f"> 数据来源：autoflow MCP · `autoflow_export_room_file(area={area})`")
        chunks.append(f"> 实体总数：**{total}** ｜ 分类数（domain）：**{len(domains)}**\n")
        chunks.append("## 分类概览\n")
        chunks.append("| 分类(domain) | 中文类别 | 实体数 | 可能状态 |")
        chunks.append("|---|---|---|---|")
        for d in domains:
            aff = affordance_for(d)
            states = aff["states"] if aff else "（见实体明细）"
            states_txt = "/".join(states) if isinstance(states, list) else str(states)
            chunks.append(f"| {d} | {self._DOMAIN_CN.get(d, d)} | {per_domain[d]} | {states_txt} |")
        chunks.append("")
        # 逐域渲染（每域 <14KB，服务端内部，不进模型上下文）
        for d in domains:
            part = self.export_room_markdown(area, domain=d)
            if part.get("ok"):
                chunks.append(part.get("markdown", ""))
        md = "\n".join(chunks)

        # 落盘
        if out_dir:
            export_dir = out_dir
        else:
            data_dir = getattr(self.cfg, "data_dir", None)
            if data_dir:
                export_dir = os.path.join(data_dir, "room_exports")
            else:
                export_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "data", "room_exports")
        os.makedirs(export_dir, exist_ok=True)
        safe = re.sub(r"[^\w一-鿿-]", "_", area)
        path = os.path.join(export_dir, f"{safe}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        return {"ok": True, "area": area_filter, "path": path, "total": total,
                "entity_per_domain": per_domain, "domains": domains,
                "size_bytes": len(md.encode("utf-8")),
                "freshness": base.get("freshness", ""),
                "note": "已服务端拼装并落盘；直接读取 path 即可交付，勿把大内容塞进上下文。"}

    def list_areas(self) -> Dict[str, Any]:
        """返回 HA 实际区域（id+name）与中文房间别名表。供 agent 确认房间名。"""
        em = self.state.get_entity_mapping()
        areas = em.get("areas", {})
        aliases = em.get("room_aliases", {})
        return {
            "areas": [{"area_id": aid, "name": n} for aid, n in areas.items()],
            "room_aliases": aliases,
            "hint": "discover(area='客厅') 会自动解析；'全屋' 不过滤。",
        }

    def get_detail(self, entity_id: str) -> Dict[str, Any]:
        """Tier1 懒加载：返回某实体完整 attributes；未缓存时拉 HA 并缓存。"""
        cat = self.state.get_device_catalog()
        e = cat.get("entities", {}).get(entity_id)
        if e is None:
            return {"ok": False, "error": f"未知实体: {entity_id}（不在 device_catalog，请先 refresh）"}
        if e.get("detail_cached") and e.get("detail"):
            return {"ok": True, "cached": True, "entity_id": entity_id, "detail": e["detail"]}
        try:
            st = self.ha.get_state(entity_id)
        except Exception as ex:
            return {"ok": False, "error": f"无法读取 HA: {ex}"}
        detail = {
            "state": st.get("state"),
            "attributes": st.get("attributes", {}),
            "last_changed": st.get("last_changed"),
            "last_updated": st.get("last_updated"),
        }
        self.state.set_entity_detail(entity_id, detail)
        return {"ok": True, "cached": False, "entity_id": entity_id, "detail": detail}

    def _entity_attribute_names(self, entity_id: str) -> Optional[set]:
        """返回实体已知属性名集合（供 dsl_engine 编译期属性名校验，WB24 NEW-F1 defense-in-depth）。

        fail-open：实体未知 / 取属性失败 / 无属性信息 → 返回 None（调用方跳过校验，绝不阻断编译）。
        优先用 catalog 已缓存的 attributes（refresh 时部分实体写入），否则拉 HA 实时状态（带缓存）。
        """
        if not entity_id:
            return None
        eid = self._resolve_best(entity_id) or entity_id
        if not eid:
            return None
        cat = self.state.get_device_catalog().get("entities", {})
        if eid not in cat:
            return None
        e = cat[eid]
        cached = e.get("attributes")
        if cached:
            return set(cached.keys())
        try:
            d = self.get_detail(eid)
        except Exception:
            return None
        if not d.get("ok"):
            return None
        attrs = (d.get("detail") or {}).get("attributes", {})
        return set(attrs.keys()) if attrs else set()

    # ── 区域解析：中文房间词 → 区域名（agent 永不需接触 area_id 坑）──
    _DEFAULT_ROOM_TERMS = ["全屋", "客厅", "书房", "主卧室", "卧室", "房间", "起居室", "卫生间"]
    _ROOM_SYN = {
        "客厅": ["living room", "living_room", "livingroom", "起居室"],
        "书房": ["study", "office", "工作室"],
        "主卧室": ["master bedroom", "master_bedroom", "主卧"],
        "卧室": ["bedroom", "卧"],
        "卫生间": ["bathroom", "厕所", "洗手间", "wc"],
        "起居室": ["living room", "living_room", "livingroom", "family room", "family_room"],
        "房间": ["room"],
    }

    @staticmethod
    def _norm(s):
        return (s or "").strip().lower().replace(" ", "_").replace("　", "")

    @staticmethod
    def _area_match(meta: Dict[str, Any], area_filter: Optional[str], raw_area: Optional[str],
                   area_index: Optional[Dict[str, str]] = None) -> bool:
        """区域过滤：catalog 实体的 area 字段可能是中文区域名（如 主卧室/机房）或 area_id（如 shu_fang）。

        - 实体**有明确区域归属**时：严格信任区域，仅做 父子区域 子串匹配
          （如 主卧室 ⊂ 主卧室浴室），**不再用 friendly_name 兜底**——
          否则把设备挪到别的区域（如路由从主卧室移到机房）后，
          仍会被旧 friendly_name（"主卧室路由..."）误归原房间。
        - 若 area 存的是 area_id（HA 注册表常见坑），经区域索引还原为中文区域名再比对，
          避免「区域存 id → discover 返回空」（test_discover_area_fallback 覆盖）。
        - 实体**无区域**（area 空，常见于 device_tracker）：退化用原始房间词
          子串匹配 friendly_name，避免空返回（优雅降级）。
        """
        if not area_filter:
            return True
        a = meta.get("area") or ""
        if a:
            # 实体 area 与 area_filter 都可能是 area_id（如 zws/shu_fang）或中文区域名，
            # 取决于 catalog 来源约定。统一经区域索引还原为区域名再比对，兼容两种约定。
            if area_index:
                a = area_index.get(a, a)
                area_filter = area_index.get(area_filter, area_filter)
            # 有明确区域：信任区域，仅允许 父→子 区域子串匹配
            # （如 主卧室 ⊂ 主卧室浴室：query 主卧室 含主卧室浴室的实体）。
            # 注意：只用 area_filter in a（父在子中），**禁止** a in area_filter（子在父中）——
            # 否则 area="卧室"(通用卧室) 是 "主卧室" 的子串，会把所有通用卧室实体误算进主卧室。
            if a == area_filter or (area_filter and area_filter in a):
                return True
            return False
        # 无区域：退化匹配 friendly_name（原兜底逻辑）
        if raw_area and raw_area in (meta.get("friendly_name") or ""):
            return True
        return False

    def _resolve_area(self, area: Optional[str]):
        """返回 (area_name_or_None, hint)。'__all__'/全屋 → None（不过滤）。"""
        if not area:
            return None, None
        aliases = self.state.get_room_aliases()
        if area in aliases:
            target = aliases[area]
            if target in ("__all__", ""):
                return None, "全屋(不过滤)"
            return target, None
        area_names = list(self.state.get_area_index().values())
        if area in area_names:
            return area, None
        # 模糊包含
        for term, name in aliases.items():
            if name not in ("__all__", "") and (area in term or term in area or area in name or name in area):
                return name, None
        for name in area_names:
            if area in name or name in area:
                return name, None
        return None, f"未识别房间: {area}（已忽略区域过滤）"

    @staticmethod
    def _build_room_aliases(area_index: Dict[str, str]) -> Dict[str, str]:
        """把中文房间词映射到实际 HA 区域名（无则留空）。全屋→__all__。

        修复 B1：原实现用 `term in name or name in term` 双向子串匹配，
        当 `卧室` 排在 `主卧室` 前时，`卧室 in 主卧室` 命中 → 把 `主卧室` 错配成 `卧室`；
        且 `aliases[term] = matched` 是覆盖式，污染后续。

        修正：
        - 去掉反向 `name in term`（短词不应反向吞掉长词）；
        - 长词优先处理（sorted by len desc），配合 setdefault 保证更具体的匹配不被覆盖；
        - 仅当 `term == name` 或 `term in name`（术语是 HA 区域名的子串）才匹配。
        """
        aliases: Dict[str, str] = {"全屋": "__all__"}
        area_names = list(area_index.values())
        for name in area_names:
            aliases.setdefault(name, name)
        for term in sorted(Gateway._DEFAULT_ROOM_TERMS, key=len, reverse=True):
            if term == "全屋":
                continue
            matched = None
            for name in area_names:
                if term == name or term in name:   # 仅术语作为区域名的子串，禁止反向
                    matched = name
                    break
                n = Gateway._norm(name)
                syns = Gateway._ROOM_SYN.get(term, [])
                if any(Gateway._norm(s) == n or Gateway._norm(s) in n for s in syns):
                    matched = name
                    break
            if matched:
                aliases.setdefault(term, matched)   # 不覆盖已设的更长词
        return aliases

    def search_entities(self, keyword: str, domain: Optional[str] = None) -> List[Dict]:
        """语义桥：中文/别名 → entity_id，再带出 catalog 信息。"""
        resolved = self.state.resolve(keyword)
        results = []
        if resolved:
            meta = self.state.get_device_catalog().get("entities", {}).get(resolved)
            if meta:
                results.append({"entity_id": resolved, "matched_by": "mapping", **meta})
        # 同时模糊搜 catalog friendly_name / entity_id
        cat = self.state.get_device_catalog()
        kw = keyword.lower()
        for eid, meta in cat.get("entities", {}).items():
            if any(eid == r["entity_id"] for r in results):
                continue
            fn = (meta.get("friendly_name") or "").lower()
            if kw in eid.lower() or kw in fn:
                results.append({"entity_id": eid, "matched_by": "catalog", **meta})
        if domain:
            results = [r for r in results if r.get("domain") == domain]
        return results

    # 按域推导「可能状态」，供 agent 自行判断同步目标状态（无需先猜域）。
    _DOMAIN_POSSIBLE_STATES = {
        "light": ["on", "off"],
        "switch": ["on", "off"],
        "input_boolean": ["on", "off"],
        "fan": ["on", "off"],
        "cover": ["open", "closed"],
        "lock": ["locked", "unlocked"],
        "climate": ["heat", "cool", "off", "auto", "dry", "fan_only"],
        "media_player": ["playing", "paused", "idle", "off"],
        "vacuum": ["cleaning", "docked", "idle", "paused"],
        "binary_sensor": ["on", "off"],
    }

    def _possible_states(self, domain: Optional[str]) -> List[str]:
        return self._DOMAIN_POSSIBLE_STATES.get(domain or "", [])

    def resolve_entity(self, name: str, area: Optional[str] = None,
                       domain: Optional[str] = None, top_n: int = 5) -> Dict[str, Any]:
        '''自然语言设备名 → Top-N 候选 entity_id（受控选择，消灭 LLM 凭记忆写错 ID）。

        排序优先级（confidence）：
          high   : state.resolve 精确别名/映射命中，或 friendly_name 完全相等
          medium : friendly_name 子串匹配（越靠前、字符串越短越优）
          low    : entity_id 子串匹配
        area 为「优先提示」而非硬约束：优先返回该区域候选；若该区域无匹配则放宽到全局，
        避免区域名不完全一致（如设备未分配区域/区域别名差异）把正确设备整段排除。'''
        # D36 防御纵深：相同查询直接命中缓存，避免重复 O(目录) 模糊扫描。
        _ck = (name, area, domain)
        if _ck in _RESOLVE_ENTITY_CACHE:
            return _RESOLVE_ENTITY_CACHE[_ck]
        cat = self.state.get_device_catalog()
        ents = cat.get('entities', {})
        if not ents:
            return {'ok': False, 'error': 'device_catalog 为空，请先 refresh_catalog()。',
                    'query': name, 'candidates': []}
        area_filter, area_hint = self._resolve_area(area) if area else (None, None)
        # A29：area 传入却解析失败(area_filter=None)时显式告警（原本 area_hint 被丢弃）。
        area_warning = area_hint if (area and area_filter is None) else None
        area_index = self.state.get_area_index()
        q = (name or '').strip().lower()

        def _score(meta, eid):
            fn = (meta.get('friendly_name') or '').lower()
            eid_l = (eid or '').lower()
            if q and fn == q:
                return (0.0, 'friendly_name_exact', 'high')
            elif q and q in fn:
                idx = fn.find(q)
                return (1.0 + idx * 0.01 + len(fn) * 0.001, 'friendly_name_substr', 'medium')
            elif q and q in eid_l:
                return (5.0 + eid_l.find(q) * 0.01, 'entity_id_substr', 'low')
            return None

        def _collect(afilter):
            out = []
            seen = set()
            mapped = self.state.resolve(name)
            if mapped and mapped in ents:
                meta = ents[mapped]
                if (not afilter or self._area_match(meta, afilter, area, area_index)) and \
                   (not domain or meta.get('domain') == domain):
                    out.append((mapped, meta, 'mapping', 'high', 0.0))
                    seen.add(mapped)
            for eid, meta in ents.items():
                if eid in seen:
                    continue
                if domain and meta.get('domain') != domain:
                    continue
                if afilter and not self._area_match(meta, afilter, area, area_index):
                    continue
                s = _score(meta, eid)
                if s is not None:
                    out.append((eid, meta, s[1], s[2], s[0]))
                    seen.add(eid)
            return out

        cands = _collect(area_filter)
        if area_filter and not cands:
            # 区域名可能不完全一致（设备未分配区域/别名差异），放宽到全局，避免漏掉正确设备
            cands = _collect(None)
        conf_rank = {'high': 0, 'medium': 1, 'low': 2}
        cands.sort(key=lambda c: (conf_rank.get(c[3], 3), c[4]))
        top = cands[:top_n]
        out = []
        for eid, meta, mb, conf, _ in top:
            out.append({
                'entity_id': eid,
                'friendly_name': meta.get('friendly_name'),
                'domain': meta.get('domain'),
                'area': meta.get('area') or '',
                'state': meta.get('state'),
                'possible_states': self._possible_states(meta.get('domain')),
                'matched_by': mb,
                'confidence': conf,
            })
        result = {'ok': True, 'query': name, 'area': area_filter,
                   'area_warning': area_warning,
                   'domain': domain, 'count': len(out), 'candidates': out}
        # D36 防御纵深：写回缓存（容量上限，超出丢弃最旧条目防无限增长）。
        if len(_RESOLVE_ENTITY_CACHE) >= _RESOLVE_ENTITY_CACHE_MAX:
            _RESOLVE_ENTITY_CACHE.clear()
        _RESOLVE_ENTITY_CACHE[_ck] = result
        return result

    def _resolve_best(self, name: str) -> Optional[str]:
        """友好名/别名 → entity_id，仅在【无歧义】时返回；有歧义/无候选返回 None。

        设计原则（用户明确）：绝不静默猜域/猜实体。本方法只自动采纳"确定无疑"的解析：
          1) 本体已是目录内 entity_id → 返回自身；
          2) 精确 mapping/别名命中（state.resolve）→ 返回；
          3) 智能候选里【恰好 1 个】或【top 置信度=high】→ 返回该 entity_id（无歧义）；
          4) 否则（多候选歧义，如"书房吊灯"→select/switch/…，或 0 候选，如"书房光照度"）
             → None。交由 _check_entities_known 拦截，迫使 agent 显式调
             autoflow_resolve_entity 从候选中选择，避免"书房吊灯→select（错）"这类静默错配。
        """
        if not name:
            return None
        cat = self.state.get_device_catalog().get("entities", {})
        if name in cat:
            return name
        mapped = self.state.resolve(name)
        if mapped:
            return mapped
        # ── D36 修复（WB83 P1 DoS 根因）──
        # 已是 entity_id 形态（domain.object，无空格/中文/大写）却不在目录 → 确定性未知，
        # 直接返回 None，**绝不**调昂贵的 resolve_entity 全目录模糊扫描。模糊解析只服务于
        # 中文/友好名输入；对 entity_id 形态字符串做模糊扫描既无意义（编造型 ID 不会命中友好名）
        # 又会因『N 个未知实体 → N 次 O(目录) 扫描』造成 propose_dsl 串行阻塞 DoS。
        # 真实实体 ID 经上方 `name in cat` 精确命中（毫秒级）；此处只拦截编造/拼错的 ID，
        # 与『绝不静默猜域』纪律一致（agent 应先用 autoflow_resolve_entity 取精确 ID）。
        if _ENTITY_ID_SHAPE_RE.match(name):
            return None
        try:
            r = self.resolve_entity(name)
        except Exception:
            return None
        cands = r.get("candidates", []) if r.get("ok") else []
        if not cands:
            return None
        if len(cands) == 1 or cands[0].get("confidence") == "high":
            return cands[0].get("entity_id")
        return None

    def get_catalog(self) -> Dict[str, Any]:
        """返回共享态『摘要』——刻意不 dump 全量实体（防 3000 实体上下文爆炸）。
        明细请用 discover()/search()/list_areas()/get_detail() 按需获取。"""
        cat = self.state.get_device_catalog()
        ents = cat.get("entities", {})
        by_domain: Dict[str, int] = {}
        areas = set()
        for e in ents.values():
            d = e.get("domain")
            if d:
                by_domain[d] = by_domain.get(d, 0) + 1
            a = e.get("area")
            if a:
                areas.add(a)
        em = self.state.get_entity_mapping()
        return {
            "summary": {
                "total_entities": len(ents),
                "by_domain": by_domain,
                "areas": sorted(areas),
                "mapping_count": len(em.get("mappings", {})),
                "area_count": len(em.get("areas", {})),
                "freshness": cat.get("freshness", ""),
            },
            "note": "实体数量大，请用 discover()/search() 按过滤获取明细；勿依赖全量返回。",
            "mapping_sample": dict(list(em.get("mappings", {}).items())[:20]),
        }

    # ───────────── 写：场景提交 ─────────────
    def propose_scene_redirect(self) -> Dict[str, Any]:
        """autoflow_propose_scene 已废弃后的重定向教学响应（不执行旧 schema 逻辑）。"""
        return {
            "ok": False,
            "deprecated": True,
            "message": "autoflow_propose_scene 已废弃。请改用 autoflow_propose_dsl 提交语义 DSL 文本。",
            "how": {
                "tool": "autoflow_propose_dsl",
                "dsl_example": (
                    "场景: 书房入户播报\n"
                    "触发: <发现的传感器 entity_id> on\n"
                    "动作: light.turn_on(<发现的灯 entity_id>)\n"
                    "调用子流程: demo_notify(text=欢迎回家, room=书房, level=一般)"
                ),
                "expected_postconditions_json_example": '[{"entity_id":"<灯 entity_id>","state":"on"},{"subflow":"demo_notify"}]',
            },
            "help": "语法/子流程随时调 autoflow_dsl_help()。",
        }

    def propose_scene(self, intent_dict: Dict[str, Any]) -> Dict[str, Any]:
        """校验 + 预览（不进待确认、不落地）。"""
        intent = SceneIntent.from_dict(intent_dict)
        cat = self.state.get_device_catalog()
        errors = validate_intent(intent, catalog=(cat if cat.get("entities") else None))
        if errors:
            return {"ok": False, "errors": errors, "intent_id": intent.intent_id}
        flow = build_scene(intent, self.nr)
        return {
            "ok": True,
            "intent_id": intent.intent_id,
            "validation": "passed",
            "preview": flow,
            "expected_postconditions": [p.__dict__ for p in intent.expected_postconditions],
        }

    def compute_flow_diff(self, flow: Dict[str, Any],
                           existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """结构化「这次变更会动什么」摘要，供确认闸给人类看（不依赖真实 NR）。

        - node_count / node_types：编译产物的节点规模与构成
        - entities：会读/写的实体（api-call-service / server-state-changed / api-current-state）
        - services：会调用的 HA 服务（domain.service）
        - subflow_entries：会跳转到的子流程入口（link out → link-in id）
        - is_update + new/removed_node_ids：若给了 target_flow_id，与线上现有 flow 比对
        """
        nodes = flow.get("nodes", [])
        node_types: Dict[str, int] = {}
        entities: List[str] = []
        services: List[str] = []
        subflow_entries: List[str] = []

        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            if isinstance(v, (list, tuple, set)):
                return list(v)
            return [v]

        for n in nodes:
            t = n.get("type")
            node_types[t] = node_types.get(t, 0) + 1
            if t == "api-call-service":
                for e in _as_list(n.get("entityId")):
                    if e:
                        entities.append(e)
                a = n.get("action") or f"{n.get('domain', '')}.{n.get('service', '')}"
                if a:
                    services.append(a)
            elif t == "server-state-changed":
                ent = n.get("entities")
                ent_val = ent.get("entity") if isinstance(ent, dict) else ent
                entities.extend(_as_list(ent_val))
            elif t == "api-current-state":
                eid = n.get("entityId") or n.get("entity_id")
                if eid:
                    entities.append(eid)
            elif t == "link out":
                for l in _link_ids(n.get("links")):
                    if l:
                        subflow_entries.append(l)
        entities = sorted(set(entities))
        services = sorted(set(services))
        subflow_entries = sorted(set(subflow_entries))
        d: Dict[str, Any] = {
            "is_update": existing is not None,
            "node_count": len(nodes),
            "node_types": node_types,
            "entities": entities,
            "services": services,
            "subflow_entries": subflow_entries,
        }
        if existing is not None:
            ex_ids = {n.get("id") for n in existing.get("nodes", [])}
            new_ids = [n.get("id") for n in nodes if n.get("id") not in ex_ids]
            d["new_node_ids"] = new_ids
            d["removed_node_ids"] = [i for i in ex_ids
                                     if i not in {n.get("id") for n in nodes}]
        return d

    def get_confirm_detail(self, op_id: str) -> Optional[Dict[str, Any]]:
        """取单个待确认项的完整信息（含 diff / 预览），供确认闸渲染。"""
        op = self.confirm.get(op_id)
        if op is None:
            return None
        return op.to_dict()

    def commit_scene(self, intent_dict: Dict[str, Any]) -> Dict[str, Any]:
        """校验 + 构建 + 进确认闸（不立即执行）。"""
        intent = SceneIntent.from_dict(intent_dict)
        cat = self.state.get_device_catalog()
        errors = validate_intent(intent, catalog=(cat if cat.get("entities") else None))
        if errors:
            return {"ok": False, "errors": errors, "intent_id": intent.intent_id}

        target_flow_id = intent_dict.get("target_flow_id")
        flow = build_scene(intent, self.nr, flow_id=target_flow_id)
        operation = "update_flow" if target_flow_id else "create_flow"
        domains = [a.domain for a in intent.action]

        # 计算「变更影响面」diff：有 target_flow_id 时尝试拉线上现有 flow 做增量比对
        existing = None
        if target_flow_id:
            try:
                existing = self.nr.get_flow(target_flow_id)
            except Exception:
                existing = None
        diff = self.compute_flow_diff(flow, existing)

        # 防御层检查
        try:
            owner = None
            if target_flow_id:
                meta = self.state.get_flow_meta(target_flow_id)
                owner = meta.get("owner_agent") if meta else None
            self.defense.check_write(
                operation=operation,
                flow_id=target_flow_id or flow["id"],
                label=flow["label"],
                owner_agent=owner,
                acting_agent=intent.agent_id,
                flows_touched=1,
            )
        except DefenseError as e:
            return {"ok": False, "errors": [f"defense: {e}"], "intent_id": intent.intent_id}

        risk = self.defense.classify_operation_risk(operation, domains)
        # 爆炸半径 = 本操作会写到的 NR flow 数（单场景提交恒为 1）；
        # 真正的「波及面」由 diff.entities 给出（实体数），确认闸一并展示。
        blast_radius = 1
        payload = {"operation": operation, "flow_id": flow["id"], "flow": flow, "diff": diff}
        summary = (f"{operation}: '{intent.name}' 节点数={len(flow['nodes'])} "
                  f"域={domains} 实体={len(diff['entities'])}")
        op = PendingOp(operation, intent.agent_id, risk, summary, blast_radius, payload,
                       target=flow["id"], owner_flow=target_flow_id or flow["id"])
        self.confirm.request(op)

        # 写意图日志（单一真相源）
        self.state.append_intent(intent.to_dict())

        return {
            "ok": True,
            "pending_id": op.id,
            "risk": risk,
            "risk_level": op.risk_level,
            "blast_radius": blast_radius,
            "needs_approval": True,
            "intent_id": intent.intent_id,
            "preview": flow,
            "diff": diff,
            "expected_postconditions": [p.__dict__ for p in intent.expected_postconditions],
        }

    def commit_ha_service(self, domain: str, service: str, data: Dict, agent_id: str) -> Dict[str, Any]:
        """HA 写服务 → 确认闸。"""
        risk = self.defense.classify_domain_risk(domain)
        try:
            self.defense.check_write(operation="ha_call", flows_touched=1,
                                     acting_agent=agent_id, label=f"ha:{domain}.{service}")
        except DefenseError as e:
            return {"ok": False, "errors": [f"defense: {e}"]}
        ent = data.get("entity_id") if isinstance(data, dict) else None
        diff = {
            "is_update": False,
            "node_count": 0,
            "node_types": {},
            "entities": [ent] if ent else [],
            "services": [f"{domain}.{service}"],
            "subflow_entries": [],
        }
        payload = {"domain": domain, "service": service, "data": data, "diff": diff}
        op = PendingOp("ha_call", agent_id, risk, f"HA {domain}.{service} {data}", 1, payload)
        self.confirm.request(op)
        return {"ok": True, "pending_id": op.id, "risk": risk,
                "risk_level": op.risk_level, "blast_radius": 1,
                "diff": diff, "needs_approval": True}

    # ───────────── DSL 自助指南（agent 边写边查）─────────────
    def dsl_help(self) -> Dict[str, Any]:
        """返回 DSL 编写自助指南，供 agent 在创作时按需调用（autoflow_dsl_help）。
        内容动态读取子流程注册表，与代码同步、永不折旧。"""
        from . import subflows as sf
        subflow_docs = []
        for name, spec in sf.SUBFLOWS.items():
            subflow_docs.append({
                "name": name,
                "title": spec.title,
                "description": spec.description,
                "params": [f"{pname} ({p.type}, {'必填' if p.required else '可选/默认=' + repr(p.default)}): {p.desc}"
                           + (f" 枚举={p.enum}" if p.enum else "") for pname, p in spec.params.items()],
                "notes": spec.notes,
            })
        return {
            "workflow": [
                "1. 读本指南 grammar/examples 确认语法",
                "2. 对场景里每个设备名调 autoflow_resolve_entity(name='书房台灯') 拿真实 entity_id",
                "3. 需要现成套路可调 autoflow_list_templates() / autoflow_render_template()",
                "4. 用解析到的真实 entity_id 写 DSL（绝不编造、绝不把中文名透传进 DSL）",
                "5. autoflow_propose_dsl(dsl=<文本>, expected_postconditions_json='[...]') 提交",
                "6. autoflow_list_pending() 查看自己刚提交的提案状态",
            ],
            "resolve_entity_rules": [
                "★ 每个设备名都要先 autoflow_resolve_entity；DSL 里只允许出现返回的真实 entity_id。",
                "★ 返回多个候选(candidates)时，网关不会替你猜——你必须按 domain + friendly_name + area 自己选对的那个。",
                "例1『书房人体传感器』会返回同一物理设备的多个子实体：Motion(binary_sensor..._motion)、光照度(sensor..._illuminance)、电量、命令。做『有人移动』触发选 _motion；做『光照度<10』数值条件选 _illuminance——同一个名字对应两个不同实体，别混。",
                "例2『书房吊灯』属 switch 域(switch...._p_3_1) 不是 light 域——动作要用 switch.turn_on 而非 light.turn_on。『书房台灯』『书房牌匾灯泡』才是 light 域。",
                "例3 名字里带『开关/射灯/电脑』的多是 switch 干扰项(如 switch.lumi..._p_2_1 墙壁射灯、switch.d4f0eaeab731_switch 电脑开关)，别拿它冒充台灯/牌匾灯泡。",
                "拿不准就把候选列表读全、按域和用途挑；宁可多调一次 resolve_entity，也绝不猜。",
            ],
            "grammar": {
                "场景": "场景: <名称>",
                "触发": "触发: <entity_id> <状态值>  例：binary_sensor.x on / 触发: 状态 <entity_id> <状态值> / 触发: inject / 触发: 定时 每天 22:30。持久等待：在状态值后加『持续N分钟/小时/秒』，如 触发: binary_sensor.x on 持续5分钟（编译为 server-state-changed 的 for 等待，实体需持续该状态 N 分钟才触发）",
                "触发(多个=OR汇聚)": "连写多行『触发:』(动作之前、彼此之间不夹动作) = 任一触发都跑同一条动作链。见 examples.OR多触发",
                "条件": "条件: <jsonata 表达式>   (可选, 场景级前置条件)  —— 作用于整条流最外层的门控：不满足则整条流不执行，且**无『否则』分支**。若需要『条件成立/否则二选一』，请改用 分支: ... 否则: 语法（见下方『分支』『否则』条目），不要把 否则: 直接接在 条件: 后面。",
                "变量": "变量: <名>=<值>   (可选)",
                "动作": "动作: <domain>.<service>(<entity_id>, 参数k=值)   多参数逗号分隔",
                "取值(数值条件)": "取值: <entity_id> <字段名>   把实体当前 state 读进 msg.<字段名>，供下面『分支』做数值判断。数值比较要用 $number(字段名)，如 分支: $number(lux) < 10。见 examples.数值条件",
                "构建": "构建: <JSON对象 或 JSONata表达式>   把 msg.payload 设为请求体；动态值用反引号包裹，如 `payload`",
                "请求": "请求: <METHOD> <url> [<字面JSON body>] [K=V headers]   不带字面 body 时自动把上游『构建』的 msg.payload 作为请求体发送",
                "调用子流程": "调用子流程: <name>(k=值, ...)   如 demo_notify(text=..., room=书房, level=一般)。见 examples.TTS播报",
                "分支": "分支: <jsonata 条件>\n  动作: ...   条件成立才走缩进块。支持【嵌套】（分支体内再写 分支: 生成多级判断）与【多路】（连续写多个 分支: 或改用 否则如果: 接更多条件分支）",
                "否则": "否则:\n  动作: ...   紧跟『分支』或『时间段/查询』门之后；不动作可留『注释:』占位",
                "否则如果": "否则如果: <jsonata 条件>\n  动作: ...   紧跟『分支』之后再追加一个条件分支，实现 if/elif/else 多路判断（也可用连续 分支: 表达；嵌套判断在 分支/否则 体内再写 分支: 即可）",
                "时间段": "时间段: [工作日|周末|周一..周日] HH:MM-HH:MM\n  动作: ...   仅在时段(可加星期限定)内才继续执行缩进块。见 examples.工作日时间段",
                "延时": "延时: <数字> 秒",
                "并行": "并行:\n  动作: ...\n  动作: ...",
            },
            "constraints": [
                "禁用 Function 节点（网关会拒绝）",
                "实体一律用 autoflow_resolve_entity 返回的真实 entity_id；引用目录外或未解析的中文名，闸门直接判 FAIL",
                "绝不提交 HA 地址/端口/令牌，不直连 HA 或 NR",
                "提交场景请用 autoflow_propose_dsl；autoflow_propose_scene 已废弃，不要再调用",
                "只要任务含『如果/才/超过/当…则』等条件语义，DSL 必须用 分支:/否则: 包裹动作，禁止把动作裸写在分支外（裸写会被 R_branch_required 闸门判 lint_error 拦截）。",
            ],
            "subflows": subflow_docs,
            "example": (
                "场景: 书房入户播报\n"
                "触发: binary_sensor.0x00158d0001a2520d_motion on\n"
                "动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light, brightness_pct=80)\n"
                "调用子流程: demo_notify(text=欢迎回到书房，已为你打开台灯, room=书房, level=一般)"
            ),
            "examples": {
                "OR多触发": (
                    "# 人体移动 或 开门 → 都开台灯（多行触发=OR 汇聚，触发之间不夹动作）\n"
                    "场景: 书房有人或开门开台灯\n"
                    "触发: binary_sensor.0x00158d0001a2520d_motion on\n"
                    "触发: binary_sensor.e4aaec34e80f_contact on\n"
                    "动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)"
                ),
                "数值条件": (
                    "# 有人移动 且 光照度<10 → 开台灯，否则不动（取值+分支+否则；注意 motion 与 illuminance 是同名设备的两个子实体，各自 resolve）\n"
                    "场景: 书房暗且有人开台灯\n"
                    "触发: binary_sensor.0x00158d0001a2520d_motion on\n"
                    "取值: sensor.0x00158d0001a2520d_illuminance lux\n"
                    "分支: $number(lux) < 10\n"
                    "  动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)\n"
                    "否则:\n"
                    "  注释: 光线足够，不动作"
                ),
                "工作日时间段": (
                    "# 工作日 20:00-23:00 内 有人移动 → 开吊灯（吊灯是 switch 域，用 switch.turn_on）\n"
                    "场景: 工作日晚间有人开吊灯\n"
                    "触发: binary_sensor.0x00158d0001a2520d_motion on\n"
                    "时间段: 工作日 20:00-23:00\n"
                    "  动作: switch.turn_on(switch.lumi_cn_lumi_158d000239c546_aq1_on_p_3_1)"
                ),
                "TTS播报": (
                    "# 有人移动 → 开台灯 并 语音播报（跨域：light 动作 + demo_notify 子流程）\n"
                    "场景: 书房有人开灯并播报\n"
                    "触发: binary_sensor.0x00158d0001a2520d_motion on\n"
                    "动作: light.turn_on(light.philips_cn_249518489_rwread_s_2_light)\n"
                    "调用子流程: demo_notify(text=书房已有人，灯已打开, room=书房, level=一般)"
                ),
                "持久等待": (
                    "# 有人移动且持续 5 分钟 → 才开吊灯（避免人一晃就亮灯；持续时长支持 分钟/小时/秒）\n"
                    "场景: 书房有人驻留开吊灯\n"
                    "触发: binary_sensor.0x00158d0001a2520d_motion on 持续5分钟\n"
                    "动作: switch.turn_on(switch.lumi_cn_lumi_158d000239c546_aq1_on_p_3_1)\n"
                    "# 等价写法：持续2小时 / 持续30秒 均可，编译器折算为 server-state-changed 的 for 等待"
                ),
                "历史查询": (
                    "# 历史查询是【请求/响应】子流程（与天气/anysearch 的单向不同）：答案写回 msg.payload，下游用 提取/分支 读取。\n"
                    "# 共 4 个：history_state_at(某时刻值) / history_occurred(区间是否发生) / history_duration(处于某态时长) / history_aggregate(聚合电量/均值)。\n"
                    "场景: 检查昨晚空调设定温度\n"
                    "触发: inject\n"
                    "调用子流程: history_state_at(entity=climate.书房空调, at=昨晚23:12, attribute=temperature)\n"
                    "提取: 设定温度 = payload.value\n"
                    "分支: $number(设定温度) > 26\n"
                    "  动作: 调用子流程: demo_notify(text=昨晚空调设到了27度以上，偏高, room=书房, level=一般)\n"
                    "否则:\n"
                    "  注释: 温度正常，不提醒\n"
                    "# 另一例：门昨天11-12点开过没？→ occurred=true/false 在 msg.payload\n"
                    "# 调用子流程: history_occurred(entity=binary_sensor.书房门_contact, start=昨天11:00, end=昨天12:00, state=on)\n"
                    "# 提取: 开过 = payload.occurred   分支: $boolean(开过) ..."
                ),
            },
            "submit": (
                "autoflow_propose_dsl(\n"
                "  dsl=<上面的 DSL 文本>,\n"
                "  expected_postconditions_json='[{\"entity_id\":\"<灯 entity_id>\",\"state\":\"on\"},"
                "{\"subflow\":\"demo_notify\"}]'\n"
                ")"
            ),
            "note": "写作中任何不确定，随时再调 autoflow_dsl_help() 复查语法、examples 与子流程参数。",
        }

    # ───────────── DSL 场景提案 + staging 闸门（P3 MVP 闸门）─────────────
    def propose_dsl(self, dsl: str, agent_id: str,
                    expected_postconditions: Optional[List[Dict]] = None,
                    resolved_entities: Optional[List[str]] = None,
                    vhass_store=None, strict: bool = False,
                    require_e2e: bool = False) -> Dict[str, Any]:
        """经 DSL 提案场景：解析 → 静态校验 → 编译 → staging 闸门(vhass 重放断言) → 落提案(raw)。

        - dsl：agent 输出的语义 DSL 文本（见 docs/dsl_design.md）。
        - expected_postconditions：[{entity_id, state}]，闸门据此断言 vhass 后置状态。
        - vhass_store：测试/外部可注入内存 vhass；缺省从 staging catalog 镜像或 demo。
        - strict：True 时，lint 存在任何 error/warning 即阻断提案（默认 False，仅随回执透出）。
        - require_e2e：True 时，提案落档带 e2e 意图标记；人类在 WebUI 点「部署到 NR」时，
          deploy_proposal 会真正先跑一次 run_e2e_trace_raw 实机验证闸（verdict≠通过则拦截部署）。
          默认 False（沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E）。修复 iss_8d3cffaa96：此前该意图
          被 JSON-RPC 静默吞掉、且主部署路径 deploy_proposal 从不调 e2e 闸。
        - 返回 {ok, proposal_id, scene_name, gate:{passed,...}, flow}；编译失败 ok=False(stage=compile)。
        """
        from .dsl_engine import parse, compile, DSLError, set_entity_resolver, set_entity_attributes_resolver
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        _slog(_tid, "propose_dsl.start", agent_id=agent_id, dsl_len=len(dsl or ""))
        # ── B6 修复（WB25 全面自动化测试报告）──
        # dsl 为空/None/纯空白 → 前置友好错误，不再让 FastMCP 把 Pydantic 原始报错
        # （"Input should be a valid string"）直接冒给调用方。必须在一切解析之前。
        if not dsl or not str(dsl).strip():
            result = {"ok": False, "stage": "empty_dsl", "result_kind": "validation_error",
                      "error": "dsl 参数不能为空，请提供语义 DSL 文本（语法调 autoflow_dsl_help）。"}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.empty", agent_id=agent_id)
            return result
        # ── B4 修复（WB25 全面自动化测试报告）──
        # DSL 长度护栏：此前编译器对 DSL 长度无上限，超长 DSL 既能绕过提案大小约束、
        # 又会撑大快照文件名/存储。解析前快速失败，返回可解析的友好错误。
        _MAX_DSL_CHARS = 8192
        if len(dsl) > _MAX_DSL_CHARS:
            result = {"ok": False, "stage": "dsl_too_long", "result_kind": "validation_error",
                      "error": f"DSL 长度 {len(dsl)} 超过上限 {_MAX_DSL_CHARS} 字符，请精简场景描述后重试。"}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.dsl_too_long", agent_id=agent_id, dsl_len=len(dsl))
            return result
        expected_postconditions = expected_postconditions or []
        # 归一化 resolved_entities：支持 [{"entity_id":"..."}] 与 ["light.x"] 两种形态。
        # autoflow_resolve_entity 返回 dict 列表；MCP 层 json.loads 后为 list[dict]，
        # 而下方闸门需对其做 set() 白名单校验 —— dict 不可哈希会抛
        # "unhashable type: 'dict'"。此处统一剥成 entity_id 字符串，既修 bug 又兼容两种传参。
        if resolved_entities:
            _norm: List[str] = []
            for _r in resolved_entities:
                if isinstance(_r, dict):
                    _eid = _r.get("entity_id") or _r.get("id")
                    if isinstance(_eid, str):
                        _norm.append(_eid)
                elif isinstance(_r, str):
                    _norm.append(_r)
            resolved_entities = _norm
        # 友好名/别名 → entity_id（仅无歧义时自动解析）。注意：_entity_resolver 是 dsl_engine
        # 模块级全局，单进程单网关下各调用点都指向同一 self 的解析逻辑 → 并发（to_thread）互覆盖
        # 在行为上等价于 no-op，无交叉污染。若未来上 uvicorn 多 worker（Option B），须改为按调用显式传 resolver。
        set_entity_resolver(lambda t: self._resolve_best(t))
        set_entity_attributes_resolver(lambda eid: self._entity_attribute_names(eid))
        # 原生节点逃逸开关（Phase 4）：关闭时禁止提交含 原生节点: 的 DSL（随时可关，免重启）
        if _RAW_NODE_KW_RE.search(dsl or "") and not is_raw_node_escape_enabled(self.cfg):
            result = {"ok": False, "stage": "feature_disabled", "result_kind": "compile_error",
                      "error": "原生节点逃逸功能已关闭（由 WebUI 开关控制）。如需使用请在 WebUI 设置中开启『原生节点逃逸』。"}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.feature_disabled", agent_id=agent_id)
            return result
        try:
            scene = parse(dsl)
            flow = compile(scene)
        except DSLError as e:
            result = {"ok": False, "stage": "compile", "error": str(e),
                      "compile_error": _compile_error_envelope(e),
                      "result_kind": "compile_error"}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.error", sub_stage="compile",
                  elapsed=round(time.perf_counter() - _t0, 3), error=str(e))
            return result
        except Exception as e:  # pragma: no cover - 解析器外部异常兜底
            result = {"ok": False, "stage": "compile", "error": f"编译异常: {e}"}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.error", sub_stage="compile",
                  elapsed=round(time.perf_counter() - _t0, 3), error=f"编译异常: {e}")
            return result

        # 静态 Linter（A1）：对编译产物做反模式预检（非阻塞，随回执透出）
        lint_issues = lint_flow(flow)
        lint_summary = [{"rule": v.get("rule"), "level": v.get("level"), "message": v.get("message")}
                        for v in lint_issues if v.get("level") in ("error", "warning")]
        lint_error_count = sum(1 for v in lint_issues if v["level"] == "error")
        lint_warning_count = sum(1 for v in lint_issues if v["level"] == "warning")
        _slog(_tid, "propose_dsl.compiled", nodes=len(flow["nodes"]),
              elapsed=round(time.perf_counter() - _t0, 3),
              lint_errors=lint_error_count, lint_warnings=lint_warning_count)

        # R_branch_required 内容触发（修复 iss_ebfe742222）：
        # DSL 含条件语义但编译产物无分支/条件门节点 → 动作将无条件执行，硬拦（无论 strict 与否）。
        # 这填补了 verify_task_dsl 的守卫只认任务池元数据 requires_branch 的盲区——
        # propose_dsl 是 MCP 主路径，自由 DSL 也必须对「意图有分支、产物无门」说不。
        if _dsl_implies_branch(dsl) and not _flow_has_branch_node(flow):
            lint_error_count += 1
            _msg = ("DSL 含条件语义（如果/才/超过/当…则/只有…才 等），但编译后不含任何分支/条件门节点"
                    "（分支:/否则:、条件:、查询:、时间段: 任一即可）——动作将无条件执行，已拦截。"
                    "请补上对应的条件分支语法再提交。")
            lint_summary.append({"rule": "R_branch_required", "level": "error", "message": _msg})
            result = {"ok": False, "stage": "lint_branch_required", "error": "R_branch_required",
                      "lint": lint_issues, "lint_summary": lint_summary,
                      "lint_error_count": lint_error_count, "lint_warning_count": lint_warning_count,
                      "message": _msg}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.branch_required", agent_id=agent_id,
                  elapsed=round(time.perf_counter() - _t0, 3))
            return result

        # 实体白名单硬拦（C01 修复，2026-07-27）：
        # 当 agent 显式传入 resolved_entities（来自 autoflow_resolve_entity 的确认结果）时，
        # DSL 引用的所有实体必须 ⊆ resolved_entities，否则视为『编造/未确认实体』硬拦，
        # 绝不落提案（fail-closed）。这是『先 resolve 再写』纪律的强制闸门——
        # 之前 run_staging_gate 虽能检出 resolve_whitelist（stage=resolve_whitelist）但未阻断落档
        #（propose_dsl 对闸门结果 fail-open，仅附在提案供人审），导致编造实体仍能进提案。
        # 仅当 resolved_entities 提供时才启用（向后兼容未传该参数的历史/外部调用）。
        if resolved_entities:
            _declared = set(resolved_entities)
            _used = set(self._collect_scene_entities(scene))
            _rogue = sorted(e for e in _used if e not in _declared)
            if _rogue:
                _msg = ("DSL 引用了未通过 autoflow_resolve_entity 确认的实体：" +
                        "、".join(_rogue) +
                        "。请先用 autoflow_resolve_entity 解析设备拿到真实 entity_id，"
                        "并作为 resolved_entities_json 传入后再提交；禁止编造/猜测 entity_id。"
                        "（编译期实体白名单校验：resolved_entities 之外的实体一律拦截，绝不落提案）")
                lint_error_count += 1
                lint_summary.append({"rule": "R_entity_whitelist", "level": "error",
                                     "message": _msg})
                result = {"ok": False, "stage": "entity_whitelist", "error": "R_entity_whitelist",
                          "lint": lint_issues, "lint_summary": lint_summary,
                          "lint_error_count": lint_error_count, "lint_warning_count": lint_warning_count,
                          "message": _msg,
                          "rogue_entities": _rogue}
                result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                                   log_path=self._telemetry_log)
                _slog(_tid, "propose_dsl.entity_whitelist", agent_id=agent_id,
                      rogue=_rogue, elapsed=round(time.perf_counter() - _t0, 3))
                return result

        # strict 模式：任何 error/真实 warning 都升级为阻断（默认非阻断，仅随回执透出）。
        # 用于「提交前必须零告警」的硬纪律场景（见 MCP 测试增强报告 P1）。
        # 例外：R22(inject 节点缺 repeat/crontab/once) 为编译器为每个 flow 生成的手动测试节点，
        # 属 by-design，strict 不拦（否则所有事件驱动自动化都会被 strict 误杀，见 iss_8f9a9fed9d）。
        if strict:
            _blocked_rules: List[str] = []
            _has_blocking = False
            for v in lint_issues:
                lvl = v.get("level")
                if lvl == "error":
                    _blocked_rules.append(v.get("rule"))
                    _has_blocking = True
                elif lvl == "warning":
                    if v.get("rule") == "R22" and v.get("node_type") == "inject":
                        continue  # by-design 手动测试节点，strict 不拦
                    _blocked_rules.append(v.get("rule"))
                    _has_blocking = True
            if _has_blocking:
                blocked_by = sorted(set(_blocked_rules))
                result = {"ok": False, "stage": "lint_strict", "strict_blocked": True,
                          "blocked_by": blocked_by, "lint": lint_issues, "lint_summary": lint_summary,
                          "lint_error_count": lint_error_count, "lint_warning_count": lint_warning_count,
                          "message": "strict 模式：lint 存在 error/真实 warning（R22 手动测试节点除外），已阻断提案。先修复下列规则再提交。"}
                result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                                   log_path=self._telemetry_log)
                _slog(_tid, "propose_dsl.strict_blocked", blocked_by=blocked_by,
                      elapsed=round(time.perf_counter() - _t0, 3))
                return result

        # DSL 内联 预期: 块 → 合并进 expected（去重，参数优先）
        for ec in getattr(scene, "expected", []):
            if ec and ec not in expected_postconditions:
                expected_postconditions.append(ec)

        # 生产落提案闸门启用分支感知（branch_aware 默认 True）：
        # 嵌套门孤儿接线 bug 已在 dsl_engine 修复（查询/时间段门返回自身 id），
        # 编译产物连线正确，分支感知可顺线评估门控、只重放命中分支的意图，不再误杀。
        gate = self.run_staging_gate(dsl, expected_postconditions,
                                      resolved_entities=resolved_entities,
                                      vhass_store=vhass_store)

        # 【WB92·O2 收口】黑箱 propose 对「未知实体」fail-open 修复（P3-F3 闭环）
        # 背景：run_staging_gate 能检出未知实体（stage=entity_check），但 propose_dsl 对
        # 闸门结果 fail-open —— 照常 ok=True 落提案，只有 verify_flow 才会拦。于是
        # 「NL→DSL→propose、不调 verify」的黑箱-only 路径可把编造/失效 entity_id 送进
        # 提案（WB84 P3-F3 / WB92 O2，六项未闭环中唯一「方向不安全」的一项）。
        # 与 run_e2e_trace（6749）与 deploy_raw（7275）对齐 —— 二者对 entity_check 均
        # fail-closed，此处也必须 fail-closed、绝不落提案。
        #
        # 爆炸半径实证（prod proposals 683 条）：gate.passed=False 共 97 条，其中本类 65 条；
        # 涉及 127 个去重未知实体里 119 个为测试探针构造（light.fake_* / *.invalid_* /
        # switch.xxx），唯一「像真实」的 media_player.xiaomi_cn_1108723976_lx05 经 HA 实况
        # 404 确认不存在（真实设备为同族的 _l17a）→ 真实误伤面 ≈ 0，fail-closed 安全。
        #
        # ★ 仅此一类硬拦；staging 闸的断言失败 / 保守拦截（verdict=拦截|未充分验证）仍保持
        #   advisory 落提案供人审，避免误伤合法流（O1/F12 的保守拦已知会误伤合法流）。
        if isinstance(gate, dict) and gate.get("stage") == "entity_check" \
                and not gate.get("passed"):
            _unknown = list(gate.get("failures") or [])
            _msg = ("DSL 引用了设备目录中不存在的 entity_id：" + "、".join(_unknown) +
                    "。请用 autoflow_resolve_entity / autoflow_list_entities 取真实 "
                    "entity_id 后重写 DSL 再提交；禁止编造或凭印象拼写 entity_id。"
                    "（未知实体硬拦，绝不落提案 —— 详见 O2/P3-F3 收口）")
            lint_error_count += 1
            lint_summary.append({"rule": "R_unknown_entity", "level": "error",
                                 "message": _msg})
            result = {"ok": False, "stage": "entity_check",
                      "error": "R_unknown_entity",
                      "unknown_entities": _unknown,
                      "proposal_id": None,   # 显式声明：绝不落提案（fail-closed 自证）
                      "gate": gate,
                      "lint": lint_issues, "lint_summary": lint_summary,
                      "lint_error_count": lint_error_count,
                      "lint_warning_count": lint_warning_count,
                      "message": _msg}
            result["_telemetry"] = _tag_action("propose_dsl", result, agent_id,
                                               log_path=self._telemetry_log)
            _slog(_tid, "propose_dsl.unknown_entity_blocked", agent_id=agent_id,
                  unknown=_unknown, elapsed=round(time.perf_counter() - _t0, 3))
            return result

        # 落提案（raw，等人审升格）。内容为 dsl + 闸门结果，便于人类复核。
        try:
            store = ProposalStore(self.cfg)
            p = store.submit(agent_id, scene.name, "skill",
                             json.dumps({"dsl": dsl,
                                         "expected_postconditions": expected_postconditions,
                                         "gate": gate,
                                         "node_count": len(flow["nodes"]),
                                         "require_e2e": bool(require_e2e)},
                                        ensure_ascii=False),
                             source="compiler", spec=dsl)
            proposal_id = p.id
        except Exception as e:
            proposal_id = None
            gate.setdefault("note", f"提案落档失败(非阻塞): {e}")
        _slog(_tid, "propose_dsl.gate", passed=bool(gate.get("passed")),
              gate_stage=gate.get("stage"), proposal_id=proposal_id,
              elapsed=round(time.perf_counter() - _t0, 3))

        # 完整编译产物快照（黑箱路径也留存，供编译器迭代对照 DSL→flow）
        snap = snapshot_flow(agent_id, "dsl", scene.name, flow,
                             dsl=dsl, gate=gate, ok=True,
                             extra={"proposal_id": proposal_id,
                                    "expected_postconditions": expected_postconditions,
                                    "node_count": len(flow["nodes"])})

        # 记录到 golden 活动日志（进程内模块级，供 autoflow_golden_eval 读取；设计者不判分）
        try:
            _record_proposal_event(agent_id, "propose_dsl", {
                "scene_name": scene.name,
                "entities": self._collect_scene_entities(scene),
                "gate_stage": gate.get("stage") or ("ok" if gate.get("passed") else "gate_failed"),
                "gate_passed": bool(gate.get("passed")),
                "proposal_id": proposal_id,
            })
        except Exception:
            pass

        _slog(_tid, "propose_dsl.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, gate_passed=bool(gate.get("passed")))
        # lint 摘要已在 lint 阶段统一计算（lint_summary / lint_error_count / lint_warning_count）
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "snapshot": snap,
            "_trace_id": _tid,
            "scene_name": scene.name,
            "dsl": dsl,
            "node_count": len(flow.get("nodes", [])),
            "static_validation": "passed",
            "lint": lint_issues,
            "lint_summary": lint_summary,
            "lint_error_count": lint_error_count,
            "lint_warning_count": lint_warning_count,
            "gate_passed": gate.get("passed"),
            "gate": gate,
            "require_e2e": bool(require_e2e),
            "flow": flow,
            "_telemetry": _tag_action(
                "propose_dsl", {"ok": True}, agent_id,
                extra={"proposal_id": proposal_id, "scene_name": scene.name,
                       "gate_passed": gate.get("passed")},
                log_path=self._telemetry_log),
        }

    def get_flow(self, flow_id: str, summary: bool = False) -> Dict[str, Any]:
        """只读：取回已部署 flow 的完整节点图 + 来源标记。

        供 agent 回看编译/部署产物（验证 propose_dsl 落地的 flow、或检视线上 tab），
        无需进 WebUI。节点图来自 NR(get_flow)，来源(source)来自 state 的 flow catalog。
        - flow_id：NR flow id（如 '57be9a8f1fca2bcd'）。
        - summary：True 时不返回 flow_json 全节点（省 token），改为返回 node_type_hist
          类型直方图；默认 False（返回完整节点图，供需要检视连线的场景）。
        - 返回 {ok, flow_id, flow_json:{nodes}|node_type_hist, source, label, node_count}。
        - 空 id / NR 无该 flow / 无 nodes → ok=False 并带原因。
        """
        if not flow_id:
            return {"ok": False, "error": "flow_id 为空", "stage": "get_flow"}
        # A31：af_scene_* 是 propose_dsl 编译产物的逻辑 id，未部署到 Node-RED；
        # get_flow 仅查已部署 flow，故必 404。明确告知，避免 agent 误以为流程失败。
        if flow_id.startswith("af_scene") or flow_id.startswith("af_"):
            return {"ok": False, "proposal": True, "flow_id": flow_id,
                    "stage": "get_flow",
                    "error": "该 flow_id 为编译提案逻辑 id（af_scene_*），尚未部署到 Node-RED。",
                    "hint": "get_flow 只查已部署 flow。如需查看节点图，请用 propose_dsl 返回的 "
                            "flow 字段；或先 deploy_proposal 部署，再用部署后返回的真实 flow id 回查。"}
        try:
            flow = self.nr.get_flow(flow_id)
        except Exception as e:
            # 注册表 ↔ NR 分叉检测（WB5#1a）：若注册表仍标记该 flow 已部署，
            # 但 NR 已无此 flow（被手动删除 / tab 重命名 / 实例切换），则明确提示
            # stale=True，让调用方区分"flow_id 本就不存在"与"注册表与 NR 已分叉"，
            # 而非笼统的"取 flow 失败"推给下游自行猜根因。
            stale = False
            try:
                if self.state.get_flow_meta(flow_id):
                    stale = True
            except Exception:
                pass
            result = {"ok": False, "error": f"NR 取 flow 失败: {e}", "stage": "get_flow",
                      "flow_id": flow_id}
            if stale:
                result["stale"] = True
                result["hint"] = ("注册表仍有该 flow 的部署记录，但 Node-RED 已无此 flow"
                                  "（可能已被手动删除 / tab 重命名 / 实例切换）。"
                                  "请确认 NR 实例与网关指向一致，或用 list_automations(only='deployed')"
                                  " 核对 stale 标记后重新部署。")
            return result
        nodes = (flow or {}).get("nodes", [])
        if not nodes:
            return {"ok": False, "error": "flow 无节点（可能 flow_id 不存在或为空 tab）",
                    "stage": "get_flow", "flow_id": flow_id}
        source = None
        try:
            meta = self.state.get_flow_meta(flow_id) or {}
            source = meta.get("source")
        except Exception:
            source = None
        label = ((flow or {}).get("label")
                 or (flow or {}).get("info", {}).get("name")
                 or flow_id)
        # summary=True：不 dump 全节点（省 token），返回类型直方图 + 关键元信息
        if summary:
            _hist: Dict[str, int] = {}
            for _n in nodes:
                _t = _n.get("type", "?")
                _hist[_t] = _hist.get(_t, 0) + 1
            return {
                "ok": True,
                "flow_id": flow_id,
                "node_count": len(nodes),
                "node_type_hist": _hist,
                "source": source,
                "label": label,
                "disabled": bool((flow or {}).get("disabled", False)),
                "summary": True,
            }
        return {
            "ok": True,
            "flow_id": flow_id,
            "flow_json": {"nodes": nodes},
            "node_count": len(nodes),
            "source": source,
            "label": label,
            # TASK_tab_enabled_state：补顶层 disabled，与 list_tabs 同源一致（NR GET /flow/{id} 原生返回）
            "disabled": bool((flow or {}).get("disabled", False)),
        }

    # ───────────── tab 启用/禁用（TASK_tab_enabled_state）─────────────
    # list_tabs 结果缓存：纯只读、幂等，5s TTL 避免高频轮询打爆 NR
    _TAB_LIST_CACHE_TTL = 5.0
    _tab_list_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

    def list_tabs(self, only_disabled: bool = False,
                  keyword: Optional[str] = None) -> Dict[str, Any]:
        """只读：列出 Node-RED 全部 type=='tab' 分页（每个 tab=一个 flow），含启用/禁用状态与节点数。

        纯旁路、幂等：仅一次 GET /flows + 有界解析 + 5s TTL 缓存，家庭规模 < 500ms。
        不 dump 任何节点内容，只回 tab 元数据（id/label/disabled/node_count/source），
        供 agent 巡检「卧室灯」「客厅」等用户手工/第三方 tab 的启停状态。
        - only_disabled=True：只回 disabled 的 tab
        - keyword：按 label/id 模糊过滤
        - count_disabled：全集里被禁用的 tab 总数（不受过滤影响）
        """
        now = time.time()
        cached = type(self)._tab_list_cache["data"]
        if cached is not None and (now - type(self)._tab_list_cache["ts"]) < self._TAB_LIST_CACHE_TTL:
            tabs = cached
        else:
            try:
                flows = self.nr.list_flows()  # GET /flows 扁平数组
            except Exception as e:
                return {"ok": False, "error": f"NR 列 flow 失败: {e}",
                        "tabs": [], "count": 0, "count_disabled": 0}
            if isinstance(flows, dict):
                flows = flows.get("flows", [])
            # 只取 tab 分页对象，剔除 subflow/config 节点
            tabs_raw = [f for f in flows
                        if isinstance(f, dict) and f.get("type") == "tab"]
            tabs = []
            for t in tabs_raw:
                tid = t.get("id")
                label = (t.get("label")
                         or (t.get("info") or {}).get("name")
                         or "未命名")
                disabled = bool(t.get("disabled", False))
                # 节点数 = 同 z 指向该 tab 且本身非 tab 的条目数
                node_count = sum(
                    1 for f in flows
                    if isinstance(f, dict) and f.get("type") != "tab" and f.get("z") == tid
                )
                source = None
                try:
                    meta = self.state.get_flow_meta(tid) or {}
                    source = meta.get("source")
                except Exception:
                    source = None
                tabs.append({
                    "id": tid,
                    "label": label,
                    "disabled": disabled,
                    "node_count": node_count,
                    "source": source,
                })
            tabs.sort(key=lambda x: x["node_count"], reverse=True)  # 按节点数降序
            type(self)._tab_list_cache["data"] = tabs
            type(self)._tab_list_cache["ts"] = now

        # 过滤（缓存存全集，每次按需过滤）
        result = list(tabs)
        if only_disabled:
            result = [t for t in result if t["disabled"]]
        if keyword:
            kw = keyword.lower()
            result = [t for t in result
                      if kw in (t["label"] or "").lower() or kw in (t["id"] or "").lower()]
        count_disabled = sum(1 for t in tabs if t["disabled"])
        return {
            "ok": True,
            "tabs": result,
            "count": len(result),
            "count_disabled": count_disabled,
        }

    def set_tab_state_submit(self, flow_id: str, enabled: bool, agent_id: str,
                             reason: str = "") -> Dict[str, Any]:
        """写：启用/禁用单个 NR tab → 经确认闸提交（人类批准才执行）。

        - 提交即校验 flow_id 存在性（NR get_flow 404 → unknown=True），避免「先落待确认、
          执行时才发现不存在」的静默错改（AC10）。
        - AC12：仅『禁用』核心 tab（心跳/HA 桥接）被拦截；『启用』核心 tab 不受限。
          用 is_protected_flow（避开 check_write 的所有权拒否——tab 多为用户/第三方创建、无 agent 归属）。
        """
        if not flow_id:
            return {"ok": False, "error": "flow_id 为空", "unknown": True}
        try:
            flow = self.nr.get_flow(flow_id)
        except Exception:
            return {"ok": False,
                    "error": f"flow_id 不存在或 NR 取 flow 失败: {flow_id}",
                    "unknown": True, "flow_id": flow_id}
        label = ((flow or {}).get("label")
                 or (flow or {}).get("info", {}).get("name")
                 or flow_id)
        # AC12：禁用核心 tab 拦截
        if not enabled and self.defense.is_protected_flow(flow_id, label):
            return {"ok": False,
                    "error": (f"拒绝：tab『{label}』为核心受保护 flow，"
                              f"不可禁用（防误关全家瘫痪）。"),
                    "protected": True, "flow_id": flow_id}
        risk = self.defense.classify_operation_risk("set_tab_state", [])
        target_state = "禁用" if not enabled else "启用"
        summary = (f"切换 tab 状态：{label}({flow_id}) → {target_state}"
                   + (f"（{reason}）" if reason else ""))
        payload = {"flow_id": flow_id, "enabled": enabled, "reason": reason}
        op = PendingOp("set_tab_state", agent_id, risk, summary, 1, payload,
                       target=flow_id, owner_flow=flow_id)
        self.confirm.request(op)
        return {"ok": True, "pending_id": op.id, "risk": risk,
                "risk_level": op.risk_level, "blast_radius": 1,
                "needs_approval": True, "flow_id": flow_id, "label": label,
                "enabled": enabled}

    def set_tab_state_execute(self, flow_id: str, enabled: bool) -> Dict[str, Any]:
        """人审批准后执行：仅翻转 tab 级 disabled，节点内容原样回写（AC9 节点字节不变）。

        直写 put_flow_raw（不经 _normalize_flow），保证节点 payload 逐字节不变。
        已是人审确认闸，跳过 prod 护栏合理。
        """
        flow = self.nr.get_flow(flow_id)
        if not isinstance(flow, dict):
            return {"ok": False, "error": f"NR 无该 flow: {flow_id}"}
        flow["disabled"] = not enabled
        res = self.nr.put_flow_raw(flow_id, flow)
        return {"ok": True, "executed": "set_tab_state", "flow_id": flow_id,
                "disabled": not enabled, "nr_result": res.get("raw", res)}

    def get_nr_subflow_integrity(self) -> Dict[str, Any]:
        """只读：扫描 NR 全部子流程定义的结构完整性（灭绝空壳假 PASS）。

        调 self.nr.list_flows()（GET /flows 全量），双兼容扁平数组 / {flows:[...]}
        嵌套两种形状，解析每个子流程定义（type=="subflow"）的：
          - internal_node_count：内部节点数（function / api-get-history / catch 等）。
          - empty_shell：内部节点数 == 0 → 即 #607 复现的「空壳无取数能力」致命态。
          - has_mustache_entity：内部含取数节点（api-get-history 等）且 entityId 仍为
            mustache 模板（{{...}}）→ 会跑但结果恒错，降级非致命，仅 warning。

        返回 {ok, source, subflows:[{id,name,internal_node_count,empty_shell,
                has_mustache_entity,internal_types}], empty_shells:[id...],
               any_empty_shell}。
        失败时 fail-open：ok=True 但 source="error"，绝不因内省故障阻断正常部署
        （金丝雀只在「确实探测到空壳」时硬拦，见 deploy_raw Step 8.5）。"""
        try:
            flows = self.nr.list_flows()
        except Exception as e:
            return {"ok": True, "source": "error",
                    "error": f"list_flows 失败（fail-open 放行）: {e}",
                    "subflows": [], "empty_shells": [], "any_empty_shell": False}

        # 归一为节点列表（双兼容扁平数组 / {flows:[...]}/{nodes:[...]} 嵌套）
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

        defs = [n for n in nodes if n.get("type") == "subflow"]
        subflows: List[Dict] = []
        empty_shells: List[str] = []
        for d in defs:
            sid = d.get("id")
            sname = d.get("name") or d.get("info") or sid
            # 内部节点：双形状兼容（def 自带嵌套 flow / 扁平 z==sid）
            internal: List[Dict] = []
            raw_flow = d.get("flow")
            if isinstance(raw_flow, dict):
                internal.extend(raw_flow.get("nodes", []))
            elif isinstance(raw_flow, list):
                internal.extend(raw_flow)
            internal.extend([n for n in nodes
                             if n.get("z") == sid and n.get("type") != "subflow"
                             and n.get("id") != sid])
            # 按 id 去重
            _seen = set()
            _dedup = []
            for n in internal:
                _id = n.get("id")
                if _id in _seen:
                    continue
                _seen.add(_id)
                _dedup.append(n)
            internal = _dedup

            internal_types = sorted({n.get("type") for n in internal if n.get("type")})
            icount = len(internal)
            empty_shell = (icount == 0)

            has_mustache = False
            for n in internal:
                nt = n.get("type", "")
                if nt in ("api-get-history", "api-current-state", "api-call-service",
                          "ha-get-history", "ha-current-state", "ha-call-service",
                          "server-state-changed", "poll-state"):
                    eid = (n.get("entityId") or n.get("entity") or n.get("topic") or "")
                    if isinstance(eid, str) and "{{" in eid:
                        has_mustache = True
                        break

            subflows.append({
                "id": sid, "name": sname,
                "internal_node_count": icount,
                "empty_shell": empty_shell,
                "has_mustache_entity": has_mustache,
                "internal_types": internal_types,
            })
            if empty_shell:
                empty_shells.append(sid)

        return {
            "ok": True,
            "source": "nr_list_flows",
            "subflows": subflows,
            "empty_shells": empty_shells,
            "any_empty_shell": bool(empty_shells),
        }

    def get_debug_read(self, flow_id: Optional[str] = None, node_id: Optional[str] = None,
                       since: Optional[int] = None, limit: Optional[int] = None,
                       full: bool = False) -> Dict[str, Any]:
        """只读：从 debug_bridge 本地缓冲读 debug 事件（绝不现打 NR）。fail-open。

        数据由后台线程旁路订阅 NR5.0.1 原生 ws://<nr>/comms debug 事件流得来，
        缓存在网关本地（每节点有界环形缓冲 + TTL + 全局上限）。本方法只从缓冲读，
        per-read 不向 NR 发任何请求、不触发任何节点、不往 flow 插采集节点。

        参数同 autoflow_debug_read：flow_id/node_id 过滤、since(Unix秒)/limit 截断、
        full 是否附完整 payload。失败时 fail-open 返回 ok=True + 空 events。"""
        # ── C28: flow 存在性结构化错误闭环（先于 debug_bridge.read，禁用态也能命中）──
        if flow_id:
            from .errors import not_found
            flow = None
            try:
                fr = self.nr.get_flow(flow_id)
            except Exception as e:  # NRLayer 通常吞异常返回 ok:False dict, 此处兜底
                fr = {"ok": False, "error": str(e)}
            if isinstance(fr, dict) and fr.get("ok") is False:
                # NR 明确 404 -> id 不存在 -> 结构化 NOT_FOUND；其余（NR 不可达）降级
                if "404" in str(fr.get("error", "")):
                    err = not_found("flow", flow_id)
                    return {
                        "ok": False,
                        "source": "debug_bridge_gate",
                        "status": "error",
                        "error": {
                            "code": err.code.value,
                            "category": "not_found",
                            "hint": err.message,
                            "flow_id": flow_id,
                        },
                    }
                # NR 不可达/超时：暂无法判定，fail-open 走原 read 路径
            else:
                flow = fr  # 真实 flow dict
            # ── 延展 B: 真实 flow 但 node_id 不在节点集合 -> 结构化 NOT_FOUND ──
            if flow is not None and node_id:
                node_ids = {n.get("id") for n in (flow.get("nodes") or [])}
                if node_id not in node_ids:
                    err = not_found("node", node_id)
                    return {
                        "ok": False,
                        "source": "debug_bridge_gate",
                        "status": "error",
                        "error": {
                            "code": err.code.value,
                            "category": "not_found",
                            "hint": err.message,
                            "flow_id": flow_id,
                            "node_id": node_id,
                        },
                    }
        try:
            return self.debug_bridge.read(flow_id=flow_id, node_id=node_id,
                                          since=since, limit=limit, full=full)
        except Exception as e:
            return {"ok": True, "source": "error",
                    "enabled": self.debug_bridge.enabled,
                    "connected": self.debug_bridge._connected,
                    "error": f"debug_bridge read 失败（fail-open）: {e}", "events": []}

    def verify_task_dsl(self, dsl: str, expected: Optional[List[Dict]] = None,
                        resolved: Optional[List[str]] = None,
                        run_gate: bool = False,
                        requires_branch: bool = False) -> Dict[str, Any]:
        """【任务池提交即时校验】解析 → 编译 → lint →（可选）staging 闸门，但不落提案/不进确认闸。
        供 autoflow_submit_result 复用 propose_dsl 的校验核心，返回客观结果给 DSL 引擎迭代参考。

        result_kind：
          · gate_pass    —— 编译通过 + lint 干净 + staging 闸门通过
          · compiled     —— 编译通过 + lint 干净（未跑/不适用闸门）
          · lint_error   —— 编译通过但 lint 含 error 级反模式
          · compile_error—— 解析/编译失败（DSLError 或异常）
          · no_response  —— 空 DSL（由调用方判定，本方法不打此标）
        返回 {ok, result_kind, node_count, lint_summary, lint_error_count, gate, gate_passed, error}。"""
        from .dsl_engine import (parse, compile, DSLError, set_entity_resolver,
                                 set_entity_attributes_resolver,
                                 detect_semantic_gaps)

        def _has_branch_node(flow):
            # #P2-3（2026-07-24 修正误报，来自问卷 tr_ 系列「时间段+否则」全误报）：
            # 判定编译产物是否含真正的分支/条件门节点。接受三类 DSL 编译产物的门控节点：
            #   · switch            —— 分支:/否则: 或复杂 JSONata 条件（恒为分支）
            #   · api-current-state —— 条件:/查询: 门控；仅当 outputs>=2 才认
            #                         （排除 取值: 的 api-current-state，其 outputs=1 仅读取非分支）
            #   · time-range-switch —— 时间段: 门控（2 路输出）
            for n in (flow or {}).get("nodes", []):
                t = n.get("type")
                if t == "switch":
                    return True
                if t in ("api-current-state", "time-range-switch"):
                    try:
                        outs = int(n.get("outputs", 0) or 0)
                    except (TypeError, ValueError):
                        outs = 0
                    if outs >= 2:
                        return True
            return False

        expected = expected or []
        resolved = resolved or []
        set_entity_resolver(lambda t: self._resolve_best(t))
        set_entity_attributes_resolver(lambda eid: self._entity_attribute_names(eid))
        # 语义缺口预检（B1，复用 dsl_engine.detect_semantic_gaps）：
        # 含历史/首次/间隔触发/自然语言条件等意图却未用对应能力时高声拒绝，
        # 避免静默降级成『看似满足、语义全反』的 flow。其中历史意图(_HIST_PHRASES)
        # 若未调用 history_* 子流程即判 FAIL——这是任务池对历史能力的硬检查(#271)。
        gaps = detect_semantic_gaps(dsl)
        if gaps:
            return {"ok": False, "stage": "semantic_gap", "result_kind": "compile_error",
                    "error": "语义缺口（高声拒绝，避免静默降级）：" + "；".join(gaps),
                    "node_count": 0, "lint_summary": [], "lint_error_count": 0,
                    "gate": None, "gate_passed": None}
        # 原生节点逃逸开关（Phase 4）：关闭时拒绝含 原生节点: 的 DSL（随时可关，免重启）
        if _RAW_NODE_KW_RE.search(dsl or "") and not is_raw_node_escape_enabled(self.cfg):
            return {"ok": False, "stage": "feature_disabled", "result_kind": "compile_error",
                    "error": "原生节点逃逸功能已关闭（由 WebUI 开关控制）。如需使用请在 WebUI 设置中开启『原生节点逃逸』。",
                    "node_count": 0, "lint_summary": [], "lint_error_count": 0,
                    "gate": None, "gate_passed": None}
        try:
            scene = parse(dsl)
            flow = compile(scene)
        except DSLError as e:
            return {"ok": False, "stage": "compile", "result_kind": "compile_error",
                    "error": str(e), "compile_error": _compile_error_envelope(e),
                    "node_count": 0, "lint_summary": [],
                    "lint_error_count": 0, "gate": None, "gate_passed": None}
        except Exception as e:  # pragma: no cover - 解析器外部异常兜底
            return {"ok": False, "stage": "compile", "result_kind": "compile_error",
                    "error": f"编译异常: {e}", "node_count": 0, "lint_summary": [],
                    "lint_error_count": 0, "gate": None, "gate_passed": None}

        lint_issues = lint_flow(flow)
        lint_summary = [
            {"rule": v.get("rule"), "level": v.get("level"), "message": v.get("message")}
            for v in lint_issues if v.get("level") in ("error", "warning")
        ]
        lint_error_count = sum(1 for v in lint_issues if v.get("level") == "error")

        # 加固（#272 / #P2-3，2026-07-24 修正误报）：任务声明需条件分支（如果…才…），
        # 但编译产物不含任何「分支/条件门」节点时，判定为 lint_error 并拦截——
        # 避免动作被无条件执行（黑箱模式易丢分支，见审查结论）。
        # 必须用编译产物节点判定，不能用 "分支" 子串：场景名如
        # 「主卧室多分支控灯扇」本身含「分支」二字，会让子串检查误判「已含分支」而放行浅版。
        # 接受的分支门控节点（#P2-3 修正误报，覆盖 条件:/查询:/时间段:/分支: 全部合法形式）：
        #   switch / api-current-state(outputs>=2) / time-range-switch(outputs>=2)。
        # 默认 requires_branch=False，对历史/无条件任务零影响。
        if requires_branch and not _has_branch_node(flow):
            lint_error_count += 1
            lint_summary.append({
                "rule": "R_branch_required",
                "level": "error",
                "message": "任务要求条件分支（如果…才…），但 DSL 编译后不含任何分支/条件门节点"
                           "（分支:/否则:、条件:、查询:、时间段: 任一即可）——动作将无条件执行。"
                           "已拦截为 lint_error，请补上条件分支再提交。",
            })

        gate = None
        gate_passed = None
        if run_gate:
            try:
                gate = self.run_staging_gate(dsl, expected, resolved_entities=resolved)
                gate_passed = bool(gate.get("passed")) if isinstance(gate, dict) else None
            except Exception as e:
                gate = {"error": f"闸门异常: {e}"}
                gate_passed = None

        if lint_error_count > 0:
            result_kind = "lint_error"
        elif run_gate and gate_passed:
            result_kind = "gate_pass"
        elif run_gate and gate is not None and not gate_passed:
            result_kind = "gate_fail"
        else:
            result_kind = "compiled"
        return {
            "ok": True, "stage": "verified", "result_kind": result_kind,
            "node_count": len(flow.get("nodes", [])),
            "lint_summary": lint_summary, "lint_error_count": lint_error_count,
            "gate": gate, "gate_passed": gate_passed, "error": None,
        }

    # ───────────── Golden 回归评测（测试助手当独立裁判）─────────────
    def run_golden_eval(self, scenario: str = "1", mode: str = "black",
                        timeout: int = 240, backend: str = "ds_bridge",
                        call_timeout: int = 600, job_id: str = None) -> Dict[str, Any]:
        """让测试助手独立执行 golden 场景并客观判分。
        backend 仅支持 "ds_bridge"（默认）：点 chrome 版 deepseek++（ds_bridge 控 Chrome）执行；
        提案落模块级 _PROPOSAL_LOG，判分逻辑共用。NIM 对照后端已移除。"""
        if backend != "ds_bridge":
            raise ValueError(f"不支持的后端: {backend!r}（NIM 对照后端已移除，仅支持 ds_bridge）")
        scen = self._parse_golden_scenario(scenario)
        if not scen:
            return {"ok": False, "error": f"未找到场景 {scenario}（scenarios.md）"}
        snap_len = len(_PROPOSAL_LOG)
        self._emit(job_id, "start", f"开始 golden 评测：场景 {scen['id']} ｜ 模式 {mode} ｜ 后端 {backend}")
        idle = self._wait_ds_bridge_idle(job_id=job_id)  # 防连续场景竞合：等上一轮 deepseek 收尾
        # 点火：ds_bridge 的 /api/chat 会阻塞到 deepseek++ 整轮跑完，故用短 HTTP 超时"发完即返回"，
        # 真正等结果靠下面的日志轮询（deepseek++ 提案会落进模块级 _PROPOSAL_LOG）。
        fired = self._fire_ds_bridge(self._build_golden_prompt(scen, mode), job_id=job_id)
        fired["idle_wait"] = idle
        entries = self._wait_for_proposals(snap_len, timeout, job_id=job_id)
        if not entries:
            self._emit(job_id, "error", "超时内无提案；可能后端未在线 / 模型未响应 / 未提交 autoflow", ok=False)
            return {"ok": False, "fired": fired, "backend": backend, "mode": mode,
                    "error": "超时内无提案；可能后端未在线 / 模型未响应 / 未提交 autoflow"}
        verdict = self._judge_golden(scen, entries)
        verdict["ok"] = True
        verdict["fire"] = fired
        verdict["mode"] = mode
        verdict["backend"] = backend
        self._emit(job_id, "done", f"golden 判分完成：{'PASS' if verdict.get('ok') else 'FAIL'}（提案 {len(entries)} 条）", ok=bool(verdict.get("ok")))
        return verdict

    def start_golden_eval(self, scenario: str = "1", mode: str = "black",
                          timeout: int = 240, backend: str = "ds_bridge",
                          call_timeout: int = 600) -> Dict[str, Any]:
        """【非阻塞】启动 golden 回归：发完后端立即返回 job_id，等待+判分在后台线程跑。
        根治死锁：原 run_golden_eval 同步空转 300s 会把 uvicorn 事件循环占死，
        导致测试助手的 autoflow_* 工具调用（正是本任务要等的提案）全部超时连不上。
        改为后台线程后，网关立刻腾出，提案调用正常进来。用 autoflow_golden_status(job_id) 轮询结果。
        backend 透传：ds_bridge(默认, deepseek++)。NIM 对照后端已移除。"""
        job_id = f"g{int(time.time() * 1000)}"
        with _GOLDEN_JOBS_LOCK:
            _GOLDEN_JOBS[job_id] = {
                "job_id": job_id, "status": "starting",
                "scenario": scenario, "mode": mode, "timeout": timeout, "backend": backend,
                "started_at": time.time(), "finished_at": None, "result": None, "events": [],
            }

        def _worker():
            with _GOLDEN_JOBS_LOCK:
                _GOLDEN_JOBS[job_id]["status"] = "running"
            try:
                result = self.run_golden_eval(scenario=scenario, mode=mode, timeout=timeout, backend=backend, call_timeout=call_timeout, job_id=job_id)
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                self._emit(job_id, "error", f"评测异常：{type(e).__name__}: {e}", ok=False)
            with _GOLDEN_JOBS_LOCK:
                _GOLDEN_JOBS[job_id].update({
                    "status": "done" if result.get("ok") else "error",
                    "finished_at": time.time(), "result": result,
                })

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "job_id": job_id, "status": "running",
                "note": "等待+判分在后台运行；用 autoflow_golden_status(job_id) 轮询结果"}

    def get_golden_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """查询 golden 回归任务状态/结果（job_id 来自 start_golden_eval 返回值）。"""
        with _GOLDEN_JOBS_LOCK:
            return dict(_GOLDEN_JOBS.get(job_id)) if job_id in _GOLDEN_JOBS else None

    def get_recent_traces(self, n: int = 50) -> List[Dict[str, Any]]:
        """P4-C：返回进程内结构化 trace 环形缓冲最近 n 条（不落盘、重启即丢，仅诊断用）。

        诊断查看器只读展示「最近网关内部发生了什么」——_slog 在 propose_dsl /
        deploy_raw / deploy_proposal / list_pending 等关键入口记录的 stage + 耗时 + 上下文。
        """
        with _GOLDEN_JOBS_LOCK:  # 复用同一把锁，与 _slog 写互斥
            ring = list(_TRACE_RING)
        return ring[-n:] if n > 0 else ring

    def list_golden_jobs(self, limit: int = 15) -> List[Dict[str, Any]]:
        """P4-C：返回最近评测（golden / acceptance）任务的紧凑快照（按 started_at 倒序），
        供诊断查看器只读展示，无需逐个轮询。每条只投影关键字段，避免把完整 result/events 塞进诊断面板。"""
        with _GOLDEN_JOBS_LOCK:
            jobs = [dict(j) for j in _GOLDEN_JOBS.values()]
        jobs.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
        out = []
        for j in jobs[:limit]:
            res = j.get("result") or {}
            out.append({
                "job_id": j.get("job_id"),
                "kind": j.get("kind") or ("golden" if j.get("scenario") else "eval"),
                "scenario": j.get("scenario"),
                "mode": j.get("mode"),
                "backend": j.get("backend"),
                "status": j.get("status"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "n_events": len(j.get("events") or []),
                "ok": res.get("ok") if res else None,
                "summary": (res.get("acceptance") or res.get("error") or "")[:120],
            })
        return out

    def _emit(self, job_id: str, phase: str, msg: str, iter: int = None, ok: bool = None):
        """向评测任务追加一条结构化过程事件（WebUI 时间线 / MCP 状态共用）。
        job_id 不存在时安全 no-op（兼容旧调用未传 job_id 的情形）。"""
        if not job_id:
            return
        with _GOLDEN_JOBS_LOCK:
            job = _GOLDEN_JOBS.get(job_id)
            if not job:
                return
            ev = {"ts": time.time(), "phase": phase, "msg": msg}
            if iter is not None:
                ev["iter"] = iter
            if ok is not None:
                ev["ok"] = ok
            job.setdefault("events", []).append(ev)

    # ───────────── Acceptance 验收（deepseek++ 当验收助手）─────────────
    def run_acceptance_eval(self, prompt: str, mode: str = "black",
                            timeout: int = 240, backend: str = "ds_bridge",
                            call_timeout: int = 600, job_id: str = None) -> Dict[str, Any]:
        """让测试助手用当前 MCP 工具面执行一段我（设计者）写的验收提示词，
        验证『改完 MCP 后，测试助手能正确驱动新工具面』这一验收闭环。
        不依赖 scenarios.md，prompt 由设计者自由编写（聚焦本次改动的工具/原语）。
        判定：timeout 内测试助手经 autoflow MCP 产生 ≥1 条提案即视为验收 PASS
        （意味着它成功解析设备、编写并提交 DSL，工具面可用）。
        backend 透传：ds_bridge(默认, deepseek++)。NIM 对照后端已移除。"""
        if backend != "ds_bridge":
            raise ValueError(f"不支持的后端: {backend!r}（NIM 对照后端已移除，仅支持 ds_bridge）")
        if not prompt or not prompt.strip():
            return {"ok": False, "error": "验收提示词为空"}
        snap_len = len(_PROPOSAL_LOG)
        self._emit(job_id, "start", f"开始验收评测：模式 {mode} ｜ 后端 {backend} ｜ 提示词 {prompt[:40]!r}…")
        idle = self._wait_ds_bridge_idle(job_id=job_id)  # 防连续场景竞合：等上一轮 deepseek 收尾
        fired = self._fire_ds_bridge(prompt, job_id=job_id)
        fired["idle_wait"] = idle
        entries = self._wait_for_proposals(snap_len, timeout, job_id=job_id)
        proposed: set = set()
        for e in entries:
            proposed.update((e.get("data") or {}).get("entities", []))
        agents = sorted({e.get("agent_id") for e in entries if e.get("agent_id")})
        self._emit(job_id, "done", f"验收完成：{'PASS' if entries else 'FAIL'}（提案 {len(entries)} 条，后端 {backend}）", ok=bool(entries))
        return {
            "ok": True,
            "fire": fired,
            "mode": mode,
            "backend": backend,
            "n_proposals": len(entries),
            "proposed_entities": sorted(proposed),
            "agents": agents,
            "prompt_used": prompt,
            "acceptance": "PASS" if entries else "FAIL",
            "entries_meta": [
                {"kind": e.get("kind"), "agent_id": e.get("agent_id"),
                 **{k: (e.get("data") or {}).get(k) for k in
                    ("scene_name", "gate_stage", "gate_passed", "proposal_id")}}
                for e in entries
            ],
        }

    def start_acceptance_eval(self, prompt: str, mode: str = "black",
                              timeout: int = 240, backend: str = "ds_bridge",
                              call_timeout: int = 600) -> Dict[str, Any]:
        """【非阻塞】发起验收：发完后端立即返回 job_id，
        等待+捕获在后台线程跑；用 autoflow_golden_status(job_id) 轮询结果。
        backend 透传：ds_bridge(默认, deepseek++)。NIM 对照后端已移除。"""
        job_id = f"a{int(time.time() * 1000)}"
        with _GOLDEN_JOBS_LOCK:
            _GOLDEN_JOBS[job_id] = {
                "job_id": job_id, "status": "starting",
                "kind": "acceptance", "mode": mode, "timeout": timeout, "backend": backend,
                "started_at": time.time(), "finished_at": None, "result": None, "events": [],
            }

        def _worker():
            with _GOLDEN_JOBS_LOCK:
                _GOLDEN_JOBS[job_id]["status"] = "running"
            try:
                result = self.run_acceptance_eval(prompt=prompt, mode=mode, timeout=timeout, backend=backend, call_timeout=call_timeout, job_id=job_id)
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                self._emit(job_id, "error", f"评测异常：{type(e).__name__}: {e}", ok=False)
            with _GOLDEN_JOBS_LOCK:
                _GOLDEN_JOBS[job_id].update({
                    "status": "done" if result.get("ok") else "error",
                    "finished_at": time.time(), "result": result,
                })

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "job_id": job_id, "status": "running",
                "note": "等待+捕获在后台运行；用 autoflow_golden_status(job_id) 轮询结果"}

    def _golden_scenarios_path(self) -> str:
        return os.environ.get("AF_GOLDEN_SCENARIOS") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "tests", "golden", "scenarios.md")

    def _parse_golden_equiv(self) -> List[set]:
        """读取 scenarios.md『等价实体组』：每行 `A = B [= C]` 解析成一个 entity_id 集合。
        判分时同组任一命中即算命中该期望（同一物理设备多集成表示，功能等价）。"""
        path = self._golden_scenarios_path()
        groups: List[set] = []
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            return groups
        m = re.search(r"## 附：等价实体组.*?(?=\n## |\Z)", text, flags=re.DOTALL)
        section = m.group(0) if m else ""
        ENT = r"`([a-z_]+\.[a-z0-9_]+)`"
        for line in section.splitlines():
            if line.lstrip().startswith("- ") and "=" in line:
                ids = re.findall(ENT, line)
                if len(ids) >= 2:
                    groups.append(set(ids))
        return groups

    def _parse_golden_scenario(self, scenario_id: str) -> Optional[Dict[str, Any]]:
        """解析 tests/golden/scenarios.md 中指定编号场景。
        期望实体 = 『期望*』行里的反引号 entity_id；禁用实体 = 『常见错误/FAIL』行里的反引号 entity_id。"""
        path = os.environ.get("AF_GOLDEN_SCENARIOS") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "tests", "golden", "scenarios.md")
        if not os.path.exists(path):
            return None
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            return None
        blocks = re.split(r"^## 场景\s+(\d+)", text, flags=re.MULTILINE)
        # blocks: [pre, id1, body1, id2, body2, ...]
        target = None
        for i in range(1, len(blocks), 2):
            if blocks[i].strip() == scenario_id:
                target = blocks[i + 1]
                break
        if target is None:
            return None
        ENT = r"`([a-z_]+\.[a-z0-9_]+)`"
        nl_m = re.search(r"\*\*自然语言\*\*[:：]\s*(.+)", target)
        nl = nl_m.group(1).strip().strip('"').strip() if nl_m else ""
        expect, forbid = [], []
        for line in target.splitlines():
            ids = re.findall(ENT, line)
            if not ids:
                continue
            if re.search(r"常见错误|FAIL|⚠", line):
                forbid.extend(ids)
            elif re.search(r"期望", line):
                expect.extend(ids)
        behavior_m = re.search(r"\*\*期望行为\*\*[:：]\s*(.+)", target)
        return {
            "id": scenario_id, "nl": nl,
            "expect": expect, "forbid": forbid,
            "behavior": behavior_m.group(1).strip() if behavior_m else "",
        }

    def _build_golden_prompt(self, scen: Dict[str, Any], mode: str) -> str:
        head = (
            f"请用 autoflow 技能执行以下 Home Assistant 自动化场景，"
            f"本任务必须以成功调用一次 autoflow_propose_dsl 收尾，然后回复『已完成』：\n"
        )
        return (
            head +
            f"场景：{scen['nl']}\n\n"
            f"执行要求（务必高效，别反复空转）：\n"
            f"1. 语法不确定就先调一次 autoflow_dsl_help()，重点看 examples（含 OR多触发/数值条件+否则/查询门控/多分支/工作日时间段/TTS播报）与 resolve_entity_rules；看完即动手，不要反复查阅。\n"
            f"2. 实体解析与消歧（关键）：对每个设备名调 autoflow_resolve_entity 一次。若只返回 1 个结果直接用；"
            f"若返回多个候选，**按『场景所在区域 + 设备用途』消歧**——例如场景说『开门亮牌匾灯』，门磁要选『玄关/大门』那个而非卧室门磁，"
            f"台灯要选『书房台灯』而非其他房间灯；切勿随机选、也别停在原地反复解析。同名多子实体（如人体传感器含 Motion 与 光照度）按用途各取所需。\n"
            f"3. 用选定的真实 entity_id 编写 DSL（动作目标用位置参数，如 light.turn_on(light.xxx)，不要写成 entity_id=）；\n"
            f"4. 立即调用 autoflow_propose_dsl 提交，并把解析出的真实 entity_id 列表作为 resolved_entities_json 传入；\n"
            f"5. 严禁凭记忆猜 entity_id，严禁用域不同的相近设备（如 switch 域冒充 light 域）；"
            f"**绝对禁止编造占位名（如 study_motion / desk_lamp / my_light）——任何设备都必须来自第 2 步的真实解析结果；"
            f"若某设备 resolve_entity 无结果，就如实说明并跳过该条件，绝不用假 id 凑数（假 id 会被闸门拦截且浪费整轮）。**\n"
            f"6. 【完整性】场景里出现的每个实体都要落进 DSL，一个都别漏：\n"
            f"   ·『光线暗/低于N/亮度低于』等数值条件 → 必须 取值: <光照度sensor> lux 再 分支: $number(lux) < N，"
            f"光照度传感器实体不可省；\n"
            f"   ·『开某灯/关某灯/打开吊灯』等动作 → 动作目标实体必须写出（吊灯是 switch 域，用 switch.turn_on(switch.xxx)）；\n"
            f"   ·『工作日/晚上N点到M点』等时间限定 → 用 时间段:，但别因为加了时间条件就漏掉后面的触发实体或动作实体；\n"
            f"7. 只解析不提交 = 失败。解析→写全 DSL（触发+条件+动作齐全）→提交，一气呵成。\n"
            f"8. 【复杂场景（多分支/多动作/含查询+子流程）】：先在本轮脑内列出『触发→条件→各分支动作』清单，"
            f"再把完整 DSL 一次性写进单个代码块提交，**不要分多次调用 propose_dsl、也不要中途反复翻 help**；"
            f"多分支用嵌套 分支/否则，TTS/大模型调用用 调用子流程: demo_notify(...)，保持每个动作一行、参数精简。\n"
        )

    def _wait_ds_bridge_idle(self, max_wait: int = 45, settle: float = 4.0, job_id: str = None) -> Dict[str, Any]:
        """点火前等待 ds_bridge/Chrome deepseek++ 空闲。
        根治『连续场景竞合』：批量回归里上一场景 deepseek 刚提交(proposal 落库)后仅隔十几秒就
        点火下一场景，此时 Chrome 里 deepseek 还在收尾（打字『已完成』/旧会话未关），新 new_session
        请求被吞或未被处理 → 下一场景整轮空转到 timeout（单独跑却能秒过）。
        判定空闲：/api/status 的 busy==False 且 state ∈ {done, idle, ''}。达标后再 settle 秒缓冲。"""
        import http.client
        from urllib.parse import urlparse
        raw = (self.cfg.ds_bridge_url or "http://localhost:9090").rstrip("/") + "/api/status"
        parsed = urlparse(raw)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        deadline = time.time() + max_wait
        last_state = None
        self._emit(job_id, "dsb_idle", f"等待 DeepSeek++ 空闲（最多 {max_wait}s，防连续场景竞合）")
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", path)
                resp = conn.get_response() if hasattr(conn, "get_response") else conn.getresponse()
                body = resp.read()
                conn.close()
                st = json.loads(body.decode("utf-8", "ignore"))
                last_state = st.get("state")
                busy = bool(st.get("busy"))
                if (not busy) and (last_state in ("done", "idle", "", None)):
                    time.sleep(settle)  # 缓冲：让 Chrome 关旧会话/腾出输入框
                    self._emit(job_id, "dsb_idle_ok", f"DeepSeek++ 已空闲（state={last_state}，等待 {round(max_wait - (deadline - time.time()), 1)}s）")
                    return {"idle": True, "state": last_state, "waited": round(max_wait - (deadline - time.time()), 1)}
            except Exception as e:
                last_state = f"probe_err:{type(e).__name__}"
            time.sleep(3)
        # 超时仍未空闲：不阻断点火（宁可点火也别卡死回归），只回报
        self._emit(job_id, "dsb_idle_wait", f"未达空闲（state={last_state}），仍点火")
        return {"idle": False, "state": last_state, "waited": max_wait}

    def _fire_ds_bridge(self, prompt: str, fire_timeout: int = 15, retries: int = 3, job_id: str = None) -> Dict[str, Any]:
        # 发后不理（fire-and-forget）：ds_bridge 的 /api/chat 是同步阻塞的，
        # 会一直等到 deepseek++ 整轮（含 autoflow MCP 调用）跑完才返回，可能耗时数分钟。
        # 我们不在此等待响应，只要请求体成功送达即视为“点火”成功；
        # 仅当连接级失败（ds_bridge 没起）时才重试，避免重复点火。
        import http.client
        from urllib.parse import urlparse
        raw = (self.cfg.ds_bridge_url or "http://localhost:9090").rstrip("/") + "/api/chat"
        parsed = urlparse(raw)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        payload = json.dumps({"prompt": prompt, "new_session": True,
                              "role": "producer"}).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Content-Length": str(len(payload))}
        last_err = None
        self._emit(job_id, "dsb_fire", "触发 DeepSeek++（Chrome 驱动 ds_bridge）执行场景…")
        for _ in range(retries):
            conn = None
            try:
                conn = http.client.HTTPConnection(host, port, timeout=fire_timeout)
                conn.request("POST", path, body=payload, headers=headers)
                # 请求体已发出。尝试读取响应（仅用于快速识别 503 忙锁）。
                got_resp = False
                try:
                    resp = conn.get_response()
                    got_resp = True
                    if resp.status == 503:  # ds_bridge 忙，未真正点火 → 重试
                        last_err = "503 busy"
                        try:
                            conn.close()
                        except Exception:
                            pass
                        time.sleep(5)
                        continue
                    # 其余响应（含 200）→ 已点火
                    try:
                        resp.read(400)
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._emit(job_id, "dsb_fired", f"已点火：{raw} (http {resp.status})")
                    return {"ok": True, "fire": "delivered", "http_status": resp.status, "url": raw}
                except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
                    if got_resp:
                        break
                    last_err = f"{type(e).__name__}: {e}"
                    try:
                        if conn:
                            conn.close()
                    except Exception:
                        pass
                    time.sleep(2)
                    continue
                except Exception:
                    # 读响应超时/其他 IO 异常（ds_bridge 正在跑整轮）→ 请求体已送达，视为点火成功
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._emit(job_id, "dsb_fired", f"已点火（读响应超时，请求体已送达）：{raw}")
                    return {"ok": True, "fire": "delivered(timeout)", "url": raw}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                time.sleep(2)
        self._emit(job_id, "dsb_fail", f"点火失败：{last_err or 'unknown'}")
        return {"ok": False, "error": last_err or "unknown", "url": raw}

    def _wait_for_proposals(self, snap_len: int, timeout: int, job_id: str = None) -> List[Dict[str, Any]]:
        self._emit(job_id, "wait_start", f"等待闸门提案（最多 {timeout}s）…")
        deadline = time.time() + timeout
        last_tick = -1
        while time.time() < deadline:
            with _PROPOSAL_LOG_LOCK:
                if len(_PROPOSAL_LOG) > snap_len:
                    found = list(_PROPOSAL_LOG[snap_len:])
                    self._emit(job_id, "wait_found", f"收到 {len(found)} 条提案")
                    return found
            elapsed = int(time.time() - (deadline - timeout))
            bucket = elapsed // 30
            if bucket > last_tick:
                last_tick = bucket
                self._emit(job_id, "wait_tick", f"已等待 {elapsed}s，暂无提案…")
            time.sleep(3)
        self._emit(job_id, "wait_timeout", "超时内无提案")
        return []

    def _judge_golden(self, scen: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        proposed: set = set()
        for e in entries:
            proposed.update((e.get("data") or {}).get("entities", []))
        report = {
            "scenario": scen["id"], "nl": scen["nl"],
            "expect": scen["expect"], "forbid": scen["forbid"],
            "proposed_entities": sorted(proposed),
            "entries": [{"kind": e.get("kind"), "agent_id": e.get("agent_id"),
                         **{k: (e.get("data") or {}).get(k) for k in
                            ("scene_name", "gate_stage", "gate_passed", "proposal_id")}}
                        for e in entries],
        }
        # 等价实体组：期望实体 x 若有等价组，组内任一被提案即算命中（同一物理设备多集成表示）。
        equiv = self._parse_golden_equiv()

        def _hit(x: str) -> bool:
            if x in proposed:
                return True
            for g in equiv:
                if x in g and (proposed & g):
                    return True
            return False

        missing = [x for x in scen["expect"] if not _hit(x)]
        forbidden_hit = [x for x in scen["forbid"] if x in proposed]
        gate_blocked = any(
            (not (e.get("data") or {}).get("gate_passed")) and
            (e.get("data") or {}).get("gate_stage") == "resolve_whitelist"
            for e in entries)
        report["missing_expect"] = missing
        report["forbidden_hit"] = forbidden_hit
        report["gate_blocked"] = gate_blocked
        if not entries:
            report["verdict"] = "FAIL"
            report["reason"] = "deepseek++ 超时内未产生任何提案（可能未在线/未执行 autoflow/未提交）"
        elif missing or forbidden_hit or gate_blocked:
            reasons = []
            if missing:
                reasons.append(f"缺失期望实体: {missing}")
            if forbidden_hit:
                reasons.append(f"命中禁用实体(错配): {forbidden_hit}")
            if gate_blocked:
                reasons.append("闸门拦截(实体不在 resolved_entities 白名单)")
            report["verdict"] = "FAIL"
            report["reason"] = "；".join(reasons)
        else:
            report["verdict"] = "PASS"
            report["reason"] = "提案实体与 golden 期望一致，无禁用实体，闸门通过"
        return report

    @staticmethod
    def proposal_requires_review(policy: str, source: str) -> bool:
        """按部署策略决定提案是否需要人类审核（按提案来源 source 分流）。

        - ``review_all``（默认）：所有提案都需人类在 WebUI 审核后部署，行为不变。
        - ``compiler_auto``：来源为 ``compiler``（编译器产物、且闸门已通过）视为可自动部署，
          无需人审；``raw``（原生手写）/``unknown`` 永远需人审。
        - 未知策略值一律回退为需人审（fail-safe）。

        这是「按来源分流部署策略」的雏形：WebUI（P4）将据此给提案打
        「编译产物(可信)/手写(需审)」徽章，并在 compiler_auto 下对 compiler 提案放行自动部署。
        """
        if policy == "compiler_auto":
            return source != "compiler"
        return True  # review_all 及未知策略 → 总是需人审

    def deploy_proposal(self, pid: str, agent_id: str = "human",
                        target_flow_id: Optional[str] = None,
                        target: str = "prod", force: bool = False,
                        validate: bool = True, allow_prod: bool = True,
                        vhass_store=None,
                        dry_run: bool = False,
                        require_e2e: Optional[bool] = None) -> Dict[str, Any]:
        """把已通过的 DSL 提案直接部署到 NR（一步确认，不再走冗余确认闸）。

        流程：重新编译 DSL（真相源）→ 冲突检测（同名非本流拒绝/force 改名）
        → 解析 HA server id（配置或 NR 自动探测）→ create_or_update_flow
        → 登记 flow_catalog + 标记提案已部署。
        target：所有 target（含 staging/e2e）状态触发器均编译为 server-state-changed
        （含 for 持久等待，WB4 #2 修复）；"staging" 在 e2e 时由 _e2e_prepare_flow
        原地转合成 inject 点燃，"prod" 走真实 HA 事件。
        返回 {ok, flow_id, created, node_count, server_resolved}；冲突/编译/防御失败 ok=False。
        """
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        _slog(_tid, "deploy_proposal.start", pid=pid, agent_id=agent_id,
              target=target, validate=validate)
        store = ProposalStore(self.cfg)
        p = store.get(pid)
        if p is None:
            r = {"ok": False, "error": f"提案不存在: {pid}"}
            r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                          extra={"pid": pid}, log_path=self._telemetry_log)
            return r
        # 部署策略：按当前策略 + 提案来源决定是否需要人类审核（仅影响徽章/提示，
        # 实际部署始终由 WebUI 人工点击触发，且仍过 staging 闸门——绝不无人值守部署）。
        policy = get_deploy_policy(self.cfg)
        requires_review = self.proposal_requires_review(policy, p.source)
        try:
            content = json.loads(p.content) if isinstance(p.content, str) else p.content
        except Exception as e:
            r = {"ok": False, "error": f"提案内容解析失败: {e}"}
            r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                          extra={"pid": pid}, log_path=self._telemetry_log)
            return r

        ctype = content.get("type", "dsl")
        if ctype == "subflow":
            # 子流程提案：人审通过后原子注册（写 NR 子流程实例 + 登记 subflow_registry）。
            # 不走 DSL/flow 部署路径（无 staging 闸门 / HA server 注入 / flow_catalog / e2e 闸）。
            return self._deploy_subflow_proposal(p, content, agent_id, force, dry_run,
                                                _tid, _t0, allow_prod=allow_prod)
        if ctype == "raw_flow":
            # 白盒提案：使用落档时的原始 flow（已做 HA server 替换），无需重新编译
            flow = content.get("flow")
            if not isinstance(flow, dict) or "nodes" not in flow:
                r = {"ok": False, "error": "raw_flow 提案不含合法 flow，无法部署"}
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r
            # 用落档时记录的 target（白盒流 trigger 已内嵌；其 target 是固有语义，
            # 调用方默认 prod 不应覆盖白盒 staging 流）
            target = content.get("target", target)
            # WB 健壮性修复：raw_flow 提案的 flow 常省略顶层 id（依赖 _gen_raw_flow_id 生成），
            # 但下方 defense.check_write(flow_id=flow["id"]) 用下标访问，
            # 在 dry_run=False 时会 KeyError 冒泡成 500。此处提前补 id，
            # 既避免崩溃，也保证 operation/defense 阶段 id 一致。
            # 3514 段的 _remap_raw_flow_ids + flow["id"]=deploy_id 会幂等覆盖（更新已有流场景）。
            if not flow.get("id"):
                flow["id"] = content.get("id") or self._gen_raw_flow_id(agent_id, flow)
        else:
            # DSL 提案：重新编译（DSL 是真相源；target=prod 生成真实 HA 事件触发器）
            dsl = content.get("dsl")
            if not dsl:
                r = {"ok": False, "error": "提案不含 DSL，无法部署"}
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r
            try:
                from .dsl_engine import set_entity_resolver, set_entity_attributes_resolver, DSLError
                set_entity_resolver(lambda t: self.state.resolve(t))
                set_entity_attributes_resolver(lambda eid: self._entity_attribute_names(eid))
                scene = _dsl_parse(dsl)
                flow = _dsl_compile(scene, target=target)
                _slog(_tid, "deploy_proposal.compiled", nodes=len(flow.get("nodes", [])),
                      elapsed=round(time.perf_counter() - _t0, 3))
            except DSLError as e:
                r = {"ok": False, "stage": "compile", "error": f"编译失败: {e}",
                     "compile_error": _compile_error_envelope(e),
                     "result_kind": "compile_error"}
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r
            except Exception as e:
                r = {"ok": False, "stage": "compile", "error": f"编译异常: {e}"}
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r

        # ── 部署前 staging 闸门（vhass 重放断言）──
        # 护城河：先在本网关内存孪生里重放 flow 的 HA 意图并断言后置条件；
        # 不通过则拒绝部署（绝不把未验证的 flow 落 NR）。无预期条件时跳过（降级，不阻塞）。
        expected = content.get("expected_postconditions") or []
        if validate and expected and not dry_run:
            # 生产落提案闸门启用分支感知（branch_aware 默认 True）：
            # 嵌套门孤儿接线 bug 已修复，编译产物连线正确，分支感知可顺线评估
            # 门控、只重放命中分支的意图，不再因接线弱点误杀正常流程。
            gate = self.run_staging_gate(dsl, expected, vhass_store=vhass_store)
            if not gate.get("passed"):
                r = {
                    "ok": False,
                    "stage": "gate",
                    "gate_passed": False,
                    "gate": gate,
                    "error": "staging 闸门未通过，未部署。请检查 DSL 与预期后置条件是否一致（"
                             + gate.get("error", "断言不通过") + "）",
                }
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r

        label = flow.get("label", "未命名场景")

        # ── 冲突检测：同名 flow 已存在且不是本网关部署的 → 拒绝覆盖用户已有 flow ──
        existing = None
        for f in self.nr.list_flows():
            if f.get("label") == label:
                existing = f
                break
        if existing and existing.get("id") not in self.state.get_flow_catalog().get("flows", {}):
            if not force:
                return {
                    "ok": False, "conflict": True,
                    "error": f"NR 中已存在同名 flow「{label}」({existing.get('id')})，且非本网关部署，避免覆盖。可改名后重试，或 force=true 以新建副本。",
                    "existing": {"id": existing.get("id"), "label": label},
                }
            # force：改名新建副本，绝不覆盖用户已有 flow
            # 注意后缀避开受保护标签（protected_flow_labels 含 "AutoFlow"，子串匹配会触发拒绝）
            label = f"{label} (网关副本)"
            flow["label"] = label

        # ── 解析 HA server id：优先配置，否则自动探测 NR 中第一个 server 节点 ──
        ha_server, unresolved = self._inject_ha_server(flow)
        if unresolved:
            return {"ok": False, "stage": "ha_server_inject",
                    "error": self._ha_server_unresolved_msg(unresolved)}

        operation = "update_flow" if target_flow_id else "create_flow"

        # 防御层检查（与 commit_scene 一致；dry-run 跳过——预览不写不需过闸）
        if not dry_run:
            try:
                owner = None
                if target_flow_id:
                    meta = self.state.get_flow_meta(target_flow_id)
                    owner = meta.get("owner_agent") if meta else None
                self.defense.check_write(
                    operation=operation,
                    flow_id=flow.get("id"),
                    label=flow["label"],
                    owner_agent=owner,
                    acting_agent=agent_id,
                    flows_touched=1,
                )
            except DefenseError as e:
                return {"ok": False, "error": f"defense: {e}"}

        # 节点注册表闸门（P0 防御）：未知节点类型直接报错，不让坏 flow 上线
        self._gate_node_types(flow)

        # 白盒 raw_flow：部署前重映射节点 id + z（消化 Agent 占位符，避免 NR duplicate id）
        if ctype == "raw_flow":
            deploy_id = target_flow_id or self._gen_raw_flow_id(agent_id, flow)
            flow, _id_map, _had_z = self._remap_raw_flow_ids(flow, deploy_id)
            flow["id"] = deploy_id
        else:
            deploy_id = flow["id"]

        # ── D4/G2 link-out 目标校验（与 deploy_raw Step 2.7 一致）──
        # 提案部署路径此前绕过 deploy_raw（独立重写的管道），缺失此校验 →
        # WB22 T3 暴露：含悬空 link out 的提案被直接部署，运行时才报
        # 『Error delivering message to node:undefined』。此处补齐覆盖。
        # fail-open：无 NR client / 连接异常时返回空（与 deploy_raw 一致）。
        _link_errs = self._validate_link_out_targets(flow)
        if _link_errs and not dry_run:
            _slog(_tid, "deploy_proposal.link_out_unresolved",
                  errors=[e.get("message") for e in _link_errs])
            r = {
                "ok": False, "stage": "link_out_unresolved",
                "error": ("存在指向不存在 link-in 的 link out 节点（部署后运行时将报 "
                          "『Error delivering message to node:undefined』）："
                          + "; ".join(e.get("message") for e in _link_errs)),
                "link_errors": _link_errs,
            }
            r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                          extra={"pid": pid}, log_path=self._telemetry_log)
            return r

        # A8：dry-run 预览——编译/替换/闸门已完成，flow 即将部署的最终形态；拉线上做节点级 diff，不落 NR。
        if dry_run:
            live = None
            if target_flow_id:
                try:
                    live = self.nr.get_flow(target_flow_id)
                except Exception:
                    live = None
            node_diff = _build_node_diff(live, flow)
            _slog(_tid, "deploy_proposal.dry_run", elapsed=round(time.perf_counter() - _t0, 3),
                  would="update" if live is not None else "create",
                  added=len(node_diff["added"]), removed=len(node_diff["removed"]),
                  changed=len(node_diff["changed"]))
            return {
                "ok": True,
                "dry_run": True,
                "would": "update" if live is not None else "create",
                "flow_id": target_flow_id or flow["id"],
                "label": flow.get("label", ""),
                "node_count": len(flow.get("nodes", [])),
                "summary": self.compute_flow_diff(flow, live),
                "node_diff": node_diff,
                "server_resolved": bool(ha_server),
                "link_errors": _link_errs,
                "_trace_id": _tid,
            }

        # ── 直接部署（DSL 已 staging 闸门验证过，不再进确认闸）──
        # 白盒 raw_flow：部署前硬拦 Lint 反模式（与 deploy_raw 一致）。
        # propose_raw 是 fail-open 落档（只报告不拦），若不在此补刀，坏 flow
        #（如 api-current-state 空 entityId → R20）会直接上线、且「重新部署」反复推送。
        if ctype == "raw_flow":
            _LINT_BLOCK_RULES = {"R10", "R13", "R15", "R20", "R17", "R22", "R24", "R30", "R32", "R_SERVICE_PARAM", "R36", "R2-ESC", "R_NO_TRIGGER", "R19", "R16", "R40"}
            _li = lint_flow(flow)
            _blk = [v for v in _li
                    if v.get("level") == "error" and v.get("rule") in _LINT_BLOCK_RULES]
            if _blk:
                r = {"ok": False, "stage": "lint",
                     "error": "raw_flow 含阻断级 Lint 问题，拒绝部署（请修正后重提/重编译）",
                     "issues": _blk}
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r

        # ── E2E 实机验证闸（修复 iss_8d3cffaa96）──
        # require_e2e=None 时继承提案落档意图（content.require_e2e），再无则默认关。
        # 此前该意图被 JSON-RPC 静默吞掉，且主部署路径 deploy_proposal 从不调 e2e 闸
        #（#613 焊在 deploy_raw 的闸对提案部署是死代码）。此处把闸真正焊到部署主路径。
        # 逻辑与 deploy_raw Step 6.5 完全一致：仅当「真实跑通且 verdict=通过」才放行；
        # verdict=断点阻止部署逼修 flow；无法验证（e2e=False）fail-open 放行，避误伤。
        if require_e2e is None:
            require_e2e = bool(content.get("require_e2e", False))
        _e2e = None  # 默认未运行；仅当 require_e2e 开启且非 dry_run 才赋值（避免成功返回 NameError）
        if require_e2e and not dry_run:
            try:
                _e2e = self.run_e2e_trace_raw(flow, target=target, live=False, allow_prod=allow_prod)
            except Exception as _ee:
                _slog(_tid, "deploy_proposal.e2e_gate_err",
                      elapsed=round(time.perf_counter() - _t0, 3), error=str(_ee)[:200])
                _e2e = {"e2e": False, "verdict": "拦截", "error": f"E2E 验证异常：{_ee}"}
            if _e2e.get("e2e") is True and _e2e.get("verdict") != "通过":
                _slog(_tid, "deploy_proposal.e2e_gate_block",
                      elapsed=round(time.perf_counter() - _t0, 3), verdict=_e2e.get("verdict"))
                r = {
                    "ok": False, "stage": "e2e_gate",
                    "error": (f"E2E 实机验证未通过（verdict={_e2e.get('verdict')}），已阻止部署。"
                              f"请修复 flow 后重试，或先单独调 autoflow_run_e2e_trace 定位断点。"),
                    "e2e": _e2e,
                    "proposal_id": pid,
                }
                r["_telemetry"] = _tag_action("deploy_proposal", r, agent_id,
                                              extra={"pid": pid}, log_path=self._telemetry_log)
                return r
            # fail-open：e2e=False（无法验证）或 verdict=通过 → 继续部署

        # D5-d（C6）：caller 显式指定 target_flow_id 时，部署前校验该 flow 在 NR 中确实存在。
        # 与 deploy_raw 一致逻辑：静默创建会造成 caller 以为在更新、实际新建副本的幻觉。
        if target_flow_id and not dry_run:
            try:
                self.nr.get_flow(target_flow_id)
            except Exception:
                return {
                    "ok": False,
                    "stage": "not_found",
                    "error": (
                        f"target_flow_id `{target_flow_id}` 在 Node-RED 中不存在。"
                        f"如果你要新建 flow，请省略 target_flow_id；"
                        f"如果要更新已有 flow，请确认 id 正确。"
                    ),
                    "hint": (
                        "target_flow_id 指定了一个 NR 中不存在的 flow。"
                        "省略此参数将以新 flow 创建。"
                    ),
                    "category": "not_found",
                    "not_found_flow_id": target_flow_id,
                    "proposal_id": pid,
                }

        try:
            result = self.nr.create_or_update_flow(deploy_id, flow, force=True,
                                                  allow_prod=allow_prod)
        except Exception as e:
            return {"ok": False, "error": f"NR 部署失败: {e}"}
        fid = result.get("id") or deploy_id
        created = result.get("created", False)

        # 登记 flow_catalog（owner=部署它的 agent）—— 撤回的唯一依据
        # deployed_node_ids：compile 产出的全部节点 id，撤回时只删这些（手术式移除）
        gateway_node_ids = [n.get("id") for n in flow.get("nodes", []) if n.get("id")]
        meta = {
            "flow_id": fid,
            "label": flow.get("label", ""),
            "owner_agent": agent_id,
            "purpose": flow.get("info", ""),
            "entities_touched": self._collect_entities(flow),
            "node_count": len(flow.get("nodes", [])),
            "deployed_node_ids": gateway_node_ids,
            "source_proposal": pid,
            "source": (p.source if p is not None else "compiler"),
            "nr_url": getattr(self.cfg, "nr_url", ""),
            "deployed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.upsert_flow(fid, meta)
        ProposalStore(self.cfg).mark_deployed(pid, fid)

        _slog(_tid, "deploy_proposal.done", elapsed=round(time.perf_counter() - _t0, 3),
              flow_id=fid, created=created, node_count=len(flow.get("nodes", [])))
        # D5-a（C5）：回显 authored→minted 映射，与 deploy_raw 一致
        _resp = {
            "ok": True,
            "flow_id": fid,
            "created": created,
            "_trace_id": _tid,
            "label": flow.get("label", ""),
            "node_count": len(flow.get("nodes", [])),
            "server_resolved": bool(ha_server),
            "gate_passed": bool(expected and validate),
            "require_e2e": bool(require_e2e),
            "gate": self._build_unified_gate(None, _e2e, None),
            "deployed_at": meta["deployed_at"],
            "deploy_policy": policy,
            "requires_review": requires_review,
            "_telemetry": _tag_action(
                "deploy_proposal", {"ok": True}, agent_id,
                extra={"pid": pid, "flow_id": fid, "label": flow.get("label", "")},
                log_path=self._telemetry_log),
        }
        if fid != deploy_id:
            _resp["authored_id"] = deploy_id
            _resp["minted_id"] = fid
        return _resp

    def _deploy_subflow_proposal(self, p, content: Dict[str, Any], agent_id: str,
                                force: bool, dry_run: bool, tid: str, t0: float,
                                allow_prod: bool = True) -> Dict[str, Any]:
        """子流程提案的注册分支（由 deploy_proposal 在人审通过后调用）。

        原子完成两步：
          1. 写 NR 子流程实例（NRLayer.create_subflow，增量 append，不整实例替换）
          2. 登记 subflow_registry（DSL 调用名 dsl_name 为主键，kind=subflow）
        两步任一步失败返回 ok=False；成功则 mark_deployed(pid, subflow_id)。
        """
        dsl_name = content.get("dsl_name")
        name = content.get("name") or dsl_name
        definition = content.get("definition") or {}
        subflow_id = definition.get("id")
        if not dsl_name or not subflow_id:
            r = {"ok": False, "error": "子流程提案缺少 dsl_name 或 definition.id"}
            r["_telemetry"] = _tag_action("deploy_subflow_proposal", r, agent_id,
                                          extra={"pid": p.id}, log_path=self._telemetry_log)
            return r

        # 冲突检测：NR 已存在同名 id 子流程 → 拒绝覆盖（除非 force）
        if not force:
            try:
                _live = self.nr.list_flows()
                _existing_ids = {n.get("id") for n in _live if isinstance(n, dict)}
            except Exception:
                _existing_ids = set()
            if subflow_id in _existing_ids:
                r = {"ok": False, "conflict": True,
                     "error": f"NR 中已存在同 id 子流程「{subflow_id}」，避免覆盖。"
                              f"可改名后重试，或 force=true 重建。"}
                r["_telemetry"] = _tag_action("deploy_subflow_proposal", r, agent_id,
                                              extra={"pid": p.id}, log_path=self._telemetry_log)
                return r

        if dry_run:
            return {
                "ok": True, "dry_run": True,
                "subflow_id": subflow_id, "dsl_name": dsl_name,
                "node_count": len(definition.get("nodes", [])),
                "_trace_id": tid,
            }

        # 第 1 步：写 NR 子流程实例（增量 append，不整实例替换）。
        # allow_prod 透传人类在 WebUI「部署到 NR」时的授权（与 flow 部署路径一致）；
        # prod 下需 allow_prod=True 才放行 _guard_prod，否则沿用 deploy_proposal 的默认 True。
        try:
            self.nr.create_subflow(
                subflow_id, name,
                definition.get("in_ports", []), definition.get("out_ports", []),
                definition.get("nodes", []),
                info=definition.get("info", content.get("description", "")),
                category=definition.get("category", "subflows"),
                env=definition.get("env"),
                allow_prod=allow_prod,
            )
        except Exception as e:
            r = {"ok": False, "stage": "nr_create_subflow", "error": f"NR 子流程创建失败: {e}"}
            r["_telemetry"] = _tag_action("deploy_subflow_proposal", r, agent_id,
                                          extra={"pid": p.id}, log_path=self._telemetry_log)
            return r

        # 第 2 步：登记 subflow_registry（DSL 调用名 dsl_name 为主键）
        reg = self.tasks.register_subflow(
            key=dsl_name, title=name, nr_subflow_id=subflow_id,
            source_type="imported", owner=(p.agent_id or ""),
            status="active", spec_ref=content.get("description", ""),
        )
        if not reg.get("ok"):
            # NR 子流程已建、但注册表写入失败：返回 ok=False 并带上 nr id 供运维诊断
            # （多数情况是 metadata 写入问题，子流程本身已在 NR，可手动 register 补救）。
            r = {"ok": False, "stage": "register_subflow",
                 "error": reg.get("error", "登记子流程注册表失败"),
                 "subflow_id": subflow_id}
            r["_telemetry"] = _tag_action("deploy_subflow_proposal", r, agent_id,
                                          extra={"pid": p.id}, log_path=self._telemetry_log)
            return r

        ProposalStore(self.cfg).mark_deployed(p.id, subflow_id)
        _slog(tid, "deploy_subflow_proposal.done", elapsed=round(time.perf_counter() - t0, 3),
              subflow_id=subflow_id, dsl_name=dsl_name)
        return {
            "ok": True,
            "subflow_id": subflow_id,
            "dsl_name": dsl_name,
            "registered": True,
            "node_count": len(definition.get("nodes", [])),
            "label": name,
            "_trace_id": tid,
            "_telemetry": _tag_action("deploy_subflow_proposal", {"ok": True}, agent_id,
                                      extra={"pid": p.id, "subflow_id": subflow_id,
                                             "dsl_name": dsl_name},
                                      log_path=self._telemetry_log),
        }

    def list_deployed(self, stale_check: bool = False) -> List[Dict[str, Any]]:
        """列出本网关部署过的 flow（flow_catalog 中只有 owner 是 agent 的）。

        stale_check=True 时附带「注册表 ↔ NR 分叉对账」（WB5#1b / #552）：
        拉一次 NR 活 flow 清单，逐行标记 stale（注册表记已部署、但 NR 实例里
        已无此 flow_id，可能已被手动删除/重命名/切换实例）。NR 不可达时
        try/except 跳过，不加 stale 字段（不阻断列表本身）。
        """
        cat = self.state.get_flow_catalog().get("flows", {})
        live_ids = None
        if stale_check:
            try:
                live_ids = {f.get("id") for f in self.nr.list_flows() if f.get("id")}
            except Exception:
                live_ids = None
        out = []
        for fid, meta in cat.items():
            if not meta.get("owner_agent"):
                continue
            m = dict(meta)
            m["flow_id"] = fid
            if live_ids is not None:
                m["stale"] = fid not in live_ids
            out.append(m)
        out.sort(key=lambda x: x.get("deployed_at", ""), reverse=True)
        return out

    def list_automations(self, keyword: Optional[str] = None, only: str = "all",
                         limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """【Automations 注册表·跨会话找回】统一列出本网关建过的自动化（编译器 DSL 路径 + 原生手写路径），
        供 agent 写新自动化前查重、或跨会话找回自己/其他 agent 建过的东西。

        范围（用户确认：仅 flow 自动化）：
        - 已部署：flow_catalog 中 owner 是 agent 的 flow（state="deployed"）；
          source 经 source_proposal 反查 ProposalStore，spec 优先取提案 spec（回退 purpose/label）。
        - 待审：ProposalStore 中 source∈{compiler,raw} 且 status≠rejected 的提案（state="pending"）。
          网关改进类经验提案（convention/fix/idea，source=unknown）不混入。

        - keyword：对 title + spec 不区分大小写模糊匹配（留空=不过滤）。
        - only："all"（默认）/"deployed"/"pending" 限定来源。
        - limit/offset：分页（默认 50，上限 200），防提案多时撑爆上下文。
        返回 {automations:[{id,title,state,source,spec,created_at,flow_id}],
              matched_count, returned, truncated, next_offset, total}。
        """
        if only not in ("all", "deployed", "pending"):
            only = "all"
        try:
            limit = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0

        store = ProposalStore(self.cfg)
        rows: List[Dict[str, Any]] = []

        # ── 已部署 ──
        if only in ("all", "deployed"):
            # 注册表 ↔ NR 分叉对账（WB5#1b）：拉一次活 flow 清单，逐行标记 stale。
            # NR 不可达时 try/except 跳过，不加 stale 字段（不阻断列表本身）。
            live_ids = None
            try:
                live_ids = {f.get("id") for f in self.nr.list_flows() if f.get("id")}
            except Exception:
                live_ids = None
            for m in self.list_deployed():
                fid = m.get("flow_id", "")
                src = "unknown"
                spec = m.get("purpose") or m.get("label") or ""
                sp = m.get("source_proposal")
                if sp:
                    p = store.get(sp)
                    if p is not None:
                        src = p.source
                        if p.spec:
                            spec = p.spec
                row = {
                    "id": sp or fid,
                    "title": m.get("label") or fid,
                    "state": "deployed",
                    "source": src,
                    "spec": spec,
                    "created_at": m.get("deployed_at", ""),
                    "flow_id": fid,
                }
                if live_ids is not None:
                    row["stale"] = fid not in live_ids
                rows.append(row)

        # ── 待审（仅 flow 自动化）──
        # 判定：编译器/原生手写路径(source 标记) 一律算；或遗留 P0 前提交、source 未标记但 kind=skill
        # 的 flow 提案也算（511 条历史提案即此类）；网关改进类经验提案(convention/fix/idea)不论 source 都不混入。
        if only in ("all", "pending"):
            for p in store.list():
                if p.source in ("compiler", "raw"):
                    pass
                elif p.source == "unknown" and p.kind == "skill":
                    pass
                else:
                    continue
                if p.status == "rejected":
                    continue
                if p.deployed_flow_id:
                    continue  # 已部署的以 deployed 行呈现，避免重复
                rows.append({
                    "id": p.id,
                    "title": p.title,
                    "state": "pending",
                    "source": p.source,
                    "spec": p.spec,
                    "created_at": p.created_at,
                    "flow_id": p.deployed_flow_id,
                })

        # ── 关键词过滤 ──
        if keyword:
            kw = keyword.lower()
            rows = [r for r in rows
                    if kw in (r["title"] or "").lower() or kw in (r["spec"] or "").lower()]

        # ── 排序（created_at 倒序）+ 分页 ──
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        matched_count = len(rows)
        page = rows[offset:offset + limit]
        truncated = (offset + limit) < matched_count
        return {
            "automations": page,
            "matched_count": matched_count,
            "returned": len(page),
            "truncated": truncated,
            "offset": offset,
            "next_offset": (offset + limit) if truncated else None,
            "total": matched_count,
        }

    # ── 白盒部署（双模路径：跳过 DSL 编译，直接接受 Agent 产出的 flow JSON）──

    def validate_flow_schema(self, flow: Dict) -> List[Dict[str, str]]:
        """对原始 flow JSON 做字段级 schema 校验（不阻塞，返回 warnings/errors）。

        检查项：
        - 顶层结构（id/label/nodes 存在）
        - 节点必要字段（type/id/z/wires）
        - http request 节点：method/url + POST 时 body/bodyType
        - HA 节点：server/domain/service/action
        - switch 节点：rules/property
        - change 节点：rules
        返回 [{"level":"error|warning","node_id":...,"message":...}] 列表。

        【R9】致命项额外带 "rule": S1..S5（见 SCHEMA_BLOCK_RULES），调用方用
        schema_blocking_issues() 提取后硬拦部署 / 置 will_deploy_block=True。
        """
        issues = []

        if not isinstance(flow, dict):
            return [{"level": "error", "rule": "S1", "node_id": "_root",
                     "message": "flow 必须是 JSON 对象"}]

        for field in ("id",):
            if not flow.get(field):
                issues.append({"level": "error", "node_id": "_root",
                               "message": f"flow 缺少顶层字段: {field}"})

        nodes = flow.get("nodes")
        if not isinstance(nodes, list):
            issues.append({"level": "error", "rule": "S1", "node_id": "_root",
                           "message": "flow.nodes 必须是数组"})
            return issues
        if not nodes:
            # R9/S5：空 flow 覆盖已有 tab = 静默清空线上内容，破坏性最强，必须硬拦。
            issues.append({"level": "error", "rule": "S5", "node_id": "_root",
                           "message": "空 flow（nodes 为空数组）：部署到已有 tab 会清空其全部"
                                      "节点。若确要清空请显式走删除/回滚接口。"})
            return issues

        node_ids = set()
        for i, n in enumerate(nodes):
            nid = n.get("id") or f"_index_{i}"
            ntype = n.get("type", "?")

            # 基础字段
            if not n.get("id"):
                issues.append({"level": "warning", "node_id": nid,
                               "message": f"[{ntype}] 缺少 id"})
            if not n.get("type"):
                issues.append({"level": "error", "rule": "S2", "node_id": nid,
                               "message": "节点缺少 type 字段"})
            elif n.get("id"):
                if n["id"] in node_ids:
                    issues.append({"level": "error", "node_id": nid,
                                   "message": f"重复节点 id: {n['id']}"})
                node_ids.add(n["id"])
            if not n.get("z"):
                issues.append({"level": "warning", "node_id": nid,
                               "message": f"[{ntype}] 缺少 z (所属 flow/tab)"})
            elif self._is_placeholder_flow_id(str(n.get("z", ""))):
                issues.append({"level": "warning", "node_id": nid,
                               "message": f"[{ntype}] z='{n.get('z')}' 是占位符，"
                                          f"部署时会重写为目标 flow id 并重新生成节点 id"})
            if "wires" not in n:
                issues.append({"level": "warning", "node_id": nid,
                               "message": f"[{ntype}] 无 wires（可能是终节点）"})

            # ── http request 特殊校验 ──
            if ntype == "http request":
                if not n.get("url"):
                    issues.append({"level": "error", "node_id": nid,
                                   "message": "[http request] 缺少 url"})
                method = (n.get("method") or "GET").upper()
                if method == "POST":
                    body = n.get("body")
                    bt = n.get("bodyType")
                    if not body and not bt:
                        issues.append({"level": "error", "node_id": nid,
                                       "message": "[http request] POST 但无 body/bodyType！"
                                                "NR 会发送 msg.payload 作为请求体（通常是日期/字符串），导致 API 422 错误。"
                                                "请添加 body (JSON 对象) 和 bodyType:'json'。"})
                    elif body is not None:
                        if not isinstance(body, (dict, list)):
                            issues.append({"level": "error", "node_id": nid,
                                           "message": f"[http request] body 类型错误:"
                                                    f" {type(body).__name__}，必须是 dict/list（JSON 对象）。"
                                                    f"不能是字符串——会被双层转义导致 NR 发送无效值。"})
                    if not n.get("headers") and "Authorization" not in (
                        h.get("key", "") for h in (n.get("headers") or [])
                    ):
                        # 仅提示，不阻塞（有些 API 不需要认证）
                        pass

            # ── HA 节点校验 ──
            if ntype in ("api-call-service", "server-state-changed"):
                if not n.get("server"):
                    # R9/S3：_inject_ha_server 只把 REPLACE_WITH_HA_SERVER 占位符换成真实 id，
                    # 「压根没有 server 字段」不在其修补范围 → 部署后节点永久未配置，必须硬拦。
                    issues.append({"level": "error", "rule": "S3", "node_id": nid,
                                   "message": f"[{ntype}] 缺少 server（HA 配置节点 id）。"
                                              f"请填真实 server id，或填占位符 "
                                              f"REPLACE_WITH_HA_SERVER 由网关注入。"})
                if ntype == "api-call-service":
                    if not n.get("action") and not n.get("domain"):
                        issues.append({"level": "error", "node_id": nid,
                                       "message": "[api-call-service] 缺少 action 或 domain+service"})

            # ── switch 校验 ──
            if ntype == "switch":
                rules = n.get("rules")
                if not rules:
                    issues.append({"level": "error", "rule": "S4", "node_id": nid,
                                   "message": "[switch] 缺少 rules"})
                else:
                    outputs = len(n.get("wires", []))
                    if outputs != len(rules):
                        # R9/S4：分支与出线错位 → 整枝消息静默丢失，静态合法运行必错。
                        issues.append({"level": "error", "rule": "S4", "node_id": nid,
                                       "message": f"[switch] wires({outputs}) 与 "
                                                f"rules({len(rules)}) 数量不匹配"})

            # ── change 校验 ──
            if ntype == "change":
                rules = n.get("rules")
                if not rules:
                    issues.append({"level": "warning", "node_id": nid,
                                   "message": "[change] 无 rules（空 change）"})

        # ── 安装即对账 · 字段级规则覆盖检测（R28, warning）──
        # 节点类型已在目标 NR 注册（node_gate 已拦截未注册类型），但网关对其
        # 没有字段级校验规则 → 静默原样部署可能缺必填字段。这里显式告警点名，
        # 把「fail-open = 不报错 ≠ 没问题」变成可见信号（对应需求报告方案 a）。
        # 仅网关侧、持有 NR 上下文时触发；未注册类型仍由 node_gate 硬拦，不双报。
        from .flow_linter import (
            _ORIGIN_TYPES, _ENTRY_TYPES, _CONFIG_TYPES, _DOWNSTREAM_TYPES,
            _CONFIG_PREFIXES,
        )
        _RECOGNIZED = (
            _ORIGIN_TYPES | _ENTRY_TYPES | _CONFIG_TYPES | _DOWNSTREAM_TYPES
            | {"http request", "api-call-service", "server-state-changed",
               "api-current-state", "switch", "change", "function", "template",
               "delay", "debug", "inject", "link in", "link out", "catch",
               # 注：不含 "merge"——NR 核心无此类型（见 dsl_engine.RAW_NODE_ALLOWED 注释）
               "status", "split", "join", "csv", "json", "xml", "html",
               "markdown", "range", "exec", "file", "mqtt in", "mqtt out",
               "tcp in", "udp in", "email", "http in", "websocket in", "time",
               "trigger", "comment", "subflow"}
        )
        try:
            _cli = getattr(self.nr, "client", None)
            _gtr = getattr(_cli, "get_installed_node_types", None) if _cli else None
            _installed = set(_gtr()) if _gtr else set()
        except Exception:
            _installed = set()
        if _installed:
            _HEX = re.compile(r"^[0-9a-fA-F]{24}$")
            for _t in sorted({n.get("type") for n in nodes if n.get("type")}):
                if (_t in _RECOGNIZED or _t.startswith(_CONFIG_PREFIXES)
                        or _t.startswith("subflow:") or _HEX.match(_t)):
                    continue
                if _t in _installed:
                    issues.append({
                        "level": "warning", "rule": "R28",
                        "node_id": "_type", "node_type": _t,
                        "message": f"节点类型『{_t}』已在目标 NR 注册，但网关无字段级"
                                   f"校验规则，将按原样部署；若为自定义/新装节点，请"
                                   f"自行确认必填字段已正确填写，否则部署后静默失效。",
                    })

        # ── 安装即对账 · 子流程 hash 漂移检测（R29, warning）──
        # 拉 NR 当前已定义子流程清单（name+id），交给 flow_linter.detect_subflow_drift
        # 判定引用是否指向已升级的旧 hash（对应需求报告方案 b）。离线/取不到 → 跳过。
        try:
            _cli = getattr(self.nr, "client", None)
            _req = getattr(_cli, "_request", None) if _cli else None
            _cur = []
            if _req is not None:
                _resp = _req("GET", "/flows")
                _data = json.loads(_resp.text) if hasattr(_resp, "text") else _resp
                _flows = _data if isinstance(_data, list) else (_data.get("flows")
                                                              if isinstance(_data, dict) else [])
                for _f in _flows:
                    if isinstance(_f, dict) and _f.get("type") == "subflow":
                        _cur.append({"id": _f.get("id"), "name": _f.get("name")})
            from .flow_linter import detect_subflow_drift
            issues.extend(detect_subflow_drift(nodes, _cur))
        except Exception:
            pass

        return issues

    # ── 白盒 sanitize 辅助 ──

    @staticmethod
    def _is_placeholder_flow_id(fid: str) -> bool:
        """判断 flow/tab id 是否是 Agent 常见的占位符（而非真实 NR 16 位 hex id）。"""
        if not fid:
            return True
        s = str(fid).strip().lower()
        if re.fullmatch(r"[0-9a-f]{16}", s):  # 真实 NR flow id 形态
            return False
        if s in ("1", "0", "flow", "new", "test", "untitled", "tab", "main",
                 "flow1", "flow_1", "myflow", "demo"):
            return True
        if len(s) <= 3:  # "1"/"abc" 这种太短，几乎肯定是占位符
            return True
        return False

    def _gen_raw_flow_id(self, agent_id: str, flow: Dict) -> str:
        """白盒部署时如果没有指定 target_flow_id，生成一个合法 NR flow id（16 位 hex）。

        NR 只在 flow id 为 16 位 hex 时才会采纳我们传入的 id；否则会重新生成，
        导致节点 z 与真实 flow id 不匹配、节点被丢到错误/缺失的 tab。
        """
        return secrets.token_hex(8)

    def _remap_raw_flow_ids(self, flow: Dict, target_flow_id: str):
        """白盒部署核心 sanitize：把 Agent 产出的 flow 重映射为 NR 可安全部署的格式。

        - 所有节点 id → 每次部署独立的全局唯一 id（避免跨次部署 NR `duplicate id`）
        - 所有节点 z → target_flow_id（消化占位符 z 如 "1"/"flow"）
        - wires / links 内部引用同步改写（外部 link 如 TTS 队列 id 保留不变）

        返回 (new_flow, id_map, had_placeholder_z)。
        """
        nodes = flow.get("nodes", []) or []
        # 节点前缀用每次部署独立的随机 token —— 不能用 target_flow_id 的固定子串，
        # 否则同一 agent 多次部署会产生相同的节点 id 而撞车。
        node_prefix = "rw" + secrets.token_hex(6)
        id_map: Dict[str, str] = {}
        for i, n in enumerate(nodes):
            old = n.get("id") or f"_auto_{i}"
            id_map[old] = f"{node_prefix}_{i:03d}"

        def _rewrite(val):
            if isinstance(val, list):
                return [_rewrite(x) for x in val]
            if isinstance(val, str):
                return id_map.get(val, val)
            return val

        new_nodes = []
        had_placeholder_z = False
        for i, n in enumerate(nodes):
            nn = dict(n)
            nn["id"] = id_map.get(n.get("id"), n.get("id"))
            zval = str(n.get("z", "")).strip()
            if zval and self._is_placeholder_flow_id(zval):
                had_placeholder_z = True
            nn["z"] = target_flow_id
            # NR 会静默丢弃缺少 x/y 坐标的节点（agent 产出常缺），
            # 白盒路径必须补上网格坐标，否则部署后节点凭空消失。
            if "x" not in nn or "y" not in nn:
                nn["x"] = 100 + (i % 5) * 240
                nn["y"] = 100 + (i // 5) * 150
            if "wires" in nn:
                nn["wires"] = _rewrite(nn["wires"])
            if nn.get("type") in ("link out", "link in") and "links" in nn:
                # D35 根因修复：Node-RED 运行时**只接受** link 节点的 `links`
                # 为【字符串数组】（["id1","id2"]）。导出 / 构造形态的对象数组
                # [{"id":"li1"}] 在真实 NR 中**无法建立 link 连接**——link out 广播
                # 不到 link in，下游全不执行，e2e 报"断点 reached=[]"（D35 / round24）。
                # 故无论输入是字符串数组还是对象数组，这里统一**归一化为字符串数组**
                # （内嵌 id 经 id_map 重映射），既修正链路断裂，又消除格式分歧。
                # （D30 旧实现仅做 id 重映射却保留对象数组形态，导致 NR 不认。）
                raw_links = nn["links"]
                if not isinstance(raw_links, list):
                    raw_links = [raw_links] if raw_links else []
                norm_links = []
                for le in raw_links:
                    if isinstance(le, str):
                        norm_links.append(id_map.get(le, le))
                    elif isinstance(le, dict):
                        lid = le.get("id")
                        if lid in id_map:
                            norm_links.append(id_map[lid])
                        elif lid:
                            norm_links.append(lid)  # 外部 link（如 TTS 队列）保留原 id
                    elif isinstance(le, (int, float)):
                        sid = str(le)
                        norm_links.append(id_map.get(sid, sid))
                nn["links"] = norm_links
            new_nodes.append(nn)

        new_flow = dict(flow)
        new_flow["nodes"] = new_nodes
        return new_flow, id_map, had_placeholder_z

    # ── D4 (G2)：部署前 link-out 目标校验 ──────────────────────────────────
    def _validate_link_out_targets(self, flow: Dict) -> List[Dict]:
        """部署前校验所有 'link out' 节点的 links 目标在目标 NR 真实存在
        （link-in 节点 / 子流程入口端口 / 同流内节点），否则运行时会出现
        'Error delivering message to node:undefined' / 'sendEvent.destination.node.receive
        is not a function' 这类极难定位的故障（B3/诊断报告 G2）。

        返回错误字典列表（空=全部合法）。无 NR client 或无法连 NR 时 fail-open 返回空列表
        （不阻塞部署，与既有闸门一致：连接问题让 NR 自己报错）。
        """
        out: List[Dict] = []
        nodes = flow.get("nodes", []) or []
        link_outs = [n for n in nodes if n.get("type") == "link out"]
        if not link_outs:
            return out
        valid_ids: set = set()
        try:
            cli = getattr(self.nr, "client", None) if getattr(self, "nr", None) else None
            if cli is None:
                return out
            live = cli._json("GET", "/flows")
            for n in live:
                nid = n.get("id")
                if nid:
                    valid_ids.add(nid)
                # 子流程 in/out 端口 id（部分子流程入口以端口 id 形式存在）
                for port in (n.get("in") or []):
                    if isinstance(port, dict) and port.get("id"):
                        valid_ids.add(port["id"])
                for port in (n.get("out") or []):
                    if isinstance(port, dict) and port.get("id"):
                        valid_ids.add(port["id"])
        except Exception:
            return out  # fail-open：无法连 NR 时不拦
        # 同流内节点 id（同流 link-out → link-in 合法）
        valid_ids |= {n.get("id") for n in nodes if n.get("id")}
        for lo in link_outs:
            lo_name = lo.get("name") or lo.get("id") or "?"
            for tgt in _link_ids(lo.get("links")):
                if tgt in valid_ids:
                    continue
                out.append({
                    "level": "error",
                    "rule": "R_LINKIN",
                    "node": lo.get("id"),
                    "message": (
                        f"link out『{lo_name}』指向不存在的 link-in/子流程入口「{tgt}」："
                        f"部署后运行时将报『Error delivering message to node:undefined』。"
                        f"请确认目标 id 正确（如 demo_notify=b595563939283231、"
                        f"anysearch_batch=af_anysearch_in）且该子流程已在目标 NR 注册。"
                    ),
                })
        return out

    @staticmethod
    def _build_unified_gate(staging_gate, e2e_result, canary_result,
                            *, require_e2e: bool = False,
                            staging_required: bool = False) -> Dict[str, Any]:
        """聚合三类闸为单一可机读门面（P3 核心聚合，#686）。

        输入：
          - staging_gate: run_staging_gate 结果 / {"skipped": True} 占位 / None
          - e2e_result: run_e2e_trace_raw 结果 / None（未开启）
          - canary_result: get_nr_subflow_integrity 结果 / {"ok":True,"source":"skipped"}
          - require_e2e: 调用方是否**要求**跑 e2e（A22）。要求了却没真跑 → 不许判 pass。
          - staging_required: 调用方是否**期望** vhass 闸真跑（A18，如 run_gate=True 且
            flow 含 HA 动作）。期望了却被 skip → 后置条件根本没验证，降级 warn。
        输出 {
          verdict: "block" | "warn" | "pass",
          passed: bool,
          layers: {vhass_staging, e2e_trace, structure_canary},
          notes: [str],
        }
        聚合规则：
          vhass 未通过 → block；e2e 真跑且非通过 → block；
          require_e2e 但 e2e 未真跑 → block（A22：拒绝「空 pass」）；
          staging_required 但 vhass 被 skip → warn（A18：拒绝「零验证的绿灯」）；
          canary 探测到空壳 / mustache 占位 → warn（fail-open，预存问题非本次部署）；
          其余（含全 SKIP 且调用方无要求）→ pass。"""
        # ── vhass staging 层 ──
        if (staging_gate is None or not isinstance(staging_gate, dict)
                or "passed" not in staging_gate):
            # A18：skip 原因必须如实回传（旧实现硬编码「run_gate=False / 无 HA 动作」，
            # 在 run_gate=True 且明明有 HA 动作时自相矛盾，误导排障）。
            _skip_reason = None
            if isinstance(staging_gate, dict):
                _skip_reason = staging_gate.get("reason") or staging_gate.get("error")
            vhass = {"ran": False, "passed": None, "verdict": "skipped",
                     "detail": _skip_reason or "未运行（run_gate=False / 无 HA 动作 / dry_run）"}
        else:
            vhass = {
                "ran": True,
                "passed": bool(staging_gate.get("passed")),
                "verdict": staging_gate.get("verdict", "?"),
                "detail": staging_gate.get("reasons") or staging_gate.get("error"),
                # 闸内「没能真正证实」的项（vhass 未建模的服务 A14 / 无法本地求值只能
                # 保守视为命中的 JSONata 分支 A15 / 字段静默丢写）。passed=True 只说明
                # 「没抓到反例」，不等于「验证过」，故单独抬出来供聚合层降级。
                "warnings": list(staging_gate.get("warnings") or []),
                # 【A4】可观测性：把 run_staging_gate 内部已算出的诚实性字段透出 MCP 面，
                # 否则调用方只能从中文 detail 字符串做子串匹配判断「是否真的重放过」。
                # 纯加字段、零判定风险。
                "fully_verified": bool(staging_gate.get("fully_verified")),
                "replay_zero": bool(staging_gate.get("replay_zero")),
                "replay_zero_policy": staging_gate.get("replay_zero_policy"),
                "replayed_services": list(staging_gate.get("replayed_services") or []),
                "external_calls": list(staging_gate.get("external_calls") or []),
                "dead_branches": list(staging_gate.get("dead_branches") or []),
            }

        # ── e2e 实机追踪层 ──
        if e2e_result is None or not isinstance(e2e_result, dict):
            e2e = {"ran": False, "passed": None, "verdict": "skipped",
                   "detail": "未运行（require_e2e 默认关 / dry_run）"}
        else:
            e2e_ran = e2e_result.get("e2e") is True
            e2e = {
                "ran": e2e_ran,
                "passed": e2e_ran and e2e_result.get("verdict") == "通过",
                "verdict": e2e_result.get("verdict", "?"),
                "detail": e2e_result.get("reasons"),
            }

        # ── 结构金丝雀层 ──
        if canary_result is None or not isinstance(canary_result, dict):
            canary = {"ran": False, "ok": None, "any_empty_shell": None,
                      "mustache_warnings": 0, "verdict": "skipped",
                      "detail": "未运行"}
        else:
            _subs = canary_result.get("subflows", []) or []
            _mustache = sum(1 for s in _subs if s.get("has_mustache_entity"))
            _any_shell = bool(canary_result.get("any_empty_shell"))
            _ran = canary_result.get("source") != "skipped"
            canary = {
                "ran": _ran,
                "ok": bool(canary_result.get("ok")),
                "any_empty_shell": _any_shell,
                "mustache_warnings": _mustache,
                "verdict": "warn" if (_any_shell or _mustache) else "pass",
                "detail": {
                    "empty_shells": canary_result.get("empty_shells", []),
                    "subflow_count": len(_subs),
                },
            }

        # ── 聚合 verdict ──
        # 分别收集 block / warn 理由再定级，避免「命中前一条就吞掉后面所有告警」。
        block_notes: List[str] = []
        warn_notes: List[str] = []

        if vhass["ran"] and not vhass["passed"]:
            block_notes.append("vhass staging 闸门未通过 → 硬拦")
        if e2e["ran"] and not e2e["passed"]:
            block_notes.append("E2E 实机验证未通过 → 硬拦")
        # 【A22】要求跑 e2e 却没真跑（PROD 写保护 / 基建异常 / 被吞成 e2e=False）→
        # 绝不能顶层 pass 制造「e2e 通过」假象。
        if require_e2e and not e2e["ran"]:
            block_notes.append(
                f"require_e2e=True 但 E2E 未真正执行（层内 verdict={e2e['verdict']}，"
                f"detail={e2e.get('detail')}）→ 拒绝空 pass[A22]")
        # 【A14/A15 / 保守 fail-closed】vhass 闸「判过」但 fully_verified=False
        # （未建模服务 / 只能保守视为命中的 JSONata 分支 / 字段静默丢写）→
        # 这类「绿灯」是「没抓到反例」而非「验证通过」，后置结论不完全可信，
        # 按 fail-closed 直接硬拦；仅当 fully_verified 为真但仍有 soft warning 时降级 warn。
        if vhass["ran"] and vhass["passed"]:
            # 用原始 staging_gate 的 fully_verified（而非 vhass 里 bool 归一后的值），
            # 避免「上游未带该字段（None）→ 被 bool 归一成 False → 误拦每个通过的闸」。
            # 仅当上游**显式**置 fully_verified=False（真有未证实项）才硬拦。
            _fv = staging_gate.get("fully_verified") if isinstance(staging_gate, dict) else None
            if _fv is False:
                block_notes.append(
                    "vhass staging 判过但 fully_verified=False（存在未证实项，后置结论不完全可信）："
                    + "；".join(str(w) for w in vhass["warnings"])
                    + " → 硬拦[A-fail-closed]")
            elif vhass.get("warnings"):
                warn_notes.append(
                    "vhass staging 判过，但存在【未证实项】："
                    + "；".join(str(w) for w in vhass["warnings"])
                    + " → 降级 warn[A14/A15]")
        # 【A18】期望 vhass 闸运行却被 skip → 后置条件一条都没验证，不许绿灯。
        if staging_required and not vhass["ran"]:
            warn_notes.append(
                f"vhass staging 闸被要求运行却未执行：{vhass['detail']}"
                f" → 后置条件【未验证】，降级 warn[A18]")
        if canary["ran"] and canary["verdict"] == "warn":
            if canary["any_empty_shell"]:
                warn_notes.append("结构金丝雀：预先存在空壳子流程（非本次部署，fail-open 放行）")
            if canary["mustache_warnings"]:
                warn_notes.append(f"结构金丝雀：{canary['mustache_warnings']} 个子流程含 mustache 占位实体"
                                  f"（降级非致命，WARN）")

        if block_notes:
            verdict = "block"
        elif warn_notes:
            verdict = "warn"
        else:
            verdict = "pass"
            if not (vhass["ran"] or e2e["ran"] or canary["ran"]):
                warn_notes.append("所有闸均跳过（dry-run / 未开启 / 无 HA 动作）→ 无聚合结论，沿用各闸独立结果")
        notes = block_notes + warn_notes

        return {
            "verdict": verdict,
            "passed": verdict != "block",
            "layers": {
                "vhass_staging": vhass,
                "e2e_trace": e2e,
                "structure_canary": canary,
            },
            "notes": notes,
        }

    def deploy_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                   label: Optional[str] = None,
                   target_flow_id: Optional[str] = None,
                   target: str = "staging", force: bool = False,
                   run_gate: bool = True, dry_run: bool = False,
                   block_on_lint_error: bool =
                       (os.environ.get("AUTOFLLOW_WHITEBOX_BLOCK_ON_LINT_ERROR", "1") != "0"),
                   block_on_logic_error: bool =
                       (os.environ.get("AUTOFLLOW_WHITEBOX_BLOCK_ON_LOGIC_ERROR", "0") != "0"),
                   block_on_schema_error: bool =
                       (os.environ.get("AUTOFLLOW_WHITEBOX_BLOCK_ON_SCHEMA_ERROR", "1") != "0"),
                   require_e2e: Optional[bool] = None,
                   allow_prod: bool = False
                   ) -> Dict[str, Any]:
        """白盒部署：直接接受 Agent 产出的原始 Node-RED flow JSON，经校验后部署。

        流程：
          1. Schema 校验（validate_flow_schema）— 致命项(S1..S5)硬拦，其余 error/warning 放行
          2. HA server 占位符替换（REPLACE_WITH_HA_SERVER → 真实 id）
          3. 冲突检测（同名非本流拒绝 / force 改名）
          4. 可选 vhass staging 闸门（run_gate=True 且有 HA 动作时）
          5. 部署到 NR（create_or_update_flow）
          6. 登记 flow_catalog（owner=agent_id）
          7. 记录失败模式日志

        参数：
          - flow_json: Agent 产出的完整 flow 对象 {id, label, nodes:[...]}
          - agent_id: 来源 agent 标识（如 "deepseek++"/"trae"/"solo"/"hand"）
          - label: 自定义标签（缺省用 flow_json.label）
          - target_flow_id: 指定已存在的 flow id 覆盖（缺省用 flow_json.id 创建新 flow）
          - target: "staging"(inject 触发) / "prod"(真实事件)
          - force: 强制覆盖冲突
          - dry_run: A8 预览模式——跑完校验/lint/重映射后，返回「将增/删/改哪些节点」
            的 diff 预览，**不落 NR、不进防御闸、不跑 vhass 闸门**；带 would_block_on_lint
            标记（若真部署会被硬伤规则拦下）。供 agent/WebUI 部署前确认。
          - run_gate: 是否跑 vhass 闸门（有 HA 动作时建议开启）
          - block_on_lint_error: B3 可配置阻塞 —— 当 lint 出现 R13(孤儿 api-call-service)/
            R15(紧环) 等硬伤时阻止部署（默认开，env AUTOFLLOW_WHITEBOX_BLOCK_ON_LINT_ERROR=0 可关）
          - block_on_schema_error: 【R9】schema 致命错误闸 —— 缺 server(S3)、switch
            wires≠rules(S4)、空 flow(S5)、节点缺 type(S2)、结构非法(S1) 等「部署即坏」的
            schema error 阻止部署（默认开，env AUTOFLLOW_WHITEBOX_BLOCK_ON_SCHEMA_ERROR=0 可关）。
            非致命 schema error（缺顶层 id、http 缺 url、POST 无 body 等）仍只报告不拦。
          - block_on_logic_error: 【Phase B·B4】L2 逻辑可达性闸门 —— 当逻辑仿真发现「任何触发
            场景都触达不到的动作终点(L1)」时阻止部署。默认【关】(AUTOFLLOW_WHITEBOX_BLOCK_ON_LOGIC_ERROR
            缺省为 "0")：仅把 logic 段附在返回里报告，不拦部署（白箱 escape hatch，先把流跑起来看真实
            行为）；设 "1" 才硬拦。仿真器自身异常 fail-open，绝不影响结构/lint 校验与部署。

          - require_e2e: 【Phase 2 / Phase C·C2】E2E 实机验证闸 —— 默认【关】（env
            AUTOFLLOW_WHITEBOX_REQUIRE_E2E 缺省 "0"，显式设 "1" 或调用时 require_e2e=True
            才跑）。开启后 deploy_raw 落 NR 前先跑一次 run_e2e_trace_raw（部署到 staging +
            触发 + 抓 trace + 比对 + 回滚），仅当 verdict=通过才放行；verdict=断点则阻止部署
            逼 agent 修 flow 重提；无法验证（e2e=False，如缺触发入口）fail-open 放行。
            日常部署由 Step 8.5 结构金丝雀快速把关，e2e 退为手动/周期回归用。

        返回 {ok, flow_id, created, label, node_count, validation:[], gate:{}, deployed_at,
              logic:{ok, logic_issues, unreachable_actions, action_endpoints, reachable_actions,
                     scenarios, summary}}。
        """
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        _slog(_tid, "deploy_raw.start", agent_id=agent_id, target=target, run_gate=run_gate,
              node_count=len(flow_json.get("nodes", []) if isinstance(flow_json, dict) else []))
        # Step 1: 输入校验
        if not isinstance(flow_json, dict) or "nodes" not in flow_json:
            _slog(_tid, "deploy_raw.error", sub_stage="input",
                  elapsed=round(time.perf_counter() - _t0, 3),
                  error="flow_json 必须是包含 'nodes' 数组的 JSON 对象")
            return {"ok": False, "stage": "input",
                    "error": "flow_json 必须是包含 'nodes' 数组的 JSON 对象"}

        # Step 1.5: 【Phase C·C3】重试预算（防控制层死循环 / agent 自动改→重部署 runaway）
        # 同一 agent 在滑动窗口内的「失败部署尝试」超过上限 N 即停止部署并转人工/报告；
        # 仅记录 ok=False 的失败（成功清零），避免误伤正常多次部署。
        _budget = int(os.environ.get("AUTOFLLOW_WHITEBOX_RETRY_BUDGET", "5"))
        _window = float(os.environ.get("AUTOFLLOW_WHITEBOX_RETRY_WINDOW_MIN", "10")) * 60
        if not hasattr(self, "_retry_budget"):
            self._retry_budget = {}
        _hist = self._retry_budget.setdefault(agent_id, [])
        _now = time.time()
        _hist[:] = [t for t in _hist if _now - t < _window]
        if _budget > 0 and len(_hist) >= _budget:
            _slog(_tid, "deploy_raw.retry_budget_exhausted", agent_id=agent_id,
                  budget=_budget, failed=len(_hist))
            return {
                "ok": False, "stage": "retry_budget_exhausted",
                "error": (
                    f"重试预算耗尽：agent `{agent_id}` 在 {_window/60:.0f} 分钟内已有 "
                    f"{len(_hist)} 次失败部署尝试（上限 {_budget}）。疑似自动修复死循环，"
                    f"已停止部署并转人工/报告。请人工介入检查 flow，或调高 "
                    f"AUTOFLLOW_WHITEBOX_RETRY_BUDGET。"
                ),
                "retry_budget": _budget, "failed_attempts_in_window": len(_hist),
            }

        def _record_fail() -> None:
            _hist.append(time.time())

        flow = dict(flow_json)  # 浅拷贝避免修改入参
        nodes = flow.get("nodes", [])

        # D5-d（C6）：快照 caller 显式传入的 target_flow_id，防止 conflict detection
        # 之后改写丢失原始意图。仅当 caller 显式传入时才需在部署前校验其存在性。
        _caller_target = target_flow_id

        if label:
            flow["label"] = label
        if not flow.get("label"):
            flow["label"] = f"{agent_id}-{datetime.now().strftime('%H%M%S')}"

        # Step 2: Schema 校验
        validation = self.validate_flow_schema(flow)
        errors = [v for v in validation if v["level"] == "error"]
        warnings = [v for v in validation if v["level"] == "warning"]

        # R9(#round4) iss_86d66844f7：此处**曾经**是 `if errors: pass  # 记录后继续`——
        # 于是缺 server / switch wires≠rules / 空 flow 这类「部署即坏」的流照样落 NR，
        # 上游自检还回 will_deploy_block=false 当绿灯。现改为：致命项(S1..S5)硬拦，
        # 其余 error（缺顶层 id、http 缺 url 等）沿用旧的 fail-open 只记录不拦。
        # dry-run 不早退，改为在预览里报 would_block_on_schema（与 lint 同策略）。
        _schema_blocking = schema_blocking_issues(validation)
        if _schema_blocking and block_on_schema_error and not dry_run:
            _srules = ",".join(sorted({b.get("rule") for b in _schema_blocking}))
            _slog(_tid, "deploy_raw.schema_block",
                  elapsed=round(time.perf_counter() - _t0, 3),
                  rules=_srules, blocking_count=len(_schema_blocking))
            _record_fail()
            return {
                "ok": False, "stage": "schema_block",
                "error": (f"Schema 致命错误 {len(_schema_blocking)} 项（{_srules}），已阻止部署："
                          + "；".join(b.get("message", "") for b in _schema_blocking[:5])),
                "validation": validation,
                "schema_blocking": _schema_blocking,
                "schema_blocking_rules": sorted({b.get("rule") for b in _schema_blocking}),
            }

        # Step 2.5：静态 Flow Linter（A1）—— 抓「静态合法、运行必错」反模式（非阻塞）
        # 专拦本次 ArcFace 排障暴露的坑：switch otherwise 前置→死代码、
        # function 读错 payload 路径、http json 但 body 未构造、JSONata 语法。
        lint_issues = lint_flow(flow, b1_unreachable=True)
        validation.extend(lint_issues)
        errors = [v for v in validation if v["level"] == "error"]
        warnings = [v for v in validation if v["level"] == "warning"]

        # Step 2.6: Bark 子流程幂等确保（A3）——前置，保证后续闸门/E2E/部署时子流程已存在。
        # dry-run 不做任何副作用（仅预览）。活体 1990 已存在 b0bbc86 → ensure 走 no-op（零风险）；
        # 仅在缺失时按声明式规格生成（env 值经 os.environ 注入，密钥绝不硬编码）。
        # 仅作用于 1990（prod 实例，AUTOFLLOW_ENV=prod）→ allow_prod=True 显式 opt-in
        # （#119 护栏订正：is_prod() 按 env 判定，写 prod 必须 allow_prod=True，否则 _guard_prod
        # 抛 NRGuardError 被下方 except 吞掉，导致子流程永不重建）。绝不动 1880。
        if not dry_run:
            from .subflows import (
                ensure_bark_subflow, flow_uses_bark_subflow,
                ensure_history_subflow, flow_uses_history_subflow,
            )
            if flow_uses_bark_subflow(flow.get("nodes", [])):
                try:
                    _bark_res = ensure_bark_subflow(self.nr.client, allow_prod=True)
                    _slog(_tid, "deploy_raw.bark_ensure",
                          created=_bark_res.get("created"), exists=_bark_res.get("exists"))
                except Exception as _be:
                    # bark 缺只影响推送，不阻塞主流程；且活体已存在不会触发生成
                    _slog(_tid, "deploy_raw.bark_ensure_err", error=str(_be)[:200])
            # Step 2.6b：历史查询子流程幂等确保（仿 bark 的 A3 模式）。
            # 4 个 af_hist_* 子流程的 ensure 与 bark 同策略：活体已存在 → no-op（零风险）；
            # 仅在缺失时从 subflows_built.json 重建（server 替换成默认 HA server，可移植）。
            # 仅作用于 1990（prod 实例）→ allow_prod=True 显式 opt-in（#119 护栏订正），绝不动 1880。
            if flow_uses_history_subflow(flow.get("nodes", [])):
                try:
                    _hist_res = ensure_history_subflow(self.nr.client, allow_prod=True)
                    _slog(_tid, "deploy_raw.history_ensure",
                          created=_hist_res.get("created"), exists=_hist_res.get("exists"),
                          rebuilt=_hist_res.get("rebuilt"))
                except Exception as _he:
                    # 历史子流程缺只影响历史查询类能力，不阻塞主流程；活体已存在不会触发生成
                    _slog(_tid, "deploy_raw.history_ensure_err", error=str(_he)[:200])

        # Step 2.7: 【D4/G2】link-out 目标校验（部署前捕获指向不存在 link-in 的悬空 link out，
        # 否则运行时报 'Error delivering message to node:undefined' 这类难定位故障）。
        _link_errs = self._validate_link_out_targets(flow)
        if _link_errs:
            if dry_run:
                validation.extend(_link_errs)  # 预览里报告，不阻断
            else:
                _record_fail()
                return {
                    "ok": False, "stage": "link_out_unresolved",
                    "error": ("存在指向不存在 link-in 的 link out 节点（部署后运行时将报 "
                              "『Error delivering message to node:undefined』）："
                              + "; ".join(e["message"] for e in _link_errs)),
                    "validation": validation,
                    "link_errors": _link_errs,
                }

        # 白盒部署前硬拦集（用户已采纳）：白盒自洽流里，以下「静态合法、运行必错」的硬伤直接阻塞部署：
        #   - R13 孤儿动作节点 / R15 紧环 / R17 悬空连线(断线) / R22 节点缺必填字段
        #   - R20 缺实体 api-current-state / server-state-changed(error 级；对 api-call-service 为 warning 不拦)
        # 其余 error 级规则（R5/R7/R8/R10/R16/R18 等）属代码风格/结构类，不阻塞以免误伤合法手搓流。
        # 默认开启（env AUTOFLLOW_WHITEBOX_BLOCK_ON_LINT_ERROR=0 可关）。
        # A8：dry-run 下不早退，改为算 would_block_on_lint 附在预览里，让用户看清「真部署会不会被拦」
        _LINT_BLOCK_RULES = {"R13", "R15", "R20", "R17", "R22", "R24", "R30", "R32", "R_SERVICE_PARAM", "R36", "R2-ESC", "R_NO_TRIGGER", "R16", "R40"}
        _blocking = [v for v in lint_issues
                     if v.get("level") == "error" and v.get("rule") in _LINT_BLOCK_RULES]
        if block_on_lint_error and not dry_run:
            if _blocking:
                _rules = ",".join(sorted({b.get("rule") for b in _blocking}))
                _slog(_tid, "deploy_raw.lint_block", elapsed=round(time.perf_counter() - _t0, 3),
                      rules=_rules, blocking_count=len(_blocking))
                _record_fail()
                return {
                    "ok": False, "stage": "lint_block",
                    "error": (f"静态检查未通过（{len(_blocking)} 个硬伤：{_rules}），"
                              f"已阻止部署。请先修复这些「静态合法、运行必错」的问题。"),
                    "validation": validation,
                    "lint": lint_issues,
                    "lint_error_count": sum(1 for v in lint_issues if v.get("level") == "error"),
                    "lint_warning_count": sum(1 for v in lint_issues if v.get("level") == "warning"),
                }

        # 【Phase B · B4】L2 逻辑可达性闸门（fail-open，默认仅报告不拦）。
        # 与 lint 硬拦集解耦：logic error = 存在「任何触发场景都触达不到的动作终点(L1)」。
        # 默认关（AUTOFLLOW_WHITEBOX_BLOCK_ON_LOGIC_ERROR=0）：只附 logic 段到返回，不阻止部署
        # （白箱 escape hatch：先把流跑起来看真实行为）。开=1 时 L1 直接 stage="logic_block"。
        try:
            _sim = simulate_flow(flow)
            _logic_block = {
                "ok": _sim.get("ok", True),
                "logic_issues": _sim.get("logic_issues", []),
                "unreachable_actions": _sim.get("unreachable_actions", []),
                "action_endpoints": _sim.get("action_endpoints", []),
                "reachable_actions": _sim.get("reachable_actions", []),
                "scenarios": _sim.get("scenarios", []),
                "summary": _sim.get("summary", ""),
            }
        except Exception as _e:
            _logic_block = {
                "ok": True, "logic_issues": [], "unreachable_actions": [],
                "action_endpoints": [], "reachable_actions": [], "scenarios": [],
                "summary": f"逻辑仿真跳过（simulator error: {_e}）",
            }
        _logic_err = _logic_block.get("unreachable_actions", [])
        if block_on_logic_error and not dry_run:
            if _logic_err:
                _slog(_tid, "deploy_raw.logic_block", elapsed=round(time.perf_counter() - _t0, 3),
                      unreachable=_logic_err)
                _record_fail()
                return {
                    "ok": False, "stage": "logic_block",
                    "error": (f"L2 逻辑预检未通过：{len(_logic_err)} 个动作终点在所有触发场景"
                              f"（含虚拟状态注入）下都触达不到（L1）。已阻止部署。"),
                    "validation": validation,
                    "lint": lint_issues,
                    "lint_error_count": sum(1 for v in lint_issues if v.get("level") == "error"),
                    "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
                    "logic": _logic_block,
                }

        # Step 3: HA server 替换
        _, unresolved = self._inject_ha_server(flow)
        if unresolved:
            return {"ok": False, "stage": "ha_server_inject",
                    "error": self._ha_server_unresolved_msg(unresolved)}

        # Step 4: 冲突检测 —— 认出我们自己上次部署的 flow（按 label 命中且 catalog 有记录 → 原地更新）
        existing = None
        flabel = flow.get("label", "")
        catalog = self.state.get_flow_catalog().get("flows", {})
        for f in self.nr.list_flows():
            if f.get("label") == flabel:
                existing = f
                break

        if existing:
            if existing.get("id") in catalog:
                # 是我们自己部署的 → 原地更新，复用同一 flow id（Lab 重复部署/迭代场景）
                target_flow_id = existing.get("id")
            elif not force:
                return {
                    "ok": False, "conflict": True, "validation": validation,
                    "error": f"NR 已存在同名 flow「{flabel}」({existing.get('id')})且非本网关部署。",
                    "existing": {"id": existing.get("id"), "label": flabel},
                }
            else:
                flow["label"] = f"{flabel} (白盒副本)"

        # Step 5: Defense check（dry-run 跳过——预览不写不需过闸）
        if not dry_run:
            try:
                self.defense.check_write(
                    operation="create_flow" if not target_flow_id else "update_flow",
                    flow_id=flow.get("id", ""),
                    label=flow.get("label", ""),
                    owner_agent=None,
                    acting_agent=agent_id,
                    flows_touched=1,
                )
            except DefenseError as e:
                _record_fail()
                return {"ok": False, "defense": str(e), "validation": validation}

        # Step 6: 可选 staging 闸门（仅当 flow 含 HA 动作节点且有实体可断言时；dry-run 跳过）
        gate = {"skipped": True, "reason": "无 HA 动作或无预期条件"}
        has_ha_actions = any(
            n.get("type") in ("api-call-service", "server-state-changed",
                              "api-current-state", "function")
            for n in nodes
        )
        _gate_unverifiable: List[str] = []
        _staging_required = bool(run_gate and has_ha_actions and not dry_run)
        if _staging_required:
            # 【A18】与 verify_flow 同源修复：改走 flow= 直通口 + 正确 kwarg（旧写法
            # expected_postconditions= 是 TypeError，dsl 又是伪造的注释文本，两头必死）。
            expected_auto, _gate_unverifiable = _auto_expected_from_nodes(nodes)
            # 【V-NEW-2 / V-F4】纯 function 流无 api-call-service → expected_auto 为空，
            # 但 function 黑箱副作用仍须经闸门诚实性判定（_function_only），故强制跑闸；
            # 其余无自动后置条件的（纯 link out / subflow 属 Tier A 人工验证，按设计跳过）。
            _force_run = bool(expected_auto) or any(
                n.get("type") == "function" for n in nodes)
            if not _force_run:
                gate = {"skipped": True,
                        "reason": ("flow 含 HA 动作，但没有任何后置条件可自动推导，闸未运行："
                                   + "；".join(sorted(set(_gate_unverifiable)) or ["未知原因"]))}
            else:
                try:
                    gate = self.run_staging_gate(dsl="", expected=expected_auto, flow=flow)
                except Exception as _ge:
                    # 闸门基建异常按 fail-open 处理（与本函数既有取向一致），但如实留痕
                    gate = {"skipped": True, "reason": f"staging 闸异常: {_ge}"}
                if not gate.get("passed") and not gate.get("skipped"):
                    _slog(_tid, "deploy_raw.gate_fail", elapsed=round(time.perf_counter() - _t0, 3))
                    _record_fail()
                    return {
                        "ok": False, "gate_passed": False, "gate": gate,
                        "validation": validation,
                        "error": f"staging 闸门未通过: {gate.get('error', '')}",
                    }

        # Step 6.5: 【Phase 2 / Phase C·C2】E2E 实机验证闸（默认开，env AUTOFLLOW_WHITEBOX_REQUIRE_E2E=0 可关）。
        # 落 NR 前先跑一次 run_e2e_trace_raw（部署到 staging + 触发 + 抓 trace + 比对 + 回滚）。
        # 仅当「真实跑通且 verdict=通过」才放行；verdict=断点（部署成功但信息流未达预期）则阻止
        # 部署，逼 agent 修 flow 重提。无法验证（e2e=False，如缺触发入口/验证基建异常）则 fail-open
        # 放行，避免验证基建故障误伤正常部署。dry-run 跳过。
        # require_e2e=None 读 env（默认值由 "1" 改为 "0"：每次部署不再强制重跑 e2e，
        # 结构性金丝雀 Step 8.5 已接管每次快速把关；e2e 退为手动/周期回归，显式 True 才跑）。
        _require_e2e = require_e2e if require_e2e is not None \
            else (os.environ.get("AUTOFLLOW_WHITEBOX_REQUIRE_E2E", "0") != "0")
        _e2e = None  # 默认未运行；仅当 require_e2e 开启且非 dry_run 才赋值（避免成功返回 NameError）
        if _require_e2e and not dry_run:
            try:
                _e2e = self.run_e2e_trace_raw(flow, target=target, live=False, allow_prod=allow_prod)
            except Exception as _ee:
                _slog(_tid, "deploy_raw.e2e_gate_err",
                      elapsed=round(time.perf_counter() - _t0, 3), error=str(_ee)[:200])
                _e2e = {"e2e": False, "verdict": "拦截", "error": f"E2E 验证异常：{_ee}"}
            if _e2e.get("e2e") is True and _e2e.get("verdict") != "通过":
                _slog(_tid, "deploy_raw.e2e_gate_block",
                      elapsed=round(time.perf_counter() - _t0, 3), verdict=_e2e.get("verdict"))
                _record_fail()
                return {
                    "ok": False, "stage": "e2e_gate",
                    "error": (f"E2E 实机验证未通过（verdict={_e2e.get('verdict')}），已阻止部署。"
                              f"请修复 flow 后重试，或先单独调 autoflow_run_e2e_trace 定位断点。"),
                    "e2e": _e2e,
                    "validation": validation,
                }
            # fail-open：e2e=False（无法验证）或 verdict=通过 → 继续部署

        # Step 7: 重映射节点 id + z（消化 Agent 占位符，避免 NR duplicate id）
        # 必须在部署前做：Agent 常用 z:"1" / 短 id n1..n7，直接透传必撞车。
        fid = target_flow_id or self._gen_raw_flow_id(agent_id, flow)
        flow, id_map, had_placeholder_z = self._remap_raw_flow_ids(flow, fid)
        nodes = flow.get("nodes", [])  # 重绑定到重映射后的节点
        if had_placeholder_z:
            validation.append({
                "level": "info", "node_id": "_root",
                "message": "已将所有节点 z 从占位符重写为目标 flow id，并重新生成全局唯一节点 id"
                           "（避免 NR `duplicate id`）。",
            })

        # 节点注册表闸门（P0 防御）：未知节点类型直接报错，不让坏 flow 上线
        self._gate_node_types(flow)

        # A8：dry-run 预览——到此为止所有校验/lint/重映射已完成，flow 已是「将部署的最终形态」。
        # 拉线上现有 flow 做节点级 diff，返回预览，绝不落 NR。
        if dry_run:
            live = None
            if target_flow_id:
                try:
                    live = self.nr.get_flow(target_flow_id)
                except Exception:
                    live = None
            node_diff = _build_node_diff(live, flow)
            # R9：dry-run 预告里把 schema 致命项与 lint 硬伤合并进 would_block_rules，
            # 否则「预览说能部署、真部署被 schema_block 拦」又是一次自相矛盾。
            _schema_rules = sorted({b.get("rule") for b in _schema_blocking})
            _blocking_rules = sorted(
                {b.get("rule") for b in _blocking} | set(_schema_rules))
            _would_block_schema = bool(_schema_blocking) and block_on_schema_error
            _slog(_tid, "deploy_raw.dry_run", elapsed=round(time.perf_counter() - _t0, 3),
                  would="update" if live is not None else "create",
                  added=len(node_diff["added"]), removed=len(node_diff["removed"]),
                  changed=len(node_diff["changed"]),
                  would_block=bool(_blocking) or _would_block_schema)
            return {
                "ok": True,
                "dry_run": True,
                # WB24 NEW-F5（透明性）：dry-run 也回显归一化后的 flow_json，便于提前核对
                # 而不真正落 NR。
                "flow_json": flow,
                "would": "update" if live is not None else "create",
                "flow_id": target_flow_id or fid,
                "label": flow.get("label", ""),
                "node_count": len(nodes),
                "summary": self.compute_flow_diff(flow, live),
                "node_diff": node_diff,
                "validation": validation,
                "lint": lint_issues,
                "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
                "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
                "would_block_on_lint": bool(_blocking),
                "would_block_on_schema": _would_block_schema,
                "schema_blocking": _schema_blocking,
                "would_block_rules": _blocking_rules,
                "logic": _logic_block,
                "would_block_on_logic": bool(_logic_err) and block_on_logic_error,
                "_trace_id": _tid,
            }

        # D5-d（C6）：caller 显式指定 target_flow_id 时，部署前校验该 flow
        # 在 NR 中确实存在。若不存在，报结构化错误而非让 create_or_update_flow
        # 静默创建（caller 以为在更新已有 flow，实则新建了一个同名不同 id 的副本）。
        if _caller_target and not dry_run:
            try:
                self.nr.get_flow(_caller_target)
            except Exception:
                _record_fail()
                return {
                    "ok": False,
                    "stage": "not_found",
                    "error": (
                        f"target_flow_id `{_caller_target}` 在 Node-RED 中不存在。"
                        f"如果你要新建 flow，请省略 target_flow_id；"
                        f"如果要更新已有 flow，请确认 id 正确。"
                    ),
                    "hint": (
                        "target_flow_id 指定了一个 NR 中不存在的 flow。"
                        "省略此参数将以新 flow 创建。"
                    ),
                    "category": "not_found",
                    "not_found_flow_id": _caller_target,
                    "validation": validation,
                }

        # Step 8: 部署到 NR
        try:
            result = self.nr.create_or_update_flow(fid, flow, force=True,
                                                   allow_prod=allow_prod)
        except Exception as e:
            _log_raw_deploy(agent_id, flabel, "DEPLOY_FAIL", f"NR error: {e}", validation)
            _record_fail()
            return {"ok": False, "stage": "deploy", "error": f"NR 部署失败: {e}",
                    "validation": validation}

        real_fid = result.get("id") or fid
        created = result.get("created", False)

        # Step 8.5: 【金丝雀】部署后立即内省 NR 子流程结构完整性，灭绝空壳假 PASS。
        # 若本次部署的 flow 自身是个空壳子流程定义（real_fid 落在 empty_shells），
        # 说明 deploy 虽返回成功但子流程内部节点未落盘（#607 复发态）→ 硬拦，
        # 阻止 catalog 登记一个「看似成功实则无取数能力」的部署。
        # 其他预先存在的空壳仅附在回执里 warning（fail-open），不误伤正常部署。
        nr_integrity: Dict[str, Any] = {"ok": True, "source": "skipped"}
        try:
            _integ = self.get_nr_subflow_integrity()
            nr_integrity = _integ
            if real_fid in (_integ.get("empty_shells") or []):
                _slog(_tid, "deploy_raw.canary_empty_shell",
                      elapsed=round(time.perf_counter() - _t0, 3), flow_id=real_fid)
                _record_fail()
                return {
                    "ok": False, "stage": "nr_canary",
                    "error": (f"部署已落 NR 但结构内省发现『空壳子流程』：flow_id={real_fid} "
                              f"在 NR 中无内部节点（无取数能力，等同 #607 复发态）。"
                              f"已阻止 catalog 登记。请重新部署该子流程定义，"
                              f"或用 autoflow_get_nr_flow 核对线上结构。"),
                    "nr_subflow_integrity": _integ,
                    "validation": validation,
                }
        except Exception as _ce:
            _slog(_tid, "deploy_raw.canary_err",
                  elapsed=round(time.perf_counter() - _t0, 3), error=str(_ce)[:200])
            nr_integrity = {"ok": False, "source": "error", "error": str(_ce)[:200]}

        # Step 9: 登记 catalog
        gateway_node_ids = [n.get("id") for n in nodes if n.get("id")]
        meta = {
            "flow_id": real_fid,
            "label": flow.get("label", ""),
            "owner_agent": agent_id,
            "purpose": "raw-deploy",
            "entities_touched": self._collect_entities(flow),
            "node_count": len(nodes),
            "deployed_node_ids": gateway_node_ids,
            "source_proposal": None,
            "source": "raw",
            "nr_url": getattr(self.cfg, "nr_url", ""),
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "validation_errors": len(errors),
            "validation_warnings": len(warnings),
        }
        self.state.upsert_flow(real_fid, meta)

        # Step 10: 日志 + 完整 flow 快照（供黑箱编译器迭代的语料）
        _log_raw_deploy(agent_id, flabel, "DEPLOY_OK",
                         f"id={real_fid} nodes={len(errors)}err/{len(warnings)}warn",
                         validation)
        snap = snapshot_flow(agent_id, "raw", flow.get("label", flabel), flow,
                             gate=gate, validation=validation, ok=True,
                             extra={"flow_id": real_fid, "created": created,
                                    "node_count": len(nodes)})

        _slog(_tid, "deploy_raw.done", elapsed=round(time.perf_counter() - _t0, 3),
              flow_id=real_fid, created=created, node_count=len(nodes))
        _hist.clear()  # 成功部署 → 清零失败计数（允许后续正常迭代）

        # D5-a（C5）：回显 authored→minted 映射。Agent 提交时用的 id 可能被 NR 重写
        # （NR 只接受 16 位 hex），真实落盘 id 与请求 id 不同时透明回显。
        _resp = {
            "ok": True,
            # WB24 NEW-F5（透明性）：回显最终归一化后的 flow_json，便于调用方/测试核对
            # 部署前的归一化结果（如 trigger-state 的 version/entities 改写、HA server 注入、id 重映射）。
            "flow_json": flow,
            "snapshot": snap,
            "flow_id": real_fid,
            "created": created,
            "_trace_id": _tid,
            "label": flow.get("label", ""),
            "node_count": len(nodes),
            "validation": validation,
            "lint": lint_issues,
            "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
            "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
            # A18：闸被要求跑却没跑 → 报告里如实降级 warn（部署侧的 fail-open 策略
            # 不改，但「验证结论」不能替它撒谎说 pass）。
            "gate": self._build_unified_gate(gate, _e2e, nr_integrity,
                                             staging_required=_staging_required),
            "logic": _logic_block,
            "deployed_at": meta["deployed_at"],
            "source_agent": agent_id,
            "nr_subflow_integrity": nr_integrity,
        }
        if real_fid != fid:
            _resp["authored_id"] = fid
            _resp["minted_id"] = real_fid
        return _resp

    def verify_flow(self, flow_json: Dict, agent_id: str = "verify",
                    run_gate: bool = True, require_e2e: bool = False,
                    target: str = "staging", allow_prod: bool = False) -> Dict[str, Any]:
        """白盒质量验证（只读，绝不部署）：跑与 deploy_raw 同源的质量闸，但不写 NR / 不登记 catalog。

        用途：agent / WB2 在部署前或回归时，按需校验一份 flow 的质量（schema + lint + 可选 vhass
        staging 闸 + 结构金丝雀 + 可选 e2e 追踪），拿到统一 verdict，而不污染部署路径。

        与 deploy_raw 的区别：
          - 复用 validate_flow_schema / lint_flow / run_staging_gate / get_nr_subflow_integrity /
            run_e2e_trace_raw 等同一套只读原语；
          - 跳过所有写副作用：Bark/history 子流程 ensure、HA server 注入早退、冲突检测、defense 检查、
            id 重映射、create_or_update_flow、catalog 登记、失败预算记录。

        参数：
          - flow_json: 待验证 flow 对象 {id, label, nodes:[...]}
          - run_gate: 是否跑 vhass staging 闸（含 HA 动作且能提取实体时）
          - require_e2e: 是否跑 e2e 实机追踪（落 staging + 触发 + 抓 trace + 回滚，默认关）
          - target: e2e 目标 staging/prod

        返回 {
          ok: True,                       # verify 恒 ok（是检查不是动作）；flow 问题落在 validation/lint
          verdict: "block"|"warn"|"pass", # 来自 _build_unified_gate
          passed: bool,
          deployed: False,                # 显式声明本次未部署
          gate: {verdict, passed, layers, notes},
          validation: [...], lint: [...], lint_error_count, lint_warning_count,
          _trace_id,
        }
        """
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        if not isinstance(flow_json, dict) or "nodes" not in flow_json:
            return {"ok": False, "stage": "input", "deployed": False,
                    "error": "flow_json 必须是包含 'nodes' 数组的 JSON 对象"}
        flow = dict(flow_json)  # 浅拷贝，避免改动入参
        nodes = flow.get("nodes", [])
        _slog(_tid, "verify_flow.start", agent_id=agent_id, run_gate=run_gate,
              require_e2e=require_e2e, node_count=len(nodes))

        # Step 1: Schema 校验（只读）
        validation = self.validate_flow_schema(flow)
        errors = [v for v in validation if v.get("level") == "error"]
        warnings = [v for v in validation if v.get("level") == "warning"]

        # Step 2: 静态 Flow Linter（只读，非阻塞）
        lint_issues = lint_flow(flow, b1_unreachable=True)
        validation.extend(lint_issues)
        errors = [v for v in validation if v.get("level") == "error"]
        warnings = [v for v in validation if v.get("level") == "warning"]

        # Step 3: 可选 vhass staging 闸（只读：断言预期后条件，不部署）
        # 【A18】三处硬伤一并修：
        #   1) 旧代码把 expected 用错 kwarg（expected_postconditions=）传给 run_staging_gate，
        #      必抛 TypeError 被 except 吞成 skipped → 闸门**从来没跑过**；
        #   2) 旧代码伪造一段注释 DSL 让闸门去 parse，注定编译失败 → 改走 flow= 直通口；
        #   3) 期望提取只认 turn_on/turn_off，非 on/off 动作一律提不出 → 现按 vhass
        #      建模表推导，推不出的如实登记为「不可验证」并让顶层降级 warn。
        has_ha_actions = any(
            n.get("type") in ("api-call-service", "server-state-changed",
                              "api-current-state", "function")
            for n in nodes
        )
        staging_required = bool(run_gate and has_ha_actions)
        unverifiable: List[str] = []
        if not run_gate:
            gate = {"skipped": True, "reason": "run_gate=False（调用方未要求跑 staging 闸）"}
        elif not has_ha_actions:
            gate = {"skipped": True, "reason": "flow 无 HA 动作节点，没有后置条件可断言"}
        else:
            expected_auto, unverifiable = _auto_expected_from_nodes(nodes)
            # 【V-NEW-2 / V-F4】纯 function 流无 api-call-service → expected_auto 为空，
            # 但 function 黑箱副作用仍须经闸门诚实性判定（_function_only），故强制跑闸；
            # 其余无自动后置条件的（纯 link out / subflow 属 Tier A 人工验证）按设计跳过。
            _force_run = bool(expected_auto) or any(
                n.get("type") == "function" for n in nodes)
            if _force_run:
                try:
                    gate = self.run_staging_gate(dsl="", expected=expected_auto, flow=flow)
                    if unverifiable:
                        gate.setdefault("warnings", []).append(
                            "部分动作的后置条件无法自动推导，未纳入断言："
                            + "；".join(sorted(set(unverifiable))))
                except Exception as _ge:
                    gate = {"skipped": True, "reason": f"staging 闸异常: {_ge}"}
            else:
                gate = {"skipped": True,
                        "reason": ("flow 含 HA 动作，但没有任何后置条件可自动推导，闸未运行："
                                   + "；".join(sorted(set(unverifiable)) or ["未知原因"]))}

        # Step 4: 结构金丝雀（只读内省 NR 子流程完整性；NR 不可达则跳过，不误伤验证）
        nr_integrity: Dict[str, Any] = {"ok": True, "source": "skipped"}
        try:
            nr_integrity = self.get_nr_subflow_integrity()
        except Exception as _ce:
            nr_integrity = {"ok": False, "source": "error",
                            "error": f"结构金丝雀内省失败（NR 不可达？）: {_ce}"}

        # Step 5: 可选 e2e 实机追踪（落 staging + 触发 + 抓 trace + 回滚；默认关）
        _e2e = None
        if require_e2e:
            try:
                _e2e = self.run_e2e_trace_raw(flow, target=target, live=False, allow_prod=allow_prod)
            except Exception as _ee:
                _e2e = {"e2e": False, "verdict": "拦截", "error": f"E2E 验证异常：{_ee}"}

        unified = self._build_unified_gate(gate, _e2e, nr_integrity,
                                           require_e2e=bool(require_e2e),
                                           staging_required=staging_required)
        if unverifiable:
            unified["notes"].append(
                "以下动作的后置条件未被验证（vhass 无法用 state 断言）："
                + "；".join(sorted(set(unverifiable))))
        # 【A / 保守 fail-closed】静态 lint 出现 error 级硬伤 → 直接硬拦（不再 fail-open 放行）。
        # warning 维持放行（warn），仅 error 升级为 block（含 C6 R2-ESC / C8 R_NO_TRIGGER）。
        _lint_err = sum(1 for v in lint_issues if v.get("level") == "error")
        if _lint_err:
            unified["verdict"] = "block"
            unified["passed"] = False
            unified["notes"].append(
                f"静态 lint 发现 {_lint_err} 个 error 级硬伤（详见 lint）→ 按 fail-closed 硬拦[A]")

        _slog(_tid, "verify_flow.done", elapsed=round(time.perf_counter() - _t0, 3),
              verdict=unified["verdict"], passed=unified["passed"])
        return {
            "ok": True,
            "deployed": False,
            "verdict": unified["verdict"],
            "passed": unified["passed"],
            "gate": unified,
            "validation": validation,
            "lint": lint_issues,
            "lint_error_count": sum(1 for v in lint_issues if v.get("level") == "error"),
            "lint_warning_count": sum(1 for v in lint_issues if v.get("level") == "warning"),
            "_trace_id": _tid,
        }

    def propose_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                    label: Optional[str] = None,
                    target: str = "staging", force: bool = False,
                    run_gate: bool = True, dry_run: bool = False,
                    require_e2e: bool = False) -> Dict[str, Any]:
        """白盒提案闸：接受 Agent 产出的原始 Node-RED flow JSON，经校验后【落提案】而非直写 NR。

        与 deploy_raw 复用同一套校验（schema + lint 硬伤集 R13/R15/R17/R20/R22 +
        L2 逻辑仿真 + HA server 替换），但**不写 NR、不登记 catalog**，而是把「准备就绪的
        flow + 校验摘要」存入 ProposalStore（kind="skill"，content.type="raw_flow"），
        交由人类在 WebUI 提案面板审核后，由 deploy_proposal 的 raw_flow 分支一步部署。

        设计取向（fail-closed 分层，方案A）：校验/lint/逻辑问题**只附在提案内容里供人审**，
        不拒绝落提案（保留 agent 探索性 fail-open）——但**无歧义硬错**（lint error 级阻断集 +
        未知节点类型）会聚合成 `deploy_blocked_reasons` 字段随提案落档并回显，明确预告
        「这提案会在部署阶段被硬拦」，消除信号模糊。仅「输入非 flow 对象」这类结构性错误才返回 ok=False。

        dry_run=True：跑完全部校验 + HA 替换 + id 重映射后返回预览（含 remap 后的 flow），
        不落提案。供 Agent/WebUI 部署前确认「这版 flow 长啥样、会不会被 lint 拦」。

        返回 {ok, proposal_id, label, node_count, validation, lint, lint_error_count,
              lint_warning_count, logic, node_gate_ok, flow(预览), dry_run}。
        """
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        _slog(_tid, "propose_raw.start", agent_id=agent_id, target=target, run_gate=run_gate,
              node_count=len(flow_json.get("nodes", []) if isinstance(flow_json, dict) else []))
        # Step 1: 输入校验（结构性错误才直接拒绝；其余问题 fail-open 进提案）
        if not isinstance(flow_json, dict) or "nodes" not in flow_json:
            _slog(_tid, "propose_raw.error", sub_stage="input",
                  elapsed=round(time.perf_counter() - _t0, 3),
                  error="flow_json 必须是包含 'nodes' 数组的 JSON 对象")
            return {"ok": False, "stage": "input",
                    "error": "flow_json 必须是包含 'nodes' 数组的 JSON 对象"}

        flow = dict(flow_json)  # 浅拷贝避免修改入参
        nodes = flow.get("nodes", [])
        if label:
            flow["label"] = label
        if not flow.get("label"):
            flow["label"] = f"{agent_id}-{datetime.now().strftime('%H%M%S')}"

        # Step 2: Schema 校验（非致命 error 仅记录，fail-open；致命项见下方 R9）
        validation = self.validate_flow_schema(flow)
        errors = [v for v in validation if v["level"] == "error"]
        warnings = [v for v in validation if v["level"] == "warning"]
        # R9(#round4)：schema 致命项（S1..S5）必须并入阻塞信号，否则提案回执会出现
        # 「would_block_on_lint=false + node_gate_ok=true」的绿灯，而这条流真去 deploy_raw
        # 会被 stage=schema_block 拦下——提案与部署两套口径，正是 A19 复现的自相矛盾。
        _schema_blocking = schema_blocking_issues(validation)

        # Step 2.5: 静态 Flow Linter（A1）
        lint_issues = lint_flow(flow, b1_unreachable=True)
        validation.extend(lint_issues)
        errors = [v for v in validation if v["level"] == "error"]
        warnings = [v for v in validation if v["level"] == "warning"]
        _LINT_BLOCK_RULES = {"R13", "R15", "R20", "R17", "R22", "R24", "R30", "R32", "R_SERVICE_PARAM", "R36", "R2-ESC", "R_NO_TRIGGER", "R16", "R40"}
        _blocking = [v for v in lint_issues
                     if v.get("level") == "error" and v.get("rule") in _LINT_BLOCK_RULES]
        _blocking = _schema_blocking + _blocking

        # 【Phase B · B4】L2 逻辑可达性闸门（fail-open，仅报告）
        try:
            _sim = simulate_flow(flow)
            _logic_block = {
                "ok": _sim.get("ok", True),
                "logic_issues": _sim.get("logic_issues", []),
                "unreachable_actions": _sim.get("unreachable_actions", []),
                "action_endpoints": _sim.get("action_endpoints", []),
                "reachable_actions": _sim.get("reachable_actions", []),
                "scenarios": _sim.get("scenarios", []),
                "summary": _sim.get("summary", ""),
            }
        except Exception as _e:
            _logic_block = {
                "ok": True, "logic_issues": [], "unreachable_actions": [],
                "action_endpoints": [], "reachable_actions": [], "scenarios": [],
                "summary": f"逻辑仿真跳过（simulator error: {_e}）",
            }
        _logic_err = _logic_block.get("unreachable_actions", [])

        # Step 3: HA server 占位符替换（REPLACE_WITH_HA_SERVER → 真实 id）
        _, unresolved = self._inject_ha_server(flow)
        if unresolved:
            return {"ok": False, "stage": "ha_server_inject",
                    "error": self._ha_server_unresolved_msg(unresolved)}

        # Step 4: 节点注册表闸门（P0 防御）—— 未知节点类型记 error 不拦提案（fail-open）
        _node_gate_ok = True
        try:
            self._gate_node_types(flow)
        except Exception as _ng:
            _node_gate_ok = False
            validation.append({"level": "error", "rule": "node_gate", "node_id": "_root",
                               "message": f"节点类型校验未通过: {_ng}"})
        # iss_b3ec73228d 修复：node_gate_ok 必须继承 lint 硬拦信号，消除响应内自相矛盾。
        # 此前同一响应既报 would_block_on_lint=True（会被硬伤规则拦下）又报
        # node_gate_ok=True（可部署绿灯），会误导上层把空参数节点当成能跑通。
        # 只要存在阻塞集中的硬伤，即视为不可部署（node_gate_ok=False）。
        if _blocking:
            _node_gate_ok = False

        # WB 提案闸 fail-closed 分层(方案A)：聚合「无歧义硬错」为 deploy_blocked_reasons。
        # 提案阶段仍 fail-open 落档（不阻 agent 探索），但把会在部署阶段(deploy_proposal
        # raw_flow 分支 / deploy_raw)被硬拦的硬伤聚合成单一可读字段，供 agent/人审一眼看清
        # 「这提案为何会被拦」，消除此前 blocking_rules/would_block_on_lint/node_gate_ok
        # 分散表述导致的信号模糊(A19 同类自相矛盾根因)。部署阶段硬拦不变。
        deploy_blocked_reasons = [
            {"rule": b.get("rule", "unknown"),
             "level": b.get("level", "error"),
             "message": b.get("message", ""),
             "node_id": b.get("node_id")}
            for b in _blocking
        ]
        # 未知节点类型(node_gate 抛错)也并入硬错聚合(其 rule="node_gate")
        if not _node_gate_ok and not any(r["rule"] == "node_gate" for r in deploy_blocked_reasons):
            deploy_blocked_reasons.append(
                {"rule": "node_gate", "level": "error",
                 "message": "含未知/未注册节点类型，部署阶段将被节点注册表闸门硬拦",
                 "node_id": "_root"})

        # dry_run → 预览（跑完校验 + HA 替换 + id 重映射，不落提案）
        if dry_run:
            fid = self._gen_raw_flow_id(agent_id, flow)
            _flow_preview, _id_map, _had_z = self._remap_raw_flow_ids(flow, fid)
            _slog(_tid, "propose_raw.dry_run", elapsed=round(time.perf_counter() - _t0, 3),
                  node_count=len(nodes))
            return {
                "ok": True, "dry_run": True,
                "flow_id": fid, "label": flow.get("label", ""),
                "node_count": len(nodes),
                "validation": validation,
                "lint": lint_issues,
                "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
                "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
                "would_block_on_lint": bool(_blocking),
                "would_block_on_schema": bool(_schema_blocking),   # R9
                "schema_blocking": _schema_blocking,               # R9
                "would_block_rules": sorted({b.get("rule") for b in _blocking}),
                "logic": _logic_block,
                "node_gate_ok": _node_gate_ok,
                "blocked": bool(_blocking),
                "deploy_blocked_reasons": deploy_blocked_reasons,
                "flow": _flow_preview,
                "_trace_id": _tid,
            }

        # A19：致命 schema 错误（S1..S5）必须阻断落提案——坏流不得静默进提案；
        # lint/logic 仍走 fail-open 供人审（仅结构性错误在更前已拦）。
        if _schema_blocking:
            _slog(_tid, "propose_raw.schema_block", schema_blocking=_schema_blocking,
                  elapsed=round(time.perf_counter() - _t0, 3))
            return {
                "ok": False, "stage": "schema_block",
                "error": "flow 含致命 schema 错误，已拒绝落提案",
                "schema_blocking": _schema_blocking,
                "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
                "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
                "would_block_rules": sorted({b.get("rule") for b in _blocking}),
                "logic": _logic_block, "node_gate_ok": _node_gate_ok,
                "_trace_id": _tid,
            }

        # Step 5: 落提案（fail-open：lint/logic 问题只附在 content，供人审决定）
        content = {
            "type": "raw_flow",
            "flow": flow,  # 已做 HA server 替换；deploy 时再 remap id + 写 NR
            "target": target,
            "force": force,
            "run_gate": run_gate,
            "agent_id": agent_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),
            "require_e2e": bool(require_e2e),
            "validation": validation,
            "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
            "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
            "blocking_rules": sorted({b.get("rule") for b in _blocking}),
            "schema_blocking_rules": sorted({b.get("rule") for b in _schema_blocking}),  # R9
            "logic": _logic_block,
            "node_gate_ok": _node_gate_ok,
            "blocked": bool(_blocking),
            "deploy_blocked_reasons": deploy_blocked_reasons,
        }
        title = flow.get("label", "") or f"raw-flow-{agent_id}"
        # 派生可检索 spec：label + 节点类型直方图（不 dump 整段 flow JSON）
        _hist: Dict[str, int] = {}
        for _n in nodes:
            _t = _n.get("type", "?")
            _hist[_t] = _hist.get(_t, 0) + 1
        _hist_str = ", ".join(f"{_t}×{_c}" for _t, _c in sorted(_hist.items()))
        _spec = f"{title}｜{len(nodes)} nodes" + (f": {_hist_str}" if _hist_str else "")
        try:
            store = ProposalStore(self.cfg)
            p = store.submit(agent_id, title, "skill", json.dumps(content, ensure_ascii=False),
                             source="raw", spec=_spec)
            proposal_id = p.id
        except Exception as e:
            _slog(_tid, "propose_raw.submit_fail", error=str(e),
                  elapsed=round(time.perf_counter() - _t0, 3))
            return {"ok": False, "stage": "proposal_store", "error": f"提案落档失败: {e}"}

        _slog(_tid, "propose_raw.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, label=flow.get("label", ""))
        return {
            "ok": True,
            # WB24 NEW-F5（透明性）：回显归一化后的 flow_json（已完成 HA server 注入/占位符回退），
            # 便于 autoflow_deploy_raw 调用方核对归一化结果（如 trigger-state 的 version/entities 改写），
            # 无需等人类在 WebUI 部署后才知情。
            "flow_json": flow,
            "proposal_id": proposal_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),
            "validation": validation,
            "lint": lint_issues,
            "lint_error_count": sum(1 for v in lint_issues if v["level"] == "error"),
            "lint_warning_count": sum(1 for v in lint_issues if v["level"] == "warning"),
            "would_block_on_lint": bool(_blocking),
            "would_block_on_schema": bool(_schema_blocking),   # R9
            "schema_blocking": _schema_blocking,               # R9
            "blocking_rules": sorted({b.get("rule") for b in _blocking}),
            "logic": _logic_block,
            "node_gate_ok": _node_gate_ok,
            "require_e2e": bool(require_e2e),
            "deploy_blocked_reasons": deploy_blocked_reasons,
            "_telemetry": _tag_action("propose_raw", {"ok": True}, agent_id,
                                      extra={"proposal_id": proposal_id,
                                             "label": flow.get("label", "")},
                                      log_path=self._telemetry_log),
        }

    def propose_subflow(self, dsl_name: str, name: str, definition: Dict[str, Any],
                        description: str = "", agent_id: str = "unknown-agent") -> Dict[str, Any]:
        """子流程提案闸：接受 Agent 产出的子流程定义，经结构校验后【落提案】而非直写 NR。

        与 propose_dsl / propose_raw 同构——都是「agent 提交 → 人类 WebUI 审核 → 部署」的
        统一提案管道，仅 content.type 不同（此处为 "subflow"）。注册动作（写 NR 子流程实例
        + 入 subflow_registry）发生在人类点「部署」时，由 deploy_proposal 的 subflow 分支原子完成。

        definition 必填字段：id（NR 子流程 id，如 "sf_my_thing"）、nodes（节点数组）、
        in_ports（输入端口定义）、out_ports（输出端口定义）；其余可选（info/category/...）。
        仅结构性错误（缺字段）返回 ok=False；其余（如 NR 重名）在部署阶段由 deploy_proposal 处理。

        返回 {ok, proposal_id, dsl_name, name, node_count, label}。"""
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        _slog(_tid, "propose_subflow.start", agent_id=agent_id, dsl_name=dsl_name)
        if not dsl_name or not str(dsl_name).strip():
            return {"ok": False, "stage": "input", "error": "dsl_name 必填（DSL 调用名）"}
        # D18/round10：dsl_name 是 DSL 调用名（subflow_registry 主键），必须符合
        # 标识符字符集（字母/数字/下划线、首字符非数字）——工具描述已声明该约束，
        # 旧实现未校验，'bad-name!' 之类被接受，问题延后到 DSL『调用子流程: bad-name!』
        # 解析/部署时才暴露。非法字符在提案阶段即拦截。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(dsl_name).strip()):
            return {"ok": False, "stage": "input",
                    "error": f"dsl_name 含非法字符：{dsl_name!r}。"
                             "dsl_name 必须是标识符（首字符为字母或下划线，"
                             "其后为字母/数字/下划线），如 my_subflow / bark_push_2。"}
        if not name or not str(name).strip():
            return {"ok": False, "stage": "input", "error": "name 必填（子流程可读名）"}
        if not isinstance(definition, dict):
            return {"ok": False, "stage": "input", "error": "definition 必须是 JSON 对象"}
        _required = ("id", "nodes", "in_ports", "out_ports")
        _missing = [k for k in _required if k not in definition]
        if _missing:
            return {"ok": False, "stage": "input",
                    "error": f"definition 缺少必填字段: {_missing}"}
        if not isinstance(definition.get("nodes"), list):
            return {"ok": False, "stage": "input", "error": "definition.nodes 必须是节点数组"}

        _nodes = definition.get("nodes", [])
        _label = name or dsl_name
        # WB85 F4：子流程提案也要跑 flow_linter（与顶层 propose_dsl 一致），
        # 让 R41 等反模式警告对子流程可见——旧实现不 lint，子流程可藏『link in→api 终点』
        # 这类 round21 竞态危险形状。非阻断：仅随回执透出，由人类在 WebUI 审核时看到。
        lint_issues = lint_flow({"nodes": _nodes})
        lint_summary = [{"rule": v.get("rule"), "level": v.get("level"), "message": v.get("message")}
                        for v in lint_issues if v.get("level") in ("error", "warning")]
        lint_error_count = sum(1 for v in lint_issues if v.get("level") == "error")
        lint_warning_count = sum(1 for v in lint_issues if v.get("level") == "warning")
        _slog(_tid, "propose_subflow.lint", lint_errors=lint_error_count,
              lint_warnings=lint_warning_count)
        content: Dict[str, Any] = {
            "type": "subflow",
            "dsl_name": dsl_name,
            "name": name,
            "definition": definition,
        }
        if description:
            content["description"] = description
        spec = description or f"{name}｜{dsl_name}｜{len(_nodes)} nodes"
        try:
            store = ProposalStore(self.cfg)
            p = store.submit(agent_id, _label, "subflow",
                             json.dumps(content, ensure_ascii=False),
                             source="subflow", spec=spec)
            proposal_id = p.id
        except Exception as e:
            _slog(_tid, "propose_subflow.submit_fail", error=str(e),
                  elapsed=round(time.perf_counter() - _t0, 3))
            return {"ok": False, "stage": "proposal_store", "error": f"提案落档失败: {e}"}
        _slog(_tid, "propose_subflow.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, dsl_name=dsl_name)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "dsl_name": dsl_name,
            "name": name,
            "node_count": len(_nodes),
            "label": _label,
            "lint": lint_issues,
            "lint_summary": lint_summary,
            "lint_error_count": lint_error_count,
            "lint_warning_count": lint_warning_count,
            "_telemetry": _tag_action("propose_subflow", {"ok": True}, agent_id,
                                      extra={"proposal_id": proposal_id,
                                             "dsl_name": dsl_name},
                                      log_path=self._telemetry_log),
        }

    def undeploy(self, flow_id: str, force: bool = False) -> Dict[str, Any]:
        """安全撤回本网关部署的 flow —— 手术式移除，绝不误删用户内容。

        原则（用户明确指定）：网关写了什么，撤回就撤什么。
        做法：只删 flow_catalog 中登记的网关节点 ID；删完若 tab 已空（无用户节点）
        → 删除整个 tab；若 tab 还有用户节点 → 仅移除网关节点、保留 tab 与用户节点。
        用户写在同 tab 里的 flow 永不丢失。

        护栏不变：账本外的 flow 直接拒绝（不删用户自己的独立 flow）。
        force 参数保留为兼容项——手术式移除本身不会删除用户数据，故无需强制确认。
        返回 {ok, action: 'trimmed_tab'|'deleted_tab'|'already_gone',
              gateway_nodes_removed, user_nodes_preserved, label}。
        """
        cat = self.state.get_flow_catalog().get("flows", {})
        meta = cat.get(flow_id)
        if not meta or not meta.get("owner_agent"):
            return {"ok": False, "error": "该 flow 不在本网关账本中（可能不是网关部署的，或已被撤回），拒绝操作。", "code": "not_ours"}

        deployed_ids = set(meta.get("deployed_node_ids") or [])
        label = meta.get("label", "")

        # 读活 flow（可能用户已手动改/加节点）
        live = None
        nr_unreachable = False
        nr_err = None
        try:
            live = self.nr.get_flow(flow_id)
        except Exception as e:
            nr_err = str(e)
            # 404 / not found 表示 flow 已不存在 → 视为 already_gone，不依赖 force
            if "404" in nr_err or "not found" in nr_err.lower():
                live = None
                nr_unreachable = False
            else:
                nr_unreachable = True
                live = None

        if live is None:
            if nr_unreachable:
                # NR 不可达：无法确认 flow 是否还在 → 保留账本，报错让用户重试。
                # 不清账本（否则会孤儿化：flow 仍在 NR、账本说 not_ours → 永久撤不掉）。
                if force:
                    # force 强制清账本：用户确认 NR 侧已手动删除或实例不可达
                    self.state.remove_flow(flow_id)
                    src = meta.get("source_proposal")
                    if src:
                        try:
                            ProposalStore(self.cfg).clear_deployed(src)
                        except Exception:
                            pass
                    return {"ok": True, "action": "already_gone", "flow_id": flow_id,
                            "label": label, "gateway_nodes_removed": 0,
                            "user_nodes_preserved": 0,
                            "note": "NR 不可达，force=true 强制清账本；请确认 NR 侧无残留"}
                return {"ok": False,
                        "error": "NR 不可达，无法确认 flow 状态，账本保留以便重试。"
                                 "若确认已手动删除，可 force=true 强制清账本。",
                        "code": "nr_unreachable", "flow_id": flow_id}
            # 活 flow 确实不在（404）：清账本，无需碰 NR
            self.state.remove_flow(flow_id)
            src = meta.get("source_proposal")
            if src:
                try:
                    ProposalStore(self.cfg).clear_deployed(src)
                except Exception:
                    pass
            return {"ok": True, "action": "already_gone", "flow_id": flow_id,
                    "label": label, "gateway_nodes_removed": 0, "user_nodes_preserved": 0}

        live_nodes = live.get("nodes", [])
        tab_node = next((n for n in live_nodes if n.get("type") == "tab"), None)
        gateway_nodes = [n for n in live_nodes if n.get("id") in deployed_ids]
        user_nodes = [n for n in live_nodes
                      if n.get("id") not in deployed_ids and n.get("type") != "tab"]

        # 带外删除防护：用户可能已在 NR UI 手动删掉本网关节点（或部分节点）。
        # 若账本登记的网关节点在活 flow 里一个都不剩 → 视为已撤回，直接清账本返回，
        # 不再去碰 NR（否则 delete_flow/update_flow_nodes 会撞「不存在/半残」而硬失败）。
        if not gateway_nodes:
            self.state.remove_flow(flow_id)
            src = meta.get("source_proposal")
            if src:
                try:
                    ProposalStore(self.cfg).clear_deployed(src)
                except Exception:
                    pass
            return {"ok": True, "action": "already_gone", "flow_id": flow_id,
                    "label": label, "gateway_nodes_removed": 0,
                    "user_nodes_preserved": len(user_nodes),
                    "note": "活 flow 中已无本网关节点（可能已被手动删除），仅清账本"}

        # 清理用户节点里指向已删网关节点的悬空连线
        valid_ids = {n["id"] for n in user_nodes}
        for n in user_nodes:
            if n.get("wires"):
                n["wires"] = self._clean_wires(n["wires"], valid_ids)

        g_removed = len(gateway_nodes)
        u_preserved = len(user_nodes)

        nr_ok = True
        nr_err = None
        if u_preserved == 0:
            # tab 已空 → 删除整个 tab（clean）
            try:
                self.nr.delete_flow(flow_id, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 删除失败: {e}"
            action = "deleted_tab"
        else:
            # 仅移除网关节点，保留 tab + 用户节点（手术式）
            reduced = dict(live)  # 保留 label/configs 等所有原始字段
            reduced["nodes"] = ([tab_node] if tab_node else []) + user_nodes
            try:
                self.nr.update_flow_nodes(flow_id, reduced, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 更新失败: {e}"
            action = "trimmed_tab"

        # NR 侧删除/更新失败后，仍清账本：避免 mutation 半残导致网关注册表永远卡死。
        # 原则：get_flow 阶段已确认这是本网关部署的 tab；mutation 失败通常是 NR 侧
        # 瞬时/权限问题，保留 ledger 会让用户无法重试/重部署。返回 nr_warning 供人审。
        self.state.remove_flow(flow_id)
        src = meta.get("source_proposal")
        if src:
            try:
                ProposalStore(self.cfg).clear_deployed(src)
            except Exception:
                pass
        if not nr_ok:
            return {"ok": True, "action": action, "flow_id": flow_id, "label": label,
                    "gateway_nodes_removed": g_removed, "user_nodes_preserved": u_preserved,
                    "nr_warning": nr_err,
                    "note": "NR 侧撤回调用失败，已清网关账本；NR 可能有残留，请手动确认"}
        return {"ok": True, "action": action, "flow_id": flow_id, "label": label,
                "gateway_nodes_removed": g_removed, "user_nodes_preserved": u_preserved}

    # ── A5 · runtime observe-correct 闭环（1990 MVP，只读观测 + 归因建议）────────

    def _flow_has_event_trigger(self, flow_id: str) -> bool:
        """判断 flow 是否含事件型触发器（server-state-changed）：这类需真实外部事件才点燃，
        短窗观测未变化不一定是失败。"""
        try:
            live = self.nr.get_flow(flow_id)
        except Exception:
            return False
        return any(n.get("type") == "server-state-changed"
                   for n in live.get("nodes", []))

    # ── 断言式「部署后观测 D」（tap 风格：断言预期后置条件 + 尽力 NR debug 快照）──
    # （旧版轮询式 observe_after_deploy_loop 已移除，属 D3 串行缓解首刀）本方法做「预期 vs 实际」断言，
    # 被 tap_observe.py / test_observe_post.py / test_observe_loop.py 依赖。
    def observe_after_deploy(self, expected: List[Dict],
                               flow_id: Optional[str] = None) -> Dict[str, Any]:
        """部署后观测（tap 风格）：HA 状态断言 + 尽力而为的 NR debug 快照。

        - HA 侧：observe_postconditions（读真实 HA 状态，与预期比对）。
        - NR 侧：若 nr 层暴露 debug 抓取能力则尽力快照，否则仅在报告中标注
          「需 1990 授权」——本方法不阻塞，真实 tap 依赖线上 1990 凭据。
        返回合并观测报告。
        """
        ha_obs = self.observe_postconditions(expected)
        nr_note = None
        nr_debug = None
        try:
            snapper = getattr(self.nr, "capture_debug", None)
            if callable(snapper) and flow_id:
                nr_debug = snapper(flow_id)
        except Exception:
            nr_debug = None
        if nr_debug is None and flow_id is not None:
            nr_note = ("NR debug 快照需 1990 实例授权（NR Admin API / debug 抓取）；"
                       "HA 侧观测已可用。")
        return {
            "ok": ha_obs["ok"],
            "ha": ha_obs,
            "nr_debug": nr_debug,
            "nr_note": nr_note,
            "flow_id": flow_id,
        }

    def run_staging_gate(self, dsl: str, expected: List[Dict],
                         resolved_entities: Optional[List[str]] = None,
                         vhass_store=None,
                         scenario: Optional[List[Dict]] = None,
                         virtual_time=None,
                         branch_aware: bool = True,
                         target: str = "staging",
                         flow: Optional[Dict[str, Any]] = None,
                         require_change: bool = False) -> Dict[str, Any]:
        """staging 闸门：编译 DSL → 把 flow 的 HA 意图重放到 vhass → 断言后置条件。

        不依赖真实 NR/HA：编译产物的 api-call-service 节点即『这个 flow 要对 HA 做的意图』，
        直接在内存 vhass 上重放并断言。子流程(link out/subflow) 属 Tier A 基础设施，
        作为外部调用记录、不参与断言（其效果是人工验证过的）。

        - flow：白箱直通口。给了就跳过 DSL 解析/编译，直接重放这份 NR flow。
          （旧实现逼白箱路径伪造一段「注释 + JSON」的假 DSL，parse 必失败 →
           闸门等于从未运行，是 A18 假 pass 的直接成因。）

        - 返回 {passed, replayed_services, external_calls, assertions, failures, entity_count}
        """
        from .dsl_engine import (parse, compile, DSLError, Action, Switch,
                                  Parallel, detect_semantic_gaps)
        from . import vhass as _vh

        scene = None
        if flow is None:
            try:
                scene = parse(dsl)
                flow = compile(scene, target=target)
            except DSLError as e:
                return {"passed": False, "stage": "compile", "error": str(e),
                        "compile_error": _compile_error_envelope(e),
                        "result_kind": "compile_error",
                        "verdict": "拦截", "reasons": [f"编译失败：{e}"]}

        # 0.5) 语义缺口预检（B1）：含历史/首次意图却未用对应原语 → 高声拦截，
        #      不让『静默降级成读当前态』的假阳性 flow 进黑名单之外的任何下游。
        #      （白箱直通口无 DSL 文本，此检查不适用）
        gaps = detect_semantic_gaps(dsl) if scene is not None else []
        if gaps:
            return {"passed": False, "stage": "semantic_gap", "error": "；".join(gaps),
                    "verdict": "拦截", "reasons": gaps}

        # 0) 实体存在性校验：引用的 entity_id 必须存在于真实设备目录
        #    （防假阳性——假/拼错的 entity_id 不应让闸门误判 PASS；vhass 是空白假 HA，
        #     会无脑接收任何 ID，所以必须靠目录校验兜底。agent 应通过发现工具取真实 ID。）
        unknown = self._check_entities_known(scene) if scene is not None else []
        if unknown:
            return {
                "passed": False,
                "stage": "entity_check",
                "failures": unknown,
                "error": "以下 entity_id 不在设备目录，请通过发现工具(autoflow_discover/"
                         "autoflow_search)确认正确实体后重试：" + "；".join(unknown),
                "verdict": "拦截",
                "reasons": [f"实体校验未通过：{', '.join(unknown)}（请通过发现工具确认正确实体后重试）"],
            }

        # 0.4) resolve 白名单校验（防『合法但错』的实体绕过语义意图）：
        #   若 agent 声明了 resolved_entities（来自 autoflow_resolve_entity 的确认结果），
        #   DSL 引用的所有实体必须 ⊆ resolved_entities，否则拦截。
        #   这直接消灭『把显示器挂灯错配成书房电脑开关』这类合法实体但语义错位的提交。
        if resolved_entities and scene is not None:
            declared = set(resolved_entities)
            used = self._collect_scene_entities(scene)
            rogue = [e for e in used if e not in declared]
            if rogue:
                return {
                    "passed": False,
                    "stage": "resolve_whitelist",
                    "failures": rogue,
                    "error": "以下实体未通过 autoflow_resolve_entity 确认：" + "；".join(rogue) +
                             "。请先用 autoflow_resolve_entity 解析设备中文名拿到真实 entity_id，"
                             "并把返回的 ID 作为 resolved_entities 传入 autoflow_propose_dsl。",
                    "verdict": "拦截",
                    "reasons": [f"实体未确认(应来自 resolve_entity)：{', '.join(rogue)}"],
                }

        # 0.7) 节点注册表闸门（P0 防御）：未知节点类型直接拦截，
        #      不让黑箱放行『编译合法但部署即坏』的 flow。
        try:
            self._gate_node_types(flow)
        except RuntimeError as e:
            return {"passed": False, "stage": "node_gate", "error": str(e),
                    "verdict": "拦截", "reasons": [str(e)]}

        store = vhass_store or self._build_vhass_from_staging()

        # 0) 世界态读取器（供条件门控评估）
        def _world(eid):
            rec = store.get_state(eid) if eid else None
            return rec.get("state") if rec else None

        # 1) 单步默认：把首个 state 触发态注入 vhass（兼容旧行为，供条件门控/断言参考）
        trig = next((t for t in scene.triggers if t.kind == "state"), None) \
            if scene is not None else None
        if trig and trig.kind == "state":
            tstate = trig.state if trig.state not in ("*", None) else "changed"
            try:
                from .dsl_engine import _STATE_ALIAS as _ALIAS
                tstate = _ALIAS.get(tstate, tstate)
            except Exception:
                pass
            try:
                store.inject_trigger(trig.entity, tstate)
            except Exception:
                pass
        elif scene is None:
            # 白箱直通口：无 scene，从 server-state-changed 节点还原触发态
            for nd in flow.get("nodes", []):
                if nd.get("type") != "server-state-changed":
                    continue
                _ent = (nd.get("entities") or {}).get("entity") or []
                _eid = (_ent[0] if isinstance(_ent, list) and _ent
                        else nd.get("entityId") or nd.get("entity_id"))
                _st = nd.get("ifState")
                if _eid and _st:
                    try:
                        store.inject_trigger(_eid, _st)
                    except Exception:
                        pass
                break

        # 2) 重放（分支感知）：单步 = 一个 step；scenario = 多步时间线
        steps = scenario if scenario else [{"expected": expected}]
        step_results = []
        warnings = []
        # 【WB84·P3-F2/P3-F4】跨步收集错域 service 命中，供 _unverified 诚实降级 fully_verified。
        _domain_mismatch_hits: List[Dict] = []
        # 【A12】flow 里声明过的全部外部调用名（不论本步是否可达）。
        # 与「本步真的被激活的 external」对照，可把失败精确归因为
        #「压根没这个子流程」还是「有但挂在死分支」。
        declared_subflows = [
            (nd.get("name") or nd.get("type") or "subflow")
            for nd in flow.get("nodes", [])
            if _vg_is_external_call(nd.get("type"))
        ]
        # 【G3】编译器 R31 判定的恒假分支（引用未声明字段）+ 其下游永不执行的动作面。
        dead_rules = _vg_dead_switch_rules(flow)
        dead_ents, dead_subs = _vg_dead_branch_reach(flow, dead_rules)
        dead_branches = [
            {"node_id": sid, "rule_index": i, "undefined_fields": toks}
            for sid, rules in dead_rules.items() for i, toks in rules.items()
        ]
        # 【G2】flow 是否**声明**了任何会产生效果的节点：有声明却 0 重放 = 什么都没验证。
        has_effect_nodes = any(
            nd.get("type") == "api-call-service" or _vg_is_external_call(nd.get("type"))
            for nd in flow.get("nodes", []))
        # 【V-F4】function 为黑箱，其潜在副作用 vhass 无法建模；若 flow 仅含 function
        # （无显式 api-call-service 效果），闸门无法证实其是否产生副作用。
        has_function_nodes = any(
            nd.get("type") == "function" for nd in flow.get("nodes", []))
        replay_zero_steps = []
        conservative_hits = []  # 【V-F1】跨步收集「保守命中」的 JSONata 分支
        _declared_effect_unreplayed_steps = []  # 【V-NEW-1】声明效果却 0 重放且不可归因于已知原因
        for step in steps:
            # 2a) 应用本步世界事件（多步场景逐步推进现实态）
            for eid, st in (step.get("world") or {}).items():
                try:
                    store.inject_trigger(eid, st)
                except Exception:
                    pass
            vt = step.get("virtual_time", virtual_time)
            step_report = {}
            # 2b) 评估当前世界态下应执行的 api-call-service（分支感知）
            if branch_aware:
                active = _vg_evaluate_active_intents(
                    flow, _world, vt, warnings, dead_rules, step_report)
            else:
                # 非分支感知：所有 HA 动作 + 所有子流程/link out 都算「会执行」，
                # 否则 external_calls 恒空，A12 的子流程断言会全体误判 FAIL。
                active = {n["id"] for n in flow.get("nodes", [])
                          if n.get("type") == "api-call-service"
                          or _vg_is_external_call(n.get("type"))}
            if step_report.get("conservative"):
                conservative_hits.extend(step_report["conservative"])
            # 2c) 重放激活意图（link out/subflow 作外部调用记录）
            replayed = []
            external = []
            # 2b-bis) 记录每个后置条件实体在重放『之前』的种子态 + 本步被重放的实体，
            # 用于区分「flow 重放导致状态变化」vs「状态在重放前已满足（巧合，未验证副作用）」。
            _pre_states = {c.get("entity_id"): _world(c.get("entity_id"))
                           for c in step.get("expected", []) if c.get("entity_id")}
            _replayed_targets: set = set()
            # 【WB84·P3-F2/P3-F3/P3-F4】service 域 vs 实体域一致性校验收集：
            # 错域调用（如 switch.turn_on 作用于 light）语义错误，两闸必须一致拦截，
            # 且不得宣称 fully_verified。homeassistant 域可作用于任意实体，豁免。
            _domain_mismatch: List[Dict] = []
            for nd in flow.get("nodes", []):
                if nd.get("type") == "api-call-service" and nd["id"] in active:
                    # 统一解析：兼容编译产物(domain/service/entityId)与手写(action/data)
                    domain, service, targets, data = _ha_node_call(nd)
                    for t in targets:
                        _ent_dom = t.split(".", 1)[0] if "." in t else ""
                        if (domain and _ent_dom and domain != "homeassistant"
                                and _ent_dom != domain):
                            _domain_mismatch.append(
                                {"entity_id": t, "domain": domain,
                                 "service": service, "ent_domain": _ent_dom})
                            replayed.append(f"{domain}.{service}({t})#domain_mismatch")
                            _replayed_targets.add(t)
                            continue
                        payload = dict(data)
                        payload["entity_id"] = t
                        try:
                            store.apply_service(domain, service, payload)
                            replayed.append(f"{domain}.{service}({t})")
                            _replayed_targets.add(t)
                        except Exception as e:  # pragma: no cover
                            replayed.append(f"{domain}.{service}({t})#err:{e}")
                elif _vg_is_external_call(nd.get("type")):
                    if nd["id"] in active:
                        external.append(nd.get("name") or nd.get("type") or "subflow")
            # 【WB91·P3-F1/P3-F2 修复】分支感知后置条件断言：
            # 设备不可能同时 on 又 off，来自「未激活分支」的后置条件在本世界态下永不可达
            # （典型即条件流的「反向切换 else」）。把这些 (entity_id, state) 收为 inactive_effects，
            # 断言时跳过（不计入失败）——否则 verify_flow 对一切含反向 else 的合法条件流恒拦，
            # 与 propose 方向相反（P3-F2 复活）。active 集合在 branch_aware=False 时含全部节点，
            # 故该逻辑对非分支感知模式自动失效（退回旧行为，全部断言），安全。
            inactive_effects = set()
            for _nd in flow.get("nodes", []):
                if _nd.get("type") == "api-call-service" and _nd["id"] not in active:
                    _dm, _sv, _tg, _dt = _ha_node_call(_nd)
                    for _t in _tg:
                        _st, _why = _expected_state_for(_dm, _sv, _dt)
                        if _st is not None:
                            inactive_effects.add((_t, _st))
            # 2d) 断言后置条件
            assertions = []
            failures = []
            for cond in step.get("expected", []):
                eid = cond.get("entity_id")
                want_sub = cond.get("subflow") or cond.get("subflow_name")
                if eid:
                    want = cond.get("state")
                    # 来自未激活分支的后置条件：当前世界态下该分支不会执行，
                    # 断言它必失败（设备处于另一态）→ 假阳性。跳过（非失败、非 N/A）。
                    if (eid, want) in inactive_effects:
                        assertions.append({
                            "kind": "state", "entity_id": eid,
                            "expected": want, "actual": None, "ok": True,
                            "branch_inactive": True,
                            "reason": ("该后置条件来自未激活分支（当前世界态下该分支不执行），"
                                       "按 P3-F1/P3-F2 修复跳过断言（非失败）")})
                        continue
                    rec = store.get_state(eid)
                    got = rec.get("state") if rec else None
                    ok = (got == want)
                    # iss_b2ecd18673：区分「服务被调用且状态改变」与「状态在重放前已满足、
                    # flow 未证明副作用」。pre_state=重放前种子态；service_called=本步是否
                    # 有针对该实体的服务被重放；changed_by_replay=重放是否真的把状态翻成 want。
                    pre = _pre_states.get(eid)
                    serv_called = eid in _replayed_targets
                    changed = (pre is not None and pre != want)
                    # A14：失败可能只是「vhass 未建模该服务」，须与「flow 真错了」区分
                    unmodeled = ((rec or {}).get("attributes") or {}).get("_unmodeled_service")
                    item = {"kind": "state", "entity_id": eid,
                            "expected": want, "actual": got, "ok": ok,
                            "pre_state": pre, "service_called": serv_called,
                            "changed_by_replay": changed}
                    # 巧合命中：状态已满足、且本步无针对该实体的服务被重放 → 没证明 flow 副作用。
                    # require_change=True 时作为真失败（fail）；否则仅告警（不推翻 verdict）。
                    if ok and not changed and not serv_called:
                        item["coincidental"] = True
                        if require_change:
                            ok = False
                            item["ok"] = False
                            item["reason"] = (
                                (item.get("reason") + "；") if item.get("reason") else ""
                            ) + ("后置条件在重放前已满足且无针对该实体的服务被重放，"
                                 "require_change=True 要求状态发生变化 → 未通过")
                        else:
                            warnings.append(
                                f"【巧合命中】后置条件 {eid}={want} 在重放前已满足，且本步无针对"
                                f"该实体的服务被重放 → 未验证 flow 的副作用（服务未调用 / 已幂等）。"
                                f"如需强制验证请传 require_change=true。")
                    if rec is None:
                        # 归因清楚：不是「状态不对」，是这个实体压根不在设备目录里
                        item["reason"] = ("实体不在 vhass staging 设备目录"
                                          "（entity_id 拼错 / 设备未接入 / 目录未同步）")
                    if unmodeled:
                        item["unmodeled_service"] = unmodeled
                    # 【G3】期望依赖的动作挂在编译器判定的恒假分支下 → 明确标 N/A，
                    # 不是「设备没响应」，而是「这条分支根本不会执行」。仍算未通过
                    # （fail-closed：N/A ≠ pass），但归因直指分支字段写错。
                    if not ok and eid in dead_ents:
                        item["na"] = True
                        item["dead_branch"] = True
                        item["reason"] = (
                            (item.get("reason") + "；") if item.get("reason") else ""
                        ) + ("该期望依赖的动作挂在【恒假分支】下（分支引用未声明字段，"
                             "编译器 R31 已告警）→ 永不执行，后置条件无法达成 → 记 N/A")
                    assertions.append(item)
                    if not ok:
                        if unmodeled:
                            # 【WB84·P3-F1】vhass 未建模该 service 的真实副作用 → 后置状态
                            # 不可验证（非必然是 flow 的错）。降级为告警，不计入硬失败
                            # （否则合法但 vhass 未建模的开启类自动化会被默认误拦，使质量闸
                            # 不可用）。断言项已保留可读；_unverified 已含 _unmodeled →
                            # fully_verified 诚实置 False，不虚假宣称充分验证。
                            item["unmodeled_service"] = unmodeled
                            warnings.append(
                                f"【未建模服务·非硬拦】{eid} 期望={want}：vhass 未建模 "
                                f"{unmodeled} 的真实副作用，后置状态无法验证（dry-run 下不据此"
                                f"硬拦；请跑 e2e 实机验证以确证）。")
                            continue
                        fail = {"entity_id": eid, "expected": want, "actual": got}
                        if item.get("reason"):
                            fail["reason"] = item["reason"]
                        if item.get("dead_branch"):
                            fail["na"] = True
                            fail["dead_branch"] = True
                            fail["reason"] = item["reason"]
                        if unmodeled:
                            fail["unmodeled_service"] = unmodeled
                            fail["hint"] = (f"vhass 未建模 {unmodeled} 的真实副作用，"
                                            f"后置状态无法验证（非必然是 flow 的错）")
                        failures.append(fail)
                elif want_sub:
                    # 【A12】真验证：期望被调用的子流程必须在本步真的可达并被激活。
                    # 旧实现只读 entity_id/state，对 {"subflow": x} 恒 ok=(None==None)=True。
                    hit = next((c for c in external if _sub_name_match(want_sub, c)), None)
                    declared = any(_sub_name_match(want_sub, d) for d in declared_subflows)
                    ok = hit is not None
                    _na = (not ok) and any(_sub_name_match(want_sub, d) for d in dead_subs)
                    if ok:
                        why = f"已调用（{hit}）"
                    elif _na:
                        why = ("该子流程挂在【恒假分支】下（分支引用未声明字段，"
                               "编译器 R31 已告警）→ 永不被调用 → 记 N/A")
                    elif declared:
                        why = ("flow 中存在该子流程节点，但本步世界态下不可达"
                               "（死分支 / 条件未命中 / 未接线）")
                    else:
                        why = (f"flow 中没有任何 link out / 子流程实例匹配「{want_sub}」；"
                               f"已声明的外部调用：{declared_subflows or '无'}")
                    _a = {"kind": "subflow", "subflow": want_sub,
                          "entity_id": None, "expected": f"调用 {want_sub}",
                          "actual": hit or "未调用", "ok": ok, "reason": why}
                    if _na:
                        _a["na"] = True
                        _a["dead_branch"] = True
                    assertions.append(_a)
                    if not ok:
                        _f = {"subflow": want_sub, "expected": f"调用 {want_sub}",
                              "actual": "未调用", "reason": why}
                        if _na:
                            _f["na"] = True
                            _f["dead_branch"] = True
                        failures.append(_f)
                else:
                    # 【A12·fail-closed】识别不了的期望项一律判失败，绝不静默放行。
                    why = ("无法识别的期望项：需含 {entity_id, state} 或 {subflow}，"
                           f"实收 {json.dumps(cond, ensure_ascii=False)}")
                    assertions.append({"kind": "unknown", "entity_id": None,
                                       "expected": cond, "actual": None,
                                       "ok": False, "reason": why})
                    failures.append({"expected": cond, "actual": None, "reason": why})
            # 【WB84·P3-F2/P3-F3/P3-F4】错域 service 失败：与后置条件断言同级，
            # 作为真实失败计入 failures（fail-closed 拦截），且在 assertions 中可观测。
            # 这让 propose 内嵌闸 与 verify_flow 对「switch.turn_on 作用于 light」这类
            # 错域调用给出一致结论，并杜绝 fully_verified 虚假宣称。
            _domain_mismatch_hits.extend(_domain_mismatch)
            for dm in _domain_mismatch:
                _dm_reason = (
                    f"service 域({dm['domain']})与实体域({dm['ent_domain']})不一致："
                    f"{dm['domain']}.{dm['service']} 作用于 {dm['entity_id']} 属语义错误"
                    f"（应为 {dm['ent_domain']}.{dm['service']}）。")
                assertions.append({
                    "kind": "domain_mismatch", "entity_id": dm["entity_id"],
                    "expected": f"{dm['domain']}.{dm['service']}",
                    "actual": f"实体域={dm['ent_domain']}", "ok": False,
                    "domain_mismatch": True, "reason": _dm_reason})
                failures.append({
                    "entity_id": dm["entity_id"],
                    "expected": f"{dm['domain']}.{dm['service']}",
                    "actual": f"实体域={dm['ent_domain']}",
                    "domain_mismatch": True, "reason": _dm_reason})
            # 2c-bis)【G2 / 报告 A15】重放归零检测：flow 明明声明了动作，本步却
            # 一个 HA 意图、一个外部调用都没重放 → 闸门**什么都没验证**。
            # 若归零可归因于「闸门无法本地求值的 JSONata」或「编译器判定的恒假分支」，
            # 就绝不能报「验证通过」——那正是 A15 的假过路径。
            _cause = []
            if step_report.get("dead"):
                _cause.append("恒假分支（R31 未定义字段）")
            if step_report.get("conservative"):
                _cause.append("无法本地求值的 JSONata")
            if step_report.get("unevaluable"):
                _cause.append("闸门无法求值的分支条件（引用未声明属性 / 不支持的类型化规则）")
            _zero = has_effect_nodes and not replayed and not external and bool(_cause)
            if _zero:
                replay_zero_steps.append(len(step_results))
                warnings.append(
                    "【重放归零】本步 0 个 HA 意图 + 0 个外部调用被重放，"
                    "原因：" + "、".join(_cause) +
                    "。闸门实际未验证任何行为，不构成『通过』。"
                    "请补 否则: 分支 / 改用闸门可求值的条件 / 修正分支字段名。")
            # 【V-NEW-1 窄修】声明了效果节点，但本步 0 重放且不可归因于
            # 恒假分支 / 保守命中 / 不可求值（即「声明了却什么都没验证」，
            # 典型如条件流分支未命中、种子态已满足后置）→ 闸门实际未验证其效果。
            # 降级 verdict=未充分验证（与 A22/V-F1/V-F4 同构，不拦截），消除
            # fully_verified 过度宣称；不误伤合法条件流的负向验证（无运动→灯恒 off）。
            if has_effect_nodes and not replayed and not external and not bool(_cause):
                _declared_effect_unreplayed_steps.append(len(step_results))
            step_results.append({
                "world": step.get("world", {}),
                "replayed_services": replayed,
                "external_calls": external,
                "assertions": assertions,
                "failures": failures,
                "replay_zero": _zero,
            })

        # 2e) A14：未建模服务导致「后置状态压根没被真实改动」，必须显式告警而非静默
        _unmodeled = list(getattr(store, "unmodeled_calls", []) or [])
        if _unmodeled:
            warnings.append(
                "以下服务 vhass 未建模真实副作用，其后置状态【未被验证】："
                + "、".join(_unmodeled))

        # 2f)【G2】重放归零的最终处置：默认 fail-closed（0 重放 ≠ 验证通过）。
        #     策略经 _replay_zero_policy() 可切换，对接 c4_replay_semantics 终裁。
        _rz_policy = _replay_zero_policy()
        _rz_block = bool(replay_zero_steps) and _rz_policy == "fail_closed"
        if replay_zero_steps and _rz_policy == "warn_only":
            warnings.append(
                "重放归零按 warn_only 策略保留放行（AUTOFLOW_REPLAY_ZERO_POLICY）："
                "该结论**未经行为验证**，请人工确认。")

        # A22：存在「被跳过/未建模/重放归零 warn_only」的验证层时，即便断言全过也不算充分验证，
        # verdict 降级为「未充分验证」（而非「放行」），消除「零验证报 pass」假象。
        # 【V-F1】复杂 JSONata 无法本地求值却「保守命中」→ 条件未经逻辑校验即视为通过，
        # 属未覆盖层，须降级（否则 fully_verified=True 假绿）。
        # 【V-F4】function-only 流（黑箱副作用不可建模）且无显式 api-call-service 效果 →
        # 闸门无法证实其是否产生副作用，fully_verified 不可视为完整验证。
        _function_only = has_function_nodes and not has_effect_nodes
        _declared_effect_unreplayed = bool(_declared_effect_unreplayed_steps)
        _unverified = (
            bool(_unmodeled) or
            (bool(replay_zero_steps) and _rz_policy == "warn_only") or
            bool(conservative_hits) or
            _function_only or
            _declared_effect_unreplayed or
            bool(_domain_mismatch_hits)  # 【WB84·P3-F4】错域 service 不得宣称 fully_verified
        )
        if _function_only:
            warnings.append(
                "flow 含 function 节点（黑箱，其潜在副作用 vhass 无法建模），且未显式声明 "
                "api-call-service 效果 → 闸门无法证实其是否产生副作用，fully_verified 不可"
                "视为完整验证（V-F4 诚实性缺口）。")
        if _declared_effect_unreplayed:
            warnings.append(
                "flow 声明了效果节点（api-call-service / 子流程），但本步 0 个 HA 意图、"
                "0 个外部调用被重放，且不可归因于恒假分支 / 保守命中 / 不可求值 → 闸门实际"
                "未验证其效果，fully_verified 不可视为完整验证（V-NEW-1 诚实性缺口）。")
        if _unverified:
            warnings.append("验证存在未覆盖层（闸跳过/未建模服务/重放归零 warn_only/"
                            "保守命中 JSONata/function 黑箱副作用），"
                            "结论未充分验证，请勿视同已通过。")

        # 3) 汇总返回（单步与旧结构兼容；多步额外给 steps）
        if scenario:
            all_pass = all(not s["failures"] for s in step_results) and not _rz_block
            _verdict = "拦截" if not all_pass else ("未充分验证" if _unverified else "放行")
            return {
                "passed": all_pass,
                "fully_verified": (not _unverified) and all_pass,
                "verdict": _verdict,
                "steps": step_results,
                "step_count": len(step_results),
                "warnings": warnings,
                "dead_branches": dead_branches,
                "replay_zero_steps": replay_zero_steps,
                "replay_zero_policy": _rz_policy,
                "entity_count": len(store.entities),
            }
        sr = step_results[0]
        passed = len(sr["failures"]) == 0 and not _rz_block
        verdict = "拦截" if not passed else ("未充分验证" if _unverified else "放行")
        reasons = []
        for a in sr["assertions"]:
            # G3：恒假分支导致的未过标 [N/A]（不是设备没响应，是分支永不执行）
            if a.get("branch_inactive"):
                mark = "[跳过]"
            elif a["ok"]:
                mark = "[通过]"
            else:
                mark = "[N/A]" if a.get("na") else "[未过]"
            # A12：断言项现在有 state / subflow / unknown 三种，标签不能只认 entity_id
            label = a.get("entity_id") or a.get("subflow") or "期望项"
            line = f"{mark} {label} 期望={a['expected']} 实测={a['actual']}"
            if a.get("reason"):
                line += f"（{a['reason']}）"
            if a.get("unmodeled_service"):
                line += f"[vhass 未建模 {a['unmodeled_service']}]"
            reasons.append(line)
        reasons.append(
            f"重放 {len(sr['replayed_services'])} 个 HA 意图、记录 "
            f"{len(sr['external_calls'])} 个外部调用"
            + ("（分支感知）" if branch_aware else "")
        )
        for w in warnings:
            reasons.append(f"[警告] {w}")
        if _rz_block:
            reasons.append("[拦截] 重放归零：闸门未验证任何行为，按 fail-closed 处置")
        return {
            "passed": passed,
            "fully_verified": (not _unverified) and passed,
            "verdict": verdict,
            "reasons": reasons,
            "warnings": warnings,
            "replayed_services": sr["replayed_services"],
            "external_calls": sr["external_calls"],
            "assertions": sr["assertions"],
            "failures": sr["failures"],
            "dead_branches": dead_branches,
            "replay_zero": bool(replay_zero_steps),
            "replay_zero_policy": _rz_policy,
            "entity_count": len(store.entities),
        }

    def observe_postconditions(self, expected: List[Dict]) -> Dict[str, Any]:
        """tap 风格观测：按 expected_postconditions 读 HA 当前状态并断言。

        离线（vhass/FakeHA）与线上（真实 HA）通吃——都走 self.ha.get_state。
        这是「部署后观测 D」的核心：deploy 到 staging(1990) 后，把预期后置条件
        与 HA 真实状态比对，给出结构化观测报告（无需 24h 轮询，按需触发）。
        """
        assertions: List[Dict] = []
        failures: List[Dict] = []
        for cond in expected:
            eid = cond.get("entity_id")
            want = cond.get("state")
            rec = None
            if eid:
                try:
                    rec = self.ha.get_state(eid)
                except Exception:
                    rec = None
            got = rec.get("state") if rec else None
            ok = (got == want)
            assertions.append({"entity_id": eid, "expected": want, "actual": got, "ok": ok})
            if not ok:
                failures.append({"entity_id": eid, "expected": want, "actual": got})
        return {
            "ok": len(failures) == 0,
            "assertions": assertions,
            "failures": failures,
            "source": "ha",
        }

    # ───────────── P5 · E2E 执行追踪（真实跑通证明 + 断点报告）─────────────
    def _instrument_flow(self, flow: Dict, trace_key: str) -> Dict:
        """给编译产物插桩（P5 核心）——工程化的『断点节点』：

        - 每个**非 sink 原节点**后串一个 `function` tap，到达时把
          {node, t, topic, payload摘要} 写入 global[trace_key]（数组）。
        - 加一个 `catch` 节点（scope=全部原节点）+ 错误记录 function，
          把运行时错误 {error:true, node, message} 也写入同一 trace。
        不改动任何原节点的下游连线（tap 作为**额外分支**，输出不接任何节点），
        故原 flow 逻辑不受影响。部署后由 run_e2e_trace 统一回滚。

        注：tap 用 function 节点是**插桩专用**（E2E 测试 harness），
        不违反『编译器永不生成 function』的铁律——那是对用户 flow 编译的约束。
        返回插桩后的新 flow（含新增 tap / catch / 错误记录节点）。
        """
        import json as _json
        nodes = [dict(n) for n in flow.get("nodes", [])]  # 浅拷贝节点（不污染入参）
        by_id = {n["id"]: n for n in nodes if "id" in n}
        SINK = E2E_SINK_TYPES

        def _tap_fn(src_id: str) -> str:
            # 纯 JS；var k 指向 trace_key，到达即把源节点 id 推入 trace 数组
            return (
                "var k = " + _json.dumps(trace_key) + ";\n"
                "var a = (global.get(k) || []).slice();\n"
                "var p = (typeof msg.payload === 'object' ? JSON.stringify(msg.payload) : String(msg.payload));\n"
                "a.push({node: " + _json.dumps(src_id)
                + ", t: Date.now(), topic: msg.topic, payload: String(p).slice(0, 120)});\n"
                "global.set(k, a);\n"
                "return msg;"
            )

        # 1) 错误记录 function（catch 的下游）——把运行时错误也写进 trace
        err_sink_id = "af_e2e_err_" + secrets.token_hex(4)
        z0 = nodes[0].get("z") if nodes else None
        err_sink = {
            "id": err_sink_id, "type": "function", "z": z0,
            "name": "__e2e_err__",
            "func": (
                "var k = " + _json.dumps(trace_key) + ";\n"
                "var a = (global.get(k) || []).slice();\n"
                "var s = (msg.error && msg.error.source && msg.error.source.id) || '??';\n"
                "var m = (msg.error && msg.error.message) || 'unknown error';\n"
                "a.push({error: true, node: s, message: String(m).slice(0, 200)});\n"
                "global.set(k, a);\n"
                "return msg;"
            ),
            "outputs": 1, "_af_err_sink": True,
            "x": 50, "y": 50, "wires": [[]],
        }

        # 2) 每个非 sink 原节点后插一个 tap（额外分支，不改原连线）
        taps = []
        scope = []
        for n in nodes:
            # D20 修复：部分 NR 版本/部署下 debug 节点虽 passthrough=true 却
            # 不把 msg 转发到下游 wires，导致 e2e 插桩后 debug 下游全部误报断点、
            # 第六轮以来所有含「观测:」的 e2e 结果不可靠。在【插桩副本】里把
            # debug 替换为一个透传 function 代理（保留 id/wires/z/位置，msg 原样
            # return），使测试副本中 debug 的上下游连线真实贯通。真实部署回滚后
            # 用户 flow 仍保留原 debug 观测语义（插桩只在副本，不影响原 flow）。
            if n.get("type") == "debug":
                n["type"] = "function"
                n["func"] = "return msg;"
                n["outputs"] = 1
                # WB30 BUG-E2E-1 修复：debug 在 NR 原始导出中 wires=[]（0 输出终节点形态），
                # 转 function 透传代理后 declared outputs=1，但 wires 仍是 0 个数组 →
                # 部署侧 R10「期望 1 个 output 却得 0 个 wires 数组」拦截，导致所有
                # flow_json= 输入的 e2e 在 stage=deploy 失败（dsl= 路径因编译产物 wires=[[]] 正常）。
                # 此处把 wires 归一化为 1 个 output 数组（保留原下游目标，无则 [[]]）。
                w = n.get("wires")
                if isinstance(w, list) and len(w) == 1 and isinstance(w[0], list):
                    pass  # 已是 [[...]] 合法形态（如 dsl 编译产物），保留原下游
                else:
                    flat = []
                    if isinstance(w, list):
                        for x in w:
                            if isinstance(x, list):
                                flat.extend(x)
                            elif isinstance(x, str):
                                flat.append(x)
                    n["wires"] = [flat]
                n["_af_debug_proxy"] = True
                # 代理自身不插 tap（保持 sink 语义，不进 trace）
                continue
            # D34 修复：link out / link in 不再在此处插桩。
            #   - link out 保持 SINK 跳过（见下方 `if n.get("type") in SINK`），
            #     **绝不**给它加 wires——link out 无 wires 输出，强行加 wires 会破坏
            #     link 广播语义并触发 NR 运行时 TypeError。其到达由上游 tap 间接覆盖，
            #     link 穿越由 link in 的 tap 覆盖。
            #   - link in 已从 E2E_SINK_TYPES 移除，故落入下方普通插 tap 分支：
            #     它有正常 wires 输出，加 tap 分支安全，e2e 可真实记录 link 穿越成功。
            if n.get("type") in SINK or n.get("_af_trace_tap") or n.get("_af_err_sink"):
                continue
            scope.append(n["id"])
            tap_id = "af_e2e_tap_" + secrets.token_hex(4)
            tap = {
                "id": tap_id, "type": "function", "z": n.get("z"),
                "name": "__e2e__ " + (n.get("name") or n.get("id")),
                "func": _tap_fn(n["id"]), "outputs": 1, "_af_trace_tap": True,
                "x": (n.get("x", 100) + 140), "y": (n.get("y", 100) + 60),
                "wires": [[]],
            }
            wires = n.get("wires")
            if not wires:
                n["wires"] = [[tap_id]]
            else:
                new_wires = []
                for out in wires:
                    if isinstance(out, list):
                        new_wires.append(out + [tap_id])
                    else:
                        new_wires.append([out, tap_id])
                n["wires"] = new_wires
            taps.append(tap)

        # 3) catch 节点（捕获本 flow 全部原节点运行时错误）
        catch_id = "af_e2e_catch_" + secrets.token_hex(4)
        catch = {
            "id": catch_id, "type": "catch", "z": z0,
            "name": "__e2e_catch__", "scope": list(scope), "uncaught": False,
            "x": 50, "y": 30, "wires": [[err_sink_id]],
        }
        out = dict(flow)
        out["nodes"] = nodes + taps + [catch, err_sink]
        return out

    def _derive_planned_path(self, flow: Dict,
                             start_ids: Optional[List[str]] = None) -> List[str]:
        """从入口节点（inject / 无入边）沿 wires 做 BFS，给出『计划路径』节点 id 序列。
        作为未显式给定 expected_path 时的默认期望路径。

        D24/round12：多触发源 flow（如 inject + server-state-changed 汇聚到同一动作）
        下，若以『无入边节点』为起点，会把**未被触发**的事件入口（server-state-changed
        无入边、但本轮只触发了 inject）也纳入期望路径 → 其下游永不被点燃 → 误报『断点』。
        故允许调用方传入实际触发节点 id（inject_ids），此时 BFS 只从该集合起算，
        未触发的事件入口不计入 expected_count。start_ids 为空/None 时回退旧行为。"""
        nodes = flow.get("nodes", [])
        by_id = {n["id"]: n for n in nodes if "id" in n}
        incoming: Dict[str, List[str]] = {}
        for n in nodes:
            for w in (n.get("wires") or []):
                if isinstance(w, list):
                    for t in w:
                        incoming.setdefault(t, []).append(n["id"])
        # D25/round12 + round17 修正：link out → link in 经 Node-RED link 机制传递
        # （无 wires），BFS 需识别 link 边，否则 link 链路后的节点永远走不到、被误报断点。
        # link out 的 links 字段本就是它广播到的 link in 目标 id 集合；据此建立
        # link out → link in 的隐式边。D30：links 可能是对象数组 [{"id":...}]，
        # 用 _link_ids 统一剥成字符串集合，避免 `set([dict])` 抛 unhashable type: 'dict'。
        link_targets: Dict[str, List[str]] = {}
        for n in nodes:
            if n.get("type") != "link out":
                continue
            # ol = 本 link out 广播到的全部 link in 目标 id（D30：links 可能是
            # 对象数组 [{"id":...}]，用 _link_ids 统一剥成字符串集合，避免
            # `set([dict])` 抛 unhashable type: 'dict'）。
            ol = _link_ids(n.get("links"))
            if not ol:
                continue
            for m in nodes:
                # D25/round17 修正：link out 的 links 列表本就是它指向的 link in
                # 目标 id 集合，故「m 是 lo 的目标」的判定应为 `m["id"] in ol`，
                # 旧实现误写成 `set(m.get("links")) & ol`（拿 link in 的源列表去
                # 和 link out 的目标列表取交集，二者方向相反，永远为空）→ 链路后的
                # 节点永远走不到、被误报断点。此处改为正确的「目标包含」判定。
                if m.get("type") == "link in" and m["id"] in ol:
                    link_targets.setdefault(n["id"], []).append(m["id"])
        if start_ids:
            starts = [s for s in start_ids if s in by_id]
        else:
            starts = [n["id"] for n in nodes
                      if n.get("type") == "inject" or not incoming.get(n["id"])]
        seen: set = set()
        order: List[str] = []
        stack = list(starts)
        while stack:
            nid = stack.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            order.append(nid)
            n = by_id.get(nid)
            if not n:
                continue
            for w in (n.get("wires") or []):
                if isinstance(w, list):
                    stack.extend(w)
            # link 机制隐式边：link out 广播到的 link in
            if nid in link_targets:
                stack.extend(link_targets[nid])
        return order

    def _node_label(self, nodes: Dict, nid) -> Optional[str]:
        if not nid:
            return None
        n = nodes.get(nid)
        if not n:
            return nid
        return n.get("name") or n.get("type") or nid

    def _breakpoint_message(self, failed_at, nodes: Dict, errors: List[Dict]) -> str:
        if not failed_at:
            return "信息流完整跑通全部环节（无断点）。"
        fl = self._node_label(nodes, failed_at)
        if errors:
            e = errors[-1]
            en = self._node_label(nodes, e.get("node"))
            return (f"信息流在『{fl}』环节中断（未到达）。"
                    f"『{en or '?'}』报告运行时错误：{e.get('message')}")
        # 无运行时错误却断在「读取类」节点：极可能是实体不存在 → 静默无输出，
        # 与『连线断裂』是两种不同根因，必须区分，否则会误导排查方向。
        n = nodes.get(failed_at) or {}
        t = n.get("type")
        if t in ("api-current-state", "api-get-history"):
            field = "当前状态" if t == "api-current-state" else "历史记录"
            hint = ("『取值/查询』读的是当前状态" if t == "api-current-state"
                     else "『历史』读的是历史记录数组")
            return (f"信息流在『{fl}』环节中断（无运行时错误）。"
                    f"该节点类型『{t}』用于读取实体{field}——若目标实体在真实 HA 上"
                    f"不存在（或历史节点无记录），节点会静默无输出（msg 不向下游传递），"
                    f"{hint}。这容易被误判为『连线断裂』。"
                    f"请先经发现工具(autoflow_discover/autoflow_search)确认实体真实存在。")
        return (f"信息流在『{fl}』环节中断，后续节点未执行"
                f"（未捕获到运行时错误，可能是连线断裂或节点未产出 msg）。")

    def _compare_trace(self, flow: Dict, trace: List[Dict],
                       expected_path: Optional[List] = None,
                       trigger_ids: Optional[List[str]] = None,
                       expected_services: Optional[List[str]] = None,
                       expected_branch_taken: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """把真实 trace 与期望路径比对，产出断点报告。

        D8/round6：switch 的未命中分支 ≠ 断点——若某 switch 有其它分支被 reached，
        其未命中分支是【正常条件分支选择】，归入 unhit_branches，不计入 missing、
        不影响 verdict（否则任何带条件分支的 flow 都会被误报「断点」）。
        D9/round6：对 reached 的 api-call-service 做参数审计（domain/service/
        entityId/data/dataType + 字符串化动态参数检测）——e2e 此前只看节点到达，
        D6 类「参数被字符串化」完全逃逸；现把参数快照附进报告供 agent/人工审查。
        D10/round6：expected_path 条目支持 id 或 name 两种写法（name 按节点 label
        反查 id），统一在 id 空间比对——旧实现 nodes.get(name) 查不到节点导致
        inject 等 sink 过滤失效、missing/extra 语义矛盾。
        """
        nodes = {n["id"]: n for n in flow.get("nodes", []) if "id" in n}
        trace = trace or []
        reached = [t.get("node") for t in trace
                   if t and t.get("node") and not t.get("error")]
        errors = [t for t in trace if t and t.get("error")]
        # D10：expected_path 条目规范化——支持 id 或 name（name 反查节点 id）
        def _norm_ep(p):
            if isinstance(p, str):
                if p in nodes:
                    return p
                for _nid, _n in nodes.items():
                    if _n.get("name") == p:
                        return _nid
                return p  # 找不到：保留原样，进 missing 合理提示
            if isinstance(p, dict):
                pid = p.get("id")
                if pid in nodes:
                    return pid
                for _nid, _n in nodes.items():
                    if _n.get("name") == p.get("name"):
                        return _nid
                return pid or ""
            return ""
        if expected_path:
            expected_ids = [_norm_ep(p) for p in expected_path]
        else:
            planned_starts = list(trigger_ids) if trigger_ids else None
            expected_ids = self._derive_planned_path(flow, start_ids=planned_starts)
        # 只比对【可插桩】节点：inject/debug/link/catch 等不加 tap，永远不会
        # 在 trace 里自报，若纳入比对会被冤枉成『断点』（真机实测：曾把 inject
        # 触发器误报为 failed_at）。故按 E2E_SINK_TYPES 过滤期望路径。
        expected_ids = [e for e in expected_ids
                        if (nodes.get(e) or {}).get("type") not in E2E_SINK_TYPES]
        reached_set = set(reached)
        raw_missing = [e for e in expected_ids if e not in reached_set]
        # D8：switch 未命中分支 → unhit_branches（正常条件分支选择，非断点）
        sw_branches: Dict[str, set] = {}
        for _n in flow.get("nodes", []):
            if _n.get("type") != "switch":
                continue
            _tgts: set = set()
            for _w in (_n.get("wires") or []):
                if isinstance(_w, list):
                    _tgts.update(_w)
            sw_branches[_n["id"]] = _tgts
        unhit_branches: List[str] = []
        missing: List[str] = []
        for e in raw_missing:
            owners = [sid for sid, tgts in sw_branches.items() if e in tgts]
            # 该 switch 有其它分支被 reached → 未命中属条件分支选择
            if owners and any((sw_branches[o] - {e}) & reached_set for o in owners):
                unhit_branches.append(e)
            else:
                missing.append(e)
        extra = [e for e in reached if e not in set(expected_ids)]
        failed_at = missing[0] if missing else None

        # ── F12 (WB93)：期望动作集对称断言 ──
        # e2e 此前只判「节点可达」，不判「走的分支是否符合意图/世界态」：条件反置流
        # 仍判通过（wb93_f12_counterexample.py 坐实）。引入 expected_services /
        # expected_branch_taken：把实际 replay 的 api-call-service 集合与期望动作集做
        # 对称比对，不一致即升级 verdict 为「断点」（分支逻辑/意图校验失败）。
        def _svc_str(n):
            if not isinstance(n, dict) or n.get("type") != "api-call-service":
                return None
            d = (n.get("domain") or "").lower()
            s = (n.get("service") or "").lower()
            eid = n.get("entityId") or ""
            return f"{d}.{s}({eid})" if eid else f"{d}.{s}"
        reached_services = set()
        for _rid in reached:
            _s = _svc_str(nodes.get(_rid))
            if _s:
                reached_services.add(_s)
        _exp_services = set()
        if expected_services:
            for _s in expected_services:
                if _s:
                    _exp_services.add(str(_s).strip())
        if expected_branch_taken:
            for _bt in expected_branch_taken:
                if not isinstance(_bt, dict):
                    continue
                _sw = nodes.get(_bt.get("switch"))
                if not _sw or _sw.get("type") != "switch":
                    continue
                _br = int(_bt.get("branch", 0) or 0)
                _outs = _sw.get("wires") or []
                if _br < 0 or _br >= len(_outs) or not _outs[_br]:
                    continue
                # BFS 收该分支首个 api-call-service（其服务即该分支意图动作）
                _q = list(_outs[_br]); _seen = set()
                while _q:
                    _x = _q.pop(0)
                    if _x in _seen:
                        continue
                    _seen.add(_x)
                    _xn = nodes.get(_x)
                    if not _xn:
                        continue
                    _s = _svc_str(_xn)
                    if _s:
                        _exp_services.add(_s)
                        break
                    for _w in (_xn.get("wires") or []):
                        if isinstance(_w, list):
                            _q.extend(_w)
        service_mismatch = []
        if _exp_services:
            for _s in sorted(_exp_services - reached_services):
                service_mismatch.append({"kind": "missing", "service": _s})
            for _s in sorted(reached_services - _exp_services):
                service_mismatch.append({"kind": "extra", "service": _s})

        verdict = "通过" if (not missing and not errors and not service_mismatch) else "断点"
        # D9：动作参数审计——对 reached 的 api-call-service 输出参数快照 +
        # 字符串化动态参数检测（dataType=json 但值含 "payload.x" 引号包裹的动态引用）
        param_audit = []
        _dyn_lit_re = re.compile(r'"((?:payload|msg|flow|global)\.[A-Za-z_一-鿿][\w一-鿿]*)"')
        for _rid in reached:
            _n = nodes.get(_rid)
            if not _n or _n.get("type") != "api-call-service":
                continue
            _data = _n.get("data") or ""
            _susp = []
            if _n.get("dataType") == "json" and isinstance(_data, str):
                for _m in _dyn_lit_re.finditer(_data):
                    _susp.append(_m.group(1))
            param_audit.append({
                "node": self._node_label(nodes, _rid),
                "node_id": _rid,
                "domain": _n.get("domain"), "service": _n.get("service"),
                "entityId": _n.get("entityId"),
                "data": _data, "dataType": _n.get("dataType"),
                "suspicious_stringified_params": _susp,
            })
        return {
            "verdict": verdict,
            "reached": [self._node_label(nodes, i) for i in reached],
            "reached_ids": reached,
            "reached_count": len(reached),
            "expected_count": len(expected_ids),
            "missing": [self._node_label(nodes, i) for i in missing],
            "missing_ids": missing,
            "unhit_branches": [self._node_label(nodes, i) for i in unhit_branches],
            "unhit_branch_ids": unhit_branches,
            "extra": [self._node_label(nodes, i) for i in extra],
            "failed_at": self._node_label(nodes, failed_at),
            "failed_at_id": failed_at,
            "param_audit": param_audit,
            "param_warnings": [
                {"node": a["node"], "node_id": a["node_id"], "params": a["suspicious_stringified_params"]}
                for a in param_audit if a["suspicious_stringified_params"]
            ],
            "runtime_errors": [
                {"node": self._node_label(nodes, e.get("node")),
                 "node_id": e.get("node"),
                 "message": e.get("message")}
                for e in errors
            ],
            "service_mismatch": service_mismatch,
            "expected_services": sorted(_exp_services),
            "breakpoint": self._breakpoint_message(failed_at, nodes, errors),
        }

    def _e2e_result(self, verdict: str, stage: str, error: Optional[str] = None,
                      flow_id: Optional[str] = None,
                      reasons: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "e2e": False, "verdict": verdict, "stage": stage,
            "error": error, "flow_id": flow_id,
            "reasons": reasons or [],
            "report": {
                "verdict": verdict, "stage": stage, "error": error,
                "reached": [], "missing": [], "runtime_errors": [],
                "breakpoint": f"E2E 在「{stage}」阶段中断：{error or ''}",
            },
        }

    def _safe_delete(self, flow_id: str, allow_prod: bool = False) -> None:
        """删除 e2e-trace 临时部署的 flow。

        旧实现 `except Exception: pass` 会静默吞掉删除失败，导致 e2e-trace
        部署后回删 NR 失败时泄漏孤儿 tab 且无人察觉。改为：失败记 warning 并重试
        一次，仍失败记 error（含 flow_id），便于定位与人工清理。
        """
        for attempt in (1, 2):
            try:
                self.nr.delete_flow(flow_id, force=True, allow_prod=allow_prod)
                return
            except Exception as e:  # noqa: BLE001 — NR 删除异常类型不定，统一兜底
                if attempt == 1:
                    _gw_logger.warning(
                        "e2e-trace 删除 flow %s 失败（将重试一次）：%s", flow_id, e
                    )
                else:
                    _gw_logger.error(
                        "e2e-trace 删除 flow %s 重试仍失败，可能遗留孤儿 tab：%s",
                        flow_id, e,
                    )

    def _gate_node_types(self, flow: Dict[str, Any]) -> None:
        """节点注册表闸门（P0 防御）。

        编译产物的每个节点类型必须在目标 NR 已安装，否则部署即坏
        （陌生节点静默丢 msg，整条下游断掉，且白/黑箱都查不出）。
        仅在持有真实 NR client（有 get_installed_node_types）时生效；
        FakeNRLayer / 取不到注册表（/nodes 不可达或空）时自动跳过，不阻塞单测或合法部署。
        """
        client = getattr(self.nr, "client", None)
        if client is None:
            return
        gtr = getattr(client, "get_installed_node_types", None)
        if gtr is None:
            return
        try:
            installed = set(gtr())
        except Exception:
            return  # /nodes 不可达 → 不阻塞
        if not installed:
            return  # 注册表取空（含失败）→ 不阻塞（避免误杀合法部署）
        # 子流程引用（编译产物里节点 type 即子流程 id，如 b0bbc86abb2172a5）也是合法的——
        # 调色板注册表里没有子流程，故单独从目标 NR 取『已定义子流程 id』合并进已知类型集。
        # 子流程存在→正确放行；子流程真缺失→不在集合内→仍被拦截（正确）。
        try:
            req = getattr(client, "_request", None)
            if req is not None:
                resp = req("GET", "/flows")
                try:
                    data = __import__("json").loads(resp.text)
                except Exception:
                    data = None
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("flows", [])
                else:
                    items = []
                for f in items:
                    if isinstance(f, dict) and f.get("type") == "subflow":
                        sid = f.get("id")
                        if sid:
                            # 实例节点 type 是 "subflow:<id>"（带前缀），须与之一致
                            # 才能命中 check_unknown_node_types 的白名单。
                            installed.add(sid)
                            installed.add(f"subflow:{sid}")
        except Exception:
            pass  # 取不到子流程清单不阻塞
        unknown = check_unknown_node_types(flow, installed)
        if unknown:
            raise RuntimeError(
                "节点类型未注册（目标 NR 无此节点，部署即坏）："
                + ", ".join(sorted(set(unknown)))
            )

    def run_e2e_trace(self, dsl: str,
                       expected_path: Optional[List] = None,
                       expected_postconditions: Optional[List[Dict]] = None,
                       target: str = "staging",
                       live: bool = False,
                       allow_prod: bool = False,
                       expected_services: Optional[List[str]] = None,
                       expected_branch_taken: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """P5 · 端到端执行追踪：把 DSL 编译产物**真实部署到 1990**并触发，
        用插桩（tap + catch）抓取信息流实际跑到的每个环节，与期望路径比对，
        产出**断点报告**——明确流程跑到哪个环节、在哪里断、报错是什么。

        流程：
          1. 编译 DSL → flow
          2. 实体存在性校验（未知实体直接拦截，避免假阳性）
          3. 插桩（每非 sink 节点后加 tap + 全局 catch）
          4. 部署到 1990（server 占位符回填真实 HA server id）
          5. 触发 inject（或入口节点）
          6. 读回 global[trace_key] 上的真实执行轨迹
          7. 与 expected_path（缺省用计划路径）比对 → 断点报告
          8. 可选：observe_postconditions 校验 HA 副作用落地
          9. 回滚插桩副本 + 清空 trace context

        返回 {e2e, flow_id, verdict(通过/断点/拦截), reasons, report, trace, postconditions, live}
        —— agent 直接消费 report.breakpoint 即可知道「在哪断、为什么」。
        """
        from .dsl_engine import parse, compile, DSLError, detect_semantic_gaps
        run_token = secrets.token_hex(8)
        trace_key = f"__trace__:{run_token}"
        # 1) 编译
        try:
            scene = parse(dsl)
            flow = compile(scene)
        except DSLError as e:
            res = self._e2e_result("拦截", stage="compile", error=str(e),
                                    reasons=[f"编译失败：{e}"])
            res["compile_error"] = _compile_error_envelope(e)
            res["result_kind"] = "compile_error"
            return res
        # 2.5) 语义缺口预检（B1）：含历史/首次意图却未用对应原语 → 高声拦截
        gaps = detect_semantic_gaps(dsl)
        if gaps:
            return self._e2e_result("拦截", stage="semantic_gap", error="；".join(gaps),
                                     reasons=gaps)
        # 2) 实体存在性校验
        unknown = self._check_entities_known(scene)
        if unknown:
            return self._e2e_result(
                "拦截", stage="entity_check", error="实体校验未通过",
                reasons=[f"实体校验未通过：{', '.join(unknown)}（请通过发现工具确认正确实体后重试）"])
        # 3) 插桩 + server 占位符回填
        fid = self._gen_raw_flow_id("e2e", flow)
        remapped, id_map, _ = self._remap_raw_flow_ids(flow, fid)
        inst = dict(remapped)
        inst["id"] = fid
        _, unresolved = self._inject_ha_server(inst)
        if unresolved:
            return self._e2e_result("拦截", stage="ha_server_inject",
                                    error=self._ha_server_unresolved_msg(unresolved))
        inst = self._instrument_flow(inst, trace_key)
        # ③ 节点注册表闸门（P0 防御）：未知节点类型直接报错，不让坏 flow 上线
        self._gate_node_types(inst)
        # 3.5) 触发前准备：事件入口转合成 inject + 防 entityId 污染（与 run_e2e_trace_raw 共用）
        # 修复 WB4 #1：强制 api-current-state 读配置实体，不被合成入口的 msg.topic 覆盖；
        # 修复 WB4 #2：staging 状态触发器现已编译为 server-state-changed，此处原地转 inject 点燃。
        _nodes, inject_ids = self._e2e_prepare_flow(inst)
        # 4) 部署（失败由 NRRollbackError 兜底，这里捕获并产出拦截报告）
        try:
            dep = self.nr.create_or_update_flow(fid, inst, force=True, allow_prod=allow_prod)
            real_fid = dep.get("id") or fid
        except Exception as e:
            return self._e2e_result("拦截", stage="deploy", flow_id=fid,
                                     error=f"部署失败：{e}",
                                     reasons=[f"部署到 1990 失败：{e}"])
        # 5) 触发（inject_ids 已由 3.5 的 _e2e_prepare_flow 准备好：含手动 inject
        #    与「状态触发器转出的合成 inject」；无则可转换入口时回退从入口点燃）
        triggered = []
        try:
            if inject_ids:
                for iid in inject_ids:
                    self.nr.trigger_inject(iid)
                    triggered.append(iid)
            else:
                # 无 inject 且无可转换入口（极少见）：从入口节点启动（live 需真实 HA 事件；
                # fake/测试环境由 nr 层从入口节点模拟执行）
                self.nr.inject_flow(real_fid)
        except Exception as e:
            self._safe_delete(real_fid, allow_prod=allow_prod)
            return self._e2e_result("拦截", stage="inject", flow_id=real_fid,
                                     error=f"触发失败：{e}",
                                     reasons=[f"inject 触发失败：{e}"])
        # 6) 读回真实执行轨迹
        #    真实 HA 服务调用是异步的：inject 打完 msg 还在 NR 内部流转，
        #    需给一点落定时间再读 global[trace_key]，否则会读到空/半截 → 假断点。
        #    读回后解包 NR 的 {"msg","format"} 信封由 nr_client.get_context 负责。
        import time as _time
        settle = getattr(self, "_e2e_settle", None)
        if settle is None:
            settle = getattr(self.cfg, "e2e_settle_seconds", None)
        if settle is None:
            settle = 1.2
        settle = float(settle)
        trace = []
        deadline = _time.time() + settle
        while True:
            try:
                cur = self.nr.get_context("global", trace_key)
            except Exception:
                cur = None
            if isinstance(cur, list) and cur:
                trace = cur
                # 轨迹已含运行时错误或已到达 sink → 提前结束等待
                if any(t.get("error") for t in cur):
                    break
            if _time.time() >= deadline:
                if isinstance(cur, list):
                    trace = cur
                break
            _time.sleep(0.25)
        if not isinstance(trace, list):
            trace = []
        # 7) 比对 → 断点报告（用 remapped 原 flow 比，id 空间与 trace 一致）
        #    D10/round6：expected_path 原始节点 id 经 id_map 映射到重映射 id 空间
        #    （此前 DSL 版漏做映射，显式 expected_path 恒匹配不上 → missing 含
        #    全部条目、reached 与 missing 语义矛盾）。
        exp = expected_path
        if isinstance(exp, str):
            try:
                exp = json.loads(exp)
            except Exception:
                exp = None
        if isinstance(exp, list):
            def _map_ep(e):
                if isinstance(e, str):
                    return id_map.get(e, e)
                if isinstance(e, dict):
                    e = dict(e)
                    if e.get("id") in id_map:
                        e["id"] = id_map[e["id"]]
                return e
            exp = [_map_ep(x) for x in exp]
        report = self._compare_trace(remapped, trace, exp, trigger_ids=inject_ids,
                                      expected_services=expected_services,
                                      expected_branch_taken=expected_branch_taken)
        # 8) 可选：HA 副作用后置校验
        post = None
        if expected_postconditions:
            post = self.observe_postconditions(expected_postconditions)
            if not post["ok"]:
                report["verdict"] = "断点"
        # 9) 回滚插桩副本 + 清 trace（context 清理走 DELETE，见 nr_client.delete_context）
        self._safe_delete(real_fid, allow_prod=allow_prod)
        try:
            self.nr.delete_context("global", trace_key)
        except Exception:
            pass
        reasons = [f"E2E 路径：到达 {report['reached_count']}/{report['expected_count']} 环节"]
        if report.get("failed_at"):
            reasons.append(f"断点在：{report['failed_at']}")
        if report.get("runtime_errors"):
            reasons.append(f"运行时错误 {len(report['runtime_errors'])} 处")
        if report.get("service_mismatch"):
            reasons.append(f"动作与预期不符 {len(report['service_mismatch'])} 处"
                           f"（分支逻辑/意图校验失败）")
        return {
            "e2e": True,
            "flow_id": real_fid,
            "verdict": report["verdict"],
            "reasons": reasons,
            "report": report,
            "trace": trace,
            "postconditions": post,
            "live": live,
            "triggered": triggered,
        }

    # ───────────── C1 · 白箱 L3 运行时追踪（raw flow 入口）─────────────

    @staticmethod
    def _e2e_has_incoming(nodes, nid):
        for n in nodes:
            for w in (n.get("wires") or []):
                if isinstance(w, list) and nid in w:
                    return True
        return False

    @staticmethod
    def _normalize_e2e_flow_input(flow_json):
        """统一 run_e2e_trace 系列工具的 flow 入参形状，与 validate_flow/simulate_flow 对齐：
        - 字符串 → json.loads
        - 裸节点数组(list) → {"nodes": [...]}
        - {"nodes":[...]} → 原样
        返回 (flow_dict, error)。error 非空表示非法（缺 nodes）。
        修复压测报告 Bug-T1 现象1：裸节点数组此前被拒「缺少 nodes」，三姊妹工具入参形状不一致。
        """
        try:
            if isinstance(flow_json, str):
                data = json.loads(flow_json)
            else:
                data = flow_json
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return None, f"flow_json 不是合法 JSON：{e}"
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            return None, "flow_json 缺少 nodes（空 flow 或格式不支持）"
        return data, None

    def _inject_ha_server(self, flow):
        """把 flow 内占位符 server(REPLACE_WITH_HA_SERVER) 替换为真实 HA server id。

        返回 (ha_server, unresolved_count)：
        - ha_server：实际使用的 server id（空串表示未配置）。
        - unresolved_count：注入后**仍残留** REPLACE_WITH_HA_SERVER 的节点数。
          >0 表示「需要 HA 凭据却解析不出」，调用方应**前置硬拦**（fail-fast），
          而非把坏 flow 推给 NR 拿到不透明的 400（见 _ha_server_unresolved_msg）。
        deploy_raw 与 run_e2e_trace 系列共用此逻辑，避免部署路径漏接注入的漂移
        （见压测报告 Bug-T1: iss_35b5d34da2）。
        """
        ha_server = (getattr(self.cfg, "nr_ha_server_id", "")
                     or self.nr.get_default_server_id() or "")
        unresolved = 0
        if ha_server:
            for n in flow.get("nodes", []):
                if n.get("server") == "REPLACE_WITH_HA_SERVER":
                    n["server"] = ha_server
        else:
            # 无可用 server id：统计仍带占位符的节点（若有则部署必败）
            for n in flow.get("nodes", []):
                if n.get("server") == "REPLACE_WITH_HA_SERVER":
                    unresolved += 1
        return ha_server, unresolved

    @staticmethod
    def _ha_server_unresolved_msg(unresolved: int) -> str:
        """占位符需解析却解析不出时的清晰配置错误文案（fail-fast 用）。"""
        return (f"无法解析 HA server：nr_ha_server_id 为空且 NR 默认 server 缺失，"
                f"仍有 {unresolved} 个 HA 节点携带 REPLACE_WITH_HA_SERVER 占位符。"
                f"请检查网关配置(cfg.nr_ha_server_id)或在 NR 1990 配置 HA 凭据。")

    def _e2e_soft_check_entities(self, flow):
        """节点级实体存在性【软校验】：收集 HA 节点引用的 entityId，
        经 _resolve_best 尝试无歧义解析；无法解析的记入 warnings（不拦截）。"""
        warns = []
        fld_by_type = {
            "api-call-service": "entityId",
            "api-current-state": "entityId",
            "server-state-changed": "entityId",
            "poll-state": "entityId",
            "api-get-history": "entityId",
        }
        for n in flow.get("nodes", []):
            fld = fld_by_type.get(n.get("type"))
            if not fld:
                continue
            eid = n.get(fld)
            if not eid:
                warns.append(f"{n.get('type')} 节点(id={n.get('id')}) 缺少 {fld}")
                continue
            try:
                if self._resolve_best(eid) is None:
                    warns.append(f"{n.get('type')} 引用实体无法无歧义解析：{eid}")
            except Exception:
                pass
        return warns

    @staticmethod
    def _e2e_parse_trigger(trigger):
        if not trigger:
            return {}
        if isinstance(trigger, str):
            try:
                trigger = json.loads(trigger)
            except Exception:
                return {}
        if not isinstance(trigger, dict):
            return {}
        return {k: trigger.get(k) for k in ("entity_id", "state", "old_state")
                if trigger.get(k) is not None}

    @staticmethod
    def _e2e_entry_entity_id(node):
        """从事件入口节点抽取真实 entity_id（覆盖各 HASS 节点版本的存储位置）。

        触发节点形态多样：server-state-changed v6 把实体放在 ``entities.entity``
        （数组或字符串）；旧版/其它类型用顶层 ``entityId`` / ``entity`` /
        ``event``；server-event 用 ``entityId``。若入口本身确实没有实体绑定
        （如纯时间/注入触发器），才回退 ``unknown.entity``。
        """
        # 1) 显式 entityId（api-current-state / server-event / 旧版 trigger / poll-state）
        eid = node.get("entityId")
        if isinstance(eid, str) and eid:
            return eid
        # 2) server-state-changed v6：entities.entity 是数组或字符串
        ents = node.get("entities")
        if isinstance(ents, dict):
            ev = ents.get("entity")
            if isinstance(ev, list) and ev:
                return ev[0]
            if isinstance(ev, str) and ev:
                return ev
            ev2 = ents.get("entityId")
            if isinstance(ev2, list) and ev2:
                return ev2[0]
            if isinstance(ev2, str) and ev2:
                return ev2
        # 3) 顶层 entity（旧版 trigger / device）
        e = node.get("entity")
        if isinstance(e, str) and e:
            return e
        if isinstance(e, list) and e:
            return e[0]
        # 4) server-event 的 event 字段
        ev = node.get("event")
        if isinstance(ev, str) and ev:
            return ev
        # 5) topic 兜底（极少用）
        t = node.get("topic")
        if isinstance(t, str) and t:
            return t
        return "unknown.entity"

    @staticmethod
    def _e2e_entry_to_inject(node, trig):
        """把事件入口节点原地改为合成 inject（保留 id/wires/z，发出 faithful
        state-change msg：payload=新状态, topic=entity_id, data={entity_id,new,old}）。
        仅作用于【插桩副本】，不影响用户原始 flow。"""
        entity_id = (trig.get("entity_id")
                     or Gateway._e2e_entry_entity_id(node))
        state = trig.get("state", "on")
        old_state = trig.get("old_state", "off")
        node["type"] = "inject"
        node["topic"] = entity_id
        node["payload"] = json.dumps({"entity_id": entity_id, "state": state,
                                       "old_state": old_state})
        node["payloadType"] = "json"
        node["repeat"] = ""
        node["crontab"] = ""
        node["once"] = False
        node["onceDelay"] = 0.1
        node["props"] = [
            {"p": "payload", "v": node["payload"], "vt": "json"},
            {"p": "topic", "v": entity_id, "vt": "str"},
            {"p": "data", "v": json.dumps({"entity_id": entity_id,
                                            "new_state": state, "old_state": old_state}),
             "vt": "json"},
        ]
        for k in ("entityId", "entity", "for", "exposeToHomeAssistant",
                  "outputInitially", "outputOnConnect"):
            node.pop(k, None)
        return node

    def _e2e_prepare_flow(self, flow, trigger=None):
        """插桩副本部署前的触发准备（run_e2e_trace 与 run_e2e_trace_raw 共用）：

        1) 收集现有 inject 节点 id；
        2) 若无 inject 但含「事件入口」节点（server-state-changed / server-event /
           trigger … 且无上游连线）→ 原地转为合成 inject（发出 faithful 的
           state-change msg），使 vhass 无 websocket 环境下也能真实点燃下游逻辑；
        3) 对 api-current-state 节点设 ``blockInputOverrides=True``，防止合成入口的
           ``msg.topic`` 污染其 entityId（修复 WB4 #1：运行时
           ``ValidationError: "entityId" is required"``）。

        返回 ``(nodes, inject_ids)``。原地修改 ``flow`` 的 nodes（仅作用于插桩副本，
        不影响用户原始 flow）。
        """
        nodes = flow.get("nodes", [])
        inject_ids = [n["id"] for n in nodes if n.get("type") == "inject"]
        if not inject_ids:
            trig = self._e2e_parse_trigger(trigger)
            entry_ids = [n["id"] for n in nodes
                         if n.get("type") in E2E_STATE_ENTRY_TYPES
                         and not self._e2e_has_incoming(nodes, n["id"])]
            if entry_ids:
                for n in nodes:
                    if n["id"] in entry_ids:
                        self._e2e_entry_to_inject(n, trig)
                inject_ids = entry_ids  # 替换后已是 inject 类型
        # 防污染：e2e 副本强制 api-current-state 读各自配置的实体，不被上游 topic 覆盖
        for n in nodes:
            if n.get("type") == "api-current-state":
                n["blockInputOverrides"] = True
        return nodes, inject_ids

    def run_e2e_trace_raw(self, flow_json, expected_path=None,
                          expected_postconditions=None, target="staging",
                          live=False, trigger=None,
                          allow_prod: bool = False,
                          expected_services: Optional[List[str]] = None,
                          expected_branch_taken: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """C1 · 白箱 L3 运行时追踪：直接吃**原始 NR flow**（不经 DSL 编译），
        真实部署到 1990 并触发，用插桩抓取实际执行轨迹，与期望路径比对 → 断点报告。

        触发策略（比黑箱版更鲁棒）：
        - 有 inject 节点 → 真实触发每个 inject（trigger_inject）；
        - 无 inject 但含「事件入口」节点(server-state-changed / server-event / trigger …)
          → 在【插桩副本】里把这些入口替换为合成 inject（发出 faithful 的 state-change
          msg），再真实触发。下游逻辑(function/switch/call-service…)因此被真实执行、可被追踪。
          ⚠️ 合成触发是 test-double：验证的是『下游在给定事件下能否跑到 sink』，
             而非『真实 HA 经 websocket 把事件推给 NR』（vhass 暂无 websocket，P3 已知缺口）。
        - 既无 inject 又无可转换入口 → 拦截（说明书式返回，避免假阳性）。

        返回结构与 run_e2e_trace 一致：{e2e, flow_id, verdict, reasons, report, trace, …}。
        """
        run_token = secrets.token_hex(8)
        trace_key = f"__trace__:{run_token}"

        # 0) 解析 flow_json（与 validate_flow/simulate_flow 同形状：接受裸节点数组或 {"nodes":[...]}）
        flow, err = self._normalize_e2e_flow_input(flow_json)
        if err:
            return self._e2e_result("拦截", "input", error=err)
        flow = dict(flow)

        # 1) 重映射 id / z（每次部署独立 id，避免 duplicate id 撞车）
        fid = self._gen_raw_flow_id("e2e", flow)
        remapped, id_map, _hpz = self._remap_raw_flow_ids(flow, fid)
        inst = dict(remapped)
        inst["id"] = fid

        # 2) HA server 占位符回填（与 deploy_raw 共用，避免漂移）
        _, unresolved = self._inject_ha_server(inst)
        if unresolved:
            return self._e2e_result("拦截", stage="ha_server_inject",
                                    error=self._ha_server_unresolved_msg(unresolved))

        # 2.5) 节点级实体存在性【软校验】（不拦截）
        entity_warns = self._e2e_soft_check_entities(inst)

        # 3) 触发前准备：事件入口转合成 inject + 防 entityId 污染（与 run_e2e_trace 共用）
        nodes, inject_ids = self._e2e_prepare_flow(inst, trigger)
        if not inject_ids:
            # 既无 inject 又无可转换的事件入口：vhass 无 websocket，无法真实点燃
            return self._e2e_result(
                "拦截", "trigger",
                error="flow 无 inject 且无可转换的事件入口节点",
                reasons=[f"{target} 环境 vhass 暂不支持 HA websocket 事件推送"
                         "（P3 已知缺口），state 触发器无法在无副作用前提下被真实点燃。"
                         "请改用 inject 触发器，或在 trigger 参数里提供合成触发事件。"])

        # 4) 捕获插桩前结构（含转换后的入口类型）→ 用于比对，避免 tap 节点污染
        compare_flow = inst
        inst = self._instrument_flow(inst, trace_key)
        try:
            self._gate_node_types(inst)
        except RuntimeError as e:
            return self._e2e_result("拦截", "gate", error=str(e), reasons=[str(e)])

        # 5) 部署
        try:
            dep = self.nr.create_or_update_flow(fid, inst, force=True, allow_prod=allow_prod)
            real_fid = dep.get("id") or fid
        except Exception as e:
            return self._e2e_result("拦截", "deploy", flow_id=fid,
                                    error=f"部署失败：{e}",
                                    reasons=[f"部署到 1990 失败：{e}"])

        # 6) 触发
        triggered = []
        try:
            for iid in inject_ids:
                self.nr.trigger_inject(iid)
                triggered.append(iid)
        except Exception as e:
            self._safe_delete(real_fid, allow_prod=allow_prod)
            return self._e2e_result("拦截", "inject", flow_id=real_fid,
                                    error=f"触发失败：{e}",
                                    reasons=[f"inject 触发失败：{e}"])

        # 7) 读回 trace（异步落定，轮询直到有运行时错误或超时）
        settle = float(getattr(self.cfg, "e2e_settle_seconds", None) or 1.2)
        trace = []
        deadline = time.time() + settle
        while True:
            try:
                cur = self.nr.get_context("global", trace_key)
            except Exception:
                cur = None
            if isinstance(cur, list) and cur:
                trace = cur
                if any(t.get("error") for t in cur):
                    break
            if time.time() >= deadline:
                if isinstance(cur, list):
                    trace = cur
                break
            time.sleep(0.25)
        if not isinstance(trace, list):
            trace = []

        # 8) 比对（expected_path 中的原始节点 id 经 id_map 映射到重映射 id 空间）
        exp = expected_path
        if isinstance(exp, str):
            try:
                exp = json.loads(exp)
            except Exception:
                exp = None
        if isinstance(exp, list):
            def _map_ep(e):
                if isinstance(e, str):
                    return id_map.get(e, e)
                if isinstance(e, dict):
                    e = dict(e)
                    if e.get("id") in id_map:
                        e["id"] = id_map[e["id"]]
                return e
            exp = [_map_ep(x) for x in exp]
        report = self._compare_trace(compare_flow, trace, exp, trigger_ids=inject_ids,
                                      expected_services=expected_services,
                                      expected_branch_taken=expected_branch_taken)
        if entity_warns:
            report["entity_warnings"] = entity_warns

        # 9) 可选后置校验（需 live HA；软失败不阻断）
        post = None
        if expected_postconditions:
            try:
                post = self.observe_postconditions(expected_postconditions)
                if not post.get("ok"):
                    report["verdict"] = "断点"
            except Exception as e:
                report.setdefault("notes", []).append(
                    f"postconditions 校验异常：{e}")

        # 10) 回滚 + 清理
        self._safe_delete(real_fid, allow_prod=allow_prod)
        try:
            self.nr.delete_context("global", trace_key)
        except Exception:
            pass

        reasons = [f"E2E 路径：到达 {report['reached_count']}/{report['expected_count']} 环节"]
        if report.get("failed_at"):
            reasons.append(f"断点在：{report['failed_at']}")
        if report.get("runtime_errors"):
            reasons.append(f"运行时错误 {len(report['runtime_errors'])} 处")
        if report.get("service_mismatch"):
            reasons.append(f"动作与预期不符 {len(report['service_mismatch'])} 处"
                           f"（分支逻辑/意图校验失败）")
        if entity_warns:
            reasons.append(f"实体软校验：{len(entity_warns)} 处未知引用（未拦截）")
        return {
            "e2e": True, "flow_id": real_fid, "verdict": report["verdict"],
            "reasons": reasons, "report": report, "trace": trace,
            "postconditions": post, "live": live, "triggered": triggered,
            "entity_warnings": entity_warns,
        }

    def modify_flow(self, flow_id: str, dsl: Optional[str] = None,
                   node_patches: Optional[List[Dict]] = None,
                   agent_id: str = "unknown-agent", force: bool = False,
                   allow_prod: bool = False) -> Dict[str, Any]:
        """外科式改 flow（C3）：不重写整条流，只做最小改动。白箱身份专用。

        - dsl 给定：用新 DSL 重新编译，复用目标 flow 的 id/label 原地更新
          （等价于『整条重编译』，适合结构性改动）。
        - node_patches 给定：逐条 JSON patch 应用到匹配节点（by id / name / type），
          不改其余节点（真正的『外科式』，适合调一个参数/文案）。
          patch 形如 {"match": {"id":"..."}|{"name":"..."}|{"type":"..."},
                        "set": {k: v}, "remove": [k, ...]}。
        两条路都经校验（实体校验 dsl 模式 + 节点注册表闸门）+ 部署。

        ★ node_patches 是 **fail-closed**（#701）：patch 格式非法、或任一 patch
          零匹配时，直接返回 ok=False（stage="patch"），**不部署、flow 零改动**。
          绝不出现「ok=True + changed_nodes=0」的谎报成功。

        返回 {ok, flow_id, label, changed_nodes, node_count, mode}。"""
        from .dsl_engine import parse, compile, DSLError
        if not dsl and not node_patches:
            return {"ok": False, "stage": "input",
                    "error": "modify_flow 需要 dsl 或 node_patches 至少其一"}
        # 取现存 flow
        try:
            base = self.nr.get_flow(flow_id)
        except Exception as e:
            return {"ok": False, "stage": "load", "error": f"读取目标 flow 失败：{e}"}
        if dsl:
            try:
                scene = parse(dsl)
                new_flow = compile(scene)
            except DSLError as e:
                return {"ok": False, "stage": "compile", "error": str(e),
                        "compile_error": _compile_error_envelope(e),
                        "result_kind": "compile_error"}
            # 实体校验（仅 dsl 模式下有意义）
            unknown = self._check_entities_known(scene)
            if unknown:
                return {"ok": False, "stage": "entity_check",
                        "error": "实体校验未通过",
                        "unchecked": unknown}
            new_flow["id"] = flow_id
            new_flow["label"] = base.get("label", new_flow.get("label"))
            target = new_flow
            changed = len(new_flow.get("nodes", []))
            mode = "dsl_recompile"
        else:
            # ★ fail-closed（#701 / R5-BLOCKER）：格式非法或零匹配一律拒绝，
            #   绝不「谎报成功 + 空转重部署」。校验全部前置，通过后才动 target。
            hint = ('正确写法：node_patches=[{"match":{"id":"n2"},'
                    '"set":{"name":"新名"}}]；match 需含 id/name/type 之一，'
                    'set(对象)/remove(数组) 至少其一')
            patches = list(node_patches or [])
            if not patches:
                return {"ok": False, "stage": "patch",
                        "error": f"node_patches 为空，无补丁可应用。{hint}"}
            for i, p in enumerate(patches):
                if not isinstance(p, dict):
                    return {"ok": False, "stage": "patch",
                            "error": f"node_patches[{i}] 不是对象。{hint}"}
                m = p.get("match")
                if not isinstance(m, dict) or not any(
                        k in m for k in ("id", "name", "type")):
                    return {"ok": False, "stage": "patch",
                            "error": (f"node_patches[{i}] 缺少合法 match"
                                      f"（需含 id/name/type 之一）。{hint}"),
                            "got_keys": sorted(str(k) for k in p.keys())}
                setmap = p.get("set")
                remove = p.get("remove")
                if setmap is not None and not isinstance(setmap, dict):
                    return {"ok": False, "stage": "patch",
                            "error": f"node_patches[{i}].set 必须是对象。{hint}"}
                if remove is not None and not isinstance(remove, list):
                    return {"ok": False, "stage": "patch",
                            "error": f"node_patches[{i}].remove 必须是数组。{hint}"}
                if setmap is None and remove is None:
                    return {"ok": False, "stage": "patch",
                            "error": (f"node_patches[{i}] 需含 set 或 remove "
                                      f"至少其一。{hint}")}
            target = dict(base)
            nodes = target.setdefault("nodes", [])

            def _np_match(n, m):
                return (("id" in m and n.get("id") == m["id"]) or
                        ("name" in m and n.get("name") == m["name"]) or
                        ("type" in m and n.get("type") == m["type"]
                         and "id" not in m and "name" not in m))

            # 【WB84·P2-F-MULTI】第一遍：先算每个 patch 的命中范围，fail-closed 拒绝
            # 模糊 type 匹配命中 >1 节点（静默批量改写多处）的脚枪。需显式 allow_bulk=True
            # 确认，或改用 id/name 精确匹配。先校验后应用，避免「先改后拒」留半截脏 flow。
            for i, p in enumerate(patches):
                m = p.get("match") or {}
                _hits = sum(1 for n in nodes if _np_match(n, m))
                _type_only = ("type" in m and "id" not in m and "name" not in m)
                if _type_only and _hits > 1 and not p.get("allow_bulk"):
                    return {"ok": False, "stage": "patch", "flow_id": flow_id,
                            "error": (f"node_patches[{i}] 按 type 模糊匹配命中 {_hits} 个节点，"
                                      f"将静默批量改写多处（脚枪风险）。请显式加 allow_bulk=True "
                                      f"确认，或改用 id/name 精确匹配。{hint}"),
                            "ambiguous_hits": _hits, "match": m}
            # 第二遍：应用
            changed = 0
            unmatched: List[Dict[str, Any]] = []
            for i, p in enumerate(patches):
                m = p.get("match") or {}
                setmap = p.get("set") or {}
                remove = p.get("remove") or []
                hits = 0
                for n in nodes:
                    if not _np_match(n, m):
                        continue
                    _actual = 0
                    for k, v in setmap.items():
                        n[k] = v
                        _actual += 1
                    for k in remove:
                        # 【WB84·P2-F-REMOVENONE】仅当字段真实存在才计改动，
                        # 删不存在字段是 no-op，不虚报 changed=1（否则掩盖「其实没改」）。
                        if k in n:
                            del n[k]
                            _actual += 1
                    changed += _actual
                    hits += 1
                if hits == 0:
                    unmatched.append({"index": i, "match": m})
            if unmatched or changed == 0:
                # 中止：不部署、不写回，flow 保持原样
                return {"ok": False, "stage": "patch", "flow_id": flow_id,
                        "error": ("node_patches 未匹配到任何节点，已中止（未部署、"
                                  f"flow 未改动）。{hint}"),
                        "unmatched": unmatched,
                        "available_nodes": [
                            {"id": n.get("id"), "name": n.get("name"),
                             "type": n.get("type")}
                            for n in nodes[:20]],
                        "changed_nodes": 0}
            target["id"] = flow_id
            mode = "node_patches"
        # 节点注册表闸门（P0 防御）
        try:
            self._gate_node_types(target)
        except RuntimeError as e:
            return {"ok": False, "stage": "node_gate", "error": str(e)}
        # ★ HA server 占位符替换（WB72 Bug#2 / #706）
        # 此前本方法【完全漏调】_inject_ha_server：dsl 重编译分支拿到的是编译器
        # 新产出的 flow，其 HA 节点 server 字段是字面量 REPLACE_WITH_HA_SERVER，
        # 直接落盘 → NR 里节点绑不到 HA 配置节点，流看着部署成功却永远不动。
        # deploy_raw(Step 3) / propose_raw(Step 3) 早已有此步，唯独 modify_flow 漏接，
        # 属部署路径漂移。与两者对齐：unresolved>0 时 fail-fast，不把坏 flow 推给 NR。
        # node_patches 分支取的是 live flow（server 已是真实 id），此处为无害 no-op；
        # 但若 patch 自己写入了占位符，同样会被这里兜住。
        _, _unresolved = self._inject_ha_server(target)
        if _unresolved:
            return {"ok": False, "stage": "ha_server_inject", "flow_id": flow_id,
                    "error": self._ha_server_unresolved_msg(_unresolved)}
        # 【WB84·P2-F-DIRECT】直写路径审计化：落盘前留预快照 + 写 apply 轨迹，
        # 使 modify_flow(node_patches) 与 apply(mode=A) 一致可回滚（apply_rollback(trace_id)
        # 可还原），消除两条写路径安全保证分叉。best-effort：快照/轨迹失败不影响部署主流程。
        _rollback_trace_id = None
        _rollback_snap = None
        if mode == "node_patches":
            try:
                _rollback_snap = snapshot_flow(agent_id, "direct_patch", flow_id, base)
                _rollback_trace_id = "mp_" + uuid.uuid4().hex[:12]
                _write_apply_trace({
                    "trace_id": _rollback_trace_id, "flow_id": flow_id,
                    "mode": "DIRECT", "agent_id": agent_id,
                    "stage": "direct_write_pending", "ok": True,
                    "applied": False, "pending": True,
                    "snapshot_path": _rollback_snap,
                    "reason": "modify_flow node_patches 直写（预回滚点）",
                })
            except Exception:
                _rollback_trace_id = None
        # 部署（force 覆盖，复用 deploy 链路）
        try:
            res = self.nr.create_or_update_flow(flow_id, target, force=True,
                                                allow_prod=allow_prod)
            real_fid = res.get("id") or flow_id
            # 部署成功后把预回滚轨迹标记为已应用（apply_rollback 可一键还原）
            if _rollback_trace_id:
                try:
                    _write_apply_trace({
                        "trace_id": _rollback_trace_id, "flow_id": real_fid,
                        "mode": "DIRECT", "agent_id": agent_id,
                        "stage": "direct_write_applied", "ok": True,
                        "applied": True, "pending": False,
                        "snapshot_path": _rollback_snap,
                        "reason": "modify_flow node_patches 直写（已落盘，可回滚）",
                    })
                except Exception:
                    pass
        except Exception as e:
            return {"ok": False, "stage": "deploy", "error": f"部署失败：{e}"}
        _ret = {"ok": True, "flow_id": real_fid, "label": target.get("label"),
                "changed_nodes": changed,
                "node_count": len(target.get("nodes", [])), "mode": mode}
        if _rollback_trace_id:
            _ret["rollback_trace_id"] = _rollback_trace_id
            _ret["snapshot_path"] = _rollback_snap
            _ret["audited_direct_write"] = True
        return _ret

    # ───────────── apply 闭环编排（WB1-F / #694）─────────────
    # 触发(autoflow_trigger_inject) → 回读(autoflow_debug_read) → **apply** 的最后一环。
    # 本方法【不造新能力】，只把既有零件安全串起来：
    #   snapshot_flow(回滚点) → 决策闸(request_decision) → modify_flow / commit_ha_service → 审计落盘
    # 三种 mode（PM 拍板全做）：
    #   A = 观测驱动自动修正流（改 flow，高风险）
    #   B = 回读数据落状态（写 HA 服务，低风险，走 commit_ha_service 自带确认闸）
    #   C = 热补丁（局部 node 改，高风险）
    _APPLY_MODES = ("A", "B", "C")

    @staticmethod
    def _state_is_on(state: Any) -> Dict[str, Any]:
        """把状态值归一为 turn_on/turn_off。fail-closed：无法识别一律拒绝，绝不静默 turn_off。

        返回 {"ok": True, "on": bool} 或 {"ok": False, "error": str}。
        unavailable/unknown 单独给更明确的错误（设备掉线，不是该关掉）。
        """
        if isinstance(state, bool):
            return {"ok": True, "on": state}
        if isinstance(state, (int, float)):
            return {"ok": True, "on": state != 0}
        s = str(state).strip().lower()
        if s in _ON_STATES:
            return {"ok": True, "on": True}
        if s in _OFF_STATES:
            return {"ok": True, "on": False}
        if s in _UNCERTAIN_STATES:
            return {"ok": False, "error": f"状态 '{state}' 为 unavailable/unknown（设备掉线或未知），"
                                          f"不应据此下发 turn_off。请显式传 state 或改用 autoflow_commit_ha_service。"}
        return {"ok": False, "error": f"状态 '{state}' 无法识别为 on/off（不在允许白名单），"
                                      f"拒绝写回 HA。请显式传 state 或改用 autoflow_commit_ha_service。"}

    def apply_flow(self, flow_id: str = "", correction: Optional[Dict] = None,
                   mode: str = "A", agent_id: str = "unknown-agent",
                   auto_approve: bool = False, allow_prod: bool = False,
                   trace_id: Optional[str] = None) -> Dict[str, Any]:
        """把「观测→修正」落回系统的唯一编排入口（apply 闭环核心）。

        correction 按 mode 取字段：
          mode A/C（改 flow）：{"dsl": "...", 或 "node_patches": [...], "reason": "为什么改"}
          mode B（落状态）  ：{"domain": "light", "service": "turn_on",
                                "data": {"entity_id": "light.x"}, "reason": "..."}

        安全模型（铁律·自愈闭环 Self-Healing Loop）：
          - A/C 改 flow = 高风险 → **默认自动写回**（不再进人审闸）：先 snapshot 落回滚点，
            再做 per-(agent, flow) 滑动窗口失败预算检查（自愈重试次数，WebUI 可配，默认 3），
            通过后直接 modify_flow 写回；预算耗尽即停止并转报告/人工
            （stage=selfheal_budget_exhausted），防止自动修复死循环。
          - 回滚点快照**保留**（作为安全网，不是闸），apply_rollback 随时可还原到写回前状态。
          - `auto_approve` 参数**已废弃**：apply 闭环恒自动写回，保留签名仅为 MCP 调用方兼容。
          - B 落状态 = 低风险 → 本层 audit auto-pass（不额外加闸），直接透传 commit_ha_service
            （它自身已进确认闸，不存在裸写 HA）；B 段不计入自愈预算。
          - #607：目标 tab 处于禁用态时**显式告警**（tab_disabled=True + warnings），
            提醒调用方别拿空回读当证据乱改；告警不阻塞（禁用 tab 上做热补丁本身是合法运维动作）。

        返回统一审计信封：
          {ok, applied, pending, mode, trace_id, flow_id, snapshot_path, decision_id?,
           pending_id?, stage, result, warnings[], error?}
        任何一次 A/C 调用都会在 data/apply_traces/<trace_id>.json 留痕，
        供 apply_rollback(trace_id) 一键还原到 apply 前的 flow 快照。"""
        correction = correction or {}
        mode = (mode or "").strip().upper()
        if mode not in self._APPLY_MODES:
            return {"ok": False, "applied": False, "pending": False, "stage": "input",
                    "mode": mode, "error": f"mode 必须是 A/B/C 之一，收到 {mode!r}"}
        trace_id = trace_id or ("ap_" + uuid.uuid4().hex[:12])
        reason = str(correction.get("reason") or "").strip()
        audit: Dict[str, Any] = {
            "ok": False, "applied": False, "pending": False,
            "mode": mode, "trace_id": trace_id, "agent_id": agent_id,
            "flow_id": flow_id or None, "reason": reason, "warnings": [],
        }

        # ── B 段：回读数据 → 落状态（低风险，audit auto-pass）──
        if mode == "B":
            domain = (correction.get("domain") or "").strip()
            service = (correction.get("service") or "").strip()
            data = correction.get("data") or {}
            if not domain or not service:
                audit.update(stage="input",
                             error="mode=B 需要 correction.domain 与 correction.service")
                _write_apply_trace(audit)
                return audit
            res = self.commit_ha_service(domain, service, data, agent_id)
            ok = bool(res.get("ok"))
            audit.update(ok=ok, stage="commit_ha_service", result=res,
                         gate="audit_auto_pass", risk="low",
                         # commit_ha_service 自带确认闸：ok 只代表已入闸，真正执行在人批准后
                         pending=bool(res.get("needs_approval")),
                         applied=ok and not res.get("needs_approval"),
                         pending_id=res.get("pending_id"))
            if not ok:
                audit["error"] = "; ".join(res.get("errors") or []) or "commit_ha_service 失败"
            else:
                audit["note"] = ("已提交 HA 写服务确认闸"
                                 + (f"（pending_id={res.get('pending_id')}，待人批准执行）"
                                    if res.get("needs_approval") else "（已执行）"))
            _write_apply_trace(audit)
            return audit

        # ── A/C 段：改 flow（高风险）──
        if not flow_id:
            audit.update(stage="input", error=f"mode={mode} 需要 flow_id")
            _write_apply_trace(audit)
            return audit
        dsl = correction.get("dsl")
        node_patches = correction.get("node_patches")
        if not dsl and not node_patches:
            audit.update(stage="input",
                         error=f"mode={mode} 需要 correction.dsl 或 correction.node_patches 至少其一")
            _write_apply_trace(audit)
            return audit
        try:
            base = self.nr.get_flow(flow_id)
        except Exception as e:
            audit.update(stage="load", error=f"读取目标 flow 失败：{e}")
            _write_apply_trace(audit)
            return audit
        label = (base or {}).get("label") or flow_id
        audit["label"] = label
        # #607：禁用 tab 显式告警（不阻塞）
        if bool((base or {}).get("disabled", False)):
            audit["tab_disabled"] = True
            audit["warnings"].append(
                f"#607：目标 tab『{label}』当前为禁用态，运行期不产生 debug 帧；"
                "若本次修正基于空回读推断，请先补足证据再批准。")
        # 回滚点：改之前先把当前 flow JSON 落盘
        snap = snapshot_flow(agent_id, "apply_pre", label, base,
                             extra={"trace_id": trace_id, "apply_mode": mode,
                                    "correction": correction, "reason": reason})
        audit["snapshot_path"] = snap
        if not snap:
            audit["warnings"].append(
                "快照落盘失败：本次 apply 无回滚点，apply_rollback 将不可用。")

        # 自愈闭环（Self-Healing Loop）：默认自动写回已部署 flow，不进人审闸；
        # 以 per-(agent, flow) 滑动窗口失败预算做有界失效保护（fail-safe，防死循环）。
        _allowed, _info = self._selfheal_budget_check(agent_id, flow_id)
        if not _allowed:
            audit.update(ok=False, applied=False, pending=False,
                         stage="selfheal_budget_exhausted",
                         error=_info.get("error"),
                         retry_budget=_info.get("retry_budget"),
                         failed_attempts_in_window=_info.get("failed_attempts_in_window"),
                         gate="selfheal_budget", risk="high")
            _write_apply_trace(audit)
            return audit

        # 走既有外科式改流链路（含节点注册表闸门 + 部署）
        res = self.modify_flow(flow_id, dsl=dsl, node_patches=node_patches,
                               agent_id=agent_id, allow_prod=allow_prod)
        ok = bool(res.get("ok"))
        self._selfheal_budget_record(agent_id, flow_id, ok)
        audit.update(ok=ok, applied=ok, pending=False, stage="modify_flow",
                     result=res, gate="selfheal_auto_write", risk="high")
        if not ok:
            audit["error"] = res.get("error") or "modify_flow 失败"
        else:
            audit["note"] = (f"已写回：{res.get('changed_nodes')} 个节点变更；"
                             f"如需还原调 apply_rollback(trace_id='{trace_id}')。")
        _write_apply_trace(audit)
        return audit

    def apply_state_from_debug(self, flow_id: str = "", node_id: str = "", since: int = 0,
                               limit: int = 50, entity_id: str = "", state: Any = "",
                               reason: str = "", agent_id: str = "unknown-agent",
                               auto_approve: bool = False, trace_id: Optional[str] = None) -> Dict[str, Any]:
        """【apply 闭环 B 段胶水】把 debug 回读帧映射成「实体+目标状态」并写回 HA。

        这是 inject 触发(autoflow_trigger_inject) → debug 回读(autoflow_debug_read) 之后、
        apply 落状态(autoflow_apply mode=B) 之前的「B 段胶水」：把观测到的 debug 帧翻译成
        HA 服务调用，再交给 apply_flow(mode="B") 走写服务确认闸。

        映射规则（极简、可测、不臆造域）：
          - 优先用显式传入的 entity_id/state（agent 已归因明确的情形）；
          - 否则从 debug 帧推断：read(full=True) 返回的帧 **payload 是字符串**（入库时已 json.dumps），
            需 json.loads 还原；read() 已按 received_at **倒序（最新在前）**，故直接正序取第一条匹配帧
            = 最新证据。显式传了 entity_id 时只认同实体帧，避免误用别的实体状态。
          - 支持 state/target_state/value 字段；超长被截断或非 JSON 的帧跳过，parse 失败会提示「可能截断」；
          - domain = entity_id 第一个 '.' 之前的部分；状态经 _state_is_on 双白名单归一
            （fail-closed：unavailable/unknown/未知一律拒绝，**绝不静默 turn_off**）。
          - 帧为空 / 无法推断 / 状态无法识别 → 直接报错，**绝不基于空观测或未知状态写回 HA（#607 证据要求）**。

        落点：self.apply_flow(mode="B", correction={"domain","service","data":{"entity_id"},"reason"})。
        """
        # 1) 证据：回读 debug 帧（禁用 tab 不产生帧，空帧即无证据）
        read = self.get_debug_read(
            flow_id=flow_id or None, node_id=node_id or None,
            since=since or None, limit=limit, full=True,
        )
        if not read.get("ok") or not read.get("events"):
            return {"ok": False, "error": "无 debug 回读帧：无法基于空观测写回 HA（#607 证据要求）。"
                                          "先 autoflow_debug_read 拿到证据再 apply。"}
        events = read["events"]
        # 2) 解析 entity_id / 目标状态
        #    read() 已按 received_at 倒序（最新在前），直接正序遍历 = 取最新证据（BLOCKER-2）。
        #    真机帧 payload 是字符串（入库时 json.dumps），需 json.loads 还原（BLOCKER-1）。
        ent, st = entity_id, state
        parse_failed = False
        saw_payload = False
        if not ent or st == "" or st is None:
            for ev in events:
                payload = ev.get("payload")
                if isinstance(payload, str):
                    saw_payload = True
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        parse_failed = True
                        continue          # 非 JSON / 被截断 → 跳过，不猜
                elif not isinstance(payload, dict):
                    continue
                if isinstance(payload, dict) and payload.get("entity_id"):
                    # NIT-4：显式指定实体时只认同实体帧，避免误用别的实体状态
                    if ent and payload.get("entity_id") != ent:
                        continue
                    ent = ent or payload.get("entity_id")
                    if st == "" or st is None:
                        for key in ("state", "target_state", "value"):
                            if key in payload and payload[key] not in ("", None):
                                st = payload[key]
                                break
                    if ent and st != "" and st is not None:
                        break
        if not ent:
            hint = ""
            if saw_payload and parse_failed:
                hint = "（部分帧 payload 为非 JSON 文本或已被 max_payload_chars 截断导致无法解析；"
                hint += "可显式传入 entity_id/state，或减小 DebugBridge max_payload_chars 避免截断）"
            return {"ok": False, "error": "无法解析 entity_id：显式传入或 debug 帧 payload 需含 entity_id。" + hint}
        if st == "" or st is None:
            hint = ""
            if saw_payload and parse_failed:
                hint = "（部分帧 payload 无法解析，可能因截断或非法 JSON；建议显式传入 state）"
            return {"ok": False, "error": "无法解析目标状态 state：显式传入或 debug 帧 payload 需含 "
                                          "state/target_state/value。" + hint}
        # 3) 映射 → HA 服务调用（fail-closed：状态无法识别直接拒绝，绝不静默 turn_off）
        mapping = self._state_is_on(st)
        if not mapping["ok"]:
            return {"ok": False, "error": mapping["error"]}
        domain = ent.split(".", 1)[0] or ent
        service = "turn_on" if mapping["on"] else "turn_off"
        correction = {
            "domain": domain,
            "service": service,
            "data": {"entity_id": ent},
            "reason": reason or f"基于 debug 回读帧落状态（证据节点 {node_id or events[0].get('node_id', 'latest')}）",
        }
        # 4) 走 apply_flow B 分支（写服务确认闸）
        return self.apply_flow(
            flow_id=flow_id, correction=correction, mode="B",
            agent_id=agent_id, auto_approve=auto_approve, trace_id=trace_id,
        )

    def apply_rollback(self, trace_id: str, agent_id: str = "unknown-agent",
                       auto_approve: bool = False,
                       allow_prod: bool = False) -> Dict[str, Any]:
        """把某次 apply（trace_id）改动的 flow 还原到 apply 前的快照（自愈闭环·回滚）。

        - 从 data/apply_traces/<trace_id>.json 找回 flow_id 与 snapshot_path；
        - 还原同样是「改 flow」＝高风险，**默认自动执行**（与 apply_flow 对称、计入同一
          (agent, flow) 自愈预算），预算耗尽即停止（stage=selfheal_budget_exhausted，fail-safe 防死循环）；
        - 执行路径复用 modify_flow 的部署链路（节点注册表闸门 + create_or_update_flow(force)），
          **不走 deploy_raw**：还原的是曾经在线的已知良好状态，不该被新增 lint 规则二次拦下。

        返回 {ok, restored, pending, trace_id, flow_id, snapshot_path, error?}。"""
        out: Dict[str, Any] = {"ok": False, "restored": False, "pending": False,
                               "trace_id": trace_id, "agent_id": agent_id, "warnings": []}
        tr = _read_apply_trace(trace_id)
        if not tr:
            out.update(stage="load_trace",
                       error=f"找不到 apply 轨迹 {trace_id}（data/apply_traces 无该记录）")
            return out
        flow_id = tr.get("flow_id")
        snap_path = tr.get("snapshot_path")
        out["flow_id"] = flow_id
        out["snapshot_path"] = snap_path
        if not flow_id or not snap_path:
            out.update(stage="load_trace",
                       error="该 apply 轨迹无 flow_id / 回滚点（可能是 mode=B 或快照失败），无法还原")
            return out
        try:
            with open(snap_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            flow = snap.get("flow") or {}
        except Exception as e:
            out.update(stage="load_snapshot", error=f"读取快照失败：{e}")
            return out
        if not flow.get("nodes"):
            out.update(stage="load_snapshot", error="快照内无节点，拒绝用空 flow 覆盖线上")
            return out
        label = flow.get("label") or flow_id
        # 自愈闭环：回滚同样默认自动执行、计入同一 (agent, flow) 自愈预算（fail-safe 防死循环）
        _allowed, _info = self._selfheal_budget_check(agent_id, flow_id)
        if not _allowed:
            out.update(ok=False, restored=False, pending=False,
                       stage="selfheal_budget_exhausted",
                       error=_info.get("error"),
                       retry_budget=_info.get("retry_budget"),
                       failed_attempts_in_window=_info.get("failed_attempts_in_window"))
            return out
        target = dict(flow)
        target["id"] = flow_id
        try:
            self._gate_node_types(target)
        except RuntimeError as e:
            out.update(stage="node_gate", error=str(e))
            return out
        try:
            res = self.nr.create_or_update_flow(flow_id, target, force=True,
                                                allow_prod=allow_prod)
        except Exception as e:
            out.update(stage="deploy", error=f"还原部署失败：{e}")
            self._selfheal_budget_record(agent_id, flow_id, False)
            return out
        self._selfheal_budget_record(agent_id, flow_id, True)
        out.update(ok=True, restored=True, stage="restored",
                   node_count=len(target.get("nodes", [])),
                   result=res if isinstance(res, dict) else {"raw": str(res)},
                   note=f"已把 {flow_id} 还原到 {snap_path}")
        _write_apply_trace({"trace_id": trace_id, "flow_id": flow_id, "mode": "ROLLBACK",
                            "ok": True, "applied": True, "pending": False,
                            "agent_id": agent_id, "stage": "restored",
                            "snapshot_path": snap_path})
        return out

    # ── 自愈闭环（Self-Healing Loop）滑动窗口失败预算 ──
    # WebUI 可配 feature_flags.selfheal_budget（默认 3），env AUTOFLLOW_SELFHEAL_BUDGET 回退；
    # 窗口 AUTOFLLOW_SELFHEAL_WINDOW_MIN（默认 10 分钟）。0=禁用自主重试（一次失败即停）。
    # 与 deploy_raw 的 retry_budget 同源思想，仅作用域换成 apply/rollback、默认值 3。
    def _selfheal_budget_check(self, agent_id: str, flow_id: str):
        """per-(agent, flow) 滑动窗口失败预算检查。

        返回 (allowed, info)：allowed=True 可继续写回；allowed=False 时 info 含 error /
        retry_budget / failed_attempts_in_window，调用方据此返回 stage=selfheal_budget_exhausted。
        """
        _budget = None
        try:
            _budget = int(load_feature_flags(self.cfg).get("selfheal_budget"))
        except (TypeError, ValueError, AttributeError):
            _budget = None
        if _budget is None:
            _budget = int(os.environ.get("AUTOFLLOW_SELFHEAL_BUDGET", "3"))
        _window = float(os.environ.get("AUTOFLLOW_SELFHEAL_WINDOW_MIN", "10")) * 60
        if not hasattr(self, "_apply_selfheal_budget"):
            self._apply_selfheal_budget = {}
        _hist = self._apply_selfheal_budget.setdefault((agent_id, flow_id), [])
        _now = time.time()
        _hist[:] = [t for t in _hist if _now - t < _window]  # 滑动窗口裁剪
        # 语义：attempts allowed = selfheal_budget（budget=0 ⇒ 禁用自主重试，任何写回都被拦）。
        if len(_hist) >= _budget:
            return False, {
                "error": (
                    f"自愈重试预算耗尽：agent `{agent_id}` 在 {_window/60:.0f} 分钟内对 flow "
                    f"`{flow_id}` 已有 {len(_hist)} 次自主修复失败（上限 {_budget}）。"
                    f"疑似自动修复死循环，已停止并转报告/人工。请人工介入检查 flow，"
                    f"或在 WebUI 调高自愈重试次数（selfheal_budget）。"
                ),
                "retry_budget": _budget,
                "failed_attempts_in_window": len(_hist),
            }
        return True, {}

    def _selfheal_budget_record(self, agent_id: str, flow_id: str, ok: bool) -> None:
        """写回结果记预算：成功清空该 (agent, flow) 计数（避免误伤后续正常修复），失败追加时间戳。"""
        if not hasattr(self, "_apply_selfheal_budget"):
            self._apply_selfheal_budget = {}
        if ok:
            self._apply_selfheal_budget.pop((agent_id, flow_id), None)
        else:
            self._apply_selfheal_budget.setdefault((agent_id, flow_id), []).append(time.time())

    def get_apply_trace(self, trace_id: str) -> Dict[str, Any]:
        """按 trace_id 读回某次 apply 的完整审计轨迹（data/apply_traces/<trace_id>.json）。

        供 agent 侧独立核对 apply 闭环证据：两阶段决策闸是否同一 trace_id 复用回滚点、
        pending→approved 是否真写回、ROLLBACK 是否落痕、审计字段（mode/agent_id/reason）是否齐全。
        只读，不改任何状态。返回 {ok, trace_id, trace} 或 {ok: False, error}。"""
        tr = _read_apply_trace(trace_id)
        if not tr:
            return {"ok": False,
                    "error": f"找不到 apply 轨迹 {trace_id}（data/apply_traces 无该记录；"
                             f"确认 trace_id 来自 autoflow_apply / autoflow_apply_rollback 返回值）。"}
        return {"ok": True, "trace_id": trace_id, "trace": tr}

    def _scene_entity_refs(self, scene) -> List[str]:
        """统一收集 DSL 场景内**所有**含实体的原语引用（含嵌套块），去重前的原始列表。

        覆盖的含实体原语（缺一即会漏判/漏抽取）：
        - Trigger(kind=state).entity            触发实体
        - Action.target                          动作目标
        - ReadState.entity                       取值:  （sensor 数值读取，如照度）
        - SubflowCall(subflow=history_*)          调用子流程: history_*（历史查询）
        - CurrentState.entity + body/else_body   查询:  （门+嵌套主链/否则分支）
        - TimeRange.body                         时间段: （门内嵌套动作，如吊灯 switch）
        - Switch.branches[].body / else_body     分支/否则
        - Parallel.children                      并行

        供 _check_entities_known（闸门校验）与 _collect_scene_entities（golden 判分/白名单）复用。
        历史查询已统一改为 调用子流程: history_*（请求/响应子流程），实体经 SubflowCall.param 抽取。
        """
        from .dsl_engine import (
            Action, Switch, Parallel, ReadState, SubflowCall,
            CurrentState, TimeRange,
        )
        _HISTORY_SUBS = ("history_state_at", "history_occurred",
                         "history_duration", "history_aggregate")
        ids: List[str] = []
        for t in scene.triggers:
            if t.kind == "state" and t.entity:
                ids.append(t.entity)

        def walk(steps):
            for st in steps:
                if isinstance(st, Action):
                    ids.append(st.target)
                elif isinstance(st, ReadState):
                    if st.entity:
                        ids.append(st.entity)
                elif isinstance(st, SubflowCall):
                    # 历史查询子流程：entity 在平铺参数 raw_args 中
                    if st.name in _HISTORY_SUBS and st.raw_args.get("entity"):
                        ids.append(st.raw_args["entity"])
                elif isinstance(st, CurrentState):
                    if st.entity:
                        ids.append(st.entity)
                    walk(st.body)
                    walk(st.else_body)
                elif isinstance(st, TimeRange):
                    walk(st.body)
                elif isinstance(st, Switch):
                    for b in st.branches:
                        walk(b.body)
                    walk(st.else_body)
                elif isinstance(st, Parallel):
                    walk(st.children)

        walk(scene.body)
        return ids

    def _check_entities_known(self, scene) -> List[str]:
        """返回无法【无歧义】解析到目录内 entity_id 的引用列表（空=全部已知）。

        遍历 DSL 的 状态触发实体 + 所有动作目标（含 取值/查询/调用子流程(history_*)/时间段 等嵌套原语），
        逐一校验：
        - 精确 entity_id / 精确 mapping / 唯一(或 high 置信)候选 → 视为已知；
        - 多候选歧义（如"书房吊灯"）或 0 候选（如"书房光照度"）→ 判未知，
          闸门据此拦截，迫使 agent 显式调 autoflow_resolve_entity 从候选里选，绝不静默猜域。

        D36 性能修复（WB83 P1 DoS 根因）：
        旧实现逐实体调 _resolve_best → 每次都 get_device_catalog()+state.resolve() 各读一次盘
        （大目录 JSON 解析 ~百毫秒/次）→ N 个实体 → O(N·目录解析) → propose_dsl 串行阻塞 DoS
        （10 层嵌套即数秒、数百层卡死）。
        现改为【单次】取目录+映射，实体_id 形态引用（domain.object）走内联快路径（不读盘、不模糊），
        仅中文/友好名引用才调昂贵的模糊解析（且 resolve_entity 已加结果缓存）。
        """
        refs = self._scene_entity_refs(scene)
        # D36 防御纵深：引用实体过多的 DSL 直接判未知拒绝，避免对上千个实体逐个解析。
        # 家自动化流极少引用 >256 个不同实体，超过即视为异常/恶意复杂度，廉价拒绝。
        if len(refs) > _MAX_ENTITY_REFS:
            return list(dict.fromkeys(refs))
        # 单次读取目录与映射（消除逐实体读盘）。
        cat = self.state.get_device_catalog().get("entities", {})
        mapping = self.state.get_entity_mapping().get("mappings", {})
        unknown: List[str] = []
        for eid in refs:
            if eid in cat or eid in mapping:
                continue  # 精确命中
            if _ENTITY_ID_SHAPE_RE.match(eid):
                # 已是 entity_id 形态却不在目录/映射 → 确定性未知，直接判未知，
                # 绝不调模糊解析（模糊只服务于中文/友好名；编造型 ID 不会命中友好名）。
                unknown.append(eid)
                continue
            # 中文/友好名输入 → 走模糊解析（结果已缓存，且仅此形态才付出代价）。
            try:
                r = self.resolve_entity(eid)
            except Exception:
                unknown.append(eid)
                continue
            cands = r.get("candidates", []) if r.get("ok") else []
            if not cands or (len(cands) > 1 and cands[0].get("confidence") != "high"):
                unknown.append(eid)
        return unknown

    def _collect_scene_entities(self, scene) -> List[str]:
        """收集 DSL 引用的所有实体（含 取值/查询/调用子流程(history_*)/时间段 等嵌套原语），尽力解析为真实
        entity_id（去重）。供 resolve 白名单校验 + golden 判分使用。友好名经 _resolve_best
        无歧义解析为 entity_id，歧义/未解析则保留原始引用（会被 _check_entities_known 拦截）。"""
        seen = set()
        out = []
        for e in self._scene_entity_refs(scene):
            resolved = self._resolve_best(e) or e  # 无歧义→真实 entity_id；否则保留原始名
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out

    def _build_vhass_from_staging(self):
        """从 staging device_catalog 镜像构建内存 vhass；不存在则 demo。"""
        from . import vhass as _vh
        store = _vh.VHassStore()
        cat = os.path.join(self.cfg.data_dir, "staging", "state", "device_catalog.json")
        if os.path.exists(cat):
            try:
                seed = _vh.build_seed_from_catalog(cat)
                store.areas = seed.get("areas", {})
                store.entities = {}
                for e in seed.get("entities", []):
                    store.entities[e["entity_id"]] = _vh.VHassStore._normalize(e)
            except Exception:
                pass
        return store

    # ───────────── 确认闸操作 ─────────────
    def list_pending(self, agent_id: Optional[str] = None) -> List[Dict]:
        _tid = _new_trace_id()
        _t0 = time.perf_counter()
        ops = self.confirm.list_pending(agent_id=agent_id)
        out = [_enrich_pending_op(op.to_dict()) for op in ops]
        _slog(_tid, "list_pending.done", elapsed=round(time.perf_counter() - _t0, 3),
              count=len(out), filtered=bool(agent_id))
        return out

    def approve(self, op_id: str, reviewer: str = "human") -> Dict[str, Any]:
        op = self.confirm.get(op_id)
        if op is None:
            return {"ok": False, "error": f"待确认项不存在: {op_id}"}
        if op.status != "pending":
            return {"ok": False, "error": f"已 {op.status}"}
        try:
            if op.operation in ("create_flow", "update_flow"):
                flow = op.payload["flow"]
                fid = op.payload["flow_id"]
                # create-or-update：全新场景 POST /flow 创建；已存在 PUT /flow/:id 更新
                res = self.nr.create_or_update_flow(fid, flow, force=True)
                real_fid = res.get("id") or fid
                created = res.get("created", False)
                # 登记 flow_catalog（owner）
                self.state.upsert_flow(real_fid, {
                    "flow_id": real_fid,
                    "label": flow.get("label", real_fid),
                    "owner_agent": op.agent_id,
                    "purpose": flow.get("info", ""),
                    "entities_touched": self._collect_entities(flow),
                    "node_count": len(flow.get("nodes", [])),
                    "source": "manual",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                self.confirm.approve(op_id, reviewer)
                return {"ok": True, "executed": "create_flow" if created else "update_flow",
                        "flow_id": real_fid, "nr_result": res.get("raw", res)}
            elif op.operation == "ha_call":
                p = op.payload
                result = self.ha.call_service(p["domain"], p["service"], p["data"])
                self.confirm.approve(op_id, reviewer)
                return {"ok": True, "executed": "ha_call", "result": result}
            elif op.operation == "set_tab_state":
                p = op.payload
                result = self.set_tab_state_execute(p["flow_id"], p["enabled"])
                self.confirm.approve(op_id, reviewer)
                return {"ok": True, "executed": "set_tab_state", "flow_id": p["flow_id"],
                        "disabled": result.get("disabled"), "nr_result": result.get("nr_result")}
            else:
                return {"ok": False, "error": f"未知操作: {op.operation}"}
        except Exception as e:
            return {"ok": False, "error": f"执行失败: {e}"}

    def reject(self, op_id: str, reviewer: str = "human", reason: Optional[str] = None) -> Dict[str, Any]:
        try:
            self.confirm.reject(op_id, reviewer, reason)
            return {"ok": True, "rejected": op_id}
        except ConfirmationError as e:
            return {"ok": False, "error": str(e)}

    # ───────────── 工作区 plan（总体/当前/最近完成）─────────────
    def get_plan(self) -> Dict[str, Any]:
        """返回工作区 plan 快照：{overall, current, completed:[{ts,text}], updated_at}。"""
        return self.plan.get_state()

    def update_plan(self, overall: Optional[str] = None,
                    current: Optional[str] = None,
                    append_completed: Optional[str] = None) -> Dict[str, Any]:
        """局部更新 plan（None 字段不变），返回更新后的完整状态。"""
        return self.plan.update(overall=overall, current=current,
                                append_completed=append_completed)

    # ───────────── 指令收件箱（人类 → deepseek 直达） ─────────────
    def _wrap_command_prompt(self, text: str) -> str:
        """把 owner 的自然语言指令包成给 deepseek++ 的任务提示词。
        deepseek 以 producer 身份、new_session 收到；提示它用 autoflow MCP 工具面执行，
        完成后经 MCP 发 Bark 回报 owner。保持轻包装，不扭曲原意。"""
        return (
            "【大佬（网关 owner）通过 WebUI 工作区下达的指令】\n"
            f"{text.strip()}\n\n"
            "请你（AutoFlow producer）用 autoflow MCP 工具面完成上述指令："
            "需要解析设备时用 autoflow_resolve_entity；需要编排自动化时写 DSL 并 autoflow_propose_dsl 提交；"
            "查询/其它操作用相应工具。执行完成后请经 MCP 简要汇报，并发一条 Bark 告知大佬结果。"
        )

    def submit_command(self, text: str, target: str = "deepseek") -> Dict[str, Any]:
        """接收 owner 指令 → 落库 → 后台线程投递给 ds_bridge/deepseek（fire-and-forget）。
        立即返回记录（status=dispatching）；投递结果由后台线程回写。非阻塞，不占事件循环。"""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "指令内容为空"}
        rec = self.commands.create(text, target=target)
        cid = rec.id

        def _worker():
            try:
                self.commands.mark(cid, "dispatching")
                if target == "deepseek":
                    self._wait_ds_bridge_idle()  # 防与在飞评测/上一条指令竞合
                    fired = self._fire_ds_bridge(self._wrap_command_prompt(text))
                    ok = bool(fired.get("ok"))
                    self.commands.mark(
                        cid, "dispatched" if ok else "failed",
                        job_id=fired.get("fire", ""),
                        result=json.dumps(fired, ensure_ascii=False),
                    )
                else:
                    self.commands.mark(cid, "failed",
                                       result=f"未知投递目标: {target}")
            except Exception as e:
                self.commands.mark(cid, "failed",
                                   result=f"{type(e).__name__}: {e}")

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "command": self.commands.get(cid).to_dict(),
                "note": "已排队投递给 deepseek；deepseek 完成后经 Bark 回报"}

    def list_commands(self, limit: int = 30) -> List[Dict[str, Any]]:
        """列出最近指令（含投递状态），供 WebUI 工作区历史区渲染。"""
        return [c.to_dict() for c in self.commands.list(limit=limit)]

    # ───────────── 多选项决策闸（人类请示） ─────────────
    def _bark_push(self, title: str, body: str, group: Optional[str] = None,
                   url: Optional[str] = None, level: Optional[str] = None) -> bool:
        """最佳努力推送 Bark（NAS 自建 bark-server，AES-128-CBC 加密，与 NR1990 bark_push 一致）。
        纯 Python（cryptography + urllib），不依赖 node；失败静默，绝不拖垮主流程。"""
        try:
            import base64
            from urllib.parse import quote
            from urllib.request import Request, urlopen
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.padding import PKCS7

            KEY = (os.environ.get("BARK_CIPHER_KEY") or "<BARK_CIPHER_KEY>").encode("utf-8")
            IV = (os.environ.get("BARK_CIPHER_IV") or "<BARK_CIPHER_IV>").encode("utf-8")
            server = (os.environ.get("BARK_SERVER") or "http://<BARK_SERVER_IP>:18273").rstrip("/")
            key = os.environ.get("BARK_KEY") or "<BARK_KEY>"

            payload = {"title": title, "body": body}
            if group:
                payload["group"] = group
            if url:
                payload["url"] = url
            if level:
                payload["level"] = level
            # 严格对齐 bark_notify.js 的 JSON.stringify：无空格 + escape Unicode
            raw = json.dumps(payload, ensure_ascii=True,
                             separators=(",", ":")).encode("utf-8")
            padder = PKCS7(128).padder()
            padded = padder.update(raw) + padder.finalize()
            enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
            ct = enc.update(padded) + enc.finalize()
            b64 = base64.b64encode(ct).decode("ascii")
            data = ("ciphertext=" + quote(b64, safe="") +
                    "&iv=" + quote(IV.decode("utf-8"), safe=""))
            req = Request(
                server + "/" + key,
                data=data.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def request_decision(self, question: str, options: List[str],
                         source: str = "deepseek") -> Dict[str, Any]:
        """接收一道选择题（人类请示）→ 落库 → 后台线程 Bark 催办。
        立即返回决策记录；人类在 WebUI 工作区点选后由 resolve_decision 闭环。非阻塞。"""
        rec = self.decisions.create(question, options, source=source)
        did = rec["id"]
        # R10(#round4) iss_fb16973875：A24 报「apply 回执 decision_id 与库错位一位」→
        # 按回执 id 调 get_decision 必「决策不存在」，apply→get_decision 闭环静默断掉。
        # 真码复盘未能复现（store 层 300 轮 create→get 零错位、id 同源无中间改写），
        # 但**「复现不出」不等于「不会发生」**——这条闭环一旦错位是完全静默的：
        # agent 会拿着一个永远查不到的 id 空等人类拍板。故加读回自检：回执 id 必须
        # 能从库里查回来，查不回就把 ok 打成 False 并如实说明，绝不把死 id 当成功回执发出去。
        # ── A24(#round5) 根因排查结论（iss_fb16973875 续）──
        # 穷举全仓库「dec_」id 去向后确认：唯一生成点是 decision_store.create 的
        #   did = "dec_" + uuid.uuid4().hex[:12]（L81），落库与回查均走同一 did（_row_to_dict 仅
        #   做 json.loads(options)，不改 id）。request_decision 回执 decision_id = did = rec["id"] =
        #   DB 行 id，三处同一来源、无中间改写/截断。调用方（mcp.autoflow_request_decision 取
        #   res["decision"]["id"]、apply_flow/apply_rollback 已从嵌套剥壳改为优先取 res["decision_id"]）
        #   均派生自同一 id，脆弱剥壳最多取 None 不会「一位之差」。故原始「dec_83d…3aea vs dec_83d…7aea」
        #   一位之差无法在当前代码任何路径复现——判定为历史传输/显示偶发，非代码缺陷。
        # 处置：不静默关单——保留上方 R4 读回自检为最终 fail-safe（任何未来错位都会被它拦成
        #   ok=False 而非发死 id），并新增 tests/test_decision_id_consistency.py 把「复现不出」固化为
        #   可执行守护（错位注入→ok:False + 300 轮 create→get→回执 id 一致性）。
        verify = None
        try:
            verify = self.decisions.get(did)
        except Exception as e:
            return {"ok": False, "decision": rec, "decision_id": did,
                    "error": f"决策已落库但读回自检异常：{e}；请勿按该 decision_id 轮询。"}
        if not verify or verify.get("id") != did:
            return {"ok": False, "decision": rec, "decision_id": did,
                    "error": (f"决策 id 读回自检失败：回执 id={did}，库中读回="
                              f"{(verify or {}).get('id')!r}。apply→get_decision 闭环会断，"
                              f"请查 decision_store 落库路径，勿按此 id 轮询。")}
        opts_preview = "\n".join(f"{i+1}. {o}" for i, o in enumerate(rec["options"]))
        threading.Thread(
            target=self._bark_push,
            args=(f"\U0001F3FB 需要你决策",
                  f"{question}\n{opts_preview}\n（在 WebUI 工作区选择）"),
            kwargs={"group": "AutoFlow-决策"},
            daemon=True,
        ).start()
        # decision_id 与 decision.id 同源（上面已读回自检），显式平铺一份便于调用方直取，
        # 避免各处各自 `((dec or {}).get("decision") or {}).get("id")` 层层剥壳时取空。
        return {"ok": True, "decision": rec, "decision_id": did,
                "note": "决策已写入工作区；请在 WebUI 工作区选择，Bark 也已催办"}

    def list_decisions(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """列出决策（pending 优先），供 WebUI 工作区「待你决策」卡渲染。"""
        return self.decisions.list(status=status, limit=limit)

    def _wrap_decision_followup(self, question: str, chosen: str) -> str:
        """把人类的选择回灌给 deepseek++ 的续跑提示词。"""
        return (
            "【人类已在 WebUI 工作区就你提出的决策做出选择】\n"
            f"你提出的问题：{question}\n"
            f"人类的选择：{chosen}\n\n"
            "请据此继续完成任务，用 autoflow 工具面执行；"
            "完成后经 MCP 发一条 Bark 告知大佬结果。"
        )

    def resolve_decision(self, did: str, choice_idx: int, by: str = "human") -> Dict[str, Any]:
        """人类点选 → 标记 resolved；若来源是 deepseek，把选择回灌 ds_bridge 续跑（双向闭环）。
        注意：回灌走 _fire_ds_bridge（可能等待 ds_bridge 空闲），调用方应放到后台线程，勿阻塞事件循环。"""
        try:
            rec = self.decisions.resolve(did, choice_idx, by=by)
        except (KeyError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        chosen = rec["chosen_text"]
        continued = False
        if rec["source"] == "deepseek":
            try:
                self._wait_ds_bridge_idle()
                fired = self._fire_ds_bridge(self._wrap_decision_followup(rec["question"], chosen))
                continued = bool(fired.get("ok"))
            except Exception:
                continued = False
        return {"ok": True, "decision": rec, "continued_to_deepseek": continued,
                "note": f"已选择「{chosen}」" + ("；已回灌 DeepSeek 继续。" if continued else "。")}

    # ───────────── 决策看门狗（超时催办，singleton） ─────────────
    def _start_watchdog(self) -> None:
        """模块级单例：无论多少 Gateway 实例，后台催办线程只启一个。"""
        global _watchdog_started
        with _watchdog_lock:
            if _watchdog_started:
                return
            _watchdog_started = True
        t = threading.Thread(target=self._watchdog_loop, name="decision-watchdog",
                             daemon=True)
        t.start()

    def _watchdog_loop(self) -> None:
        """每 60s 巡检 pending 决策，按间隔/上限发 Bark 催办（不强制默认、不隐藏）。"""
        while True:
            try:
                self._tick_decisions()
                # 成功一拍即清零连续失败计数（consecutive_failures）
                self._watchdog_failures = 0
            except Exception as e:
                # ★A-3 安全修复：原 except Exception: pass 静默吞掉所有看门狗异常，
                # 决策催办持续失败也无人知晓。改为记日志 + 连续失败计数，≥10 标 degraded。
                self._watchdog_failures = getattr(self, "_watchdog_failures", 0) + 1
                _gw_logger.error(
                    "watchdog tick failed (consecutive=%d): %s",
                    self._watchdog_failures, e, exc_info=True,
                )
                if self._watchdog_failures >= 10:
                    _gw_logger.error(
                        "watchdog degraded: %d consecutive failures, decision reminders may be stuck",
                        self._watchdog_failures,
                    )
            time.sleep(60)

    def _tick_decisions(self) -> None:
        interval = float(os.environ.get("AF_DECISION_REMINDER_MIN", "10"))  # 相邻催办间隔(分)
        max_r = int(os.environ.get("AF_DECISION_MAX_REMINDERS", "3"))       # 最多催办次数
        now = datetime.now(timezone.utc)
        for d in self.decisions.list(status="pending"):
            reminded = int(d.get("reminder_count") or 0)
            if reminded >= max_r:
                continue
            if reminded == 0:
                # 首次：决策创建满 interval 分钟才催
                age_min = (now - self._parse_dt(d.get("created_at"))).total_seconds() / 60.0
                due = age_min >= interval
            else:
                # 后续：距上次催办满 interval 分钟再催（保证间隔，不连发）
                last = self._parse_dt(d.get("escalated_at"))
                due = (now - last).total_seconds() / 60.0 >= interval
            if due:
                opts = "\n".join(f"{i+1}. {o}" for i, o in enumerate(d["options"]))
                self._bark_push(
                    "🗳️ 待决策提醒",
                    f"{d['question']}\n{opts}\n（仍在等待你在 WebUI 工作区选择）",
                    group="AutoFlow-决策",
                )
                self.decisions.record_reminder(d["id"])

    @staticmethod
    def _parse_dt(s: Optional[str]) -> datetime:
        try:
            return datetime.fromisoformat(s) if s else datetime.now(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    # ───────────── 辅助 ─────────────
    @staticmethod
    def _clean_wires(wires, valid_ids: set) -> Any:
        """清理连线中指向已删除节点的 target。

        兼容 NR 两种 wires 结构：单输出节点为 [[t1,t2]]（外层 list 内一层 list），
        多输出节点为 [[..],[..]]。仅保留仍存在于 valid_ids 的 target。

        对畸形嵌套（某 target 自身是 list，如 [['a']]）递归拍平并保留合法字符串 id，
        非字符串 / 嵌套中的非字符串元素直接丢弃——不再抛 `unhashable type: 'list'`。
        """
        if not wires:
            return wires

        def _keep(targets):
            result = []
            for t in targets:
                if isinstance(t, list):
                    result.extend(_keep(t))
                elif isinstance(t, str) and t in valid_ids:
                    result.append(t)
                # 其他类型（None/数字/dict）丢弃
            return result

        if isinstance(wires[0], list):
            return [_keep(out) for out in wires]
        return _keep(wires)

    @staticmethod
    def _collect_entities(flow: Dict) -> List[str]:
        ents = set()
        for n in flow.get("nodes", []):
            raw = n.get("entityId") or n.get("entity_id")
            if isinstance(raw, list):
                for e in raw:
                    if isinstance(e, str):
                        ents.add(e)
            elif isinstance(raw, str):
                ents.add(raw)
            ents_ = n.get("entities", {})
            if isinstance(ents_, dict):
                for e in ents_.get("entity", []):
                    if isinstance(e, str):
                        ents.add(e)
        return sorted(ents)

    def refresh_catalog(self, full: bool = False, domain: Optional[str] = None,
                        area: Optional[str] = None) -> Dict[str, Any]:
        """快照/增量同步 HA 实体进 device_catalog + entity_mapping。

        - 仅一次 get_states()（HA 调用最小）；
        - 按 last_changed 做增量 diff 落盘（不全量重写）；
        - area / device 经 HA websocket 注册表获取（替换已失效的 hass-cli 路径）；
          区域解析链 entity.area_id → device.area_id，**缺失时优雅降级** area 留空；
        - 播种语义映射(friendly_name→entity_id) + 区域索引 + 中文房间别名；
        - 已消失的实体标记 gone=True（不删除，保留映射与历史）。
        """
        try:
            states = self.ha.get_states(domain)
        except Exception as e:
            return {"ok": False, "error": f"无法读取 HA states: {e}"}

        # area / device 信息：经 HA websocket 注册表（entity_registry + device_registry）。
        # 每次 refresh 强制重拉，避免缓存陈旧；失败则 area 留空、核心能力不受影响。
        area_map: Dict[str, str] = {}
        device_map: Dict[str, str] = {}
        ha_areas: Dict[str, str] = {}
        ws_ok = False
        try:
            self.ha.client.invalidate_registries()
            ha_areas = self.ha.get_areas() or {}
            area_map = self.ha.client.entity_areas() or {}
            device_map = self.ha.client.entity_device_ids() or {}
            ws_ok = bool(ha_areas and area_map)
        except Exception:
            ha_areas = {}
            area_map = {}
            device_map = {}
        # websocket 注册表缺失/为空时（如虚拟 HA vhass 仅暴露 REST /api/areas，
        # 或真实 HA 该版本 websocket 注册表为空但 REST 可用），主动尝试 REST 兜底。
        # 注意：_get_registries 失败是「静默返回空」而非抛异常，故不能只依赖 except 分支。
        if not ha_areas:
            try:
                ha_areas = self.ha.get_areas_http() or {}
            except Exception:
                ha_areas = {}

        now = datetime.now(timezone.utc).isoformat()
        cat = self.state.get_device_catalog()
        ents = cat.setdefault("entities", {})
        # 既有区域/设备映射底（仅 websocket 抓取失败时启用）：防止一次瞬时失败
        # 把全库区域清零（2026-07-16 事故——07-13 一次 ws 失败致 area_available=False，
        # 所有实体 area="" ，黑箱按书房过滤设备清单时整片漏设备）。
        prev_areas: Dict[str, str] = {eid: e.get("area") for eid, e in ents.items() if e.get("area")}
        prev_devs: Dict[str, str] = {eid: e.get("device_id") for eid, e in ents.items() if e.get("device_id")}

        seen: set = set()
        added = changed = 0
        for s in states:
            eid = s.get("entity_id")
            if not eid:
                continue
            seen.add(eid)
            fn = (s.get("attributes") or {}).get("friendly_name") or ""
            existing = ents.get(eid)
            if ws_ok:
                # 正常路径：websocket 注册表实体映射（含 device 兜底）
                area_name = area_map.get(eid) or s.get("area") or ""
                dev_id = device_map.get(eid) or ""
            else:
                # websocket 抓取失败：保留既有区域/设备映射，绝不把全库区域清零；
                # 仅对新实体用 state 上的 area 兜底。
                area_name = (prev_areas.get(eid)
                             or (existing.get("area") if existing else "")
                             or s.get("area") or "")
                dev_id = (prev_devs.get(eid)
                          or (existing.get("device_id") if existing else "")
                          or "")
            caps = self._infer_caps(s)
            lc = s.get("last_changed")
            existing = ents.get(eid)
            if (not existing) or (existing.get("last_changed") != lc) or full:
                # B5a：把 attributes 写进 catalog（契约来源：options/hvac_modes/
                # supported_color_modes/effect_list/unit_of_measurement...）。
                # 之前漏存，导致「只有当前 state、没有所有可能状态」。
                # 剥掉纯展示、易膨胀的键（entity_picture/icon），保留契约信息。
                attrs = s.get("attributes", {}) or {}
                attrs = {k: v for k, v in attrs.items()
                         if k not in ("entity_picture", "icon")}
                ents[eid] = {
                    "entity_id": eid,
                    "domain": eid.split(".", 1)[0],
                    "friendly_name": fn,
                    "area": area_name,
                    "device_id": dev_id,
                    "capabilities": caps,
                    "attributes": attrs,
                    "state": s.get("state"),
                    "last_changed": lc,
                    "last_updated": s.get("last_updated"),
                    "indexed_at": now,
                    "gone": False,
                    "detail_cached": bool(existing and existing.get("detail_cached")),
                }
                if existing:
                    changed += 1
                else:
                    added += 1
            else:
                existing["gone"] = False  # 仍在，取消消失标记

        # 标记已消失实体（保留映射）
        gone = 0
        for eid, e in ents.items():
            if eid not in seen and not e.get("gone"):
                e["gone"] = True
                gone += 1

        self.state.set_device_catalog({
            "version": 1,
            "freshness": now,
            "entities": ents,
            "meta": {"incremental": (not full), "source": "ha_snapshot",
                     "area_available": bool(area_map) or bool(prev_areas)},
        })

        # 语义映射 + 区域索引 + 房间别名
        em = self.state.get_entity_mapping()
        em.setdefault("mappings", {})
        em.setdefault("areas", {})
        em.setdefault("room_aliases", {})
        for eid, e in ents.items():
            fn = e.get("friendly_name")
            if fn and fn not in em["mappings"]:
                em["mappings"][fn] = eid
        for aid, name in (ha_areas or {}).items():
            em["areas"][aid] = name
        em["room_aliases"] = self._build_room_aliases(em["areas"])
        em["freshness"] = now
        self.state.save_entity_mapping(em)

        return {
            "ok": True,
            "mode": "full" if full else "incremental",
            "entity_total": len(ents),
            "added": added,
            "changed": changed,
            "gone_marked": gone,
            "mapping_count": len(em["mappings"]),
            "area_count": len(em["areas"]),
            "area_available": bool(area_map) or bool(ha_areas),
            "freshness": now,
        }

    # ───────────── P2：虚拟孪生 staging 镜像 ─────────────
    def build_vhass_seed(self, src_catalog_path: Optional[str] = None,
                         out_path: Optional[str] = None) -> Dict[str, Any]:
        """从真实 device_catalog 生成 vhass 种子（同构 entity_id / 区域）。
        src 缺省用当前 env 的 device_catalog。"""
        from . import vhass as _vh
        src = src_catalog_path or os.path.join(
            self.cfg.data_dir, self.cfg.env, "state", "device_catalog.json")
        if not os.path.exists(src):
            return {"ok": False, "error": f"源 catalog 不存在: {src}"}
        seed = _vh.build_seed_from_catalog(src, out_path)
        return {"ok": True, "seed_path": out_path or "(returned)",
                "entity_count": len(seed["entities"]),
                "area_count": len(seed["areas"])}

    def mirror_catalog_to_staging(self, src_catalog_path: Optional[str] = None,
                                  staging_env: str = "staging",
                                  seed_out: Optional[str] = None) -> Dict[str, Any]:
        """把真实 catalog（含区域）镜像进 staging 的 device_catalog，
        使 staging 无需 hass-cli 即带区域；可选同时生成 vhass 种子。"""
        from .config import GatewayConfig
        src = src_catalog_path or os.path.join(
            self.cfg.data_dir, "prod", "state", "device_catalog.json")
        if not os.path.exists(src):
            return {"ok": False, "error": f"源 catalog 不存在: {src}"}
        with open(src, "r", encoding="utf-8") as f:
            cat = json.load(f)
        src_ents = cat.get("entities", {})
        if isinstance(src_ents, dict):
            src_ents = list(src_ents.values())

        now = datetime.now(timezone.utc).isoformat()
        ents = {}
        for e in src_ents:
            eid = e.get("entity_id")
            if not eid:
                continue
            ents[eid] = {
                "entity_id": eid,
                "domain": eid.split(".", 1)[0],
                "friendly_name": e.get("friendly_name") or eid,
                "area": e.get("area") or "",
                "capabilities": e.get("capabilities") or [],
                "state": e.get("state"),
                "last_changed": e.get("last_changed"),
                "last_updated": e.get("last_updated"),
                "indexed_at": now,
                "gone": False,
                "detail_cached": False,
            }
        # 写进 staging 共享态（独立 env）
        scfg = GatewayConfig()
        scfg.env = staging_env
        scfg.make_dirs()
        sst = SharedState(scfg)
        sst.set_device_catalog({
            "version": 1, "freshness": now, "entities": ents,
            "meta": {"source": "mirror_from_prod", "incremental": False},
        })
        # 语义映射 + 区域索引
        em = sst.get_entity_mapping()
        em.setdefault("mappings", {})
        em.setdefault("areas", {})
        em.setdefault("room_aliases", {})
        areas = {}
        for eid, e in ents.items():
            fn = e.get("friendly_name")
            if fn and fn not in em["mappings"]:
                em["mappings"][fn] = eid
            a = e.get("area")
            if a:
                areas.setdefault(f"area_{len(areas)}", a)
        em["areas"] = areas
        em["room_aliases"] = self._build_room_aliases(areas)
        em["freshness"] = now
        sst.save_entity_mapping(em)

        result = {"ok": True, "staging_env": staging_env,
                  "entity_total": len(ents), "mapping_count": len(em["mappings"]),
                  "area_count": len(areas)}
        if seed_out:
            seed = self.build_vhass_seed(src, seed_out)
            result["vhass_seed"] = seed
        return result

    @staticmethod
    def _infer_caps(state: Dict) -> List[str]:
        caps = []
        attrs = state.get("attributes") or {}
        sf = attrs.get("supported_features", 0)
        if isinstance(sf, int):
            # 粗略：依据 domain 推断常见能力
            dom = state.get("entity_id", "").split(".", 1)[0]
            if dom == "light":
                caps = ["on_off"]
                if sf & 1:
                    caps.append("brightness")
                if sf & 16:
                    caps.append("color_temp")
                if sf & 32:
                    caps.append("rgb_color")
            elif dom == "cover":
                caps = ["open", "close"]
                if sf & 4:
                    caps.append("position")
            else:
                caps = ["on_off"] if dom in ("switch", "fan", "automation") else []
        return caps

# ── 白盒部署日志 ──

def _project_data_dir() -> str:
    """从 gateway.py 位置推导项目 data 目录（<root>/data），不依赖 cwd / import 顺序。
    旧的回退路径用 dirname(__file__)/'..'/'data' 会落到 src/data（错），这里直接推到 <root>/data。"""
    here = os.path.dirname(os.path.abspath(__file__))   # .../src/autoflow_gateway
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))  # .../AutoFlow
    return os.path.join(root, "data")

_RAW_DEPLOY_LOG = os.path.join(_project_data_dir(), "raw_deploys.jsonl")

def _log_raw_deploy(agent: str, label: str, status: str, detail: str,
                    validation: List[Dict] = None):
    """追加一行到 raw_deploys.jsonl（失败模式库的数据源）。"""
    try:
        os.makedirs(os.path.dirname(_RAW_DEPLOY_LOG), exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "label": label,
            "status": status,  # DEPLOY_OK / DEPLOY_FAIL / SCHEMA_ERROR
            "detail": detail,
            "errors": len([v for v in (validation or []) if v.get("level") == "error"]),
            "warnings": len([v for v in (validation or []) if v.get("level") == "warning"]),
        }
        with open(_RAW_DEPLOY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入不阻塞主流程

# ── flow 快照留存（供黑箱编译器迭代的语料库）──
def _snapshot_dir() -> str:
    """快照根目录：落在 config.data_dir（与 autoflow.db 同处）；失败时回退到项目 data 目录。"""
    try:
        from .config import get_config
        d = get_config().data_dir
        if d:
            return os.path.join(d, "flow_snapshots")
    except Exception:
        pass
    return os.path.join(_project_data_dir(), "flow_snapshots")

def _sanitize_name(s: str, maxlen: int = 40) -> str:
    """把 label 变成安全文件名片段。"""
    import re as _re
    s = _re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (s or "").strip())
    return (s or "unnamed")[:maxlen]

def snapshot_flow(agent_id: str, kind: str, label: str, flow: Dict,
                  *, dsl: str = None, gate: Dict = None,
                  validation: List[Dict] = None, ok: bool = True,
                  extra: Dict = None) -> Optional[str]:
    """留存每一次 agent 产出的完整 flow JSON（黑白箱都存），供人/黑箱编译器迭代。

    - kind：'raw'(白箱 deploy_raw) / 'dsl'(黑箱 propose_dsl 编译产物)。
    - 落盘：data/flow_snapshots/YYYY-MM-DD/<ts>_<agent>_<kind>_<label>.json
    - 内容含：完整 flow、可选 dsl 源文、闸门结果、校验、状态。
    返回写入路径（失败返回 None，不阻塞主流程）。
    """
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        d = os.path.join(_snapshot_dir(), day)
        os.makedirs(d, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S_%f")[:-3]
        fname = f"{ts}_{_sanitize_name(agent_id, 20)}_{kind}_{_sanitize_name(label)}.json"
        path = os.path.join(d, fname)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "kind": kind,
            "label": label,
            "ok": ok,
            "dsl": dsl,
            "gate": gate,
            "validation": validation or [],
            "flow": flow,
        }
        if extra:
            payload.update(extra)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None  # 快照失败不影响部署/提案主流程

# ── apply 轨迹留痕（WB1-F / #694：回滚点索引 + 审计）──
def _apply_trace_dir() -> str:
    """apply 轨迹目录：与快照同处 data 根（config.data_dir 优先，回退项目 data）。"""
    try:
        from .config import get_config
        d = get_config().data_dir
        if d:
            return os.path.join(d, "apply_traces")
    except Exception:
        pass
    return os.path.join(_project_data_dir(), "apply_traces")

def _write_apply_trace(audit: Dict) -> Optional[str]:
    """把一次 apply 的审计信封追加进 data/apply_traces/<trace_id>.json。

    同一 trace_id 会被多次写入（先 pending 后 applied、以及 ROLLBACK），
    故文件内 events 为追加列表；顶层 flow_id / snapshot_path 取**首个非空**值，
    保证「人批准后用同一 trace_id 重调」时回滚点不被后写的空值冲掉。
    失败返回 None，绝不阻塞 apply 主流程。"""
    try:
        tid = audit.get("trace_id")
        if not tid:
            return None
        d = _apply_trace_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{_sanitize_name(str(tid), 40)}.json")
        rec = {"trace_id": tid, "events": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rec = json.load(f) or rec
            except Exception:
                rec = {"trace_id": tid, "events": []}
        ev = dict(audit)
        ev["ts"] = datetime.now(timezone.utc).isoformat()
        rec.setdefault("events", []).append(ev)
        for k in ("flow_id", "snapshot_path", "mode", "agent_id", "reason", "label"):
            if not rec.get(k) and audit.get(k):
                rec[k] = audit[k]
        rec["updated_at"] = ev["ts"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None

def _read_apply_trace(trace_id: str) -> Optional[Dict]:
    """读回 apply 轨迹（供 apply_rollback 找回滚点）。不存在/损坏返回 None。"""
    try:
        path = os.path.join(_apply_trace_dir(), f"{_sanitize_name(str(trace_id), 40)}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

