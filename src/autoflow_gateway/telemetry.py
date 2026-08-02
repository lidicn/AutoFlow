"""AutoFlow 遥测标签（P6 §10）。

给每次网关动作打**结构化失败标签**，是归因与经验沉淀的数据基础。
PLAN §10 明确："没有它既分不清网关 vs agent，也沉淀不出有用经验。"

四个标签：
- ``gateway_error``    — 接口缺失/bug（网关的问题）
- ``agent_plan_error`` — 规划错/映射错/JSON 非法（agent 的问题）
- ``safety_block``     — 被白名单/防御层拦（安全策略拒绝）
- ``sim_mismatch``     — staging 断言不通过（验证失败）
- ``ok``               — 成功（无失败）

设计原则：
- **零侵入**：gateway 方法返回 dict，telemetry 徽附加在 ``_telemetry`` 字段；
  不改变已有返回结构。
- **自动分类**：``tag_action()`` 根据 stage/error 内容自动推断标签。
- **持久化**：append-only JSONL 日志 ``data/telemetry.jsonl``。
- **查询**：``recent()`` / ``summary()`` 供 WebUI / MCP 工具消费。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── 标签定义 ──
TAG_GATEWAY_ERROR = "gateway_error"
TAG_AGENT_PLAN_ERROR = "agent_plan_error"
TAG_SAFETY_BLOCK = "safety_block"
TAG_SIM_MISMATCH = "sim_mismatch"
TAG_OK = "ok"

ALL_TAGS = {TAG_GATEWAY_ERROR, TAG_AGENT_PLAN_ERROR, TAG_SAFETY_BLOCK, TAG_SIM_MISMATCH, TAG_OK}

# stage → 标签推断表（用于自动分类）
_STAGE_TAG_MAP = {
    "compile": TAG_AGENT_PLAN_ERROR,       # DSL 解析/编译失败 = agent 产出有问题
    "parse": TAG_AGENT_PLAN_ERROR,
    "validate": TAG_AGENT_PLAN_ERROR,
    "entity_check": TAG_AGENT_PLAN_ERROR,   # 实体不存在 = agent 引用了错误实体
    "gate": TAG_SIM_MISMATCH,              # 闸门断言失败 = 验证不通过
    "defense": TAG_SAFETY_BLOCK,           # 被防御层拦截
    "confirm": TAG_SAFETY_BLOCK,           # 被确认闸拦截
    "deploy": TAG_GATEWAY_ERROR,           # 部署失败 = 网关/NR 接口问题
    "undeploy": TAG_GATEWAY_ERROR,
    "schema": TAG_AGENT_PLAN_ERROR,        # schema 校验失败
    "conflict": TAG_SAFETY_BLOCK,          # 冲突检测拒绝
}


def _infer_tag(result: Dict[str, Any]) -> str:
    """根据网关方法返回的 dict 自动推断失败标签。"""
    if result.get("ok", True):
        return TAG_OK
    stage = (result.get("stage") or "").lower()
    error = (result.get("error") or "").lower()
    # safety / defense 关键词
    if any(kw in error for kw in ("defense", "protected", "whitelist", "not_ours", "conflict")):
        return TAG_SAFETY_BLOCK
    # sim / gate 关键词
    if any(kw in error for kw in ("gate_passed", "assertion", "mismatch", "expected")):
        return TAG_SIM_MISMATCH
    # stage 表
    if stage in _STAGE_TAG_MAP:
        return _STAGE_TAG_MAP[stage]
    # 兜底：未知失败归 gateway_error（宁可多归网关，不冤枉 agent）
    return TAG_GATEWAY_ERROR


def tag_action(
    action: str,
    result: Dict[str, Any],
    agent_id: str = "unknown",
    extra: Optional[Dict[str, Any]] = None,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    """给一次网关动作打标签并返回 telemetry 字段。

    - ``action``：方法名（如 ``propose_dsl`` / ``deploy_proposal`` / ``deploy_raw``）。
    - ``result``：网关方法的返回 dict。
    - ``agent_id``：执行 agent 的身份 id。
    - ``extra``：附加上下文（如 ``proposal_id`` / ``flow_id``）。
    - ``log_path``：telemetry 日志路径；None 则不持久化。

    返回 ``{"tag": ..., "action": ..., "agent_id": ..., "ts": ...}``，
    并将此条目 append 到日志（若 log_path 提供）。
    """
    tag = _infer_tag(result)
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts,
        "tag": tag,
        "action": action,
        "agent_id": agent_id,
        "ok": result.get("ok", True),
        "stage": result.get("stage"),
        "error": result.get("error", "") if not result.get("ok", True) else "",
        "extra": extra or {},
    }
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 遥测失败不影响主流程
    return entry


def recent(log_path: str, n: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取最近 n 条遥测记录，可按标签过滤。"""
    if not os.path.exists(log_path):
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag and entry.get("tag") != tag:
            continue
        entries.append(entry)
        if len(entries) >= n:
            break
    return entries


def summary(log_path: str) -> Dict[str, Any]:
    """汇总遥测统计：各标签计数、最近失败、成功率。"""
    if not os.path.exists(log_path):
        return {"total": 0, "by_tag": {}, "success_rate": 0.0}
    counts: Dict[str, int] = {}
    total = 0
    last_failures: List[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                tag = entry.get("tag", TAG_GATEWAY_ERROR)
                counts[tag] = counts.get(tag, 0) + 1
                if tag != TAG_OK and len(last_failures) < 10:
                    last_failures.append(entry)
    except Exception:
        pass
    ok_count = counts.get(TAG_OK, 0)
    rate = (ok_count / total * 100) if total else 0.0
    return {
        "total": total,
        "by_tag": counts,
        "success_rate": round(rate, 1),
        "recent_failures": last_failures,
    }
