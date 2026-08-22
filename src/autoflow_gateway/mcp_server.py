#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — MCP 服务（双面板）+ 身份鉴权 + WebUI 合一

设计原则（使用者视角 · 三端点按能力分层）：
  · 用户面 /mcp        —— 任何 active 身份都能连，做「发现实体 → 写 DSL(编译器路径) → 提交 → 上报缺陷 / 自愈技能」。
                          仅编译器身份(mode=black)的专属入口；原生手写/admin 连这里只能拿到用户工具（无部署刀）。
  · 原生手写面 /mcp-white  —— 具原生手写能力(white/dual/admin)身份可连（拒仅编译器身份）；在用户工具基础上追加原生手写部署刀
                          (deploy_raw / modify_flow / commit_ha_service)，即「直接写 flow」最小集。
  · 管理员面 /mcp-admin —— 仅限管理员身份(mode=admin)可连（你专用）：原生手写部署刀 + 测试杠杆
                          (golden/acceptance 评测) + 网关自重启 + 任务池发布/重置/统计 + 缺陷闭环。
                          普通原生手写身份连 /mcp-admin 会被中间件直接 403，看不到也调不到运维刀。
  · 刻意不暴露：设备翻页浏览(discover/room_summary/export_room…)、废弃提交入口、
    经验沉淀(submit_proposal)、approve/reject 等冗余/危险/控制面工具，避免工具海与零信任破坏。

传输：Streamable HTTP（/mcp、/mcp-white、/mcp-admin 同端口 :8000 三个 path）。
身份：原生 ASGI 中间件在 /mcp 与 /mcp-admin 请求上强制校验 `Authorization: Bearer <身份码>`，
      解析不到/失效即 401 拒绝匿名；并把已认证 agent 注入 contextvars 供 tool 读取。
安全：MCP 面只暴露「读 + 提案 + 提交(进确认闸)」；approve/reject 仅留在 WebUI 控制面，
      杜绝 agent 自己批准自己的写操作。运维刀严格隔离在 /mcp-admin 且只认 admin 身份。
运行：
  python run.py serve                # MCP(/mcp,/mcp-white,/mcp-admin) + WebUI(/) 同端口 :8000
  python run.py mcp --no-webui      # 仅 MCP（仍强制身份）
"""
import argparse
import asyncio
import json
import os
import re
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from .gateway import Gateway, schema_blocking_issues
from .flow_linter import lint_flow
from .flow_simulator import simulate_flow
from .identity import AgentStore, AcpTokenStore, get_current_agent
from .template_lib import list_templates, render_template, TemplateValidationError
from .webui import build_webui_asgi
from .config import get_config, is_task_pool_enabled, is_submit_gate_enabled, is_acp_enabled
from typing import Optional
from . import acp_client  # 仅用 stdlib(urllib)，安全常驻导入
# llm_client 含 `import httpx` —— 改为惰性导入（见 autoflow_ask_llm），
# 避免 httpx 未安装时网关启动期 ImportError 全功能宕机（ACP 属小众，不应绑架 boot）。

def _gw():
    return Gateway()

def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

def _with_ok(r, ok: bool = True):
    """给只读类工具的原始数据字典补上统一的 ok 字段（仅当尚未包含时），
    消除 MCP 工具返回格式不一致——两份测试报告共同核实的真 bug：
    list_entities / dsl_help / list_templates / list_pending / list_automations
    此前缺 ok 字段，导致客户端需为不同工具写不同解析逻辑。
    非字典或已含 ok 的字典原样返回，避免重复包裹或吞掉错误结构。"""
    if isinstance(r, dict) and "ok" not in r:
        return {"ok": ok, **r}
    return r

def _task_pool_disabled() -> str:
    """任务池关闭时的统一返回（WebUI 开关控制）。"""
    return _js({"ok": False, "error": "DSL 验证任务池已关闭（由 WebUI 开关控制），暂不可用。"
                            "如需启用请在 WebUI 打开『DSL 验证任务池』开关。"})

# 单用户面（编译器/原生手写/管理员身份都连这里；工具按 agent.mode 在 tools/list 分层显隐）
#   · black：仅用户工具（无部署刀）
#   · white/dual/both/admin：用户工具 + 6 部署/自检刀
# /mcp-white 是 /mcp 的兼容别名（原生手写身份旧端点不失效）。
mcp = FastMCP("autoflow-gateway")
# 管理面（仅管理员 mode=admin 可连：用户工具 + 部署刀 + 测试杠杆 + 运维刀 + 任务池 + 缺陷闭环）
mcp_admin = FastMCP("autoflow-gateway-admin")

# autoflow_list_pending 的 settled（已落地提案）回吐条数上限。
# 历史队列可积压数百条，全量回吐即上下文炸弹；只给最近若干条供 agent 自查结局，
# 总数另以 settled_total 如实告知（WB72 F9）。
_SETTLED_LIMIT = 20

# 原生手写专属「部署/自检刀」——仅这些工具对 black 身份隐藏（其余用户工具三面板通用）
_DEPLOY_KNIVES = {
    "autoflow_deploy_raw", "autoflow_validate_flow", "autoflow_simulate_flow",
    "autoflow_run_e2e_trace", "autoflow_modify_flow", "autoflow_commit_ha_service",
    "autoflow_create_subflow",
    "autoflow_set_tab_state", "autoflow_verify_flow",
    "autoflow_apply", "autoflow_apply_rollback", "autoflow_apply_state_from_debug",
    "autoflow_get_trace",
}

# ───────────── 读：自然语言设备名 → 实体候选（跨域，不引导猜域）─────────────
@mcp.tool()
def autoflow_resolve_entity(name: str, area: str = "", top_n: int = 8) -> str:
    """【设备名→entity_id 唯一正路】把自然语言设备名（如「显示器挂灯」「书房牌匾灯泡」）解析成所有沾边候选 entity_id。
    写 DSL 之前必须先调用，拿到真实 entity_id 后只许用返回里的 ID，禁止凭记忆编造。

    - 网关【不过滤域】：同一设备名在书房可能对应 light.xxx / switch.yyy / cover.zzz 等多个实体，
      全部一并返回，由你自行判断该用哪一个（例如「显示器挂灯」可能是 light 也可能是 switch，
      看返回里的 domain + friendly_name 决定）。不要预设它是 light 还是 switch。
    - 每个候选返回 {entity_id, friendly_name, domain, area, state(当前状态),
      possible_states(该域可能状态), confidence}；
      possible_states 直接告诉你这个设备能同步到哪些状态（如 ["on","off"] / ["open","closed"]），
      省去你猜。
    - area 可传中文房间词(书房/主卧室...)优先在该房间内找；找不到自动放宽到全局。
    - confidence=high 是强匹配（精确别名/同名），medium/low 是模糊，优先取 confidence=high 或排序第一。
    - 写 DSL 时把选中的 entity_id 作为 resolved_entities 传入 autoflow_propose_dsl，闸门会强制校验。"""
    r = _gw().resolve_entity(name, area or None, None, top_n)
    return _js(r)

# ───────────── 读：实体目录·过滤浏览（与 resolve_entity 互补）─────────────
@mcp.tool()
def autoflow_list_entities(domain: str = "", area: str = "", keyword: str = "",
                           limit: int = 50, offset: int = 0) -> str:
    """【全屋实体目录·过滤浏览】按域/区域/关键词过滤返回实体目录（读本地设备目录，不触真实 HA）。

    与 autoflow_resolve_entity（自然语言设备名→候选 entity_id，写 DSL 前必调）互补：
    - resolve_entity：你已知大概设备名，想拿到唯一真实 entity_id；
    - list_entities：你想「按条件浏览目录」，例如「书房有哪些 light？」「全屋 cover 各在什么状态？」。

    参数（均可选，留空=不过滤）：
    - domain：按域过滤（light/switch/cover/climate/media_player/binary_sensor…）。
    - area：中文房间词/别名/区域名（书房/主卧室…），自动解析；找不到自动忽略区域过滤。
    - keyword：模糊匹配 entity_id / 中文名 / 区域（不区分大小写）。
    - limit/offset：分页（默认 50/页，上限 200），防止全屋 2976 实体一次撑爆上下文。

    返回每个实体 {entity_id, friendly_name, domain, area, state(当前状态),
    possible_states(该域可能状态)}；possible_states 直接告诉你它能同步到哪些状态
    （如 light→["on","off"]、cover→["open","closed"]），写 flow 立即知道目标状态怎么填。
    并透明回报 matched_count / returned / truncated / next_offset，方便翻页。
    💡 默认已按 limit=50 裁剪（全屋 2976 实体不会一次撑爆上下文）；需要更多时再放宽 limit。"""
    r = _gw().list_entities(domain or None, area or None, keyword or None, limit, offset)
    return _js(_with_ok(r))

# ───────────── 读：强制刷新 HA 实体目录（缓存填充，解除建 flow 死锁）─────────────
@mcp.tool()
def autoflow_refresh_catalog(full: bool = False, domain: str = "", area: str = "") -> str:
    """【一次性拉取 HA 实体目录】强制从 HA 重新快照全屋设备进本地缓存（device_catalog）。

    - 仅一次 HA get_states() + 注册表抓取，结果落本地缓存；之后 autoflow_resolve_entity /
      autoflow_list_entities 只读缓存、毫秒返回，agent 不再被网络拉取卡住。
    - 何时需要：首次连上 HA 后、设备大幅增减后、或 resolve/list 返回『device_catalog 为空』时。
    - 日常写 DSL 不需要重复调本工具（缓存已够用）。
    - 返回 {ok, added, changed, total, ...}；total 为当前目录设备总数。"""
    r = _gw().refresh_catalog(full=full, domain=domain or None, area=area or None)
    return _js(r)

# ───────────── 读：Automations 注册表·跨会话找回 ─────────────
@mcp.tool()
def autoflow_list_automations(keyword: str = "", only: str = "all",
                              limit: int = 50, offset: int = 0) -> str:
    """【Automations 注册表·跨会话找回】统一列出本网关建过的自动化（编译器 DSL 路径 + 原生手写路径），
    写新自动化前先查重、或跨会话找回自己/其他 agent 建过的东西。

    与 autoflow_list_entities（设备目录）互补：本工具找的是「已建成的自动化（flow）」，不是设备。
    范围（仅 flow 自动化）：已部署到 NR 的 flow（state=deployed）+ 待人类审核的 flow 提案（state=pending）；
    网关改进类经验提案不混入。

    参数（均可选）：
    - keyword：对 标题+spec 模糊匹配（不区分大小写），例如「夜灯」「客厅」「tts」；留空=不过滤。
    - only："all"（默认）/"deployed"/"pending" 限定来源。
    - limit/offset：分页（默认 50/页，上限 200）。

    返回每个自动化 {id, title, state, source(编译器/原生手写), spec(可读说明), created_at, flow_id?}；
    并透明回报 matched_count / returned / truncated / next_offset。"""
    r = _gw().list_automations(keyword or None, only, limit, offset)
    return _js(_with_ok(r))

# ───────────── DSL 自助指南 ─────────────
@mcp.tool()
def autoflow_dsl_help() -> str:
    """写 DSL 时随时调用：返回完整语法 + 可用子流程清单(含参数) + 写作范例 + 提交方式。
    这是 agent 边写边查的权威参考，内容随代码同步更新。"""
    return _js(_with_ok(_gw().dsl_help()))

# ───────────── 模板库（读：list / render）─────────────
@mcp.tool()
def autoflow_list_templates() -> str:
    """列出可用 DSL 模板。返回 {templates:[{name,description,tags,params}]}。
    先 list 拿到 name + params，再用 autoflow_render_template 填空生成合规 DSL，避免从零写、降幻觉。"""
    return _js(_with_ok({"templates": list_templates(),
                "next": "用 autoflow_render_template(name, values_json) 填空生成 DSL；"
                        "语法/子流程清单调 autoflow_dsl_help()；生成后交 autoflow_propose_dsl 提交。"}))

@mcp.tool()
def autoflow_render_template(name: str, values_json: str = "{}") -> str:
    """按名渲染 DSL 模板：把 values 填入 {{占位符}}（支持 {{var|默认值}}）生成合规 DSL 文本。
    - name：来自 autoflow_list_templates 的模板名。
    - values_json：JSON 对象，键为占位符名（如 {"room":"书房","sensor":"binary_sensor.study_door","light":"light.study_main"}）。
    - 默认严格校验：模板正文里无默认值的必填占位符（{{x}}）若未提供非空值，返回 ok=false 并列出 missing；
      多传模板不存在的键名则列出 unknown。这能避免把带空占位符的坏 DSL 推给下游 propose_dsl。
    - 返回 {ok, name, dsl}；模板不存在或参数校验失败 ok=false（附 missing/unknown/hint）。
    生成后交 autoflow_propose_dsl(dsl=..., expected_postconditions_json=...) 提交（建议把模板 预期: 块一并带上）。"""
    try:
        values = json.loads(values_json or "{}")
    except json.JSONDecodeError:
        return _js({"ok": False, "error": "values_json 非法 JSON"})
    try:
        dsl = render_template(name, values)
    except KeyError:
        return _js({"ok": False, "error": f"模板不存在: {name}", "available": [t["name"] for t in list_templates()]})
    except TemplateValidationError as e:
        return _js({
            "ok": False,
            "error": "模板必填参数缺失或包含未知参数",
            "missing": e.missing,
            "unknown": e.unknown,
            "hint": "请补齐 required 占位符（模板正文里无默认值的 {{x}}），或检查 values_json 的键名是否拼写错误。",
        })
    return _js({"ok": True, "name": name, "dsl": dsl,
                "next": "把 dsl 连同模板声明的 预期: 块交 autoflow_propose_dsl 提交。"})

# ───────────── 写：场景提交（进确认闸）─────────────
@mcp.tool()
async def autoflow_propose_dsl(dsl: Optional[str] = None, expected_postconditions_json: str = "[]",
                               resolved_entities_json: str = "[]", strict: bool = False,
                               require_e2e: bool = False) -> str:
    """【★推荐·提交场景首选入口】经 DSL 提案：解析→静态校验→编译→staging 闸门(vhass 重放断言)→落提案(raw)。

    ⭐ 这是 agent 提交场景的**首选**路径：用高层语义 DSL 描述意图，编译器自动生成合规 flow，
    比手写 Node-RED JSON 短一个数量级、且天然规避接线/节点类型坑。仅在 DSL 语法无法表达
    你的特定结构时，才退到 autoflow_deploy_raw（逃生舱）。

    用法：
      autoflow_propose_dsl(
        dsl="场景: 书房入户播报\\n触发: <传感器entity_id> on\\n动作: light.turn_on(<灯entity_id>)\\n调用子流程: demo_notify(text=欢迎回家, room=书房, level=一般)",
        expected_postconditions_json='[{"entity_id":"<灯entity_id>","state":"on"},{"subflow":"demo_notify"}]'
      )
    - dsl：语义 DSL 文本（场景/触发/动作/调用子流程/分支/否则/延时/并行）。语法与子流程清单调 autoflow_dsl_help()。
    - expected_postconditions_json：JSON 数组；元素为 {"entity_id","state"} 或 {"subflow":"<名>"}。
    - resolved_entities_json：差异式实体白名单，接受三种形式之一：① 字符串数组
      '["light.x","switch.y"]'；② 对象数组 '[{"entity_id":"light.x"}, ...]'；③ 不传或 "[]"
      （仅依赖 DSL 内实体引用 + 闸门强制校验）。闸门只放行白名单内实体，引用白名单外实体直接判 FAIL。
    - 注意：实体一律用 autoflow_resolve_entity 返回的真实 entity_id；引用目录外的实体闸门直接判 FAIL。
    - agent_id 由已认证身份自动注入；提案进入 raw，等待人类在 WebUI 审核升格。
    - 返回 {ok, proposal_id, scene_name, gate:{passed,replayed_services,assertions}, flow}。
    - strict：True 时 lint 存在任何 error/warning 即阻断提案（默认 False，仅随回执透出）。
    - require_e2e：True 时提案带 e2e 意图，人类在 WebUI 点「部署到 NR」时会真正先跑一次
      实机验证闸（verdict≠通过即拦截部署）。默认 False（沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E）。
      修复 iss_8d3cffaa96：此前该意图被静默吞掉、主部署路径从不调 e2e 闸。
    - 返回 {ok, proposal_id, scene_name, gate:{passed,...}, require_e2e, flow}。
    ⚠️ 不要用已废弃的 autoflow_propose_scene。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    try:
        expected = json.loads(expected_postconditions_json or "[]")
    except json.JSONDecodeError:
        expected = []
    try:
        resolved = json.loads(resolved_entities_json or "[]")
    except json.JSONDecodeError:
        resolved = []
    # 注：不再在请求热路径里 importlib.reload(dsl_engine)——重编译整模块既贵又非线程安全
    # （并发 reload 同一模块会污染模块命名空间）。改代码请走「重启网关」纪律。
    # propose_dsl 是纯 CPU/内存编译（零网络 I/O、零 await），直接跑会占满单 worker 事件循环、
    # 把并发请求串行化（iss_07ef8f36bb）。卸载到默认线程池，让并发编译在事件循环上重叠。
    gw = _gw()
    return _js(await asyncio.to_thread(
        gw.propose_dsl, dsl, agent.agent_id, expected,
        resolved_entities=resolved, strict=strict, require_e2e=require_e2e))

