#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 多选项决策存储（人类请示闸）

区别于确认闸(ConfirmationGate)的 binary approve/reject：这里支持 N 选 1 的
开放式请示。典型场景：deepseek 执行中遇到歧义/不可逆分叉，抛出一道选择题，
用户在 WebUI「工作区」点选，结果回灌 deepseek 继续（双向闭环）。

落 SQLite autoflow.db，跨进程/重启持久。pending 优先排序。
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from .config import get_config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionStore:
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
                    """CREATE TABLE IF NOT EXISTS decisions (
                        id TEXT PRIMARY KEY,
                        question TEXT NOT NULL,
                        options TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'pending',
                        source TEXT NOT NULL DEFAULT 'deepseek',
                        chosen_idx INTEGER,
                        chosen_text TEXT,
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        resolved_by TEXT
                    )"""
                )
                conn.commit()
            finally:
                conn.close()
        # 向后兼容：老表可能缺催办相关列，静默补列
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("ALTER TABLE decisions ADD COLUMN escalated_at TEXT")
                conn.execute("ALTER TABLE decisions ADD COLUMN reminder_count INTEGER DEFAULT 0")
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()

    def create(self, question: str, options: List[str], source: str = "deepseek") -> Dict[str, Any]:
        q = (question or "").strip()
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not q:
            raise ValueError("question 必填")
        if len(opts) < 1:
            raise ValueError("options 至少 1 项")
        did = "dec_" + uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO decisions (id, question, options, status, source, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (did, q, json.dumps(opts, ensure_ascii=False), "pending", source, now),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(did)

    def get(self, did: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(r) if r else None

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._conn()
            try:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM decisions WHERE status=? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                else:
                    # pending 优先，其次按时间倒序
                    rows = conn.execute(
                        "SELECT * FROM decisions "
                        "ORDER BY (status='pending') DESC, created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            finally:
                conn.close()
        return [self._row_to_dict(r) for r in rows]

    def resolve(self, did: str, chosen_idx: int, by: str = "human") -> Dict[str, Any]:
        rec = self.get(did)
        if rec is None:
            raise KeyError(did)
        if rec["status"] != "pending":
            raise ValueError(f"决策 {did} 已 {rec['status']}，不能重复决定")
        opts = rec["options"]
        if not (0 <= chosen_idx < len(opts)):
            raise ValueError(f"选项下标越界: {chosen_idx} (共 {len(opts)} 项)")
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE decisions SET status='resolved', chosen_idx=?, "
                    "chosen_text=?, resolved_at=?, resolved_by=? WHERE id=?",
                    (chosen_idx, opts[chosen_idx], now, by, did),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(did)

    def record_reminder(self, did: str) -> None:
        """记录一次催办（供看门狗调用），幂等于决策本身的状态。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE decisions SET reminder_count = COALESCE(reminder_count,0)+1, "
                    "escalated_at=? WHERE id=?",
                    (_now(), did),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(r) -> Dict[str, Any]:
        d = dict(r)
        try:
            d["options"] = json.loads(d["options"]) if d["options"] else []
        except (json.JSONDecodeError, TypeError):
            d["options"] = []
        d["reminder_count"] = d.get("reminder_count") or 0
        return d
