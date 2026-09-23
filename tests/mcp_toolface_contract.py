# -*- coding: utf-8 -*-
"""MCP 工具面契约 —— 工具面可见性的**唯一登记处**（single source of truth）。

## 为什么独立成模块
工具面契约此前散落在多个测试里各写一份（刀集合、运维刀集合、外加 `45/47/27` 这类
硬编码计数）。改一次工具面要手动同步多处，漏一处就红一片 —— 2026-09-22 加
`autoflow_list_apply_traces` 时就漏了 3 处（工具数 3 个 + stage 白名单 + stub 签名）。

把「哪些工具存在、对谁可见」收敛到本模块后，各守卫只做一件事：
**把本模块的设计意图与运行时注册表对账**，并用 symdiff 报错（直接告诉你多了/少了谁）。
计数一律由集合派生（`len()`），**不得再出现魔数**。

## 改工具面的正确姿势
1. 在 `src/autoflow_gateway/mcp_server.py` 增删工具（含 `_DEPLOY_KNIVES` 登记）；
2. 在本模块对应集合里登记 —— 这一步是**强制**的：不登记则对账断言红，
   且报错会列出差异工具名，而不是一个没有信息量的「45 != 46」；
3. 跑 `pytest tests/test_mcp_server_merge.py tests/test_toolface_minimal.py -q`。

## 三层工具面（与 ARCHITECTURE.md 一致）
  `/mcp` + `/mcp-white` → `mcp`（用户服务器）   = USER_VISIBLE ∪ DEPLOY_KNIVES
  `/mcp-admin`          → `mcp_admin`（管理面） = ADMIN_USER_VISIBLE ∪ DEPLOY_KNIVES ∪ OPS_KNIVES
  black（普通身份）经 tools/list 过滤后可见     = USER_VISIBLE（刀已剥除）
"""

# ── 1. 部署/自检刀：挂用户服务器，但对 black（普通身份）隐藏 ──────────────
# 这批工具能真实改动 NR 实例或跳过审批，属「专家档 / 原生手写身份」能力。
DEPLOY_KNIVES = {
    "autoflow_apply",
    "autoflow_apply_rollback",
    "autoflow_apply_state_from_debug",   # #692 胶水
    "autoflow_commit_ha_service",
    "autoflow_create_subflow",
    "autoflow_deploy_raw",
    "autoflow_get_trace",                # #701 apply 轨迹读取
    "autoflow_list_apply_traces",        # #20 审计轨迹索引（与 get_trace 同治理层）
    "autoflow_modify_flow",
    "autoflow_restore_snapshot",         # #16 实例还原
    "autoflow_run_e2e_trace",
    "autoflow_set_tab_state",
    "autoflow_simulate_flow",
    "autoflow_snapshot_instance",        # #16 实例快照
    "autoflow_surgical_edit",            # #7 手术刀编辑
    "autoflow_trigger_inject",           # #19 决策1：手动扳机（设备真实动作、跳过审批）→ 专家档
    "autoflow_validate_flow",
    "autoflow_verify_flow",
}

# ── 2. 运维刀：**只挂管理服务器**，用户服务器上根本不存在 ──────────────────
# 接管/重启网关、重置/发布任务池、池统计、提交与缺陷审计闭环。
OPS_KNIVES = {
    "autoflow_list_issues",
    "autoflow_list_submissions",
    "autoflow_pool_stats",
    "autoflow_publish_tasks",
    "autoflow_reset_pool",
    "autoflow_resolve_issue",
    "autoflow_restart_gateway",
}

# ── 3. 用户服务器上对 black 可见的最小可用集 ──────────────────────────────
USER_VISIBLE = {
    "autoflow_ask_llm",
    "autoflow_claim_task",
    "autoflow_debug_read",
    "autoflow_delegate_to_memory_worker",
    "autoflow_dsl_help",
    "autoflow_get_decision",
    "autoflow_get_entity_state",
    "autoflow_get_flow",
    "autoflow_get_inventory",
    "autoflow_get_nr_flow",
    "autoflow_get_skill",
    "autoflow_list_automations",
    "autoflow_list_decisions",
    "autoflow_list_entities",
    "autoflow_list_pending",
    "autoflow_list_tabs",
    "autoflow_list_tasks",
    "autoflow_list_templates",
    "autoflow_propose_dsl",
    "autoflow_refresh_catalog",
    "autoflow_render_template",
    "autoflow_report_issue",
    "autoflow_request_decision",
    "autoflow_resolve_entity",
    "autoflow_set_plan",
    "autoflow_submit_result",
    "autoflow_whoami",
}