@mcp.tool()
def autoflow_get_flow(flow_id: str, summary: bool = True) -> str:
    """【只读】取回已部署 flow 的元信息 + 来源标记（source）。

    用于回看 propose_dsl 落地的编译产物、或检视线上 NR tab 的真实节点，
    无需进 WebUI。节点图来自 Node-RED(get_flow)，来源来自网关 flow catalog。
    - flow_id：NR flow id（如 '57be9a8f1fca2bcd'）。
    - summary：默认 True —— 只返回 {node_count, node_type_hist(节点类型直方图),
      source, label, disabled}，**不 dump 全节点图（省 token，绝大多数场景够用）**；
      确需检视连线时传 summary=False 取完整 flow_json 节点图。
    - 空 id / flow 不存在 / 无节点 → ok=False 并带原因。只读，绝不修改任何状态。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    return _js(_gw().get_flow(flow_id, summary=summary))

@mcp.tool()
def autoflow_list_tabs(only_disabled: bool = False, keyword: str = "") -> str:
    """【只读·巡检】列出 Node-RED 中所有 tab 流程（每个 tab=一个 flow，含用户手工/第三方创建的「卧室灯」「客厅」等），含启用/禁用状态与节点数。

    - only_disabled=True：只回被禁用的 tab（如临时停用「客厅语音播报」）。
    - keyword：按 label/id 模糊过滤（如 keyword="客厅" 只看含「客厅」的 tab）。
    - 返回 {ok, tabs:[{id,label,disabled,node_count,source}], count, count_disabled}：
      * disabled=true 表示该 tab 当前被停用（节点仍在，只是不跑）。
      * node_count：该 tab 内节点数（按降序排，节点多的排前面）。
      * source：来自网关 flow catalog 的来源标记（用户手工创建的通常为 null）。
      * count_disabled：全集里被禁用的 tab 总数（不受 only_disabled/keyword 影响）。
    纯旁路只读、幂等（含 5s 缓存），不 dump 节点内容，可放心频繁调用。
    想看某 tab 的节点详情，拿 id 调 autoflow_get_flow（其返回已含顶层 disabled，与本条一致）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    return _js(_gw().list_tabs(only_disabled=only_disabled, keyword=keyword or None))

@mcp.tool()
def autoflow_get_nr_flow() -> str:
    """【只读·诊断】扫描 Node-RED 全部子流程定义的结构完整性，灭绝「空壳假 PASS」。

    调网关 get_nr_subflow_integrity()（底层走网关自有凭据 GET /flows，无需任何额外 token，
    只读绝不修改状态）。返回 {ok, source, subflows:[{id,name,internal_node_count,
    empty_shell,has_mustache_entity,internal_types}], empty_shells:[id...], any_empty_shell}。

    用途（落地 iss_3d99371fe6 #1）：测试者/运维在 deploy_raw 之后、或日常巡检时，一次调用
    即可确认线上子流程 def 是否真实带满内部节点（空壳=内部节点数 0=无取数能力，比取空更糟）、
    是否存在 entityId 仍为 mustache 的降级节点。只读，可放心频繁调用。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    return _js(_gw().get_nr_subflow_integrity())

@mcp.tool()
def autoflow_debug_read(flow_id: str = "", node_id: str = "", since: int = 0,
                        limit: int = 50, full: bool = False) -> str:
    """【只读·诊断】读取 Node-RED 内部节点的实时 debug 事件（debug 回读，两条热路径都不碰）。

    数据来自网关后台线程旁路订阅的 NR5.0.1 原生 ws://<nr>/comms debug 事件流，
    缓存在网关本地（每节点有界环形缓冲 + TTL + 全局上限，零炸裂半径）。
    本工具只从本地缓冲读，**绝不**向 NR 发任何请求、也绝不往 flow 插采集节点。

    参数（均为可选过滤）：
      flow_id：只返回该 flow 内的事件（节点所属 flow 由事件 _path 自动归属）。
      node_id：只返回该节点 id 的事件。
      since  ：只看 received_at >= since（Unix 秒）之后的事件（配合 TTL 做增量回看）。
      limit  ：最多返回条数（默认 50，保护传输体积）。
      full   ：True 时附完整 payload（默认只返回截断预览 payload_preview，保护传输）。

    返回 {ok, source, enabled, connected, count, events:[{flow_id,node_id,name,
           topic,payload_preview,timestamp,received_at,retain}]}。无事件时 events=[]。
    注意：staging 环境多数 tab 被禁用（#607），仅 enabled 且被触发的 flow/subflow
          才会产生 debug 帧；要让托管子流程内部节点有事件，须在其 build 期
          (build_subflows.py) 预置 debug 节点，而非运行期注入（红线）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    return _js(_gw().get_debug_read(
        flow_id=flow_id or None, node_id=node_id or None,
        since=since or None, limit=limit, full=full))

@mcp.tool()
def autoflow_trigger_inject(flow_id: str = "", inject_id: str = "") -> str:
    """【诊断·触发】真实触发 Node-RED 中的 inject 节点（让 flow 跑一次，产生 debug 帧供 debug 回读）。

    这是 #644 debug 回读闭环的「触发」半环：先经本工具触发 → 节点运行产生 debug 事件 →
    再由 autoflow_debug_read 从本地缓冲读回（两条热路径都不碰）。仅点火 inject，**不修改**任何 flow。

    参数（二选一）：
      inject_id：直接触发该 inject 节点 id（精确，常见于已知节点 id）。
      flow_id ：触发该 flow 内**所有** inject 节点（批量，便于一键跑通整个 flow 看链路）。

    返回 {ok, flow_id?, inject_id?, triggered:[{id,name,status}], errors:[...], warning?}。
    flow 内无 inject 节点时返回 warning（无触发目标）。触发被禁用的 inject（#607 多数 tab 禁用）会静默无效，属预期。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    nr = _gw().nr
    try:
        if inject_id:
            code = nr.trigger_inject(inject_id)
            return _js({"ok": True, "inject_id": inject_id,
                        "triggered": [{"id": inject_id, "status": code}]})
        if flow_id:
            flow = nr.get_flow(flow_id)
            injects = [n for n in (flow.get("nodes") or []) if n.get("type") == "inject"]
            if not injects:
                return _js({"ok": True, "flow_id": flow_id, "triggered": [], "errors": [],
                            "warning": "该 flow 没有 inject 节点，无触发目标"})
            triggered, errors = [], []
            for n in injects:
                try:
                    code = nr.trigger_inject(n["id"])
                    triggered.append({"id": n["id"], "name": n.get("name", ""), "status": code})
                except Exception as e:
                    errors.append({"id": n["id"], "error": str(e)})
            return _js({"ok": True, "flow_id": flow_id, "triggered": triggered, "errors": errors})
        return _js({"ok": False, "error": "必须提供 flow_id 或 inject_id 之一"})
    except Exception as e:
        return _js({"ok": False, "error": str(e)})

@mcp.tool()
def autoflow_list_pending() -> str:
    """列出当前 agent 的待人工确认操作（按身份隔离）。提交后在此查进度。

    返回两类：
      · source="confirm" —— 经确认闸的写操作（原生手写部署/HA 服务调用等，等人类在 WebUI 批准）；
      · source="proposal" —— 本 agent 经 autoflow_propose_dsl 提交、尚处 raw 的场景提案
        （等人类在 WebUI 升格）。编译器 agent 提交场景后，进度应在这里看到，而非凭空消失。

    ★ 已部署的提案不在 pending 里，改列在 settled（WB72 F9 / iss_3fc501da8c）：
      提案被人类在 WebUI 批准部署后，只写 deployed_flow_id，status 仍是 "raw"。
      此前本工具按 status="raw" 一把捞，于是**已经部署、已经真实生效**的提案
      永远滞留在「待办」里 —— 队列谎报待办，agent 也无从确认自己那条批没批。
      WebUI 侧列表（gateway.py:3448）早有「有 deployed_flow_id 就不算待审」这条
      过滤，此处补齐同一契约，消除两条呈现路径的漂移。
      settled 保留结局供 agent 自查（带 deployed_flow_id），并做条数上限防上下文炸弹。"""
    agent = get_current_agent()
    aid = agent.agent_id if agent else None
    pending = [{"source": "confirm", **op} for op in _gw().list_pending(aid)]
    settled: list = []
    settled_total = 0
    # 同时列出本 agent 经 propose_dsl 提交的原始提案(raw)，使其能跟踪自己提交的场景。
    try:
        from .proposals import ProposalStore
        store = ProposalStore(_gw().cfg)
        for p in store.list(agent_id=aid, status="raw"):
            entry = {
                "source": "proposal",
                "id": p.id,
                "title": p.title,
                "kind": p.kind,
                "status": p.status,
                "created_at": p.created_at,
            }
            if p.deployed_flow_id:
                # 已落地：结局已定，不再是待办
                settled_total += 1
                if len(settled) < _SETTLED_LIMIT:
                    settled.append({**entry, "state": "deployed",
                                    "deployed_flow_id": p.deployed_flow_id})
            else:
                pending.append(entry)
    except Exception:
        pass
    return _js(_with_ok({"agent_id": aid, "pending": pending,
                "settled": settled, "settled_total": settled_total}))

@mcp.tool()
def autoflow_set_plan(current: str = "", overall: str = "",
                      completed_append: str = "") -> str:
    """【同步工作区进度】更新 WebUI「工作区」tab 的 plan 状态，让你（人类）在手机上实时看到进度。

    - current：当前正在做的事（一句话，如「正在编译 H7 场景 DSL」）。每次推进都更新。
    - overall：总体计划/里程碑（可选，覆盖式更新；一般只在阶段开始时设一次）。
    - completed_append：刚完成的一件事（如「H1 场景已落库并通过 staging 闸门」），追加到「最近完成」日志。
    至少传一个字段。返回更新后的 plan 快照（含 completed 列表）。"""
    gw = _gw()
    if not (current or overall or completed_append):
        return _js({"ok": False, "error": "至少传 current / overall / completed_append 之一"})
    state = gw.plan.update(
        overall=overall or None,
        current=current or None,
        append_completed=completed_append or None,
    )
    return _js({"ok": True, "plan": state})

@mcp.tool()
def autoflow_request_decision(question: str, options: list) -> str:
    """【向人类请示 / 请求决策 · WebUI 通道】当你（deepseek）执行任务遇到需人类拍板的分叉点时，
    用此工具抛出一道选择题：question 是问题，options 是 2~N 个候选（字符串列表）。
    人类会在 WebUI「工作区」看到此题并点选。

    ⚠️ 通道选择：若你是**交互式 chat agent 且人类就在这段对话里**，轻量可逆的歧义
    （如选哪盏灯、用什么亮度）请**直接用自然语言在聊天框问**，不要调本工具——把人赶到
    WebUI 是割裂体验。本工具仅用于：(1) agent 无人值守(headless/任务池/定时)，唯一取回人类
    选择的途径；(2) 需把选择持久化/可审计地记进网关供稍后复查。

    闭环协议（必须照此续跑，否则回路在 MCP 通道上断开）：
      1. 调本工具 → 拿到 decision_id；
      2. 把 decision_id 原样告诉人类，然后停下等人类在 WebUI 工作区拍板——
         不要在同一个回合反复轮询（人类要离开会话去 WebUI 点选，轮询拿不到结果只是空转）；
      3. 人类选完后（新回合触发）调一次 autoflow_get_decision(decision_id)，
         确认 status=="resolved" 后取出 chosen_text（人类的选择文本）继续任务。
    人类的选择也会经 WebUI 自动回灌，但你作为纯 MCP agent 必须靠 autoflow_get_decision 取回结果。
    返回决策 id 与提示。不要滥用——普通进度同步用 autoflow_set_plan 即可，只有真正需人类拍板才请示。"""
    gw = _gw()
    if not question or not isinstance(options, list) or len(options) < 1:
        return _js({"ok": False, "error": "question 必填且 options 至少 1 项"})
    res = gw.request_decision(question, options, source="deepseek")
    if not res.get("ok"):
        return _js(res)
    d = res["decision"]
    return _js({"ok": True, "decision_id": d["id"],
                "question": d["question"], "options": d["options"],
                "note": "已提交人类决策队列；人类在 WebUI 工作区选择后，用 autoflow_get_decision(decision_id) 取回选择续跑。"})

@mcp.tool()
def autoflow_get_decision(decision_id: str) -> str:
    """【取回人类决策结果】对应 autoflow_request_decision 抛出的决策。
    人类在 WebUI 拍板后，你在「新回合」调一次本工具取回结果即可；
    不要在单回合内循环轮询（人类需离开会话操作 WebUI，轮询只会空转）。
    - decision_id：autoflow_request_decision 返回的 decision_id。
    返回完整决策记录：{id, question, options, status, chosen_idx, chosen_text, created_at, resolved_at}。
      · status=="pending"  → 人类尚未选择，等人类在 WebUI 拍板后再取回；
      · status=="resolved" → 可用 chosen_text 续跑。
    典型协议：request_decision →（让出回合，人类在 WebUI 拍板）→ 新回合 get_decision 一次 → 读 chosen_text 续跑。"""
    gw = _gw()
    rec = gw.decisions.get(decision_id)
    if rec is None:
        return _js({"ok": False, "error": f"决策 {decision_id} 不存在"})
    return _js({"ok": True, **rec})

@mcp.tool()
def autoflow_list_decisions(status: str = "", limit: int = 50) -> str:
    """【查看决策队列】列出人类决策（pending 优先，其次时间倒序）。
    - status：按状态过滤——"pending"（待人类选择）/ "resolved"（已选）/ 留空=全部。
    - limit：最多返回几条（默认 50）。
    返回列表，每项含 {id, question, options, status, chosen_idx, chosen_text, created_at, resolved_at}。
    用途：先 list pending 看是否有在等人类的决策；人类在 WebUI 选完后，用 autoflow_get_decision(decision_id) 取回具体选择。"""
    gw = _gw()
    rows = gw.list_decisions(status=status or None, limit=limit)
    return _js({"ok": True, "count": len(rows), "decisions": rows})

# ───────────── DSL 验证任务池（分布式多 agent 协作）─────────────
@mcp.tool()
def autoflow_list_tasks(only_mine: bool = False, status: str = "", limit: int = 0,
                        offset: int = 0, fields: str = "") -> str:
    """【任务池·看板】列出 DSL 验证任务池里可领的任务。每条含：场景说明(task_text) +
    已注入的真实实体 hint(entity_id/friendly_name/domain/area/possible_states) +
    需用到的子流程(subflow_hint) + 期望后置条件(expected)，以及『你（当前身份）』的领用/提交状态。
    先看本工具挑任务；再用 autoflow_claim_task 领一条开始写 DSL。
    - only_mine=True：只回显本 agent 已领用/已提交的任务（用于断点续传回顾）。
    - status：按状态过滤（如 claimed/unclaimed/submitted/done），留空=全部。
    - limit：最多返回几条（默认 0=不限制，但建议设 50 避免大包倾倒；任务池满载时尤其重要）。
    - offset：分页偏移（配合 limit 翻页）。
    - fields：只返回指定字段（逗号分隔，如 "id,title,status"），留空=全字段。
    返回体附 summary：{total, returned, by_status} 便于一眼看清池子状态。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    aid = agent.agent_id if agent else None
    qstatus = status or ("claimed" if only_mine else None)
    tasks = _gw().tasks.list(agent_id=aid, status=qstatus)
    total = len(tasks)
    # 字段投影
    if fields:
        keep = [f.strip() for f in fields.split(",") if f.strip()]
        tasks = [{k: t.get(k) for k in keep} for t in tasks]
    # 分页
    if limit and limit > 0:
        tasks = tasks[offset:offset + limit]
    by_status = {}
    for t in tasks:
        s = t.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return _js({"agent_id": aid, "total": total, "returned": len(tasks),
                "by_status": by_status, "tasks": tasks,
                "next": "用 autoflow_claim_task() 领一条；写完 DSL 调 "
                        "autoflow_submit_result(task_id, dsl) 提交。语法随时调 autoflow_dsl_help()。"})

