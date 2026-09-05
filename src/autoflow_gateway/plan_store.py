#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 工作区 plan 结构化存储（单一真相源）

供 WebUI「工作区」tab 与 agent（经 MCP autoflow_set_plan）共享：
  · overall   总体计划 / 长期目标（owner 在 WebUI 编辑，agent 一般不动）
  · current   当前正在做的事（agent 实时更新）
  · completed  最近完成日志（agent 追加 {ts, text}）
所有写操作落 SQLite autoflow.db，跨进程/重启持久，避免 AUTONOMOUS_PLAN.md 与界面漂移。
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from .config import get_config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 单例行 id（plan 只有一份全局状态）
_ROW_ID = "singleton"
_MAX_COMPLETED = 50


class PlanStore:
    _lock = threading.Lock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS plan_state (
                        id TEXT PRIMARY KEY,
                        overall TEXT NOT NULL DEFAULT '',
                        current TEXT NOT NULL DEFAULT '',
                        completed TEXT NOT NULL DEFAULT '[]',
                        updated_at TEXT NOT NULL
                    )"""
                )
                conn.commit()
            finally:
                conn.close()

    def _load(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT overall, current, completed, updated_at FROM plan_state WHERE id=?",
                    (_ROW_ID,),
                ).fetchone()
            finally:
                conn.close()
        if r is None:
            return {"overall": "", "current": "", "completed": [], "updated_at": ""}
        try:
            completed = json.loads(r["completed"]) if r["completed"] else []
        except (json.JSONDecodeError, TypeError):
            completed = []
        return {
            "overall": r["overall"],
            "current": r["current"],
            "completed": completed,
            "updated_at": r["updated_at"],
        }

    def _save(self, state: dict) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO plan_state (id, overall, current, completed, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         overall=excluded.overall,
                         current=excluded.current,
                         completed=excluded.completed,
                         updated_at=excluded.updated_at""",
                    (
                        _ROW_ID,
                        state["overall"],
                        state["current"],
                        json.dumps(state["completed"], ensure_ascii=False),
                        state["updated_at"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    # ── 读 ──
    def get_state(self) -> dict:
        return self._load()

    # ── 写（便捷）──
    def set_overall(self, text: str) -> dict:
        return self.update(overall=text)

    def set_current(self, text: str) -> dict:
        return self.update(current=text)

    def append_completed(self, text: str) -> dict:
        return self.update(append_completed=text)

    def update(self, overall: Optional[str] = None,
               current: Optional[str] = None,
               append_completed: Optional[str] = None) -> dict:
        """局部更新；传 None 的字段保持不变。返回更新后的完整状态。"""
        state = self._load()
        if overall is not None:
            state["overall"] = overall
        if current is not None:
            state["current"] = current
        if append_completed:
            state["completed"].insert(0, {"ts": _now(), "text": append_completed})
            state["completed"] = state["completed"][:_MAX_COMPLETED]
        state["updated_at"] = _now()
        self._save(state)
        return state
