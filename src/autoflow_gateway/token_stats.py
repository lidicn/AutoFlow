#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 统计模块 —— 记录和统计 API 调用的 Token 消耗。

估算方式：
  输入 token ≈ 输入字符数 / 4
  输出 token ≈ 输出字符数 / 4
  固定开销 ≈ SKILL.md 大小（已知，~3K token，由调用方传入）

存储（P1-5，审计报告第二批）：
  改为 append-only JSONL（data/<env>/token_stats/<YYYY-MM-DD>.jsonl，每行一次调用），
  O(1) 追加、天然并发安全、按天轮转。查询时再聚合。
  旧的全量聚合文件 data/<env>/token_stats/token_stats.json 在首次 record/get_stats 时
  自动一次性迁移为历史日聚合行（重命名为 .migrated），不丢历史预算条数据。
"""
import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional


# 软性每日 Token 预算（估算 token，≈字符数/4）。用于「token 预算条」可视化：
# 让使用者一眼看到当天用量占预算的比例（可观测，非硬限额）。可按需调大/调小。
DEFAULT_DAILY_TOKEN_BUDGET = 200_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _utcnow().strftime("%Y-%m-%d")


class TokenStatsStore:
    """Token 消耗统计存储（JSONL 追加式，并发安全）。"""

    # 类级锁：串行化"迁移 + 追加"，避免并发进程/线程交错损坏 JSONL。
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        # 旧聚合文件（迁移源，存在则一次性迁走）
        self.legacy_file = os.path.join(data_dir, "token_stats.json")
        # 已迁移标记（进程内去重；配合文件存在性检查，跨实例也安全）
        self._migrated = False

    def _jsonl_path(self, day_str: str) -> str:
        return os.path.join(self.data_dir, f"{day_str}.jsonl")

    # ───────────── 迁移（旧 JSON → JSONL） ─────────────
    def _maybe_migrate(self) -> None:
        """若存在旧 token_stats.json，折叠为历史日聚合行写入对应 JSONL，再重命名归档。
        幂等：实例标记 + 文件存在性双重保险，并发下不会重复迁移。"""
        if self._migrated:
            return
        self._migrated = True
        if not os.path.isfile(self.legacy_file):
            return
        try:
            with open(self.legacy_file, encoding="utf-8") as f:
                legacy = json.load(f)
            daily = (legacy.get("daily") or {}) if isinstance(legacy, dict) else {}
            for day, d in daily.items():
                if not isinstance(d, dict):
                    continue
                # 该日若已有新 JSONL（迁移后新写入），跳过避免重复计入
                if os.path.isfile(self._jsonl_path(day)):
                    continue
                # 历史日无逐调用明细，回放为一条聚合行：保留日级总量 + 端点/agent/模式明细，
                # 供预算条连续性；逐调用粒度对历史日不可恢复（可接受，已部署前的数据）。
                row = {
                    "endpoint": "_migrated",
                    "agent_id": "*",
                    "input_chars": d.get("input_chars", 0),
                    "output_chars": d.get("output_chars", 0),
                    "mode": "migrated",
                    "success": True,
                    "ts": day + "T00:00:00+00:00",
                    "day_agg": {
                        "calls": d.get("calls", 0),
                        "by_endpoint": d.get("by_endpoint", {}) or {},
                        "by_agent": d.get("by_agent", {}) or {},
                        "by_mode": d.get("by_mode", {}) or {},
                    },
                }
                with open(self._jsonl_path(day), "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            # 迁移完成：归档旧文件，避免下次重复迁移
            try:
                os.replace(self.legacy_file, self.legacy_file + ".migrated")
            except OSError:
                pass
        except Exception as e:
            # 迁移失败不能阻断埋点；保留原文件，下个调用重试
            self._migrated = False
            try:
                import logging
                logging.getLogger("autoflow.token_stats").warning(
                    "token_stats 迁移旧 JSON 失败，保留原文件: %s", e)
            except Exception:
                pass

    # ───────────── 写入 ─────────────
    def record(self, endpoint: str, agent_id: str,
               input_chars: int, output_chars: int,
               mode: str = "dsl", success: bool = True) -> None:
        """记录一次 API 调用的 Token 消耗（O(1) 追加到当日 JSONL）。"""
        with TokenStatsStore._lock:
            self._maybe_migrate()
            row = {
                "endpoint": endpoint,
                "agent_id": agent_id,
                "input_chars": int(input_chars),
                "output_chars": int(output_chars),
                "mode": mode,
                "success": bool(success),
                "ts": _utcnow().isoformat(),
            }
            path = self._jsonl_path(_today_str())
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ───────────── 读取 / 聚合 ─────────────
    @staticmethod
    def _empty_day() -> Dict[str, Any]:
        return {"calls": 0, "input_chars": 0, "output_chars": 0,
                "by_agent": {}, "by_endpoint": {}, "by_mode": {}}

    @staticmethod
    def _add_bucket(target: Dict[str, Dict[str, int]], key: str,
                    calls: int, inp: int, out: int) -> None:
        b = target.get(key)
        if b is None:
            b = {"calls": 0, "input_chars": 0, "output_chars": 0}
            target[key] = b
        b["calls"] += calls
        b["input_chars"] += inp
        b["output_chars"] += out

    def _read_day(self, day_str: str) -> Dict[str, Any]:
        agg = self._empty_day()
        path = self._jsonl_path(day_str)
        if not os.path.isfile(path):
            return agg
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if "day_agg" in row:  # 迁移聚合行
                    da = row["day_agg"]
                    agg["calls"] += int(da.get("calls", 0))
                    agg["input_chars"] += int(row.get("input_chars", 0))
                    agg["output_chars"] += int(row.get("output_chars", 0))
                    for k, v in (da.get("by_endpoint") or {}).items():
                        if isinstance(v, dict):
                            self._add_bucket(agg["by_endpoint"], k,
                                             int(v.get("calls", 0)),
                                             int(v.get("input_chars", 0)),
                                             int(v.get("output_chars", 0)))
                    for k, v in (da.get("by_agent") or {}).items():
                        if isinstance(v, dict):
                            self._add_bucket(agg["by_agent"], k,
                                             int(v.get("calls", 0)),
                                             int(v.get("input_chars", 0)),
                                             int(v.get("output_chars", 0)))
                    for k, v in (da.get("by_mode") or {}).items():
                        if isinstance(v, dict):
                            self._add_bucket(agg["by_mode"], k,
                                             int(v.get("calls", 0)),
                                             int(v.get("input_chars", 0)),
                                             int(v.get("output_chars", 0)))
                else:  # 普通逐调用行
                    agg["calls"] += 1
                    agg["input_chars"] += int(row.get("input_chars", 0))
                    agg["output_chars"] += int(row.get("output_chars", 0))
                    self._add_bucket(agg["by_endpoint"], str(row.get("endpoint", "")),
                                     1, int(row.get("input_chars", 0)),
                                     int(row.get("output_chars", 0)))
                    self._add_bucket(agg["by_agent"], str(row.get("agent_id", "")),
                                     1, int(row.get("input_chars", 0)),
                                     int(row.get("output_chars", 0)))
                    self._add_bucket(agg["by_mode"], str(row.get("mode", "dsl")),
                                     1, int(row.get("input_chars", 0)),
                                     int(row.get("output_chars", 0)))
        return agg

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取最近 N 天的 Token 统计（聚合自 JSONL，结构向后兼容）。"""
        with TokenStatsStore._lock:
            self._maybe_migrate()
            today = _utcnow()
            result: Dict[str, Any] = {
                "period": f"最近 {days} 天",
                "total_calls": 0,
                "total_input_chars": 0,
                "total_output_chars": 0,
                "estimated_tokens": 0,
                "daily": [],
                "by_agent": {},
                "by_mode": {},
                "by_endpoint": {},
            }

            daily_map: Dict[str, Dict[str, Any]] = {}
            for i in range(days):
                day_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                daily_map[day_str] = self._read_day(day_str)

            for day_str, day_data in daily_map.items():
                result["daily"].append({
                    "date": day_str,
                    "calls": day_data["calls"],
                    "input_chars": day_data["input_chars"],
                    "output_chars": day_data["output_chars"],
                    "estimated_tokens": (day_data["input_chars"] + day_data["output_chars"]) // 4,
                })
                result["total_calls"] += day_data["calls"]
                result["total_input_chars"] += day_data["input_chars"]
                result["total_output_chars"] += day_data["output_chars"]

                for agent, stats in day_data["by_agent"].items():
                    self._add_bucket(result["by_agent"], agent,
                                     stats["calls"], stats["input_chars"], stats["output_chars"])
                for mode, stats in day_data["by_mode"].items():
                    self._add_bucket(result["by_mode"], mode,
                                     stats["calls"], stats["input_chars"], stats["output_chars"])
                for endpoint, stats in day_data["by_endpoint"].items():
                    self._add_bucket(result["by_endpoint"], endpoint,
                                     stats["calls"], stats["input_chars"], stats["output_chars"])

            result["estimated_tokens"] = (
                result["total_input_chars"] + result["total_output_chars"]) // 4
            result["avg_tokens_per_call"] = result["estimated_tokens"] // max(result["total_calls"], 1)

            # 软预算可视化字段（向后兼容：纯新增，不改既有字段）
            _today = _today_str()
            _today_day = daily_map.get(_today, self._empty_day())
            _today_est = (_today_day["input_chars"] + _today_day["output_chars"]) // 4
            _today_calls = _today_day["calls"]
            result["budget"] = DEFAULT_DAILY_TOKEN_BUDGET
            result["budget_used_pct"] = round(
                100.0 * _today_est / max(DEFAULT_DAILY_TOKEN_BUDGET, 1), 1)
            result["today_estimated_tokens"] = _today_est
            result["today_rounds"] = _today_calls  # 当天调用次数 ≈ agent 迭代轮次

            return result