@mcp.tool()
def autoflow_claim_task(task_id: str = "") -> str:
    """【任务池·领用】领一条验证任务，自动断点续传。
    优先返回『你已领用但还没提交』的任务；否则挑一条你还没做的新任务标记为 claimed。
    多名 agent 各自独立做同一任务、互不打扰（每人一条 claim 记录）。
    返回任务详情（场景 + 实体 hint + 子流程 hint + 期望）。
    - task_id：指定要认领的任务 id（如 "auto_xxx" / "wb_xxx"）。填了就直领这条，
      不再随机轮询——适合你从 list_tasks 里挑好了已知新任务。不存在或不属于你身份
      可领的 tier（编译器误领原生手写 wb_* 会被拒）时返回错误。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    # 按身份模式隔离 tier：
    #   white → 只领 auto_wb（白箱专属原生节点任务，黑箱绝不误领）
    #   black → 只领 auto（黑箱 DSL 任务）
    #   dual  → 先领 auto_wb，空了再领 auto（一套身份干两池活）
    #   both  → 向后兼容旧身份，按黑箱处理（仅 auto）
    # （wb_* 原生节点任务为白箱专属，黑箱绝不误领）
    agent_mode = getattr(agent, "mode", "black") or "black"
    tiers = {
        "white": ["auto_wb"],
        "dual": ["auto_wb", "auto"],
    }.get(agent_mode, ["auto"])
    # 直领指定 task_id（避免随机轮询）
    if task_id:
        t = _gw().tasks.claim_specific(agent.agent_id, task_id, allowed_tiers=tiers)
        if t is None:
            return _js({"ok": False, "error":
                        f"task_id={task_id} 不存在，或不属于你身份可领的 tier"
                        f"（身份={agent_mode}，允许 {tiers}）。请确认 id 正确且未被越权认领。"})
        return _js({"ok": True, "claimed": t["id"], "task": t,
                    "next": "根据实体 hint 与子流程 hint 写 DSL，写完调 "
                            "autoflow_submit_result(task_id, dsl)。语法调 autoflow_dsl_help()。"})
    # 原随机/断点续传逻辑
    t = None
    for tier in tiers:
        t = _gw().tasks.claim(agent.agent_id, prefer_mine=True, tier=tier)
        if t is not None:
            break
    if t is None:
        return _js({"ok": False, "done": True,
                    "message": "没有你可领的新任务了：要么你都做完了，要么任务池已被领光。"
                               "可换身份，或等管理员 autoflow_publish_tasks 发布新任务。"})
    return _js({"ok": True, "claimed": t["id"], "task": t,
                "next": "根据实体 hint 与子流程 hint 写 DSL，写完调 "
                        "autoflow_submit_result(task_id, dsl)。语法调 autoflow_dsl_help()。"})

# ───────────── 只读自助：whoami / 实体实时状态 ─────────────
def _tools_of(server) -> list:
    """同步取某 MCP 面板已注册的工具名（list_tools 为同步方法）。失败返回空。"""
    try:
        tm = getattr(server, "_tool_manager", None)
        if tm is None:
            return []
        tools = tm.list_tools()
        return [t.name for t in tools]
    except Exception:
        return []

# 各 mode 默认可连的面板（用于 whoami 报告「你此刻能调哪些工具」）
_MODE_PANEL = {
    "black": "mcp",
    "white": "mcp-white",
    "dual": "mcp-white",
    "both": "mcp-white",   # both 为旧式未限制身份，白箱面板工具集为其超集
    "admin": "mcp-admin",
}
_MODE_CAP = {
    "black": "只能走编译器路径（autoflow_propose_dsl），无原生手写部署刀；可写 DSL、查实体、领任务。",
    "white": "可直写 Node-RED flow（autoflow_deploy_raw / modify_flow / commit_ha_service）+ 全部用户工具 + L2 逻辑仿真。",
    "dual": "编译器/原生手写双任务池都能领（auto_wb 优先，空了再 auto）；其余同 white。",
    "both": "旧式未限制身份（向后兼容）；可见原生手写面板全部工具。建议后续收敛为 white/black/dual。",
    "admin": "全部用户工具 + 原生手写部署刀 + 测试杠杆(golden/acceptance 评测) + 运维刀(重启网关/发布重置任务池/缺陷闭环)。",
}

@mcp.tool()
@mcp_admin.tool()
def autoflow_whoami() -> str:
    """【自检·我是谁】返回当前身份的连接信息，避免「不知道自己是谁/能干嘛/连错面板」的困惑。
    返回：agent 身份(name/agent_id/mode/tier/status) + 本 mode 的能力说明 +
          你当前面板实际可调用的工具清单（实时取自网关注册表，不会过期）+ 端点提示。
    用户/原生手写/管理员三面板都能调；结果随你连的面板与身份模式自动适配。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    mode = getattr(agent, "mode", "black") or "black"
    panel = _MODE_PANEL.get(mode, "mcp")
    server = {"mcp": mcp, "mcp-white": mcp, "mcp-admin": mcp_admin}.get(panel, mcp)
    tools = _tools_of(server)
    if mode == "black":
        tools = [t for t in tools if t not in _DEPLOY_KNIVES]
    return _js({
        "ok": True,
        "agent": {
            "name": agent.name,
            "agent_id": agent.agent_id,
            "mode": mode,
            "tier": getattr(agent, "tier", ""),
            "status": getattr(agent, "status", ""),
        },
        "endpoint": panel,
        "mode_capability": _MODE_CAP.get(mode, ""),
        "available_tools": tools,
        "tool_count": len(tools),
        "hint": "以上为『当前面板』可调用工具；调之前先核对本清单，避免调不存在的工具。"
                "若想换能力（如拿部署刀），请改用对应模式的身份码连对应面板。",
    })

@mcp.tool()
@mcp_admin.tool()
def autoflow_get_entity_state(entity_id: str) -> str:
    """【只读·查实体实时状态】直连网关配置的 HA，返回某实体当前状态，省去为「查当前状态」
    专门搭一个 api-current-state 节点。

    用法：autoflow_get_entity_state(entity_id="light.study_main")
    - entity_id：真实实体 id（先用 autoflow_resolve_entity 取，勿凭记忆编造）。
    - 返回 HA REST /api/states/<id> 原样结构：{entity_id, state, attributes,
      last_changed, last_updated, context}；source="live" 表示实时读取。
    - 若实时 HA 不可达（离线/令牌失效），自动回退到网关设备目录缓存（source="catalog_cache"，
      并在 note 标注『可能非最新』），便于离线也能拿到大致状态。
    - 三面板均可调，纯只读、不改任何设备状态。"""
    if not entity_id or not entity_id.strip():
        return _js({"ok": False, "error": "entity_id 必填，先用 autoflow_resolve_entity 取真实 id。"})
    eid = entity_id.strip()
    gw = _gw()
    # 1) 实时读（网关配置的 HA，即真实 HA <NAS_IP>:8123 或 vhass）
    try:
        live = gw.ha.get_state(eid)
        if live:
            return _js({"ok": True, "source": "live", "state": live})
    except Exception as _e:
        pass  # 实时失败，走缓存兜底
    # 2) 回退到设备目录缓存
    try:
        cat = gw.state.get_device_catalog().get("entities", {})
        if eid in cat:
            meta = cat[eid]
            return _js({"ok": True, "source": "catalog_cache",
                        "note": "实时 HA 读取失败，已回退到网关设备目录缓存（可能非最新）。",
                        "state": {
                            "entity_id": eid,
                            "state": meta.get("state"),
                            "attributes": {"friendly_name": meta.get("friendly_name"),
                                           "area": meta.get("area")},
                            "domain": meta.get("domain"),
                        }})
    except Exception:
        pass
    return _js({"ok": False, "error":
                f"无法读取实体 {eid}：实时 HA 不可达且该实体不在网关设备目录缓存中。"
                f"请先 autoflow_resolve_entity 确认 id 存在，或检查网关 HA 配置(HASS_SERVER/HASS_TOKEN)。"})

@mcp.tool()
def autoflow_delegate_to_memory_worker(task: str, context_json: str = "{}") -> str:
    """【ACP 反向委派 memory-worker】经 ACP 把任务委派给对端 memory-worker（取家庭记忆/知识检索）。

    与 memory-worker 的 delegate_to_autoflow 对称（规格 §8）；本工具本身只读、不发起任何写操作，
    既有「共享态→防御层→确认闸」写护栏对 ACP 调用同样生效（委派不会绕过）。
    - task：自然语言任务描述（如『记一下昨晚书房灯关了之后，牌匾灯还常亮』）。
    - context_json：可选 JSON 对象，透传给 memory-worker（对齐 delegate_to_autoflow 的 context 字段），
      例如 '{"entity_id":"light.study_main"}'。
    - 未配置 MEMORY_WORKER_ACP_URL / MEMORY_WORKER_ACP_TOKEN → 返回 ok=false + 友好提示（不报错）；
      请在网关连接设置或环境变量填入 memory-worker 的 /acp 地址与 acp_ 令牌后重试。
    - 成功返回 {ok, session_id, status, text, blocks}；text 即 memory-worker 的 completed 文本。"""
    if not is_acp_enabled(get_config()):
        return _js({"ok": False, "error": "ACP 已关闭（WebUI「ACP 令牌」页开关）",
                    "hint": "在 WebUI 将 ACP 开关打开即可使用。"})
    try:
        ctx = json.loads(context_json or "{}")
    except json.JSONDecodeError:
        return _js({"ok": False, "error": "context_json 非法 JSON"})
    res = acp_client.delegate_to_memory_worker(task, context=ctx)
    if not res.get("ok"):
        return _js({"ok": False, "error": res.get("error", "委派失败"),
                    "hint": "请确认网关已配置 MEMORY_WORKER_ACP_URL / MEMORY_WORKER_ACP_TOKEN。"})
    return _js({"ok": True, "session_id": res.get("session_id"), "status": res.get("status"),
                "text": res.get("text"), "blocks": res.get("blocks")})

@mcp.tool()
async def autoflow_ask_llm(prompt: str, model: str = "", system: str = "") -> str:
    """【自带 LLM 钩子】调网关内置大模型（OpenAI 兼容 /chat/completions，多后端 fallback）做通用问答 / 摘要。

    与 delegate_to_memory_worker 对称：autoflow 自身具备 LLM 能力，不再依赖 memory-worker 转发。
    - prompt：必填，问题或指令文本（如『用一句话总结刚发生的灯光变化』）。
    - model：可选，覆盖默认模型（如 gpt-4o / deepseek-chat）；留空用网关默认。
    - system：可选，系统提示词。
    - 未配置 LLM（缺 AUTOFLOW_LLM_* 环境变量 / llm_backends）返回 {ok:False} 友好提示，不崩；
      全部后端 429/5xx/超时/鉴权失败自动 fallback，仍失败返回 {ok:False, error}。"""
    if not is_acp_enabled(get_config()):
        return _js({"ok": False, "error": "ACP 已关闭（WebUI「ACP 令牌」页开关）",
                    "hint": "在 WebUI 将 ACP 开关打开即可使用。"})
    if not prompt or not prompt.strip():
        return _js({"ok": False, "error": "prompt 必填"})
    try:
        from . import llm_client  # 惰性导入：httpx 缺失仅在本工具调用时报错，不绑架网关 boot
        router = llm_client.get_llm_router()
        text = await router.chat([{"role": "user", "content": prompt}],
                                 model=model or None, system=system or None)
        return _js({"ok": True, "text": text})
    except Exception as e:  # 含 LLMError / ImportError(httpx 缺失) / 网络超时 —— 均不裸崩
        return _js({"ok": False, "error": f"LLM 调用失败: {e}"})

@mcp.tool()
def autoflow_submit_result(task_id: str, dsl: str) -> str:
    """【任务池·提交】提交你对某任务的 DSL 写法。网关即时校验（解析→编译→lint；可选 staging 闸门），
    记录通过/失败及原因，供 DSL 引擎迭代参考。
    - task_id：来自 autoflow_claim_task 返回的 id。
    - dsl：你写的语义 DSL 文本（语法见 autoflow_dsl_help；实体一律用任务里的真实 entity_id，勿编造）。
    返回 {ok, result_kind(compiled/compile_error/gate_pass/gate_fail/lint_error/no_response),
          error_msg, node_count, lint_summary}。提交后可继续 autoflow_claim_task 领下一条。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    # 先确认任务存在，避免幻影提交：对不存在的 id 仍编译+lint 返回 gate_pass 并写脏 claim
    t = _gw().tasks.get(task_id, agent.agent_id)
    if t is None:
        return _js({"ok": False, "result_kind": "task_not_found",
                    "error": "task_id 不存在，请先 autoflow_claim_task 领一条有效任务再提交。"
                             "常见原因：id 拼写错误（幻影 id）或任务未发布。"})
    # tier 隔离：提交必须与领用同口径——原生手写只能交 wb_*、编译器只能交 auto_*、
    # dual 可交两者。否则白箱 agent 拿黑箱 task_id 直接 submit 就能越界污染黑箱池
    # （已发生的真实 bug：opencode【white】提交了 252 条 auto_*）。映射与 autoflow_claim_task 保持一致。
    agent_mode = getattr(agent, "mode", "black") or "black"
    _tiers = {
        "white": ["auto_wb"],
        "dual": ["auto_wb", "auto"],
    }.get(agent_mode, ["auto"])
    _task_tier = (t or {}).get("tier")
    if _task_tier not in _tiers:
        return _js({"ok": False, "result_kind": "tier_forbidden",
                    "error": f"task_id={task_id} 属 tier={_task_tier}，不属于你身份可提交的 tier"
                             f"（身份={agent_mode}，允许 {_tiers}）。提交越界被拒——请只提交你身份对应的任务池。"})
    if not dsl or not dsl.strip():
        _gw().tasks.submit(task_id, agent.agent_id, "", "no_response", "空 DSL")
        return _js({"ok": False, "result_kind": "no_response", "error": "DSL 为空，未提交。"})
    # 取该任务期望后置条件作校验输入（若有）
    expected = (t or {}).get("expected") or []
    requires_branch = bool((t or {}).get("requires_branch"))
    # 提交时闸门（默认关闭，由 feature_flags.submit_run_gate 控制，免重启）。
    # 开启后会对每次提交跑 run_staging_gate（branch-aware vhass 重放断言），
    # 缺分支/逻辑错误的浅版 DSL 被判 gate_fail 拦截，正确版得 gate_pass。
    run_gate = is_submit_gate_enabled(get_config())
    res = _gw().verify_task_dsl(dsl, expected=expected, run_gate=run_gate,
                                requires_branch=requires_branch)
    rk = res.get("result_kind", "compile_error")
    _gw().tasks.submit(task_id, agent.agent_id, dsl, rk,
                       res.get("error") or "", res.get("node_count", 0),
                       res.get("gate_passed"))
    return _js({"ok": res.get("ok", False), "task_id": task_id,
                "result_kind": rk, "error_msg": res.get("error"),
                "node_count": res.get("node_count"),
                "lint_summary": res.get("lint_summary", []),
                "gate": res.get("gate"),
                "gate_passed": res.get("gate_passed")})

@mcp.tool()
def autoflow_report_issue(title: str, body: str, task_id: str = "",
                          severity: str = "medium", category: str = "defect") -> str:
    """【缺陷/建议上报】执行任务中发现网关/DSL/实体解析等问题的能力，或改进建议，用此工具登记。
    与 autoflow_set_plan（进度同步）不同：本工具专用于「值得人类跟进的缺陷或需求」，会落库成 backlog。
    - title：一句话标题（必填）。
    - body：详细描述（必填），建议含：现象 / 复现步骤 / 期望 / 实际 / 关联任务 id。
    - task_id：可选，关联的验证任务 id（如 hist2_xxx）；非任务类缺陷可空。
    - severity：low|medium|high|critical（默认 medium）。
    - category：defect(缺陷)|doc(文档)|dsl(语法)|entity(实体解析)|feature(新需求)|other（默认 defect）。
    返回 issue_id；人类在 WebUI/CLI 审阅后处理。不要滥用——普通进度同步请用 autoflow_set_plan。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if not title or not title.strip():
        return _js({"ok": False, "error": "title 必填"})
    if not body or not body.strip():
        return _js({"ok": False, "error": "body 必填"})
    res = _gw().tasks.report_issue(
        agent.agent_id, title.strip(), body.strip(),
        task_id or None, severity, category,
    )
    if not res.get("ok"):
        return _js(res)
    return _js({"ok": True, "issue_id": res["issue_id"],
                "note": "已登记到缺陷 backlog；人类会在审阅时看到并处理。"})

