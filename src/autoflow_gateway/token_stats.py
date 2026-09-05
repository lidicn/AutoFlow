#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 统计模块 —— 记录和统计 API 调用的 Token 消耗。

估算方式：
  输入 token ≈ 输入字符数 / 4
  输出 token ≈ 输出字符数 / 4
  固定开销 ≈ SKILL.md 大小（已知，~3K token，由调用方传入）

存储：data/<env>/token_stats.json（按天聚合）
"""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _utcnow().strftime("%Y-%m-%d")


class TokenStatsStore:
    """Token 消耗统计存储。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.stats_file = os.path.join(data_dir, "token_stats.json")
        os.makedirs(data_dir, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        if os.path.isfile(self.stats_file):
            try:
                with open(self.stats_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"daily": {}, "total": {"calls": 0, "input_chars": 0, "output_chars": 0}}
        return {"daily": {}, "total": {"calls": 0, "input_chars": 0, "output_chars": 0}}

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.stats_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.stats_file)

    def record(self, endpoint: str, agent_id: str,
               input_chars: int, output_chars: int,
               mode: str = "dsl", success: bool = True) -> None:
        """记录一次 API 调用的 Token 消耗。"""
        data = self._load()
        today = _today_str()

        # 按天聚合
        if today not in data["daily"]:
            data["daily"][today] = {
                "calls": 0, "input_chars": 0, "output_chars": 0,
                "by_agent": {}, "by_endpoint": {}, "by_mode": {},
            }
        day = data["daily"][today]
        day["calls"] += 1
        day["input_chars"] += input_chars
        day["output_chars"] += output_chars

        # 按 Agent 统计
        if agent_id not in day["by_agent"]:
            day["by_agent"][agent_id] = {"calls": 0, "input_chars": 0, "output_chars": 0}
        day["by_agent"][agent_id]["calls"] += 1
        day["by_agent"][agent_id]["input_chars"] += input_chars
        day["by_agent"][agent_id]["output_chars"] += output_chars

        # 按端点统计
        if endpoint not in day["by_endpoint"]:
            day["by_endpoint"][endpoint] = {"calls": 0, "input_chars": 0, "output_chars": 0}
        day["by_endpoint"][endpoint]["calls"] += 1
        day["by_endpoint"][endpoint]["input_chars"] += input_chars
        day["by_endpoint"][endpoint]["output_chars"] += output_chars

        # 按模式统计（dsl vs raw）
        if mode not in day["by_mode"]:
            day["by_mode"][mode] = {"calls": 0, "input_chars": 0, "output_chars": 0}
        day["by_mode"][mode]["calls"] += 1
        day["by_mode"][mode]["input_chars"] += input_chars
        day["by_mode"][mode]["output_chars"] += output_chars

        # 总计
        data["total"]["calls"] += 1
        data["total"]["input_chars"] += input_chars
        data["total"]["output_chars"] += output_chars

        # 只保留最近 30 天的数据
        if len(data["daily"]) > 30:
            sorted_days = sorted(data["daily"].keys())
            for old_day in sorted_days[:-30]:
                del data["daily"][old_day]

        self._save(data)

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取最近 N 天的 Token 统计。"""
        data = self._load()
        today = _utcnow()
        result = {
            "period": f"最近 {days} 天",
            "total_calls": 0,
            "total_input_chars": 0,
            "total_output_chars": 0,
            "estimated_tokens": 0,
            "daily": [],
            "by_agent": {},
            "by_mode": {},
        }

        for i in range(days):
            day_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            day_data = data["daily"].get(day_str, {
                "calls": 0, "input_chars": 0, "output_chars": 0,
            })
            result["daily"].append({
                "date": day_str,
                "calls": day_data.get("calls", 0),
                "input_chars": day_data.get("input_chars", 0),
                "output_chars": day_data.get("output_chars", 0),
                "estimated_tokens": (day_data.get("input_chars", 0) + day_data.get("output_chars", 0)) // 4,
            })
            result["total_calls"] += day_data.get("calls", 0)
            result["total_input_chars"] += day_data.get("input_chars", 0)
            result["total_output_chars"] += day_data.get("output_chars", 0)

            # 聚合 by_agent 和 by_mode
            for agent, stats in day_data.get("by_agent", {}).items():
                if agent not in result["by_agent"]:
                    result["by_agent"][agent] = {"calls": 0, "input_chars": 0, "output_chars": 0}
                result["by_agent"][agent]["calls"] += stats["calls"]
                result["by_agent"][agent]["input_chars"] += stats["input_chars"]
                result["by_agent"][agent]["output_chars"] += stats["output_chars"]

            for mode, stats in day_data.get("by_mode", {}).items():
                if mode not in result["by_mode"]:
                    result["by_mode"][mode] = {"calls": 0, "input_chars": 0, "output_chars": 0}
                result["by_mode"][mode]["calls"] += stats["calls"]
                result["by_mode"][mode]["input_chars"] += stats["input_chars"]
                result["by_mode"][mode]["output_chars"] += stats["output_chars"]

        result["estimated_tokens"] = (result["total_input_chars"] + result["total_output_chars"]) // 4
        result["avg_tokens_per_call"] = result["estimated_tokens"] // max(result["total_calls"], 1)

        return result
