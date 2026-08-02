#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 身份层（MCP 身份识别 + per-agent 存档）

核心价值落点：
  - 标准化/可复用：任何能发 MCP 的 agent 都先「亮身份码」才能连，匿名直接拒。
  - 可归因：每个 agent 有唯一 agent_id，其意图/提案/待确认都打标，便于考核「网关烂还是 agent 弱」。
  - 权限分级：tier 决定能碰 staging 还是 prod；可疑 agent 吊销即失效。

身份码 = `af_` + secrets.token_urlsafe(24)，只存储 sha256 哈希，创建时明文仅返回一次。
MCP 连接时客户端在 `Authorization: Bearer <身份码>` 携带；中间件解析不到/失效即 401。
"""
import contextvars
import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .config import get_config

# ── 请求级上下文：当前已认证 agent ──
# 由 ASGI 鉴权中间件在每个 MCP 请求里 set，tool 函数读取。
current_agent: contextvars.ContextVar["Agent"] = contextvars.ContextVar(
    "autoflow_current_agent", default=None
)


def get_current_agent() -> Optional["Agent"]:
    return current_agent.get()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_mode(notes: str) -> str:
    """从旧式 notes 魔法串解析 mode（向后兼容：列空时回退）。
    返回 black / white / both。"""
    n = (notes or "").lower()
    if "mode=black" in n:
        return "black"
    if "mode=white" in n:
        return "white"
    return "both"


@dataclass
class Agent:
    agent_id: str
    name: str
    tier: str                  # staging | prod | sandbox
    status: str               # active | revoked
    identity_code_hash: str
    created_at: str
    last_seen: Optional[str] = None
    notes: str = ""
    mode: str = "both"        # black | white | dual | both | admin
                               # black：只允许 DSL 路径（propose_dsl），禁白箱刀
                               # white：允许白箱刀（autoflow_deploy_raw 现已统一为提案闸，落提案待人审，不再直写 NR）
                               # dual ：黑白双池都领（auto_wb 优先，空了再 auto）
                               # both （缺省/未标记）：不限制（向后兼容旧身份）
                               # admin：仅限连 /mcp-admin（管理面），可通吃白箱刀+运维刀+测试杠杆；
                               #        普通 agent 不可持有，避免白箱身份越权重启/发布任务池。

    def to_dict(self, include_code: bool = False, code: Optional[str] = None):
        d = {
            "agent_id": self.agent_id,
            "name": self.name,
            "tier": self.tier,
            "status": self.status,
            "mode": self.mode,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "notes": self.notes,
        }
        if include_code:
            d["identity_code"] = code  # 仅创建/重置时返回一次
        return d


class AgentStore:
    """agent 身份与存档（SQLite，单库网关级，跨 env 共享）。"""

    _lock = threading.Lock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        self._init_db()

    # ── 连接 ──
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS agents (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        tier TEXT NOT NULL DEFAULT 'staging',
                        status TEXT NOT NULL DEFAULT 'active',
                        identity_code_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_seen TEXT,
                        notes TEXT DEFAULT '',
                        mode TEXT DEFAULT 'both'
                    )"""
                )
                # 迁移：旧库可能没有 mode 列 → 加列并把 notes 里的魔法串回填
                cols = [c[1] for c in conn.execute("PRAGMA table_info(agents)")]
                if "mode" not in cols:
                    conn.execute("ALTER TABLE agents ADD COLUMN mode TEXT DEFAULT 'both'")
                    for row in conn.execute("SELECT agent_id, notes FROM agents"):
                        conn.execute(
                            "UPDATE agents SET mode=? WHERE agent_id=?",
                            (_parse_mode(row["notes"]), row["agent_id"]),
                        )
                conn.commit()
            finally:
                conn.close()

    # ── CRUD ──
    def create_agent(self, name: str, tier: str = "staging", notes: str = "",
                     mode: str = "both") -> (Agent, str):
        """创建 agent，返回 (Agent, 明文身份码)。身份码仅此刻可见。
        mode: black | white | dual | both | admin（黑白箱路由，存为列）。
        admin 仅用于网关自身运维身份（连 /mcp-admin），普通 agent 不应持有。"""
        if mode not in ("black", "white", "dual", "both", "admin"):
            raise ValueError(f"非法 mode: {mode}")
        import secrets
        code = "af_" + secrets.token_urlsafe(24)
        agent_id = "agt_" + uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO agents (agent_id,name,tier,status,identity_code_hash,created_at,notes,mode) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (agent_id, name, tier, "active", _sha256(code), now, notes, mode),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"agent 名已存在: {name}")
            finally:
                conn.close()
        agent = Agent(agent_id, name, tier, "active", _sha256(code), now, None, notes, mode)
        return agent, code

    def list_agents(self) -> List[Agent]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM agents ORDER BY created_at DESC"
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_agent(r) for r in rows]

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM agents WHERE agent_id=?", (agent_id,)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_agent(r) if r else None

    def get_agent_by_name(self, name: str) -> Optional[Agent]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM agents WHERE name=?", (name,)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_agent(r) if r else None

    def resolve_by_code(self, code: str) -> Optional[Agent]:
        """用明文身份码解析 agent；不存在/失效返回 None（中间件据此拒匿名）。"""
        if not code:
            return None
        h = _sha256(code)
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM agents WHERE identity_code_hash=?", (h,)
                ).fetchone()
            finally:
                conn.close()
        if not r:
            return None
        agent = self._row_to_agent(r)
        if agent.status != "active":
            return None
        return agent

    def revoke_agent(self, agent_id: str) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE agents SET status='revoked' WHERE agent_id=?", (agent_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def update_agent(self, agent_id: str,
                     name: Optional[str] = None,
                     tier: Optional[str] = None,
                     status: Optional[str] = None,
                     notes: Optional[str] = None,
                     mode: Optional[str] = None) -> bool:
        """更新 agent 元数据（name/tier/status/notes/mode）。仅传入的字段会被改写。
        mode 直接存为列（black/white/dual/both/admin）；不再从 notes 魔法串推断。"""
        fields, vals = [], []
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("name 不能为空")
            fields.append("name=?")
            vals.append(name)
        if tier is not None:
            fields.append("tier=?")
            vals.append(tier)
        if status is not None:
            fields.append("status=?")
            vals.append(status)
        if notes is not None:
            fields.append("notes=?")
            vals.append(notes)
        if mode is not None:
            if mode not in ("black", "white", "dual", "both", "admin"):
                raise ValueError(f"非法 mode: {mode}")
            fields.append("mode=?")
            vals.append(mode)
        if not fields:
            return True  # 无变更
        vals.append(agent_id)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    f"UPDATE agents SET {', '.join(fields)} WHERE agent_id=?", vals
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def regenerate_code(self, agent_id: str) -> Optional[str]:
        """吊销旧码、发新码；返回明文新码（仅此刻可见）。"""
        import secrets
        code = "af_" + secrets.token_urlsafe(24)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE agents SET identity_code_hash=? WHERE agent_id=? AND status='active'",
                    (_sha256(code), agent_id),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
            finally:
                conn.close()
        return code

    def record_last_seen(self, agent_id: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE agents SET last_seen=? WHERE agent_id=?",
                    (_now(), agent_id),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _row_to_agent(r) -> Agent:
        mode = r["mode"] or ""
        if not mode:
            mode = _parse_mode(r["notes"])
        return Agent(
            agent_id=r["agent_id"],
            name=r["name"],
            tier=r["tier"],
            status=r["status"],
            identity_code_hash=r["identity_code_hash"],
            created_at=r["created_at"],
            last_seen=r["last_seen"],
            notes=r["notes"] or "",
            mode=mode,
        )
