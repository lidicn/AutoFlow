#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验数据收集模块 —— 操作日志、实体关联、DSL 模式挖掘。

设计原则：
  · 自动收集：propose-dsl/deploy-raw 等操作自动记录，无需人工干预
  · 结构化存储：JSON 文件，按天滚动，便于分析
  · 关联挖掘：实体共现、触发-动作模式、成功/失败模式
  · 可导出：提供 API 供下游分析和经验库使用

存储：data/<env>/experience/
  - logs/YYYY-MM-DD.jsonl  操作日志（按天）
  - entity_cooccur.json    实体共现统计
  - dsl_patterns.json      DSL 模式统计
"""
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _today_str() -> str:
    return _utcnow().strftime("%Y-%m-%d")


class ExperienceLogger:
    """经验数据收集器。"""

    def __init__(self, data_dir: str):
        self.base_dir = os.path.join(data_dir, "experience")
        self.logs_dir = os.path.join(self.base_dir, "logs")
        self.entity_file = os.path.join(self.base_dir, "entity_cooccur.json")
        self.pattern_file = os.path.join(self.base_dir, "dsl_patterns.json")
        os.makedirs(self.logs_dir, exist_ok=True)

    def _log_file(self, date_str: str = None) -> str:
        date_str = date_str or _today_str()
        return os.path.join(self.logs_dir, f"{date_str}.jsonl")

    def log_operation(self, operation: str, agent_id: str,
                      input_data: Dict[str, Any], output_data: Dict[str, Any],
                      success: bool, duration_ms: int = 0,
                      metadata: Dict[str, Any] = None) -> None:
        """记录一次操作。"""
        entry = {
            "timestamp": _utcnow_iso(),
            "operation": operation,
            "agent_id": agent_id,
            "success": success,
            "duration_ms": duration_ms,
            "input": self._truncate(input_data, 2000),
            "output": self._truncate(output_data, 2000),
            "metadata": metadata or {},
        }
        log_file = self._log_file()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 如果是成功的 propose-dsl，更新实体关联和 DSL 模式
        if operation == "propose-dsl" and success:
            dsl = input_data.get("dsl", "")
            if dsl:
                self._update_entity_cooccur(dsl)
                self._update_dsl_patterns(dsl)

    def _truncate(self, data: Any, max_len: int) -> Any:
        """截断过大的数据。"""
        if isinstance(data, str):
            return data[:max_len]
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if isinstance(v, str) and len(v) > max_len:
                    result[k] = v[:max_len] + "...(truncated)"
                elif isinstance(v, (dict, list)):
                    result[k] = self._truncate(v, max_len)
                else:
                    result[k] = v
            return result
        if isinstance(data, list):
            return [self._truncate(item, max_len) for item in data[:100]]
        return data

    def _extract_entities(self, dsl: str) -> List[str]:
        """从 DSL 中提取 entity_id。"""
        # 匹配常见的 entity_id 格式：domain.entity
        pattern = r'\b([a-z_]+\.[a-z0-9_]+)\b'
        entities = re.findall(pattern, dsl)
        # 过滤掉非实体的匹配（如 light.turn_on 是服务不是实体）
        service_suffixes = [".turn_on", ".turn_off", ".toggle", ".set_temperature",
                           ".set_brightness", ".set_color", ".select_option",
                           ".play_media", ".media_play", ".media_pause"]
        real_entities = []
        for e in entities:
            if not any(e.endswith(s) for s in service_suffixes):
                real_entities.append(e)
        return list(set(real_entities))

    def _extract_trigger_action(self, dsl: str) -> List[Dict[str, str]]:
        """从 DSL 中提取触发-动作对。"""
        patterns = []
        lines = dsl.split("\n")
        current_trigger = None
        for line in lines:
            line = line.strip()
            if line.startswith("trigger:") or line.startswith("trigger :"):
                current_trigger = line.split(":", 1)[1].strip()
            elif (line.startswith("action:") or line.startswith("action :")) and current_trigger:
                action = line.split(":", 1)[1].strip()
                patterns.append({"trigger": current_trigger, "action": action})
        return patterns

    def _update_entity_cooccur(self, dsl: str) -> None:
        """更新实体共现统计。"""
        entities = self._extract_entities(dsl)
        if len(entities) < 2:
            return

        data = self._load_json(self.entity_file, {"cooccur": {}, "entity_count": {}})

        # 更新单个实体计数
        for e in entities:
            data["entity_count"][e] = data["entity_count"].get(e, 0) + 1

        # 更新共现对
        for i in range(len(entities)):
            for j in range(i + 1, len(entities)):
                pair = tuple(sorted([entities[i], entities[j]]))
                key = f"{pair[0]}|{pair[1]}"
                data["cooccur"][key] = data["cooccur"].get(key, 0) + 1

        self._save_json(self.entity_file, data)

    def _update_dsl_patterns(self, dsl: str) -> None:
        """更新 DSL 模式统计。"""
        patterns = self._extract_trigger_action(dsl)
        if not patterns:
            return

        data = self._load_json(self.pattern_file, {"patterns": {}, "trigger_count": {}, "action_count": {}})

        for p in patterns:
            trigger = p["trigger"]
            action = p["action"]
            key = f"{trigger}|{action}"
            data["patterns"][key] = data["patterns"].get(key, 0) + 1
            data["trigger_count"][trigger] = data["trigger_count"].get(trigger, 0) + 1
            data["action_count"][action] = data["action_count"].get(action, 0) + 1

        self._save_json(self.pattern_file, data)

    def _load_json(self, path: str, default: Any) -> Any:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default
        return default

    def _save_json(self, path: str, data: Any) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def get_logs(self, days: int = 7, operation: str = None,
                 agent_id: str = None, limit: int = 100) -> Dict[str, Any]:
        """获取操作日志。"""
        logs = []
        cutoff = _utcnow() - timedelta(days=days)

        for i in range(days + 1):
            date_str = (cutoff + timedelta(days=i)).strftime("%Y-%m-%d")
            log_file = self._log_file(date_str)
            if not os.path.exists(log_file):
                continue
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            if operation and entry.get("operation") != operation:
                                continue
                            if agent_id and entry.get("agent_id") != agent_id:
                                continue
                            logs.append(entry)
                        except Exception:
                            continue
            except Exception:
                continue

        # 按时间倒序
        logs = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)
        total = len(logs)
        logs = logs[:limit]

        return {"ok": True, "logs": logs, "total": total, "period": f"最近 {days} 天"}

    def get_entity_cooccur(self, entity: str = None, top_n: int = 20) -> Dict[str, Any]:
        """获取实体共现统计。"""
        data = self._load_json(self.entity_file, {"cooccur": {}, "entity_count": {}})

        if entity:
            # 查找与指定实体共现的其他实体
            related = []
            for key, count in data["cooccur"].items():
                parts = key.split("|")
                if entity in parts:
                    other = parts[0] if parts[1] == entity else parts[1]
                    related.append({"entity": other, "cooccur_count": count})
            related = sorted(related, key=lambda x: x["cooccur_count"], reverse=True)[:top_n]
            return {
                "ok": True,
                "entity": entity,
                "entity_count": data["entity_count"].get(entity, 0),
                "related_entities": related,
            }

        # 返回最常见的共现对
        top_pairs = sorted(data["cooccur"].items(), key=lambda x: x[1], reverse=True)[:top_n]
        top_entities = sorted(data["entity_count"].items(), key=lambda x: x[1], reverse=True)[:top_n]

        return {
            "ok": True,
            "top_entity_pairs": [{"pair": k.split("|"), "count": v} for k, v in top_pairs],
            "top_entities": [{"entity": k, "count": v} for k, v in top_entities],
            "total_entities": len(data["entity_count"]),
            "total_pairs": len(data["cooccur"]),
        }

    def get_dsl_patterns(self, top_n: int = 20) -> Dict[str, Any]:
        """获取 DSL 模式统计。"""
        data = self._load_json(self.pattern_file, {"patterns": {}, "trigger_count": {}, "action_count": {}})

        top_patterns = sorted(data["patterns"].items(), key=lambda x: x[1], reverse=True)[:top_n]
        top_triggers = sorted(data["trigger_count"].items(), key=lambda x: x[1], reverse=True)[:top_n]
        top_actions = sorted(data["action_count"].items(), key=lambda x: x[1], reverse=True)[:top_n]

        return {
            "ok": True,
            "top_patterns": [{"trigger": k.split("|")[0], "action": k.split("|")[1], "count": v}
                            for k, v in top_patterns],
            "top_triggers": [{"trigger": k, "count": v} for k, v in top_triggers],
            "top_actions": [{"action": k, "count": v} for k, v in top_actions],
            "total_patterns": len(data["patterns"]),
        }

    def get_best_practices(self, top_n: int = 10) -> Dict[str, Any]:
        """从经验数据中提取最佳实践。"""
        pattern_data = self._load_json(self.pattern_file, {"patterns": {}})
        entity_data = self._load_json(self.entity_file, {"entity_count": {}})

        # 热门触发-动作模式
        top_patterns = sorted(pattern_data.get("patterns", {}).items(),
                             key=lambda x: x[1], reverse=True)[:top_n]

        practices = []
        for key, count in top_patterns:
            parts = key.split("|", 1)
            trigger = parts[0] if len(parts) > 0 else ""
            action = parts[1] if len(parts) > 1 else ""
            practices.append({
                "trigger": trigger,
                "action": action,
                "usage_count": count,
                "confidence": min(95, 50 + count * 5),  # 使用越多置信度越高
                "dsl_example": f"trigger: {trigger}\n  action: {action}",
            })

        # 热门实体
        top_entities = sorted(entity_data.get("entity_count", {}).items(),
                             key=lambda x: x[1], reverse=True)[:top_n]

        return {
            "ok": True,
            "best_practices": practices,
            "hot_entities": [{"entity": k, "usage_count": v} for k, v in top_entities],
            "total_patterns": len(pattern_data.get("patterns", {})),
        }

    def get_agent_comparison(self, days: int = 7) -> Dict[str, Any]:
        """对比不同 Agent 的行为表现。"""
        logs_result = self.get_logs(days=days, limit=10000)
        logs = logs_result["logs"]

        agent_stats = defaultdict(lambda: {
            "total": 0, "success": 0, "fail": 0,
            "total_duration": 0, "operations": Counter(),
        })

        for log in logs:
            agent = log.get("agent_id", "unknown")
            stats = agent_stats[agent]
            stats["total"] += 1
            if log.get("success"):
                stats["success"] += 1
            else:
                stats["fail"] += 1
            stats["total_duration"] += log.get("duration_ms", 0)
            stats["operations"][log.get("operation", "unknown")] += 1

        comparison = []
        for agent, stats in agent_stats.items():
            total = stats["total"]
            comparison.append({
                "agent_id": agent,
                "total_operations": total,
                "success_count": stats["success"],
                "fail_count": stats["fail"],
                "success_rate": round(stats["success"] / total * 100, 1) if total else 0,
                "avg_duration_ms": stats["total_duration"] // total if total else 0,
                "operations": dict(stats["operations"].most_common(5)),
            })

        # 按成功率排序
        comparison = sorted(comparison, key=lambda x: x["success_rate"], reverse=True)

        return {
            "ok": True,
            "period": f"最近 {days} 天",
            "agents": comparison,
            "total_agents": len(comparison),
        }

    def recommend_templates(self, keyword: str = "", top_n: int = 5) -> Dict[str, Any]:
        """根据关键词推荐模板（基于经验数据关联）。"""
        # 加载模板库
        templates_file = os.path.join(os.path.dirname(self.base_dir), "templates.json")
        templates = []
        if os.path.exists(templates_file):
            try:
                with open(templates_file, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                    templates = tdata.get("templates", []) if isinstance(tdata, dict) else tdata
            except Exception:
                pass

        # 基于经验数据的热门模式推荐
        pattern_data = self._load_json(self.pattern_file, {"patterns": {}})
        hot_patterns = sorted(pattern_data.get("patterns", {}).items(),
                             key=lambda x: x[1], reverse=True)[:10]

        recommendations = []
        keyword_lower = keyword.lower() if keyword else ""

        # 匹配模板
        for tpl in templates:
            score = 0
            name = tpl.get("name", "").lower()
            desc = tpl.get("description", "").lower()
            if keyword_lower and (keyword_lower in name or keyword_lower in desc):
                score += 100
            recommendations.append({
                "template_id": tpl.get("id", ""),
                "name": tpl.get("name", ""),
                "description": tpl.get("description", ""),
                "score": score,
                "source": "template_library",
            })

        # 从热门模式生成推荐
        for key, count in hot_patterns:
            parts = key.split("|", 1)
            trigger = parts[0] if len(parts) > 0 else ""
            action = parts[1] if len(parts) > 1 else ""
            score = count * 10
            if keyword_lower and (keyword_lower in trigger.lower() or keyword_lower in action.lower()):
                score += 50
            recommendations.append({
                "template_id": f"pattern_{key}",
                "name": f"{trigger} → {action}",
                "description": f"热门模式：{trigger} 触发 {action}（已使用 {count} 次）",
                "score": score,
                "source": "experience_pattern",
                "dsl_example": f"trigger: {trigger}\n  action: {action}",
            })

        # 按分数排序
        recommendations = sorted(recommendations, key=lambda x: x["score"], reverse=True)[:top_n]

        return {
            "ok": True,
            "keyword": keyword,
            "recommendations": recommendations,
        }

    def find_similar_cases(self, dsl: str, top_n: int = 3) -> Dict[str, Any]:
        """查找相似的成功案例。"""
        # 从操作日志中找成功的 propose-dsl 案例
        logs_result = self.get_logs(days=30, operation="propose-dsl", limit=500)
        success_logs = [l for l in logs_result["logs"] if l.get("success")]

        if not success_logs or not dsl:
            return {"ok": True, "similar_cases": [], "total": 0}

        # 简单相似度：提取 DSL 中的关键词，计算重叠度
        dsl_entities = set(self._extract_entities(dsl))
        dsl_keywords = set(re.findall(r'[a-zA-Z一-龥]+', dsl.lower()))

        scored = []
        for log in success_logs:
            log_dsl = log.get("input", {}).get("dsl", "")
            if not log_dsl or log_dsl == dsl:
                continue
            log_entities = set(self._extract_entities(log_dsl))
            log_keywords = set(re.findall(r'[a-zA-Z一-龥]+', log_dsl.lower()))

            # 计算相似度：实体重叠 + 关键词重叠
            entity_overlap = len(dsl_entities & log_entities) / max(len(dsl_entities | log_entities), 1)
            keyword_overlap = len(dsl_keywords & log_keywords) / max(len(dsl_keywords | log_keywords), 1)
            similarity = entity_overlap * 0.6 + keyword_overlap * 0.4

            if similarity > 0.1:  # 最低相似度阈值
                scored.append({
                    "dsl": log_dsl[:300],
                    "similarity": round(similarity * 100, 1),
                    "agent_id": log.get("agent_id", ""),
                    "timestamp": log.get("timestamp", ""),
                    "proposal_id": log.get("output", {}).get("proposal_id", ""),
                })

        scored = sorted(scored, key=lambda x: x["similarity"], reverse=True)[:top_n]
        return {"ok": True, "similar_cases": scored, "total": len(scored)}

    def suggest_fix(self, error_msg: str, stage: str = "", dsl: str = "") -> Dict[str, Any]:
        """根据错误信息提供修复建议。"""
        # 从错误知识库中查找相似错误
        error_file = os.path.join(os.path.dirname(self.base_dir), "error_knowledge.json")
        similar_errors = []
        if os.path.exists(error_file):
            try:
                with open(error_file, "r", encoding="utf-8") as f:
                    ek_data = json.load(f)
                errors = ek_data.get("errors", [])
                error_keywords = set(re.findall(r'[a-zA-Z一-龥]+', error_msg.lower()))
                for e in errors[-100:]:  # 最近100条
                    e_msg = e.get("error", "")
                    e_keywords = set(re.findall(r'[a-zA-Z一-龥]+', e_msg.lower()))
                    overlap = len(error_keywords & e_keywords) / max(len(error_keywords | e_keywords), 1)
                    if overlap > 0.2:
                        similar_errors.append({
                            "error": e_msg[:200],
                            "dsl": e.get("dsl", "")[:200],
                            "error_type": e.get("error_type", ""),
                            "similarity": round(overlap * 100, 1),
                        })
            except Exception:
                pass

        # 基于错误类型的通用修复建议
        from .error_knowledge import classify_error
        error_type = classify_error(error_msg, stage)
        fix_suggestions = {
            "unknown_entity": [
                "使用 /api/core/resolve-entity 接口，用自然语言查询正确的 entity_id",
                "检查 DSL 中的实体名称是否拼写正确",
                "使用 /api/core/entities 接口查看可用实体列表",
            ],
            "syntax_error": [
                "检查 DSL 语法：trigger: 和 action: 关键字是否正确",
                "确保每行一个 action，使用两个空格缩进",
                "参考 SKILL.md 中的 DSL 语法速查表",
            ],
            "lint_error": [
                "检查节点连接是否完整（每个 trigger 至少有一个 action）",
                "检查 action 参数格式是否正确",
                "查看完整的 lint 警告信息，逐条修复",
            ],
            "gate_failed": [
                "检查 API Key 的 authorized_tabs 是否包含目标 tab",
                "确认操作的实体在授权范围内",
                "查看 gate 字段中的具体拦截原因",
            ],
            "e2e_failed": [
                "检查 expected_postconditions 是否正确描述了期望状态",
                "确认 HA 中实体状态变化需要时间，适当增加等待",
                "检查实体是否真实存在且可操作",
            ],
            "deploy_failed": [
                "检查 Node-RED 是否在线且可访问",
                "检查 flow JSON 格式是否正确",
                "查看 NR 日志获取详细错误信息",
            ],
            "other": [
                "查看完整错误信息，定位具体失败阶段",
                "检查网关日志获取更多上下文",
                "尝试简化 DSL，逐步定位问题",
            ],
        }

        return {
            "ok": True,
            "error_type": error_type,
            "suggestions": fix_suggestions.get(error_type, fix_suggestions["other"]),
            "similar_errors": similar_errors[:3],
            "note": "修复建议基于历史错误模式，仅供参考",
        }

    def recommend_entities(self, keyword: str = "", domain: str = "", top_n: int = 10) -> Dict[str, Any]:
        """根据关键词推荐实体（基于经验数据的使用频率）。"""
        entity_data = self._load_json(self.entity_file, {"entity_count": {}})
        entity_count = entity_data.get("entity_count", {})

        # 按使用频率排序
        entities = sorted(entity_count.items(), key=lambda x: x[1], reverse=True)

        # 过滤
        if keyword:
            kw = keyword.lower()
            entities = [(e, c) for e, c in entities if kw in e.lower()]
        if domain:
            entities = [(e, c) for e, c in entities if e.startswith(domain + ".")]

        entities = entities[:top_n]
        return {
            "ok": True,
            "keyword": keyword,
            "domain": domain,
            "entities": [{"entity_id": e, "usage_count": c} for e, c in entities],
            "total": len(entities),
        }


    def get_summary(self, days: int = 7) -> Dict[str, Any]:
        """获取经验数据汇总。"""
        logs_result = self.get_logs(days=days, limit=10000)
        logs = logs_result["logs"]

        by_operation = Counter()
        by_agent = Counter()
        success_count = 0
        total_duration = 0

        for log in logs:
            by_operation[log.get("operation", "unknown")] += 1
            by_agent[log.get("agent_id", "unknown")] += 1
            if log.get("success"):
                success_count += 1
            total_duration += log.get("duration_ms", 0)

        entity_data = self._load_json(self.entity_file, {"cooccur": {}, "entity_count": {}})
        pattern_data = self._load_json(self.pattern_file, {"patterns": {}})

        return {
            "ok": True,
            "period": f"最近 {days} 天",
            "total_operations": len(logs),
            "success_rate": round(success_count / len(logs) * 100, 1) if logs else 0,
            "avg_duration_ms": total_duration // len(logs) if logs else 0,
            "by_operation": dict(by_operation.most_common(10)),
            "by_agent": dict(by_agent.most_common(10)),
            "entity_count": len(entity_data.get("entity_count", {})),
            "entity_pairs": len(entity_data.get("cooccur", {})),
            "dsl_patterns": len(pattern_data.get("patterns", {})),
        }