@mcp.tool()
def autoflow_get_skill(name: str) -> str:
    """【技能指导自愈】读取最新的 skill 指导文档全文（如 autoflow），返回 markdown。
    MCP 重连只刷新工具 schema，不会刷新 agent 系统提示里加载的 skill 文档；当发现本工具说明与
    系统提示里的 skill 不一致、或技能已更新时，调用本工具拉取最新版即可自愈，无需重启 agent。
    - name：技能名，仅允许字母/数字/下划线/连字符，映射到 <skills_dir>/<name>.md。
    返回 {ok, name, path, content, bytes}；找不到或非法名返回 {ok:false}。只读，绝不修改文件。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", name or ""):
        return _js({"ok": False, "error": "invalid skill name (allowed: [A-Za-z0-9_-]+)"})
    cfg = _gw().cfg
    base = getattr(cfg, "skills_dir", "") or ""
    if not base:
        return _js({"ok": False, "error": "skills_dir 未配置"})
    base_norm = os.path.normpath(base)
    target = os.path.normpath(os.path.join(base_norm, name + ".md"))
    if target != base_norm + ".md" and not target.startswith(base_norm + os.sep):
        return _js({"ok": False, "error": "path traversal denied"})
    if not os.path.isfile(target):
        return _js({"ok": False, "error": f"skill '{name}' not found in {base_norm}"})
    try:
        with open(target, encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return _js({"ok": False, "error": f"read failed: {e}"})
    return _js({"ok": True, "name": name, "path": target,
                "content": text, "bytes": len(text.encode("utf-8"))})

# 用户工具挂到三个端点：/mcp（用户面，任何身份）、/mcp-white（原生手写面）、/mcp-admin（管理面）。
# 原生手写/管理员在各自端点都拿到完整用户能力；原生手写部署刀/运维刀分别只在对应端点追加。
_USER_TOOLS = (autoflow_resolve_entity, autoflow_list_entities, autoflow_refresh_catalog,
               autoflow_list_automations,
               autoflow_dsl_help,
               autoflow_list_templates,
               autoflow_render_template, autoflow_propose_dsl, autoflow_list_pending,
               autoflow_set_plan, autoflow_request_decision,
               autoflow_get_decision, autoflow_list_decisions,
               autoflow_list_tasks, autoflow_claim_task, autoflow_submit_result,
               autoflow_report_issue, autoflow_get_skill,
               autoflow_get_flow, autoflow_list_tabs)
for _fn in _USER_TOOLS:
    mcp_admin.tool()(_fn)

# ═══════════════════════════════════════════════════════════════════════════
# 原生手写部署刀（deploy_raw / validate_flow / simulate_flow / run_e2e_trace / modify_flow / commit_ha_service）
#   —— 注册在【用户面 /mcp】与【管理面 /mcp-admin】两处（admin 是全集）。
#      编译器身份(mode=black)连 /mcp 时，tools/list 由 _MCPApp 过滤掉这 6 把刀（调用时也有守卫兜底）。
# ═══════════════════════════════════════════════════════════════════════════

@mcp_admin.tool()
@mcp.tool()
def autoflow_deploy_raw(flow_json: str, label: str = "", target: str = "staging",
                        force: bool = False, require_e2e: bool = False) -> str:
    """【⚠️逃生舱·非首选】把 Agent 产出的 Node-RED flow JSON 提交为**提案**（不直接部署到 NR）。

    🚨 这是**逃生舱（escape hatch）**，不是首选路径：手写裸 NR 节点 JSON 既费 token（一个 flow
    几百行进上下文）、又易踩节点类型/接线语义坑（如未注册节点会被 node_gate 硬拦）。
    **默认请先用 autoflow_propose_dsl（★推荐）**——仅在 DSL 语法确实无法表达你的特定结构时
    才退到此工具。退到此工具时，提案落档会附 deploy_blocked_reasons 预告部署阶段将被硬拦的硬伤。

    用法：
      autoflow_deploy_raw(
        flow_json='{"id":"my-flow","label":"测试","nodes":[...]}',
        label="我的测试flow",
        target="staging"
      )

    - flow_json：Node-RED flow 描述。可直接传「完整 flow 对象字符串」
      '{"id":"my-flow","label":"测试","nodes":[...]}'，也可传「节点数组字符串」'[{...},{...}]'
      （网关自动包成 {nodes:[...]}）。务必是字符串（不要传已解析的对象）。
    - label：自定义标签（缺省从 flow_json 提取）。
    - target："staging"(inject 触发，默认) / "prod"(真实 HA 事件)。
    - force：是否强制覆盖同名已存在 flow（默认不覆盖）。
    - require_e2e：True 时提案带 e2e 意图，人类在 WebUI 点「部署到 NR」时会真正先跑一次
      实机验证闸（verdict≠通过即拦截部署）。默认 False（沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E）。
      修复 iss_8d3cffaa96：此前该意图被静默吞掉、主部署路径从不调 e2e 闸。

    网关会自动执行（仅校验，不落 NR）：
      1. Schema 校验（http body 格式、HA 节点完整性、节点 id 唯一等）
      2. HA server 占位符替换
      3. 静态 Linter 硬伤集（R13/R15/R17/R20/R22）+ L2 逻辑可达性仿真（fail-open）
      4. 把「已校验的 flow + 校验摘要」落为提案（kind=skill, content.type=raw_flow）

    ⚠️ 此工具**不再直接部署**：返回 {ok, proposal_id, ...}。提案需人类在 WebUI「场景提案」
       面板审核后，点「部署到 NR」才真正写入 Node-RED。这是与编译器 DSL 路径（autoflow_propose_dsl）
       完全统一的提案闸，避免一轮原生手写任务就刷出几十个未经人审的 tab。
    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用；黑箱身份只能用 autoflow_propose_dsl。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，只能用 "
                    "autoflow_propose_dsl 走 DSL 规则路径；原生手写直写请改用『原生手写身份码』。"})
    aid = agent.agent_id
    try:
        if isinstance(flow_json, str):
            data = json.loads(flow_json)
        else:
            data = flow_json
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict):
            return _js({"ok": False, "error": "flow_json 必须是 JSON 对象或节点数组"})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _js({"ok": False, "error": f"flow_json 非法 JSON: {e}"})
    # 原生手写统一：不再直写 NR，改为落提案（content.type=raw_flow），返回 proposal_id 待人类审核部署。
    return _js(_gw().propose_raw(data, agent_id=aid, label=label or None,
                                 target=target, force=force, require_e2e=require_e2e))

@mcp_admin.tool()
@mcp.tool()
def autoflow_create_subflow(dsl_name: str, name: str, definition_json: str,
                            description: str = "") -> str:
    """【子流程提案闸】把 Agent 产出的 Node-RED 子流程定义提交为**提案**（不直接注册到网关）。

    用法：
      autoflow_create_subflow(
        dsl_name="my_subflow",                       # DSL 调用名（调用子流程 <my_subflow>）
        name="我的子流程",                            # 人类可读名
        definition_json='{"id":"sf_my_subflow","nodes":[...],"in_ports":[...],"out_ports":[...]}',
        description="做什么用的"
      )

    - dsl_name：DSL 调用名（MCP/编译器侧用 调用子流程 <dsl_name> 引用），必填，字母/数字/下划线。
    - name：人类可读名（提案卡片标题），必填。
    - definition_json：子流程的 Node-RED 定义（**字符串**）。必填字段：
        id（NR 子流程 id，如 "sf_my_subflow"）、
        nodes（节点数组）、
        in_ports（输入端口定义数组）、
        out_ports（输出端口定义数组）。
      其余可选：info/category/env_requirements/input_schema 等随定义透传。
    - description：自定义说明，仅用于提案卡片检索，缺省回退 name。

    网关会自动执行（仅结构校验，不写 NR）：
      1. 校验 dsl_name / name 非空
      2. 校验 definition 含必填字段 id / nodes / in_ports / out_ports
      3. 把「子流程定义」落为提案（kind=subflow, content.type=subflow）

    ⚠️ 此工具**不直接注册**：返回 {ok, proposal_id, ...}。提案需人类在 WebUI「场景提案」
       面板审核后，点「部署到 NR」才真正写入 Node-RED 子流程实例并登记到网关子流程注册表
       （subflow_registry），此后 DSL / 原生手写即可用 调用子流程 <dsl_name> 引用它。
      这是与 DSL 路径（autoflow_propose_dsl）/ 原生手写路径（autoflow_deploy_raw）完全统一的提案闸。
    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用；黑箱身份只能用 autoflow_propose_dsl。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，只能用 "
                    "autoflow_propose_dsl 走 DSL 规则路径；子流程编写请改用『原生手写/管理员身份码』。"})
    aid = agent.agent_id
    try:
        if isinstance(definition_json, str):
            definition = json.loads(definition_json)
        else:
            definition = definition_json
        if not isinstance(definition, dict):
            return _js({"ok": False, "error": "definition_json 必须是 JSON 对象字符串"})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _js({"ok": False, "error": f"definition_json 非法 JSON: {e}"})
    return _js(_gw().propose_subflow(dsl_name, name, definition,
                                     description=description, agent_id=aid))

@mcp_admin.tool()
@mcp.tool()
def autoflow_validate_flow(flow_json: str) -> str:
    """【原生手写部署前自检】静态校验 Agent 产出的 Node-RED flow JSON，**不部署**、不触碰 NR。

    用法：
      autoflow_validate_flow(
        flow_json='{"id":"my-flow","label":"测试","nodes":[...]}'
      )
      —— 也可传节点数组字符串 '[{...},{...}]'（自动包成 {nodes:[...]}）。务必是字符串。

    它跑与 deploy_raw 完全相同的静态检查：
      1. Schema 校验（http body 格式、HA 节点完整性、节点 id 唯一等）
      2. 静态 Flow Linter（lint_flow）：含 R19（entityId 字段名写错）、
         R20（api-current-state / api-get-history / server-state-changed 缺/空 entityId/监听实体 → 硬伤）、R13（孤儿动作节点）、
         R15（紧环）、R17（悬空连线/断线 → 硬伤）、R21（switch 死分支）、
         R22（节点缺必填字段 → 硬伤）、R10（多数组连线）等「静态合法、运行必错」反模式。

    返回结构：
      {
        "ok": true,                       // 是否通过（无 error 级问题）
        "error_count": N,
        "warning_count": M,
        "errors":  [ {level,rule,node_id,node_type,message}, ... ],
        "warnings":[ {level,rule,node_id,node_type,message}, ... ],
        "will_deploy_block": true|false,  // 真去 deploy_raw 是否会被硬伤规则拦下
                                          // （含 lint 硬伤 R13/R15/... 与 schema 致命项 S1..S5）
        "schema_blocking": [ ... ],       // schema 致命项：S1 结构非法 / S2 缺 type /
                                          // S3 HA 缺 server / S4 switch wires≠rules / S5 空 flow
        "summary": "一句话结论",
        "logic": {                        // 【Phase B 新增】L2 逻辑可达性仿真（仅报告，不阻断部署）
          "ok": true|false,               // 所有动作终点在静态场景下都可达则为 true
          "logic_issues": [ ... ],        // 逻辑层问题（如 L1 动作永不达）
          "unreachable_actions": [ ... ], // 永不可达的动作节点 id 列表
          "summary": "逻辑层一句话结论"
        }
      }

    ⚠️ 关键约定：构造完 flow 后**先调本工具自检**，errors 非空（或 will_deploy_block=true）
       必须先修再调 autoflow_deploy_raw。实体 id 先用 autoflow_resolve_entity 取真实
       entity_id 填入 `entityId` 字段（NR5 ha-websocket 契约是 camelCase，不是 entity_id）。
    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，"
                    "请改用『原生手写身份码』调用本工具。"})
    # 解析 flow_json（与 deploy_raw 同逻辑）
    try:
        if isinstance(flow_json, str):
            data = json.loads(flow_json)
        else:
            data = flow_json
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict):
            return _js({"ok": False, "error": "flow_json 必须是 JSON 对象或节点数组"})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _js({"ok": False, "error": f"flow_json 非法 JSON: {e}"})

    gw = _gw()
    # 1) Schema 校验
    schema_issues = gw.validate_flow_schema(data) if hasattr(gw, "validate_flow_schema") else []
    # 2) 静态 Flow Linter（与 deploy_raw 同参数）
    lint_issues = lint_flow(data, b1_unreachable=True)

    # 3) 【Phase B】L2 逻辑可达性仿真（flow_simulator）。纯静态、不部署。
    #    本段【仅报告】，不并入 ok / will_deploy_block 的计算（硬拦由 B4 阶段
    #    LOGIC_BLOCK_ON_ERROR 配置决定）。仿真器自身异常 fail-open，绝不影响结构校验结果。
    try:
        _sim = simulate_flow(data)
        logic_block = {
            "ok": _sim.get("ok", True),
            "logic_issues": _sim.get("logic_issues", []),
            "unreachable_actions": _sim.get("unreachable_actions", []),
            "action_endpoints": _sim.get("action_endpoints", []),
            "reachable_actions": _sim.get("reachable_actions", []),
            "scenarios": _sim.get("scenarios", []),
            "summary": _sim.get("summary", ""),
        }
    except Exception as _e:
        logic_block = {
            "ok": True,
            "logic_issues": [],
            "unreachable_actions": [],
            "action_endpoints": [],
            "reachable_actions": [],
            "scenarios": [],
            "summary": f"逻辑仿真跳过（simulator error: {_e}）",
        }

    all_issues = list(schema_issues) + list(lint_issues)
    errors = [v for v in all_issues if v.get("level") == "error"]
    warnings = [v for v in all_issues if v.get("level") == "warning"]

    # 计算「真部署是否会被硬伤规则拦下」（对齐 deploy_raw 的 _LINT_BLOCK_RULES）
    # 注意：只有 R13/R15/R20/R17/R22 的 error 级才会被 deploy_raw 硬拦；
    # R19 字段名写错、R21 switch 死分支属报告不阻塞。
    # R9(#round4) iss_86d66844f7：schema **致命**错误（S1..S5：结构非法/缺 type/缺 server/
    # switch wires≠rules/空 flow）现在也会被 deploy_raw 以 stage=schema_block 硬拦，
    # 必须计入 will_deploy_block——此前它们只进 errors 不进 blocking，导致坏流拿到
    # will_deploy_block=false 的绿灯，一部署就炸。非致命 schema error 仍只报告不阻塞。
    # D15/round9：R10（多数组连线——第 2+ 数组目标永不触发）与 R19（entity_id
    # 字段名写错——部署即报 Joi 校验错）同为 error 级且后果严重（功能失效/部署失败），
    # 与 R13/R15/R17/R20 一致纳入阻断；旧实现漏了它们 → validate 报 error 却
    # will_deploy_block=false，deploy_raw 放行坏流。
    _BLOCK_RULES = {"R10", "R13", "R15", "R20", "R17", "R19", "R22", "R30", "R32"}
    blocking = list(schema_blocking_issues(schema_issues)) + [
        v for v in lint_issues
        if v.get("level") == "error" and v.get("rule") in _BLOCK_RULES]
    will_block = bool(blocking)

    ok = (len(errors) == 0)
    if not ok:
        summary = (f"校验未通过：{len(errors)} 个 error、{len(warnings)} 个 warning。"
                   f"请先修复 error 再部署（其中 {len(blocking)} 个属硬伤，会被 deploy_raw 直接拦下）。")
    elif warnings:
        summary = f"通过（{len(warnings)} 个 warning，不影响部署，建议顺手修）。"
    else:
        summary = "通过，无 error 无 warning，可放心部署。"

    return _js({
        "ok": ok,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "will_deploy_block": will_block,
        "blocking_rules": sorted({b.get("rule") for b in blocking}),
        # R9：把 schema 致命项单列，让调用方一眼看出是「结构没写对」还是「lint 硬伤」
        "schema_blocking": schema_blocking_issues(schema_issues),
        "summary": summary,
        "logic": logic_block,
    })

