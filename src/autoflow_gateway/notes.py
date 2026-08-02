#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 用户笔记（关于智能家居系统的想法，未必马上能落地）

这是人类（网关 owner）的私人思考区，不是 agent 产物。WebUI Notes 面板 CRUD。
与 proposals 不同：notes 不进入升格流程，仅作长期想法存档，可贴标签、可搜索。
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
class Note:
    id: str
    title: str
    body: str
    tags: List[str]
    created_at: str
    updated_at: str

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class NoteStore:
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
                    """CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        body TEXT NOT NULL DEFAULT '',
                        tags TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                conn.commit()
            finally:
                conn.close()

    def create(self, title: str, body: str, tags: Optional[List[str]] = None) -> Note:
        nid = "nt_" + uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO notes (id,title,body,tags,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (nid, title, body, json.dumps(tags or [], ensure_ascii=False), now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(nid)

    def list(self, tag: Optional[str] = None, q: Optional[str] = None) -> List[Note]:
        sql = "SELECT * FROM notes WHERE 1=1"
        params = []
        if q:
            sql += " AND (title LIKE ? OR body LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        notes = [self._row_to_note(r) for r in rows]
        if tag:
            notes = [n for n in notes if tag in n.tags]
        return notes

    def get(self, nid: str) -> Optional[Note]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            finally:
                conn.close()
        return self._row_to_note(r) if r else None

    def update(self, nid: str, title: Optional[str] = None,
               body: Optional[str] = None, tags=None) -> Optional[Note]:
        n = self.get(nid)
        if n is None:
            return None
        title = title if title is not None else n.title
        body = body if body is not None else n.body
        tags = tags if tags is not None else n.tags
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE notes SET title=?, body=?, tags=?, updated_at=? WHERE id=?",
                    (title, body, json.dumps(tags, ensure_ascii=False), _now(), nid),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(nid)

    def delete(self, nid: str) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM notes WHERE id=?", (nid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _row_to_note(r) -> Note:
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        return Note(
            id=r["id"],
            title=r["title"],
            body=r["body"],
            tags=tags,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