# ── 4. 管理面额外的用户工具（管理面**不挂**用户面独有的诊断工具）──────────
# 用户面独有（见 USER_FACE_ONLY）的是诊断/委派类：让运维身份少几条对外读取通道。
ADMIN_USER_VISIBLE = {
    "autoflow_claim_task",
    "autoflow_dsl_help",
    "autoflow_get_decision",
    "autoflow_get_entity_state",
    "autoflow_get_flow",
    "autoflow_get_skill",
    "autoflow_list_automations",
    "autoflow_list_decisions",
    "autoflow_list_entities",
    "autoflow_list_pending",
    "autoflow_list_tabs",
    "autoflow_list_tasks",
    "autoflow_list_templates",
    "autoflow_propose_dsl",
    "autoflow_refresh_catalog",
    "autoflow_render_template",
    "autoflow_report_issue",
    "autoflow_request_decision",
    "autoflow_resolve_entity",
    "autoflow_set_plan",
    "autoflow_submit_result",
    "autoflow_whoami",
}

# ── 派生量（一律由集合算出，禁止硬编码计数）──────────────────────────────
ALL_KNOWN_TOOLS = USER_VISIBLE | ADMIN_USER_VISIBLE | DEPLOY_KNIVES | OPS_KNIVES

# 仅用户面、不进管理面的工具（诊断 + 委派）
USER_FACE_ONLY = USER_VISIBLE - ADMIN_USER_VISIBLE

EXPECTED_MCP_TOOLS = USER_VISIBLE | DEPLOY_KNIVES                    # /mcp
EXPECTED_MCP_ADMIN_TOOLS = (
    ADMIN_USER_VISIBLE | DEPLOY_KNIVES | OPS_KNIVES)                 # /mcp-admin
EXPECTED_BLACK_VISIBLE = USER_VISIBLE                                # black 过滤后


# ── 契约自洽性（不依赖运行时，纯集合代数）────────────────────────────────
def contract_self_check():
    """返回设计意图自身的矛盾列表（空 = 自洽）。"""
    problems = []
    if USER_VISIBLE & DEPLOY_KNIVES:
        problems.append(f"USER_VISIBLE ∩ DEPLOY_KNIVES 非空: "
                        f"{sorted(USER_VISIBLE & DEPLOY_KNIVES)}")
    if ADMIN_USER_VISIBLE & DEPLOY_KNIVES:
        problems.append(f"ADMIN_USER_VISIBLE ∩ DEPLOY_KNIVES 非空: "
                        f"{sorted(ADMIN_USER_VISIBLE & DEPLOY_KNIVES)}")
    if ADMIN_USER_VISIBLE & OPS_KNIVES:
        problems.append(f"ADMIN_USER_VISIBLE ∩ OPS_KNIVES 非空: "
                        f"{sorted(ADMIN_USER_VISIBLE & OPS_KNIVES)}")
    if DEPLOY_KNIVES & OPS_KNIVES:
        problems.append(f"DEPLOY_KNIVES ∩ OPS_KNIVES 非空: "
                        f"{sorted(DEPLOY_KNIVES & OPS_KNIVES)}")
    if OPS_KNIVES & EXPECTED_MCP_TOOLS:
        problems.append(f"运维刀泄漏进用户服务器: {sorted(OPS_KNIVES & EXPECTED_MCP_TOOLS)}")
    if not ADMIN_USER_VISIBLE <= USER_VISIBLE:
        problems.append(f"管理面用户工具不在用户面集合内: "
                        f"{sorted(ADMIN_USER_VISIBLE - USER_VISIBLE)}")
    return problems


def symdiff_msg(label, expected, actual):
    """给对账断言用的可读差异串（直指多了谁 / 少了谁）。"""
    exp, act = set(expected), set(actual)
    extra = sorted(act - exp)     # 运行时有、意图里没有 → 新增未登记
    missing = sorted(exp - act)   # 意图里有、运行时没有 → 删除/改名未同步
    if not extra and not missing:
        return f"{label}: 一致（{len(exp)} 项）"
    lines = [f"{label} 与运行时不一致（意图 {len(exp)} 项 / 实际 {len(act)} 项）："]
    if extra:
        lines.append(f"  · 新增未登记（请在 mcp_toolface_contract.py 补登）: {extra}")
    if missing:
        lines.append(f"  · 已消失/改名（请从 mcp_toolface_contract.py 移除）: {missing}")
    return "\n".join(lines)