@mcp_admin.tool()
@mcp.tool()
def autoflow_verify_flow(flow_json: str, run_gate: bool = True,
                        require_e2e: bool = False, target: str = "staging",
                        allow_prod: bool = False) -> str:
    """【白箱质量验证·只读·绝不部署】按需跑与 deploy_raw 同源的质量闸，但不写 NR / 不登记 catalog。

    用法：
      autoflow_verify_flow(
        flow_json='{"id":"my-flow","label":"测试","nodes":[...]}',
        run_gate=True,            # 是否跑 vhass staging 闸（含 HA 动作时）
        require_e2e=False,        # 是否跑 e2e 实机追踪（落 staging + 回滚，默认关）
        target="staging",
        allow_prod=False          # e2e 需临时写 prod 实例时必须显式 True（见下方说明）
      )
      —— 也可传节点数组字符串 '[{...},{...}]'（自动包成 {nodes:[...]}）。务必是字符串。

    它跑：
      1. Schema 校验（与 deploy_raw 同源）
      2. 静态 Flow Linter（R13/R15/R17/R20/R22 等硬伤集，fail-open 不拦）
      3. 可选 vhass staging 闸（仅当含 HA 动作且能提取实体）
      4. 结构金丝雀（内省 NR 子流程完整性：空壳 / mustache 占位 → WARN）
      5. 可选 e2e 实机追踪（require_e2e=True 才跑，落 staging 后回滚）

    返回统一 verdict：
      {
        "ok": true,                       // verify 恒 ok（是检查不是动作）
        "deployed": false,               // 显式声明未部署
        "verdict": "pass"|"warn"|"block",
        "passed": bool,
        "gate": { "verdict", "passed", "layers": {vhass_staging, e2e_trace, structure_canary}, "notes" },
        "validation": [...], "lint": [...],
        "lint_error_count": N, "lint_warning_count": M
      }

    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用；黑箱身份不可见。
    ⚠️ 与 autoflow_validate_flow 区别：本工具额外跑 vhass / e2e / 结构金丝雀，给出统一
        deploy 前质量 verdict；validate_flow 只做静态 schema+lint+logic 仿真。
    ⚠️ allow_prod（A20）：require_e2e=True 时 e2e 会往 NR 实例临时写插桩副本再回滚。
        若目标 NR 实例被判定为 prod（AUTOFLLOW_ENV=prod 或 NR_PROD=1，与端口无关），
        默认会被 prod 写护栏拦下并报 NRGuardError。需要在该实例上跑 e2e 时，
        显式传 allow_prod=True 表示知情放行；默认 False，prod 锁保持生效。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，"
                    "请改用『原生手写身份码』调用本工具。"})
    aid = agent.agent_id
    # 解析 flow_json（与 validate_flow / deploy_raw 同逻辑）
    try:
        if isinstance(flow_json, str):
            data = json.loads(flow_json)
        else:
            data = flow_json
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict):
            return _js({"ok": False, "error": "flow_json 必须是 JSON 对象或节点数组"})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _js({"ok": False, "error": f"flow_json 非法 JSON: {e}"})
    res = _gw().verify_flow(data, agent_id=aid, run_gate=run_gate,
                            require_e2e=require_e2e, target=target,
                            allow_prod=allow_prod)
    return _js(res)

@mcp_admin.tool()
@mcp.tool()
def autoflow_simulate_flow(flow_json: str, virtual_states_json: str = "") -> str:
    """【原生手写逻辑预检】对 Node-RED flow JSON 做 L2 逻辑可达性仿真，**不部署**、不触碰 NR。

    与 autoflow_validate_flow 的 logic 段同源（都调 flow_simulator.simulate_flow），
    但本工具是「纯逻辑预检」独立入口：当你只想看「这条流在静态场景下哪些动作永远跑不到」
    而不想跑一遍完整 lint 时用它。

    用法：
      autoflow_simulate_flow(
        flow_json='{"id":"my-flow","label":"测试","nodes":[...]}'
      )
      —— 也可传节点数组字符串 '[{...},{...}]'（自动包成 {nodes:[...]}）。务必是字符串。

      # 可选：注入虚拟实体状态，反证某个分支是否恒假
      autoflow_simulate_flow(
        flow_json='...',
        virtual_states_json='{"light.study_desk": "on"}'
      )

    virtual_states_json 格式：JSON 对象 {entity_id: state_value}，用于验证「若某实体
      处于某状态，分支是否可达」。例如 flow 里有个 switch 判断 light.x 是否 off，
      你注入 {"light.x":"on"} 就能验证「on 时」那条分支的动作是否可达。

    返回结构：
      {
        "ok": true|false,                 // 无 L1 级逻辑错误（动作永不可达）
        "logic_issues": [ ... ],          // 逻辑层问题（含 L0 无入口 / L1 动作永不可达）
        "action_endpoints": [ ... ],      // 图中全部动作终点 id
        "reachable_actions": [ ... ],     // 至少在一场景可达的动作终点 id
        "unreachable_actions": [ ... ],   // 任何场景都触达不到的动作终点 id（即 L1）
        "scenarios": [                    // 每个触发源派生出的触发场景
          { "entry_id", "entry_type", "fires", "note", "reached_actions":[...], "notes":[...] }
        ],
        "summary": "逻辑层一句话结论"
      }

    ⚠️ 本工具仅【报告】逻辑问题，绝不阻断部署（阻断由 autoflow_deploy_raw 的逻辑闸门
       按配置决定）。本工具与 validate 一样，仿真器异常时 fail-open（ok=true+跳过说明），
       不影响你继续其他检查。
    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，"
                    "逻辑预检属原生手写能力，请改用『原生手写身份码』调用本工具。"})

    # 解析 flow_json（与 validate_flow / deploy_raw 同逻辑）
    try:
        if isinstance(flow_json, str):
            data = json.loads(flow_json)
        else:
            data = flow_json
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict):
            return _js({"ok": False, "error": "flow_json 必须是 JSON 对象或节点数组"})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _js({"ok": False, "error": f"flow_json 非法 JSON: {e}"})

    # 解析 virtual_states_json（可选）
    virtual_states = None
    if virtual_states_json:
        try:
            virtual_states = json.loads(virtual_states_json)
            if not isinstance(virtual_states, dict):
                return _js({"ok": False,
                            "error": "virtual_states_json 必须是 JSON 对象 {entity_id: state_value}"})
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _js({"ok": False, "error": f"virtual_states_json 非法 JSON: {e}"})

    # 【Phase B】L2 逻辑可达性仿真（flow_simulator）。纯静态、不部署。
    # 仿真器异常 fail-open，绝不影响返回结构。
    try:
        _sim = simulate_flow(data, virtual_states=virtual_states)
        logic_block = {
            "ok": _sim.get("ok", True),
            "logic_issues": _sim.get("logic_issues", []),
            "unreachable_actions": _sim.get("unreachable_actions", []),
            "action_endpoints": _sim.get("action_endpoints", []),
            "reachable_actions": _sim.get("reachable_actions", []),
            "scenarios": _sim.get("scenarios", []),
            "summary": _sim.get("summary", ""),
        }
    except Exception as _e:
        logic_block = {
            "ok": True,
            "logic_issues": [],
            "unreachable_actions": [],
            "action_endpoints": [],
            "reachable_actions": [],
            "scenarios": [],
            "summary": f"逻辑仿真跳过（simulator error: {_e}）",
        }

    return _js(logic_block)

@mcp_admin.tool()
@mcp.tool()
def autoflow_run_e2e_trace(dsl: str = "", flow_json: str = "",
                           expected_path_json: str = "",
                           expected_postconditions_json: str = "",
                           trigger_json: str = "",
                           live: bool = False,
                           allow_prod: bool = False) -> str:
    """【原生手写 L3 运行时追踪】把 flow **真实部署到 NR** 并触发，用插桩抓取实际执行轨迹，
    与期望路径比对 → 断点报告（明确跑到哪、在哪断、报错是什么）。

    两种输入二选一：
      - flow_json：原生手写原始 Node-RED flow（不经 DSL 编译）→ 走 run_e2e_trace_raw。
      - dsl：编译器 DSL 文本（网关编译成 flow）→ 走 run_e2e_trace（编译器路径）。

    触发策略（原生手写更鲁棒）：
      - flow 含 inject 节点 → 真实触发每个 inject；
      - flow 无 inject 但含「事件入口」(server-state-changed / trigger 等)
        → 在插桩副本里替换为合成 inject（发出 faithful state-change msg）再触发，
          真实执行下游逻辑（⚠️ 这是 test-double：验证『下游在给定事件下能否跑到 sink』，
          而非『真实 HA 经 websocket 推事件』——vhass 暂无 websocket，P3 已知缺口）；
      - 两者皆无 → 拦截并说明书式返回。

    参数：
      - expected_path_json：可选，期望经过的节点 id/name 列表（JSON 数组字符串）。
        缺省自动按入口 BFS 推导计划路径。
      - expected_postconditions_json：可选，HA 副作用后置校验（需 live HA，软失败不阻断）。
      - trigger_json：可选，合成触发事件 {"entity_id","state","old_state"}（state 入口替换用）。
      - live：是否真实 HA 模式（影响后置校验语义），默认 False。
      - allow_prod（A20）：追踪必须往 NR 实例临时写插桩副本再回滚。若该实例被判定为
        prod（AUTOFLLOW_ENV=prod 或 NR_PROD=1，与端口无关），默认被 prod 写护栏拦下
        （NRGuardError）。需要在该实例上追踪时显式传 allow_prod=True 知情放行；
        默认 False，prod 锁保持生效。

    返回：{e2e, flow_id, verdict(通过/断点/拦截), reasons, report, trace, triggered, entity_warnings}
      —— 直接消费 report.breakpoint 即知「在哪断、为什么」。

    ⚠️ 仅原生手写/管理员身份可经 /mcp-white（或 /mcp-admin）调用。追踪是自愈式的：
      部署的插桩副本会在比对后自动回滚 + 清空 trace context，不留残留。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，"
                    "运行时追踪属原生手写能力，请改用『原生手写身份码』调用本工具。"})

    # 解析可选 JSON 参数
    def _parse(opt, name, as_list=True):
        if not opt:
            return None
        try:
            v = json.loads(opt)
            return v
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _js({"ok": False, "error": f"{name} 非法 JSON: {e}"})

    exp_path = _parse(expected_path_json, "expected_path_json")
    if isinstance(exp_path, str):
        return exp_path  # 已是错误 JSON 串
    exp_post = _parse(expected_postconditions_json, "expected_postconditions_json")
    if isinstance(exp_post, str):
        return exp_post
    trig = _parse(trigger_json, "trigger_json", as_list=False)
    if isinstance(trig, str):
        return trig

    gw = _gw()
    try:
        if flow_json:
            result = gw.run_e2e_trace_raw(
                flow_json,
                expected_path=exp_path,
                expected_postconditions=exp_post,
                live=live,
                trigger=trig,
                allow_prod=allow_prod,
            )
        elif dsl:
            result = gw.run_e2e_trace(
                dsl,
                expected_path=exp_path,
                expected_postconditions=exp_post,
                live=live,
                allow_prod=allow_prod,
            )
        else:
            return _js({"ok": False, "error": "必须提供 flow_json 或 dsl 之一。"})
    except Exception as e:
        return _js({"ok": False, "error": f"运行时追踪异常：{e}",
                    "stage": "trace"})
    # run_e2e_trace* 已带 e2e/verdict；统一包一层 ok 便于 agent 判读
    result = dict(result)
    result["ok"] = (result.get("verdict") == "通过")
    return _js(result)

@mcp_admin.tool()
@mcp.tool()
def autoflow_modify_flow(flow_id: str, dsl: str = "", node_patches: str = "") -> str:
    """外科式修改已存在的 flow（原生手写身份专用）。

    - dsl：提供新 DSL 字符串 → 重新编译并复用该 flow 的 id/label 原地更新
      （结构性改动，等价于整条重编译）。
    - node_patches：JSON 字符串，形如
      [{"match":{"name":"开灯"},"set":{"name":"开主卧灯"}}] → 只改匹配节点的指定字段（最小改动）。
      match 支持 id / name / type；set 为要写入的字段字典；remove 为要删除的字段名列表。
    二选一即可。

    返回 {ok, flow_id, changed_nodes, node_count, mode}。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，"
                    "外科式改 flow 仅『原生手写身份』可用。"})
    patches = None
    if node_patches:
        try:
            patches = json.loads(node_patches)
        except Exception as e:
            return _js({"ok": False, "error": f"node_patches 不是合法 JSON：{e}"})
    r = _gw().modify_flow(flow_id, dsl=dsl or None, node_patches=patches)
    return _js(r)

