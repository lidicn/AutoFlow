#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 防御层（防 agent 抽风）

核心原则：
1. 结构上根本不提供「清空/整体替换(replace-all)」「删除全部」原语——网关只暴露
   细粒度、单 flow 操作。上次"agent 全删 flow"事故正是整体替换类操作作恶，这里从
   接口层杜绝。
2. 爆炸半径：单次写操作波及的 flow 数有上限（默认 1）。
3. 受保护流：核心流（label 含 core/system 或 id 前缀 core_）agent 永不能改/删。
4. 所有权（ownership）：flow 记 owner_agent，agent 只能改自己认领的；改别人的需更高权限。
5. 域分级：高危 domain（锁/水阀/加热）升级确认级别。
"""
from typing import List, Set, Optional
from .config import get_config


class DefenseError(RuntimeError):
    """防御层拒绝，原因即来自 agent 的越权/越界，归类为 agent_plan_error。"""
    pass


class DefenseLayer:
    def __init__(self, config=None):
        self.cfg = config or get_config()

    # ── 受保护判定 ──
    def is_protected_flow(self, flow_id: str, label: str = "") -> bool:
        low = (label or "").lower()
        if any(p.lower() in low for p in self.cfg.protected_flow_labels):
            return True
        for pre in self.cfg.protected_flow_id_prefixes:
            if flow_id.startswith(pre):
                return True
        return False

    # ── 域分级 → 风险等级 ──
    def classify_domain_risk(self, domain: str) -> str:
        if domain in self.cfg.elevated_domains:
            return "high"
        if domain in self.cfg.safe_domains:
            return "low"
        return "medium"  # 未知域默认中风险，需确认

    def classify_operation_risk(self, operation: str, domains: List[str]) -> str:
        """综合操作类型与涉及 domain 定风险级。"""
        levels = {"low": 1, "medium": 2, "high": 3}
        worst = 1
        if operation in ("delete_flow",):
            worst = max(worst, 2)
        for d in domains:
            worst = max(worst, levels[self.classify_domain_risk(d)])
        # 取最高
        for name, val in levels.items():
            if val == worst:
                return name
        return "medium"

    # ── 写前检查：受保护 + 所有权 + 爆炸半径 ──
    def check_write(self, *, operation: str, flow_id: str = "", label: str = "",
                    owner_agent: Optional[str] = None, acting_agent: str = "unknown",
                    flows_touched: int = 1) -> None:
        # 1) 受保护流永不可动
        if (flow_id or label) and self.is_protected_flow(flow_id, label):
            raise DefenseError(
                f"拒绝：flow '{label or flow_id}' 在受保护集合内，agent 不可修改/删除。"
            )
        # 2) 所有权：改他人 flow 需更高权限（此处直接拒绝，留待人工 elevated 通道）
        if owner_agent and owner_agent not in (acting_agent, None, "", "system"):
            raise DefenseError(
                f"拒绝：flow 归属 agent '{owner_agent}'，acting_agent '{acting_agent}' 无权修改他人 flow。"
            )
        # 3) 爆炸半径：单次不可波及超过上限的 flow 数
        if flows_touched > self.cfg.blast_radius_max_flows:
            raise DefenseError(
                f"拒绝：本次操作波及 {flows_touched} 个 flow，超过爆炸半径上限 "
                f"{self.cfg.blast_radius_max_flows}。请拆分为单 flow 操作。"
            )

    # ── 结构性原则：网关绝不暴露 replace-all ──
    @staticmethod
    def forbid_whole_replace():
        """占位：提醒调用方——网关任何写路径都不得调用 deploy_all / replace_all。
        此函数无副作用，仅作文档式守卫入口被代码评审引用。"""
        return True
