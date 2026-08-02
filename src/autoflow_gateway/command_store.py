#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 指令收件箱（人类 → deepseek 直达指令）

owner 在 WebUI「工作区」的指令框里下达自然语言指令，落此表 → 网关发射器（_fire_ds_bridge）
把指令直达 ds_bridge/Chrome deepseek++，由其用 autoflow MCP 工具面执行并经 Bark 回报。

与 notes/proposals 区别：
  · notes    人类私人想法存档，不执行。
  · proposals agent 产出的 DSL 候选，进升格/部署流程。
  · commands  人类下达、面向 deepseek 的可执行指令，网关负责投递（fire-and-forget）。

状态机：queued → dispatching → dispatched（投递成功）/ failed（投递失败）。
注意：dispatched 仅表示指令已送达 ds_bridge，不代表 deepseek 已完成——完成情况经
proposals + _bark_watch 回报给 owner，本表只负责投递审计。
"""
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .config import get_config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Command:
    id: str
    text: str
    target: str
    status: str
    job_id: str
    result: str
    created_at: str
    dispatched_at: str

    def to_dict(self):
        return {
            "id": self.id,
            "text": self.text,
            "target": self.target,
            "status": self.status,
            "job_id": self.job_id,
            "result": self.result,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
        }


class CommandStore:
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
                    """CREATE TABLE IF NOT EXISTS commands (
                        id TEXT PRIMARY KEY,
                        text TEXT NOT NULL DEFAULT '',
                        target TEXT NOT NULL DEFAULT 'deepseek',
                        status TEXT NOT NULL DEFAULT 'queued',
                        job_id TEXT NOT NULL DEFAULT '',
                        result TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        dispatched_at TEXT NOT NULL DEFAULT ''
                    )"""
                )
                conn.commit()
            finally:
                conn.close()

    def create(self, text: str, target: str = "deepseek") -> Command:
        cid = "cmd_" + uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO commands (id,text,target,status,job_id,result,created_at,dispatched_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (cid, text, target, "queued", "", "", now, ""),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(cid)

    def list(self, limit: int = 30) -> List[Command]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM commands ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [self._row(r) for r in rows]

    def get(self, cid: str) -> Optional[Command]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute("SELECT * FROM commands WHERE id=?", (cid,)).fetchone()
            finally:
                conn.close()
        return self._row(r) if r else None

    def mark(self, cid: str, status: str,
             job_id: Optional[str] = None, result: Optional[str] = None) -> Optional[Command]:
        c = self.get(cid)
        if c is None:
            return None
        job_id = job_id if job_id is not None else c.job_id
        result = result if result is not None else c.result
        dispatched = c.dispatched_at or (_now() if status in ("dispatched", "failed") else "")
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE commands SET status=?, job_id=?, result=?, dispatched_at=? WHERE id=?",
                    (status, job_id, result, dispatched, cid),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(cid)

    @staticmethod
    def _row(r) -> Command:
        return Command(
            id=r["id"],
            text=r["text"],
            target=r["target"],
            status=r["status"],
            job_id=r["job_id"],
            result=r["result"],
            created_at=r["created_at"],
            dispatched_at=r["dispatched_at"],
        )