@mcp_admin.tool()
@mcp.tool()
def autoflow_apply(mode: str, correction_json: str, flow_id: str = "",
                   auto_approve: bool = False, trace_id: str = "") -> str:
    """【apply 闭环·唯一落地入口】把「触发→回读→归因」得出的修正真正落回系统（原生手写/管理员专用）。

    这是 inject 触发(autoflow_trigger_inject) → debug 回读(autoflow_debug_read) 之后的最后一环。
    三种 mode：
      A = 观测驱动修正流（改 flow：结构性修正，correction 用 dsl 或 node_patches）
      B = 回读数据落状态（写 HA 服务：correction 用 domain/service/data）
      C = 热补丁（改 flow：局部改一个参数/文案，correction 用 node_patches）

    correction_json（JSON 字符串）按 mode 取字段：
      A/C：{"node_patches":[{"match":{"name":"开灯"},"set":{"name":"开主卧灯"}}], "reason":"为什么改"}
           或 {"dsl":"场景: ...", "reason":"..."}
      B  ：{"domain":"light","service":"turn_on","data":{"entity_id":"light.study"},"reason":"..."}
      reason 强烈建议写（会原样呈现给人类审批，写不清楚容易被拒）。

    ★自愈闭环（Self-Healing Loop）安全模型（改 flow 是高风险的，但有界自动写回）：
      - 默认**自动写回**：autoflow_apply 调一次即落 apply 前快照做回滚点、直接 modify_flow 写回，
        不进人审闸（否则 agent 永远没法自己调试已部署 flow）。
      - 有界失效保护（三次机会）：per-(agent, flow) 滑动窗口失败预算（自愈重试次数，WebUI 可配，
        默认 3），同一 (agent, flow) 连续失败达上限即停止并转报告/人工
        （stage=selfheal_budget_exhausted），防自动修复死循环；中间一次成功即清零计数。
      - `auto_approve` 参数**已废弃**：本闭环恒自动写回，保留签名仅为调用方兼容。
      mode=B 属低风险，本层直接放行给 HA 写服务确认闸（返回 pending_id，人批准即执行）。

    出错/告警：
      - 目标 tab 处于禁用态 → 返回 tab_disabled:true + warnings（含 #607 提示）。禁用 tab 不产生
        debug 帧，若你的修正是基于空回读推断出来的，先补证据别硬改。
      - 改砸了：调 autoflow_apply_rollback(trace_id) 还原到 apply 前快照。
    ⚠️ 黑箱身份不可见也不可调（_DEPLOY_KNIVES）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)；"
                    "apply 闭环属原生手写能力，请改用『原生手写身份码』调用本工具。"})
    try:
        correction = json.loads(correction_json or "{}")
    except Exception as e:
        return _js({"ok": False, "error": f"correction_json 不是合法 JSON：{e}"})
    if not isinstance(correction, dict):
        return _js({"ok": False, "error": "correction_json 必须是 JSON 对象（{...}）"})
    return _js(_gw().apply_flow(flow_id=flow_id, correction=correction, mode=mode,
                                agent_id=agent.agent_id, auto_approve=auto_approve,
                                allow_prod=True, trace_id=trace_id or None))

@mcp_admin.tool()
@mcp.tool()
def autoflow_apply_rollback(trace_id: str, auto_approve: bool = False) -> str:
    """【apply 闭环·回滚】把某次 apply（trace_id）改动的 flow 还原到 apply 前的快照。

    - trace_id 来自 autoflow_apply 的返回值（两阶段全程同一个）。
    - 与 apply 对称：还原本身也是改 flow → 默认自动执行，计入同一 (agent, flow) 自愈预算；
      预算耗尽即停止（stage=selfheal_budget_exhausted，fail-safe 防死循环）。
    - 只还原「有回滚点」的 apply：mode=B（写 HA 服务）不改 flow，无回滚点，会明确报错。
    - 空快照拒绝写回（绝不用空 flow 覆盖线上）。
    返回 {ok, restored, pending, flow_id, snapshot_path, decision_id?, error?}。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)；"
                    "apply 回滚属原生手写能力，请改用『原生手写身份码』调用本工具。"})
    return _js(_gw().apply_rollback(trace_id, agent_id=agent.agent_id,
                                    auto_approve=auto_approve, allow_prod=True))

@mcp_admin.tool()
@mcp.tool()
def autoflow_get_trace(trace_id: str) -> str:
    """【apply 闭环·轨迹读取】按 trace_id 读回某次 apply 的完整审计轨迹（data/apply_traces/<trace_id>.json）。

    用于独立核对 apply 闭环证据：两阶段决策闸是否同一 trace_id 复用回滚点、
    pending→approved 是否真写回、ROLLBACK 是否落痕、审计字段（mode/agent_id/reason）是否齐全。
    - trace_id 来自 autoflow_apply / autoflow_apply_rollback 的返回值（两阶段全程同一个）。
    - 返回 {ok, trace_id, trace:{events:[...], flow_id, snapshot_path, ...}}；不存在返回 {ok:False, error}。
    - 只读，不改任何状态；⚠️ 黑箱身份不可见也不可调（_DEPLOY_KNIVES）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)；"
                    "apply 轨迹读取属原生手写/运维能力，请改用『原生手写身份码』调用本工具。"})
    return _js(_gw().get_apply_trace(trace_id))

@mcp_admin.tool()
@mcp.tool()
def autoflow_apply_state_from_debug(flow_id: str = "", node_id: str = "", since: int = 0,
                                    limit: int = 50, entity_id: str = "", state: str = "",
                                    reason: str = "", auto_approve: bool = False,
                                    trace_id: str = "") -> str:
    """【自愈闭环 B 段胶水·入口】把 debug 回读帧映射成「实体+目标状态」并写回 HA。

    这是 inject 触发(autoflow_trigger_inject) → debug 回读(autoflow_debug_read) → 本工具
    （B 段胶水）→ apply_flow(mode="B") 落状态 的最后一环。适用于「观测到某实体该是 on、
    但当前 off，于是把它翻成 HA 写服务调用」这类数据驱动修正式。

    参数：
      - flow_id / node_id / since / limit：定位 debug 回读帧（证据来源）。
      - entity_id / state：可显式传入（agent 已归因明确）；不传则从上一条含 entity_id 的
        帧 payload（state/target_state/value 字段）推断。
      - reason：强烈建议写（原样呈现给人审批）。
    行为：
      - 帧为空 / 无法推断 → 直接报错，绝不基于空观测写回 HA（#607 证据要求）。
      - mode=B 低风险，本层直接放行给 HA 写服务确认闸（返回 pending_id，人批准即执行）。
    ⚠️ 黑箱身份不可见也不可调（_DEPLOY_KNIVES）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)；"
                    "apply 闭环属原生手写能力，请改用『原生手写身份码』调用本工具。"})
    return _js(_gw().apply_state_from_debug(
        flow_id=flow_id, node_id=node_id, since=since, limit=limit,
        entity_id=entity_id, state=state, reason=reason,
        agent_id=agent.agent_id, auto_approve=auto_approve,
        trace_id=trace_id or None))

@mcp_admin.tool()
@mcp.tool()
def autoflow_commit_ha_service(domain: str, service: str, data_json: str = "{}") -> str:
    """提交 HA 服务调用（进确认闸，需 WebUI 批准）。agent_id 由已认证身份自动注入。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    try:
        data = json.loads(data_json or "{}")
    except json.JSONDecodeError:
        return _js({"ok": False, "error": "data_json 非法 JSON"})
    return _js(_gw().commit_ha_service(domain, service, data, agent.agent_id))

@mcp_admin.tool()
@mcp.tool()
def autoflow_set_tab_state(flow_id: str, enabled: bool, reason: str = "") -> str:
    """【写·确认闸】启用/禁用单个 Node-RED tab 流程（进确认闸，需 WebUI 批准后才真正执行）。

    - flow_id：要切状态的 tab 的 NR flow id（用 autoflow_list_tabs 拿）。
    - enabled=True 启用该 tab；enabled=False 禁用（节点仍在，只是不跑）。
    - reason：可选说明，写入待确认项供人类审查。
    行为：
      * 提交即校验 flow_id 存在性（不存在 → unknown=True 直接拒绝，不会落个「幽灵待确认」）。
      * 禁用『核心受保护 tab』（心跳/HA 桥接）会被拦截（防误关全家瘫痪）；启用核心 tab 不受限。
      * 提交后返回 {ok, pending_id, needs_approval:true}；WebUI 批准后才能真正切 NR 的 tab.disabled。
    仅管理员/原生手写面板可见；黑箱身份不可见（_DEPLOY_KNIVES）。
    注意：这是「切开关」不是「删 flow」——要彻底移除已部署的 flow，请在 WebUI 提案面板
    撤回/驳回对应提案（网关侧不暴露 MCP 删除工具，删除仅 WebUI 可操作）。"""
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent：MCP 连接需携带有效身份码。"})
    return _js(_gw().set_tab_state_submit(
        flow_id, enabled, agent.agent_id, reason=reason))

# ───────────── 以下工具【仅管理面 /mcp-admin 暴露，且只认 mode=admin】─────────────
# 网关自重启 + 任务池发布/重置/统计 + 缺陷闭环（golden/acceptance 评测杠杆已迁 archive，见 C4）。
# 普通原生手写身份连 /mcp-admin 会被中间件 403，上述工具对其不可见也不可调。

# （C4）Golden/Acceptance 评测工作台（autoflow_golden_eval / autoflow_golden_status /
# autoflow_acceptance_eval）已从网关剥离，迁移至 archive/agent-loop-migration/。

@mcp_admin.tool()
def autoflow_restart_gateway() -> str:
    """热重启网关进程，使刚改的代码/配置立即生效，无需人工在终端操作。
    实现（已验证可靠）：网关以 SYSTEM 身份运行，可写 nssm 注册表设 AppExit=Restart（幂等、
    持久化）。然后【让本进程自己退出】(os._exit)——nssm 的进程监管看到 app 退出后，
    按 AppExit=Restart 用新代码重拉起服务。
    ⚠️ 关键：绝不能改用 `nssm stop` 自停——那是受控停止，nssm 不触发 AppExit，服务会停在
    Stopped 需人工救。必须让进程"自己死"，nssm 才会自动重启。
    调用后约数秒网关会短暂中断再恢复；返回 {ok,status} 即表示重启已发起。
    注意：仅授权 agent 可调用；这是迭代期便利能力，生产部署应改用进程管理器(systemd/supervisor)。"""
    import os as _os
    import subprocess as _sp
    import sys as _sys
    # 防护：有活跃 golden 回归任务时拒绝自重启，否则会杀掉正在进行的评测（自杀）。
    # 终态集合 {"done","error"} 之外的均视为活跃。
    try:
        from .gateway import _GOLDEN_JOBS
        _active = [v for v in _GOLDEN_JOBS.values()
                   if v.get("status") not in ("done", "error")]
    except Exception:
        _active = []
    if _active:
        return _js({"ok": False,
                    "error": "有活跃 golden 回归任务，拒绝自重启以免中断评测。",
                    "active_jobs": [v.get("job_id") for v in _active]})
    _pkg = _os.path.dirname(_os.path.abspath(__file__))   # .../src/autoflow_gateway
    _gateway_dir = _os.path.dirname(_os.path.dirname(_pkg))  # .../autoflow_gateway/（含 run.py 与 nssm.exe）
    _nssm = _os.path.join(_gateway_dir, "nssm.exe")
    try:
        if _os.path.exists(_nssm):
            # 可靠自重启方案（已验证）：
            # 1) 确保 nssm 在网关进程退出后自动重拉起（AppExit Default Restart；网关以 SYSTEM
            #    运行，可写服务注册表，幂等；注册表持久化，不随进程退出丢失）。
            # 2) ⚠️ 关键：不能用 `nssm stop`（那是受控停止，nssm 不触发 AppExit，服务停在
            #    Stopped 需人工救）。必须让【本进程自己退出】——nssm 监管看到 app 退出 →
            #    按 AppExit 重拉起新代码。故这里只设 AppExit，再派 detached 计时线程等 HTTP
            #    响应发完后 os._exit(1) 自杀；nssm 独立进程负责重启，绝不丢 start 半程。
            try:
                _sp.run([_nssm, "set", "AutoFlowGateway", "AppExit", "Default", "Restart"],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        creationflags=0x00000008, timeout=15)
                _sp.run([_nssm, "set", "AutoFlowGateway", "AppRestartDelay", "3000"],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        creationflags=0x00000008, timeout=15)
            except Exception:
                pass  # 即使设置失败，下面自杀仍会执行（最坏情况需用户手动重启）
            import threading as _th
            import time as _time
            def _self_destruct():
                _time.sleep(1.5)          # 先让 HTTP 响应发完
                _os._exit(1)              # 进程自己退出 → nssm AppExit=Restart 重拉起（新代码）
            _th.Thread(target=_self_destruct, daemon=True).start()
            return _js({"ok": True, "status": "restart_initiated", "method": "self_exit_apprestart",
                        "note": "已设 AppExit=Restart 并将自杀；nssm 监听到进程退出后用新代码重拉起，约数秒后 8000 恢复。"})
        _ps1 = _os.path.join(_pkg, "restart_gateway.ps1")
        if not _os.path.exists(_ps1):
            return _js({"ok": False, "error": f"重启脚本缺失: {_ps1}"})
        _sp.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _ps1,
             "-PythonExe", _sys.executable, "-GatewayDir", _gateway_dir],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            creationflags=0x00000008, close_fds=True,
        )
        return _js({"ok": True, "status": "restart_initiated", "method": "ps1",
                    "note": "旧网关即将被 detached 脚本杀掉并重拉起；约数秒后 8000 端口恢复。"})
    except Exception as e:
        return _js({"ok": False, "error": f"重启发起失败: {e}"})

# ───────────── DSL 验证任务池（管理面：发布/重置/统计）─────────────
@mcp_admin.tool()
def autoflow_publish_tasks(json_path: str = "", scenes_json: str = "") -> str:
    """【任务池·发布 · 管理员】把场景批量灌入 DSL 验证任务池。
    - json_path：场景 JSON 文件（绝对/相对路径）；或
    - scenes_json：内联 JSON 字符串（数组，或含 "prompts" 字段的对象）。
    每条场景形如 {id, tier, task, entities:[entity_id...], subflows:[...], expected:[...]}；
    实体 hint 在发布时即时从 device_catalog.json 富化（补 friendly_name/domain/area/possible_states）。
    返回 {inserted, skipped, errors}。黑箱身份(mode=black)禁止发布。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，只有原生手写/管理员可发布任务。"})
    if json_path:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            return _js({"ok": False, "error": f"读取场景文件失败: {e}"})
    elif scenes_json:
        try:
            data = json.loads(scenes_json)
        except json.JSONDecodeError as e:
            return _js({"ok": False, "error": f"scenes_json 非法 JSON: {e}"})
    else:
        return _js({"ok": False, "error": "需提供 json_path 或 scenes_json 之一。"})
    if isinstance(data, dict):
        scenes = data.get("scenes") or data.get("prompts") or data
    else:
        scenes = data
    if not isinstance(scenes, list):
        return _js({"ok": False, "error": "场景须为数组（或含 prompts 字段的对象）。"})
    return _js(_gw().tasks.publish(scenes))

@mcp_admin.tool()
def autoflow_reset_pool() -> str:
    """【任务池·重置 · 管理员】清空任务池（tasks + task_claims 全删）。谨慎使用，不可恢复。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent。"})
    if agent.mode == "black":
        return _js({"ok": False, "error": "当前身份为『黑箱』(mode=black)，只有原生手写/管理员可重置任务池。"})
    return _js(_gw().tasks.reset())

@mcp_admin.tool()
def autoflow_pool_stats() -> str:
    """【任务池·统计 · 管理员】聚合任务池进度：任务总数、各 agent 提交数、
    按 result_kind 分布（compiled/gate_pass/lint_error/compile_error/no_response）、
    按 tier 通过率。给 DSL 引擎迭代指路（哪层失败多 = 哪层语法/语义需补强）。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent。"})
    return _js(_gw().tasks.stats())

@mcp_admin.tool()
def autoflow_list_submissions(tier: str = "", agent_id: str = "", task_id: str = "",
                              result_kind: str = "", limit: int = 200,
                              include_dsl: bool = False) -> str:
    """【管理面·审阅提交】按条件列出任务池提交(claim)记录，用于回归闭环定位失败项。
    - tier：按任务 tier 过滤（如 auto / auto_wb / cov_* / hist）。
    - agent_id：只看某 agent 的提交（如 agt_af0881494f81）。
    - task_id：只看某任务的跨 agent 提交（对比同一任务不同 agent 的结果）。
    - result_kind：过滤结果类型（compiled|gate_pass|lint_error|compile_error|no_response|…）。
    - limit：返回上限（默认 200）。
    - include_dsl：True 时附带 submitted_dsl 全文（抽查 agent 实际提交用），默认 False 省体积。
    返回 {count, submissions:[{task_id, agent_id, task_tier, status, result_kind, error_msg, node_count, gate_passed, updated_at, ...}]}。"""
    if not is_task_pool_enabled(get_config()):
        return _task_pool_disabled()
    agent = get_current_agent()
    if agent is None:
        return _js({"ok": False, "error": "未识别 agent。"})
    rows = _gw().tasks.list_submissions(
        tier=tier or None, agent_id=agent_id or None, task_id=task_id or None,
        result_kind=result_kind or None, limit=limit, include_dsl=include_dsl)
    return _js({"count": len(rows), "submissions": rows})

@mcp_admin.tool()
def autoflow_list_issues(status: str = "", limit: int = 100) -> str:
    """【管理面·审阅缺陷】列出 agent 经 autoflow_report_issue 上报的 issue。
    - status：可空（默认全量）；取值 open|ack|resolved|wontfix。
    - limit：返回条数上限（默认 100）。
    返回 issue 列表（含 issue_id/agent_id/task_id/severity/category/title/body/status/created_at）。"""
    rows = _gw().tasks.list_issues(status=status or None, limit=limit)
    return _js({"count": len(rows), "issues": rows,
                "next": "用 autoflow_resolve_issue(issue_id, status) 更新状态闭环。"})

