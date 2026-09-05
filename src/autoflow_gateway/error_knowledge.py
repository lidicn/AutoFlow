#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误知识库模块 —— 自动记录 propose-dsl 编译失败案例，按错误类型分类。

设计原则：
  · 自动记录：propose-dsl 失败时自动入库，无需人工干预
  · 按类型分类：unknown_entity / syntax_error / lint_error / gate_failed 等
  · 可搜索：按错误类型、DSL 关键词、时间范围搜索
  · 统计：各类型错误数量、常见错误模式

存储：data/<env>/error_knowledge.json
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


# 错误类型分类规则
ERROR_PATTERNS = [
    ("unknown_entity", r"unknown.entity|R_unknown_entity|entity.*not.*found"),
    ("syntax_error", r"syntax|parse|invalid.*dsl|expected"),
    ("lint_error", r"lint|warning|style"),
    ("gate_failed", r"gate|blocked|forbidden|security"),
    ("e2e_failed", r"e2e|verify|assert|postcondition"),
    ("deploy_failed", r"deploy|node.?red|nr.*error"),
    ("empty_dsl", r"empty|dsl.*required"),
]


def classify_error(error_msg: str, stage: str = "") -> str:
    """根据错误信息和阶段分类错误类型。"""
    text = (error_msg + " " + stage).lower()
    for error_type, pattern in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return error_type
    return "other"


class ErrorKnowledgeStore:
    """错误知识库存储管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.store_file = os.path.join(data_dir, "error_knowledge.json")
        os.makedirs(data_dir, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        if os.path.isfile(self.store_file):
            try:
                with open(self.store_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"errors": [], "stats": {}}
        return {"errors": [], "stats": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.store_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.store_file)

    def record(self, dsl: str, error_msg: str, stage: str = "",
               agent_id: str = "", proposal_id: str = "") -> Dict[str, Any]:
        """记录一条错误案例。"""
        error_type = classify_error(error_msg, stage)
        entry = {
            "id": "err_" + os.urandom(6).hex(),
            "timestamp": _utcnow_iso(),
            "dsl": dsl[:500],  # 截断，避免存储过大
            "dsl_length": len(dsl),
            "error": error_msg[:500],
            "error_type": error_type,
            "stage": stage,
            "agent_id": agent_id,
            "proposal_id": proposal_id,
        }
        data = self._load()
        data["errors"].append(entry)

        # 更新统计
        stats = data.get("stats", {})
        stats[error_type] = stats.get(error_type, 0) + 1
        stats["_total"] = stats.get("_total", 0) + 1
        data["stats"] = stats

        # 只保留最近 500 条
        if len(data["errors"]) > 500:
            data["errors"] = data["errors"][-500:]

        self._save(data)
        return {"ok": True, "error_type": error_type, "id": entry["id"]}

    def list_errors(self, error_type: Optional[str] = None,
                    keyword: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """列出错误案例，支持过滤。"""
        data = self._load()
        errors = data.get("errors", [])

        if error_type:
            errors = [e for e in errors if e.get("error_type") == error_type]
        if agent_id:
            errors = [e for e in errors if e.get("agent_id") == agent_id]
        if keyword:
            kw = keyword.lower()
            errors = [e for e in errors
                      if kw in (e.get("dsl", "").lower()
                                or kw in (e.get("error", "").lower()))]

        # 按时间倒序
        errors = sorted(errors, key=lambda x: x.get("timestamp", ""), reverse=True)
        total = len(errors)
        errors = errors[offset:offset + limit]

        return {
            "ok": True,
            "errors": errors,
            "total": total,
            "stats": data.get("stats", {}),
        }

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取错误统计。"""
        data = self._load()
        all_errors = data.get("errors", [])

        # 按天统计
        daily = {}
        cutoff = _utcnow() - timedelta(days=days)
        for e in all_errors:
            try:
                ts = datetime.fromisoformat(e.get("timestamp", ""))
                if ts < cutoff:
                    continue
                day = ts.strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {"total": 0, "by_type": {}}
                daily[day]["total"] += 1
                et = e.get("error_type", "other")
                daily[day]["by_type"][et] = daily[day]["by_type"].get(et, 0) + 1
            except Exception:
                continue

        return {
            "ok": True,
            "period": f"最近 {days} 天",
            "total_errors": data.get("stats", {}).get("_total", 0),
            "by_type": data.get("stats", {}),
            "daily": daily,
        }

    def get_suggestion(self, error_msg: str, stage: str = "") -> Optional[Dict[str, Any]]:
        """根据错误信息推荐相似案例和修复建议。"""
        error_type = classify_error(error_msg, stage)
        data = self._load()
        similar = [e for e in data.get("errors", [])
                   if e.get("error_type") == error_type][-5:]  # 最近5条

        suggestions = {
            "unknown_entity": "检查 entity_id 是否正确，先用 resolve-entity 获取真实 entity_id",
            "syntax_error": "检查 DSL 语法，参考 SKILL.md 中的 DSL 语法速查表",
            "lint_error": "DSL 有 lint 警告，检查节点连接和参数格式",
            "gate_failed": "安全闸门拦截，检查是否操作了未授权的实体或 tab",
            "e2e_failed": "端到端验证失败，检查 expected_postconditions 是否正确",
            "deploy_failed": "部署到 NR 失败，检查 NR 是否在线、flow 格式是否正确",
            "empty_dsl": "DSL 不能为空",
            "other": "未知错误，查看完整错误信息排查",
        }

        return {
            "ok": True,
            "error_type": error_type,
            "suggestion": suggestions.get(error_type, suggestions["other"]),
            "similar_cases": similar,
            "total_same_type": data.get("stats", {}).get(error_type, 0),
        }