@mcp_admin.tool()
def autoflow_resolve_issue(issue_id: str, status: str) -> str:
    """【管理面·闭环缺陷】更新某 issue 状态。
    - issue_id：来自 autoflow_list_issues 的 issue_id（如 iss_xxxxxxxxxx）。
    - status：open|ack|resolved|wontfix。
    返回 {ok, issue_id, status} 或错误。"""
    res = _gw().tasks.resolve_issue(issue_id, status)
    return _js(res)

# ⚠️ 刻意不在 MCP 暴露 approve/reject：批准是控制面动作，仅 WebUI 提供，
# 防止 agent 自己批准自己的写操作（零信任最后一道闸）。

# ───────────── 身份鉴权中间件（原生 ASGI）─────────────
class AgentAuthMiddleware:
    """在 /mcp、/mcp-white、/mcp-admin 三端点请求上强制校验 Bearer 身份码；
    解析不到/失效 → 401 拒绝匿名。认证成功后把 agent 注入 contextvars，供 tool 读取。
    端点级零信任门禁：
      · /mcp-admin 仅管理员身份(mode=admin)可进；
      · /mcp-white 拒编译器身份(mode=black)，原生手写/管理员/双箱可进；
      · /mcp 任意 active 身份均可。"""

    def __init__(self, user_path: str, white_path: str, admin_path: str, store: AgentStore,
                 acp_path: str = "/acp", acp_store: "AcpTokenStore | None" = None):
        self.user_path = user_path
        self.white_path = white_path
        self.admin_path = admin_path
        self.store = store
        self.acp_path = acp_path
        self.acp_store = acp_store

    # ── ACP 端点鉴权（独立 acp_ 令牌体系，与 /mcp 的 af_ 身份码完全隔离）──
    # /acp 用 kind=acp 令牌（AcpTokenStore 落库），鉴权失败返回 HTTP 200 + JSON-RPC
    # error -32000（不是 MCP 的 401），满足工单 A6。任何改动不得影响 /mcp / WebUI / NR。
    def _acp_token_from_scope(self, scope) -> str:
        """从 Authorization: Bearer acp_xxx 或 x-acp-token: acp_xxx 取令牌；无则返回空串。"""
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"authorization")
        if raw:
            try:
                tok = raw.decode().removeprefix("Bearer ").strip()
            except Exception:
                tok = ""
            if tok:
                return tok
        x = headers.get(b"x-acp-token")
        if x:
            try:
                return x.decode().strip()
            except Exception:
                return ""
        return ""

    async def _acp_unauthorized(self, send) -> None:
        """acp_ 令牌缺失/非法/非 acp → HTTP 200 包体 JSON-RPC error code -32000（工单 A6）。"""
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000,
                "message": "unauthorized: /acp 需要 kind=acp 令牌（Authorization: Bearer acp_xxx "
                           "或 x-acp-token: acp_xxx）。acp_ 令牌与 MCP af_ 身份码、WebUI JWT "
                           "三套隔离、互不可混用；非 acp 令牌（如 af_）也不可进 /acp。",
            },
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _acp_disabled(self, send) -> None:
        """ACP 被 WebUI 开关关闭时，/acp 直接拒（JSON-RPC error -32099），不进入鉴权。"""
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32099,
                "message": "acp_disabled: ACP 已由 WebUI 开关关闭（将 /api/acp/enabled 置 true 可重新启用）。",
            },
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _handle_acp(self, scope, receive, send, app) -> None:
        """/acp 鉴权 + 转发到 Starlette 路由的 _ACPApp。OPTIONS 预检放行（补 CORS）。"""
        if scope.get("method", "").upper() == "OPTIONS":
            origin = self._cors_origin(scope)
            await self._cors_preflight(send, origin)
            return
        origin = self._cors_origin(scope)

        async def send_with_cors(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = self._with_cors(msg.get("headers", []), origin)
            await send(msg)

        token = self._acp_token_from_scope(scope)
        if not token:
            await self._acp_unauthorized(send_with_cors)
            return
        rec = self.acp_store.resolve_by_token(token) if self.acp_store else None
        if rec is None:
            await self._acp_unauthorized(send_with_cors)
            return
        if self.acp_store is not None:
            self.acp_store.record_last_seen(rec["token_id"])
        # 认证通过 → 交给 Starlette 路由的 _ACPApp 处理（JSON-RPC 分发/SSE）
        await app(scope, receive, send_with_cors)

    # ── CORS 支持（浏览器端 MCP 客户端必需）────────────────────────────────
    # deepseek++ 等浏览器扩展的 MCP 客户端跨域连 http://<NAS_IP>:8000/mcp 时，
    # 浏览器会先发 OPTIONS 预检；按 CORS 规范预检【不携带 Authorization】，故必须在
    # 中间件层放行预检并回 CORS 头，否则浏览器拦截真实 POST → 表现即「连不上 / 401 /
    # 授权被拒」。真实 POST/GET 仍强制 Bearer 校验，此处仅补 CORS 响应头，不改变鉴权语义。
    _CORS_ALLOW_HEADERS = "authorization, content-type, accept, mcp-session-id, mcp-protocol-version"
    _CORS_EXPOSE_HEADERS = "mcp-session-id"

    def _cors_origin(self, scope):
        """CORS Origin 策略（开源安全收敛）：默认拒绝任意 Origin 反射。

        仅回显两类安全源：① 显式 allowlist（env AF_MCP_CORS_ORIGINS，逗号分隔）；
        ② 同源（请求 Origin 的 host:port == 请求 Host）。
        其余跨域一律返回 None（不反射）。Bearer 鉴权不受影响（POST/GET 仍强制校验）。"""
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin")
        if not origin:
            return None  # 非浏览器 / 同源简单请求无需 CORS 头
        origin = origin.decode()
        # ① 显式 allowlist（跨域浏览器 MCP 客户端需把自身 origin 加入）
        allow = [o.strip() for o in os.environ.get("AF_MCP_CORS_ORIGINS", "").split(",") if o.strip()]
        if origin in allow:
            return origin
        # ② 同源判定：Origin 的 host:port == 请求 Host → 安全反射
        host = headers.get(b"host")
        if host:
            from urllib.parse import urlparse
            op = urlparse(origin)
            if op.netloc and op.netloc == host.decode():
                return origin
        # ③ 跨域且不在 allowlist → 拒绝（返回 None，不反射任意 Origin）
        return None

    async def _cors_preflight(self, send, origin):
        headers = [
            (b"content-type", b"text/plain"),
            (b"content-length", b"0"),
        ]
        if origin:
            # 仅对允许源返回 CORS 预检头；跨域未授权则省略 ACAO → 浏览器拦截真实请求
            headers += [
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                (b"access-control-allow-headers", self._CORS_ALLOW_HEADERS.encode()),
                (b"access-control-expose-headers", self._CORS_EXPOSE_HEADERS.encode()),
                (b"access-control-max-age", b"86400"),
            ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    def _with_cors(self, headers, origin):
        out = list(headers)
        keys = {k.lower() for k, _ in out}
        if origin:
            # 仅对允许源补 CORS 响应头；origin=None（跨域未授权）不发 ACAO / credentials
            if b"access-control-allow-origin" not in keys:
                out.append((b"access-control-allow-origin", origin.encode()))
            if b"access-control-allow-credentials" not in keys:
                out.append((b"access-control-allow-credentials", b"true"))
        if b"access-control-expose-headers" not in keys:
            out.append((b"access-control-expose-headers", self._CORS_EXPOSE_HEADERS.encode()))
        return out

    def wrap(self, app):
        async def middleware(scope, receive, send):
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return
            path = scope.get("path", "")
            # ── ACP 端点（独立 acp_ 令牌体系，与 /mcp 的 af_ 身份码完全隔离）──
            if path == self.acp_path or path.startswith(self.acp_path + "/"):
                if not is_acp_enabled(get_config()):
                    await self._acp_disabled(send)
                    return
                await self._handle_acp(scope, receive, send, app)
                return
            # 判定请求归属哪个端点（按前缀；长路径优先，避免 /mcp 与 /mcp-white /mcp-admin 混淆）
            ep = None
            if path == self.admin_path or path.startswith(self.admin_path + "/"):
                ep = "admin"
            elif path == self.white_path or path.startswith(self.white_path + "/"):
                ep = "white"
            elif path == self.user_path or path.startswith(self.user_path + "/"):
                ep = "user"
            if ep is None:
                await app(scope, receive, send)
                return

            # 1) CORS 预检：无条件放行，回 CORS 头（不要求鉴权）
            if scope.get("method", "").upper() == "OPTIONS":
                origin = self._cors_origin(scope)
                await self._cors_preflight(send, origin)
                return

            # 2) 给所有 MCP 响应补 CORS 头（含 401/403，便于浏览器正确暴露错误）
            origin = self._cors_origin(scope)

            async def send_with_cors(msg):
                if msg["type"] == "http.response.start":
                    msg["headers"] = self._with_cors(msg.get("headers", []), origin)
                await send(msg)

            headers = dict(scope.get("headers", []))
            raw = headers.get(b"authorization")
            agent = None
            if raw:
                try:
                    tok = raw.decode().removeprefix("Bearer ").strip()
                except Exception:
                    tok = ""
                if tok:
                    agent = self.store.resolve_by_code(tok)
            if agent is None:
                await self._unauthorized(send_with_cors)
                return
            # 端点级零信任门禁：
            #  · /mcp-admin 仅管理员身份(mode=admin)可进；普通原生手写/编译器均 403。
            if ep == "admin" and getattr(agent, "mode", None) != "admin":
                await self._forbidden(send_with_cors,
                    "管理面 /mcp-admin 仅限管理员身份(mode=admin)；"
                    "原生手写部署刀请连 /mcp-white，用户工具请连 /mcp。")
                return
            #  · /mcp-white 拒编译器身份；原生手写/管理员/双箱可进。
            if ep == "white" and getattr(agent, "mode", None) == "black":
                await self._forbidden(send_with_cors,
                    "编译器身份(mode=black)禁止访问原生手写面 /mcp-white；"
                    "请改用原生手写身份码，或连 /mcp 走 DSL 路径。")
                return
            #  · /mcp 任意 active 身份均可（编译器/原生手写/管理员/双箱/both）。
            self.store.record_last_seen(agent.agent_id)
            t = get_current_agent_var().set(agent)
            try:
                await app(scope, receive, send_with_cors)
            finally:
                get_current_agent_var().reset(t)
        return middleware

    async def _unauthorized(self, send):
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32001,
                "message": "unauthorized: 缺少或无效的身份识别码，请在 Authorization: Bearer <身份码> 携带（在 WebUI 的 Agents 面板生成）。",
            },
            "id": None,
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _forbidden(self, send, msg: str):
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32003, "message": msg},
            "id": None,
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

# ───────────── ACP（Agent Client Protocol）服务端（拓扑 X peer-to-peer）─────────────
# 与 memory-worker 对称：/acp 暴露 JSON-RPC 2.0 over HTTP+SSE。autoflow 网关无 LLM，
# 采用确定性意图分发器（关键词→只读网关工具 + 反向 delegate）；未来若引入 LLM，仅需替换
# _acp_agent_run 的实现，外层 SSE/会话/分发逻辑不变。会话存储为单进程内存（规格 §9，重启丢上下文可接受）。
_ACP_SESSIONS: "dict[str, _ACPSession]" = {}
_ACP_SESSIONS_LOCK = threading.Lock()
_ACP_VERSION = "1.0.0"

# ACP /acp 首版工具面（工单范围默认保守只读；写/变更类不进默认 ACP 工具面）。
_ACP_TOOLS = [
    {"name": "list_entities", "description": "列出全屋 Home Assistant 实体目录（按域/区域/关键词过滤），只读。",
     "input_schema": {"type": "object",
                       "properties": {"domain": {"type": "string"}, "area": {"type": "string"},
                                      "keyword": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "get_entity_state", "description": "查询单个实体的实时状态（直连 HA，只读）。",
     "input_schema": {"type": "object", "properties": {"entity_id": {"type": "string"}}}},
    {"name": "list_automations", "description": "列出本网关已建/待审的自动化（flow 注册表），只读。",
     "input_schema": {"type": "object",
                       "properties": {"keyword": {"type": "string"}, "only": {"type": "string"}}}},
    {"name": "delegate_to_memory_worker",
     "description": "反向委派给 memory-worker（取家庭记忆/知识检索），跨容器 HTTP。",
     "input_schema": {"type": "object",
                       "properties": {"task": {"type": "string"}, "context": {"type": "object"}}}},
]


def _acp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acp_new_session_id() -> str:
    return "acp_s_" + uuid.uuid4().hex


class _ACPSession:
    """单条 ACP 会话（内存态）：累积 content 块 + 取消事件。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.status = "running"
        self.history: list = []          # 累积 content 块（tool_call / text）
        self.cancel_event = None         # asyncio.Event，由 cancel 方法 set
        self.created_at = _acp_now()

    def is_cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())


def _acp_get_or_create_session(session_id: str) -> "_ACPSession":
    with _ACP_SESSIONS_LOCK:
        s = _ACP_SESSIONS.get(session_id)
        if s is None:
            s = _ACPSession(session_id)
            _ACP_SESSIONS[session_id] = s
        return s


def _acp_list_sessions() -> list:
    with _ACP_SESSIONS_LOCK:
        return [{"sessionId": s.session_id, "status": s.status,
                 "created_at": s.created_at, "block_count": len(s.history)}
                for s in _ACP_SESSIONS.values()]


def _acp_delete_session(session_id: str) -> bool:
    with _ACP_SESSIONS_LOCK:
        return _ACP_SESSIONS.pop(session_id, None) is not None


def _acp_extract_entity_id(text: str):
    """从自然语言里抽取 domain.object 形如的实体 id（不区分大小写）。"""
    m = re.search(r"\b([a-z_]+)\.([a-z0-9_]+)\b", text)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _acp_agent_run(messages, context, session: "_ACPSession") -> None:
    """确定性意图分发（无 LLM）。把 content 块增量 append 到 session.history；
    每个步骤前检查 session.cancel_event，取消则提前返回（保留部分结果）。

    只暴露只读意图 + 反向 delegate（不发起任何写操作，确认闸对 ACP 同样生效）。"""
    ctx = context or {}
    # 取最后一条 user 文本
    last_text = ""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                last_text = c
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        last_text = blk.get("text", "")
            if last_text:
                break
    text = (last_text or "").strip()
    low = text.lower()

    def _emit(tool_name: str, arguments: dict, result):
        session.history.append({"type": "tool_call", "name": tool_name,
                                "arguments": arguments, "result": result})
        session.history.append({
            "type": "text",
            "text": (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))[:4000],
        })

    if session.is_cancelled():
        return
    # （测试钩子）模拟长任务以便验证 cancel：仅当 context._acp_slow_seconds 存在
    slow = ctx.get("_acp_slow_seconds")
    if isinstance(slow, (int, float)) and slow > 0:
        import time as _t
        steps = max(1, int(slow * 10))
        for _ in range(steps):
            if session.is_cancelled():
                return
            _t.sleep(0.1)
    if not text and not ctx:
        session.history.append({"type": "text",
            "text": "未收到有效指令：prompt 需包含 role=user 的文本或 context。"})
        return

    # 1) 反向委派 memory-worker（意图优先：记忆/知识/检索/委派）
    if any(k in low for k in ("memory", "记忆", "知识库", "检索", "delegate", "委派", "recall")):
        if not is_acp_enabled(get_config()):
            _emit("delegate_to_memory_worker", {"task": text},
                  "ACP 已关闭（WebUI「ACP 令牌」页开关）；将 /api/acp/enabled 置 true 可重新启用。")
            return
        res = acp_client.delegate_to_memory_worker(text, context=ctx)
        if not res.get("ok"):
            _emit("delegate_to_memory_worker", {"task": text}, res.get("error", "委派失败"))
            return
        _emit("delegate_to_memory_worker", {"task": text}, res.get("text") or "（memory-worker 无文本返回）")
        return

    # 2) 实体状态查询
    if ("状态" in text) or ("state" in low) or ctx.get("entity_id"):
        eid = ctx.get("entity_id") or _acp_extract_entity_id(text)
        if eid:
            _emit("get_entity_state", {"entity_id": eid}, autoflow_get_entity_state(eid))
            return
        # 没解析到 entity_id：兜底给实体目录，避免空响应
        _emit("list_entities", {"limit": 20, "keyword": text}, autoflow_list_entities(limit=20))

    # 3) 自动化 / 子流程查询
    elif ("自动化" in text) or ("子流程" in text) or ("automation" in low) or ("flow" in low):
        _emit("list_automations", {"keyword": ctx.get("keyword", ""), "only": ctx.get("only", "all")},
              autoflow_list_automations(keyword=ctx.get("keyword", ""), only=ctx.get("only", "all")))

    # 4) 默认：列出全屋实体目录（或关键词过滤）
    else:
        kw = ctx.get("keyword", "") or text
        _emit("list_entities",
              {"domain": ctx.get("domain", ""), "area": ctx.get("area", ""), "keyword": kw,
               "limit": ctx.get("limit", 20)},
              autoflow_list_entities(domain=ctx.get("domain", ""), area=ctx.get("area", ""),
                                     keyword=kw, limit=ctx.get("limit", 20)))


async def _acp_read_body(receive) -> bytes:
    parts = []
    while True:
        evt = await receive()
        if evt.get("type") == "http.request":
            parts.append(evt.get("body") or b"")
            if not evt.get("more_body", False):
                break
        elif evt.get("type") == "http.disconnect":
            break
    return b"".join(parts)


async def _acp_json_response(send, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _acp_json_error(send, req_id, code: int, message: str) -> None:
    await _acp_json_response(send, {"jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": code, "message": message}})


def _acp_result_initialize(req: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req.get("id"),
            "result": {
                "agent": {"name": "AutoFlow Gateway", "version": _ACP_VERSION,
                          "role": "home-automation-gateway"},
                "capabilities": {"streaming": True, "sessions": True, "tools": True},
                "tools": _ACP_TOOLS,
            }}


def _acp_handle_cancel(req: dict) -> dict:
    sid = (req.get("params") or {}).get("sessionId")
    with _ACP_SESSIONS_LOCK:
        s = _ACP_SESSIONS.get(sid)
    if s is None:
        return {"jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": -32001, "message": f"session not found: {sid}"}}
    if s.cancel_event:
        s.cancel_event.set()  # 置位；正在跑的 prompt 循环检测到后下发 aborted
    return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"cancelled": True, "sessionId": sid}}


def _acp_handle_session(method: str, req: dict) -> dict:
    req_id = req.get("id")
    sub = method.split(".", 1)[1] if "." in method else ""
    params = req.get("params") or {}
    if sub == "new":
        sid = _acp_new_session_id()
        _acp_get_or_create_session(sid)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": sid}}
    if sub == "list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"sessions": _acp_list_sessions()}}
    if sub == "history":
        sid = params.get("sessionId")
        s = _ACP_SESSIONS.get(sid)
        if s is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32001, "message": f"session not found: {sid}"}}
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"sessionId": sid, "status": s.status, "content": s.history}}
    if sub == "delete":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"deleted": _acp_delete_session(params.get("sessionId")),
                           "sessionId": params.get("sessionId")}}
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown session method: {method}"}}


async def _acp_write_frame(send, session_id: str, status: str, content: list, more: bool = False) -> None:
    notif = {"jsonrpc": "2.0", "method": "session_update",
             "params": {"sessionId": session_id, "status": status, "content": content}}
    body = f"event: message\ndata: {json.dumps(notif, ensure_ascii=False)}\n\n".encode("utf-8")
    await send({"type": "http.response.body", "body": body, "more_body": more})


class _ACPApp:
    """Starlette Route 端点：GET 返回服务说明（非 JSON-RPC、探活）；
    POST 解析 JSON-RPC 2.0 并分发 initialize/prompt(SSE)/cancel/session.*。"""

    def __init__(self, acp_store: "AcpTokenStore", cfg):
        self.acp_store = acp_store
        self.cfg = cfg

    async def __call__(self, scope, receive, send):
        method = scope.get("method", "GET").upper()
        if method == "GET":
            await self._service_desc(send)
            return
        if method != "POST":
            await _acp_json_error(send, None, -32600, f"unsupported method {method}")
            return
        body = await _acp_read_body(receive)
        try:
            req = json.loads(body)
        except Exception:
            await _acp_json_error(send, None, -32700, "parse error")
            return
        if not isinstance(req, dict) or "method" not in req:
            await _acp_json_error(send, req.get("id") if isinstance(req, dict) else None,
                                  -32600, "invalid request")
            return
        m = req.get("method")
        if m == "initialize":
            await _acp_json_response(send, _acp_result_initialize(req))
        elif m == "prompt":
            await self._handle_prompt(req, send)
        elif m == "cancel":
            await _acp_json_response(send, _acp_handle_cancel(req))
        elif isinstance(m, str) and m.startswith("session."):
            await _acp_json_response(send, _acp_handle_session(m, req))
        else:
            await _acp_json_error(send, req.get("id"), -32601, f"method not found: {m}")

    async def _service_desc(self, send) -> None:
        desc = {
            "name": "AutoFlow Gateway ACP Endpoint",
            "protocol": "Agent Client Protocol (JSON-RPC 2.0 over HTTP+SSE)",
            "version": _ACP_VERSION,
            "auth": "kind=acp Bearer token (Authorization: Bearer acp_xxx 或 x-acp-token: acp_xxx)",
            "methods": ["initialize", "prompt", "cancel",
                        "session.new", "session.list", "session.history", "session.delete"],
            "note": "POST JSON-RPC 到本端点；GET 仅用于探活。",
        }
        body = json.dumps(desc, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _handle_prompt(self, req: dict, send) -> None:
        params = req.get("params")
        if params is None:
            params = {}
        # 规格 §6：参数结构不合法 → -32602（先于开流校验，避免半截 SSE）
        if not isinstance(params, dict):
            await _acp_json_error(send, req.get("id"), -32602, "params must be an object")
            return
        msgs = params.get("messages")
        if msgs is not None and not isinstance(msgs, list):
            await _acp_json_error(send, req.get("id"), -32602, "params.messages must be an array")
            return
        sid_in = params.get("sessionId")
        if sid_in is not None and not isinstance(sid_in, str):
            await _acp_json_error(send, req.get("id"), -32602, "params.sessionId must be a string")
            return
        session_id = sid_in or _acp_new_session_id()
        session = _acp_get_or_create_session(session_id)
        session.cancel_event = asyncio.Event()
        session.status = "running"
        # 开启 SSE 流（先发 start，再发帧；完成帧 more_body=False）
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache"),
                                (b"connection", b"keep-alive")]})
        await _acp_write_frame(send, session_id, "running", [])
        try:
            # 在 worker 线程跑确定性分发器，使事件循环保持空闲以处理并发的 cancel 请求
            await asyncio.to_thread(_acp_agent_run, params.get("messages", []),
                                    params.get("context"), session)
        except Exception as e:  # 分发器异常 → 下发 error 帧（仍结束流，不发额外 result）
            session.status = "error"
            await _acp_write_frame(send, session_id, "error",
                                   [{"type": "text", "text": f"ACP 处理异常: {e}"}], more=False)
            return
        if session.is_cancelled():
            session.status = "aborted"
            await _acp_write_frame(send, session_id, "aborted", session.history, more=False)
        else:
            session.status = "completed"
            # 流末 completed 即结束（不再单独发 JSON-RPC result，满足工单 A3）
            await _acp_write_frame(send, session_id, "completed", session.history, more=False)


def get_current_agent_var():
    # 延迟取 identity.current_agent，避免循环导入风险
    from .identity import current_agent
    return current_agent

def _filter_tools_list(body: bytes) -> bytes:
    """若 body 是 tools/list 的 JSON-RPC 响应，剥除 _DEPLOY_KNIVES（black 身份不可见）。
    非 tools/list / 解析失败 / 无变化 → 原样返回（不改字节）。"""
    if not body:
        return body
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not (isinstance(data, dict) and isinstance(data.get("result"), dict)):
        return body
    tools = data["result"].get("tools")
    if not isinstance(tools, list):
        return body
    filtered = [t for t in tools
                if (t.get("name") if isinstance(t, dict) else t) not in _DEPLOY_KNIVES]
    if len(filtered) == len(tools):
        return body
    data["result"]["tools"] = filtered
    return json.dumps(data).encode("utf-8")

def _peek_tools_call(events):
    """从 ASGI 请求事件里提取 tools/call 的工具名（params.name，兼容 params.tool）。
    非 tools/call / 解析失败 / 空体 → (None, None)。"""
    parts = []
    for evt in events:
        if evt.get("type") == "http.request":
            parts.append(evt.get("body") or b"")
    if not parts:
        return (None, None)
    try:
        req = json.loads(b"".join(parts))
    except Exception:
        return (None, None)
    if isinstance(req, dict) and req.get("method") == "tools/call":
        params = req.get("params") or {}
        return (params.get("name") or params.get("tool"), req.get("id"))
    return (None, None)

def _blackbox_should_block(events):
    """black 身份调用级身份闸判定：若 tools/call 的工具属于 _DEPLOY_KNIVES 返回该工具名，否则 None。"""
    tool, _ = _peek_tools_call(events)
    if tool is not None and tool in _DEPLOY_KNIVES:
        return tool
    return None

async def _send_jsonrpc_error(send, req_id, code, message):
    """直接回一个 JSON-RPC error 响应（调用级身份闸命中时用，不转发到 session handler）。"""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload, "more_body": False})

# ───────────── 组合应用（MCP + WebUI + 鉴权）─────────────
def build_app(cfg=None, with_webui: bool = True, gateway: Gateway = None):
    """返回一个可交给 uvicorn 的 ASGI app：MCP 挂 /mcp（用户面，按 mode 分层显隐工具）、
    /mcp-white（/mcp 的兼容别名，原生手写旧端点）、/mcp-admin（管理面，仅 admin）三端点，
    WebUI 挂 / 与 /api，并对三个 MCP path 强制身份鉴权
    （/mcp-admin 仅 admin；/mcp-white 拒编译器；/mcp 任意 active 身份；black 在 /mcp 仅见用户工具）。"""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    cfg = cfg or _gw().cfg
    sm_user = StreamableHTTPSessionManager(app=mcp._mcp_server, json_response=True)
    # /mcp-white 是 /mcp 的兼容别名：复用同一 mcp 服务器（工具全集），
    # 原生手写身份旧端点继续有效；mode 分层显隐由 _MCPApp 的 tools/list 过滤实现。
    sm_white = StreamableHTTPSessionManager(app=mcp._mcp_server, json_response=True)
    sm_admin = StreamableHTTPSessionManager(app=mcp_admin._mcp_server, json_response=True)
    store = AgentStore(cfg)
    acp_store = AcpTokenStore(cfg)
    webui = build_webui_asgi(cfg, gateway=gateway) if with_webui else None

    # 关键：用 Route 而非 Mount。Mount 会剥离 path 前缀导致 session manager 收到空 path → 404；
    # Route 保留完整 path，与 FastMCP 官方挂载方式一致。
    # _MCPApp 对 user/white 端点包裹 tools/list 过滤：black 身份看不到部署/自检刀。
    class _MCPApp:
        def __init__(self, sm, filter_tools: bool = False):
            self.sm = sm
            self.filter_tools = filter_tools

        async def __call__(self, scope, receive, send):
            if not self.filter_tools:
                await self.sm.handle_request(scope, receive, send)
                return
            # 仅对 black 身份过滤 tools/list 响应，剥除部署/自检刀（其余响应原样透传）
            agent = get_current_agent()
            if agent is None or getattr(agent, "mode", None) != "black":
                await self.sm.handle_request(scope, receive, send)
                return

            # GET（SSE 持久连接等）无调用体，直接转发（send_filter 对 GET 响应本就是透传）
            if scope.get("method") != "POST":
                await self.sm.handle_request(scope, receive, send)
                return

            # POST：先缓冲请求体做调用级身份闸；命中 _DEPLOY_KNIVES 直接拒，绝不转发到 handler
            req_events = []
            while True:
                evt = await receive()
                req_events.append(evt)
                if evt.get("type") == "http.request" and not evt.get("more_body", False):
                    break
                if evt.get("type") == "http.disconnect":
                    break
            blocked_tool = _blackbox_should_block(req_events)
            if blocked_tool is not None:
                _, caller_id = _peek_tools_call(req_events)
                await _send_jsonrpc_error(
                    send, caller_id, -32601,
                    f"tool '{blocked_tool}' is not available to black-box agents "
                    f"(deployment/self-test knives are hidden by design)")
                return

            # 放行：原样重放请求 + tools/list 响应过滤（沿用既有 send_filter）
            replay_idx = 0

            async def replay_receive():
                nonlocal replay_idx
                e = req_events[replay_idx]
                replay_idx += 1
                return e

            buffered = []
            start_msg = {}

            async def send_filter(msg):
                if msg["type"] == "http.response.start":
                    start_msg["msg"] = msg
                elif msg["type"] == "http.response.body":
                    buffered.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        body = b"".join(buffered)
                        new_body = _filter_tools_list(body)
                        headers = [(k, v) for k, v in start_msg["msg"].get("headers", [])
                                   if k.lower() != b"content-length"]
                        headers.append((b"content-length", str(len(new_body)).encode()))
                        await send({"type": "http.response.start",
                                    "status": start_msg["msg"]["status"], "headers": headers})
                        await send({"type": "http.response.body",
                                    "body": new_body, "more_body": False})
                else:
                    await send(msg)

            await self.sm.handle_request(scope, replay_receive, send_filter)

    @asynccontextmanager
    async def lifespan(app):
        async with sm_user.run(), sm_white.run(), sm_admin.run():
            yield

    routes = [
        Route(cfg.mcp_path, endpoint=_MCPApp(sm_user, filter_tools=True)),
        Route(cfg.mcp_white_path, endpoint=_MCPApp(sm_white, filter_tools=True)),
        Route(cfg.mcp_admin_path, endpoint=_MCPApp(sm_admin)),
        # ACP 端点：独立 acp_ 令牌体系（与 /mcp 的 af_ 身份码隔离），由中间件单独鉴权
        Route(cfg.acp_path, endpoint=_ACPApp(acp_store, cfg)),
    ]
    if webui is not None:
        routes.append(Mount("/", app=webui))
    starlette_app = Starlette(lifespan=lifespan, routes=routes)

    auth = AgentAuthMiddleware(cfg.mcp_path, cfg.mcp_white_path, cfg.mcp_admin_path, store,
                               acp_path=cfg.acp_path, acp_store=acp_store)
    return auth.wrap(starlette_app)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="streamable-http",
                    choices=["streamable-http", "sse"])
    ap.add_argument("--webui", dest="webui", action="store_true", default=True,
                    help="同时启动 WebUI（默认开）")
    ap.add_argument("--no-webui", dest="webui", action="store_false")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = _gw().cfg
    if args.transport == "sse":
        # SSE 模式暂只跑用户面 MCP（WebUI 仍可用，但 /mcp 走 SSE）
        mcp.run(transport="sse")
        return

    import uvicorn
    app = build_app(cfg, with_webui=args.webui)
    host = args.host or cfg.mcp_host
    port = args.port or cfg.mcp_port
    print(f"[AutoFlow] serving MCP(user/white={cfg.mcp_path} (white 经 {cfg.mcp_white_path} 兼容别名), "
          f"admin={cfg.mcp_admin_path})"
          f"{' + WebUI(/)' if args.webui else ''} at http://{host}:{port}")
    print(f"[AutoFlow] 拒绝匿名 MCP 连接；agent 需在 WebUI 生成身份码后配置。")
    print(f"[AutoFlow] 单用户端点 {cfg.mcp_path}：工具按 agent.mode 分层显隐"
          f"（black=用户工具；white/dual/admin=用户工具+部署刀）；管理员连 {cfg.mcp_admin_path}（全量+运维刀）。")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
