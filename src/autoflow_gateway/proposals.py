#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 提案 / 经验沉淀存储（raw → candidate → public）

agent 可通过 MCP 提交提案（认为网关该改的地方 / 产出的经验 skill / 约定修正）。
人类在 WebUI 审核：升格（raw→candidate→public）或拒绝。
升格到 public 时，把内容落盘为公用 skill 文档 `data/<env>/experience/public/<slug>.md`，
实现「经验复利 / 集体智能」——agent 留下的经验反哺网关、可被多 agent 复用。
"""
import json
import os
import re
import hashlib
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .config import get_config

VALID_KINDS = ("skill", "convention", "fix", "idea", "subflow")
STATUS_FLOW = {"raw": "candidate", "candidate": "public"}

# 同一 (agent_id, content) 在时间窗内的重复提交（多为 MCP 客户端超时重试 fan-out）
# 直接返回已存在记录，避免「2 意图 → 7 提案」式的重复落库。默认 120s 窗口既覆盖
# 秒级重试，又允许隔很久后的真实重复提案。
DEDUP_WINDOW_SEC = 120

# 测试 / 基础设施身份（非真实 agent）隔离：默认在用户面 list() 隐藏，避免污染提案区。
# 真实 agent 统一以 "agt_" 前缀；其余视为 test/infra。可在实例化时覆盖。
TEST_AGENT_IDS = {
    "agent_test", "corpus-verify", "corpus-test", "golden_test", "t",
    "wire-trace", "webui-spotcheck", "trace-body", "definitive-test",
    "agent_deepseek_test", "_offline_test_",
}
TEST_AGENT_PREFIXES = ("agent_", "corpus", "golden_", "wire-", "webui-",
                       "trace-", "definitive", "_offline", "test")


def _is_test_agent(agent_id: str) -> bool:
    if agent_id in TEST_AGENT_IDS:
        return True
    return any(agent_id.startswith(p) for p in TEST_AGENT_PREFIXES)


def _test_agent_sql(negate: bool = True):
    """构造「是否测试/基础设施身份」的 SQL 谓语片段，与 _is_test_agent() 等价。

    命中规则：agent_id 在 TEST_AGENT_IDS 集合内，或以任一 TEST_AGENT_PREFIXES
    前缀开头。返回 (fragment, params)。
    - negate=True（默认）：排除测试身份，即用户面 list/count 默认隐藏；
      include_test=True 时调用方不应拼接此片段。
    用 SQL 而非 Python 后过滤，是为了让 LIMIT/OFFSET/COUNT 与返回行一致
    （否则先取后过滤会让分页错位、total 对不上）。
    """
    id_placeholders = ",".join("?" * len(TEST_AGENT_IDS))
    clauses = [f"agent_id IN ({id_placeholders})"]
    params: List[Any] = list(TEST_AGENT_IDS)
    for p in TEST_AGENT_PREFIXES:
        clauses.append("agent_id LIKE ?")
        params.append(p + "%")
    fragment = "(" + " OR ".join(clauses) + ")"
    if negate:
        fragment = "NOT " + fragment
    return fragment, params


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(title: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", title.strip().lower()).strip("-")
    return s or "proposal"


@dataclass
class Proposal:
    id: str
    agent_id: str
    title: str
    kind: str
    content: str
    status: str
    tags: List[str]
    created_at: str
    decided_at: Optional[str]
    reviewer: Optional[str]
    public_path: Optional[str]
    deployed_flow_id: Optional[str]
    source: str = "unknown"
    spec: str = ""
    archived_at: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "title": self.title,
            "kind": self.kind,
            "content": self.content,
            "status": self.status,
            "tags": self.tags,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "reviewer": self.reviewer,
            "public_path": self.public_path,
            "deployed_flow_id": self.deployed_flow_id,
            "source": self.source,
            "spec": self.spec,
            "archived_at": self.archived_at,
        }

    def to_summary(self) -> Dict[str, Any]:
        """列表接口用轻量视图：content 仅保留卡片渲染/搜索所需字段
        （dsl/type/node_count/lint_*/blocking_rules/logic.unreachable_actions），
        剔除 flow/validation 等撑体积的节点数组，避免 /api/proposals 一次性返回
        2.4MB 在单 worker uvicorn 下阻塞事件循环导致 WebUI「加载中」卡死。
        前端 JSON.parse(p.content) 读取字段名不变，无需改前端。"""
        content_trim = self.content
        try:
            j = json.loads(self.content or "{}")
            if isinstance(j, dict):
                if j.get("type") == "subflow":
                    # 子流程提案：保留注册所需的轻量字段（dsl_name/name/定义 id/节点数），
                    # 剔除完整 definition.nodes（避免列表接口体积膨胀）。
                    d = j.get("definition") or {}
                    keep: Dict[str, Any] = {
                        "type": "subflow",
                        "dsl_name": j.get("dsl_name"),
                        "name": j.get("name"),
                        "description": j.get("description", ""),
                        "definition_id": d.get("id"),
                        "node_count": len(d.get("nodes") or []),
                    }
                else:
                    keep: Dict[str, Any] = {}
                    for k in ("dsl", "type", "node_count", "lint_error_count",
                              "lint_warning_count", "blocking_rules"):
                        if k in j:
                            keep[k] = j[k]
                    if isinstance(j.get("logic"), dict):
                        keep["logic"] = {
                            "unreachable_actions": j["logic"].get("unreachable_actions", [])
                        }
                content_trim = json.dumps(keep, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "title": self.title,
            "kind": self.kind,
            "content": content_trim,
            "status": self.status,
            "tags": self.tags,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "reviewer": self.reviewer,
            "public_path": self.public_path,
            "deployed_flow_id": self.deployed_flow_id,
            "source": self.source,
            "spec": self.spec,
            "archived_at": self.archived_at,
        }


class ProposalStore:
    _lock = threading.Lock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL：读写互不阻塞（golden 回归频繁写提案时，WebUI 读取不再被写锁拖卡）。
        # busy_timeout：偶发锁竞争时自动重试而非立即报 database is locked。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS proposals (
                        id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        content TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'raw',
                        tags TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        decided_at TEXT,
                        reviewer TEXT,
                        public_path TEXT
                    )"""
                )
                conn.commit()
            finally:
                conn.close()

        # 已部署追踪列（可能已存在，忽略报错）
        try:
            c2 = self._conn()
            try:
                c2.execute("ALTER TABLE proposals ADD COLUMN deployed_flow_id TEXT")
                c2.commit()
            finally:
                c2.close()
        except Exception:
            pass

        # source 列（compiler/raw/unknown：标识提案来源路径，供部署策略按来源分流；可能已存在，忽略报错）
        try:
            c3 = self._conn()
            try:
                c3.execute("ALTER TABLE proposals ADD COLUMN source TEXT")
                c3.commit()
            finally:
                c3.close()
        except Exception:
            pass

        # spec 列（人类可读的自动化说明，供 autoflow_list_automations 跨会话检索；
        # flow 提案存 DSL 文本/节点直方图摘要，经验提案缺省回退 title；可能已存在，忽略报错）
        try:
            c4 = self._conn()
            try:
                c4.execute("ALTER TABLE proposals ADD COLUMN spec TEXT")
                c4.commit()
            finally:
                c4.close()
        except Exception:
            pass

        # content_hash 列（去重用；可能已存在，忽略报错）
        try:
            c5 = self._conn()
            try:
                c5.execute("ALTER TABLE proposals ADD COLUMN content_hash TEXT")
                c5.commit()
            finally:
                c5.close()
        except Exception:
            pass

        # archived_at 列（归档时间戳；NULL=未归档。归档是「退休」语义，从活跃视图隐藏；
        # 默认 list()/count() 排除之。可能已存在，忽略报错）
        try:
            c6 = self._conn()
            try:
                c6.execute("ALTER TABLE proposals ADD COLUMN archived_at TEXT")
                c6.commit()
            finally:
                c6.close()
        except Exception:
            pass

    def submit(self, agent_id: str, title: str, kind: str, content: str,
               tags: Optional[List[str]] = None, source: str = "unknown",
               spec: str = "") -> Proposal:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind 必须是 {VALID_KINDS}，收到: {kind}")

        # 时间窗去重：同 (agent_id, content) 在 DEDUP_WINDOW_SEC 内的重复提交
        # （MCP 客户端超时重试 fan-out）直接返回已存在记录，不再新建。
        h = hashlib.sha256(f"{agent_id}\x00{content}".encode("utf-8")).hexdigest()
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=DEDUP_WINDOW_SEC)).isoformat()
        # 在锁内只算出最终要返回的 id（命中去重则复用旧记录，否则新建）；
        # self.get() 放到锁外调用，避免与普通 Lock（不可重入）自死锁。
        target_id = None
        with self._lock:
            conn = self._conn()
            try:
                existing = conn.execute(
                    "SELECT id FROM proposals "
                    "WHERE agent_id=? AND content_hash=? AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (agent_id, h, cutoff),
                ).fetchone()
                if existing:
                    target_id = existing["id"]   # 去重命中：复用已存在记录
                else:
                    pid = "pr_" + uuid.uuid4().hex[:12]
                    now = _now()
                    spec = spec or title  # 缺省回退 title，保证 spec 永非空、可被检索
                    conn.execute(
                        "INSERT INTO proposals (id,agent_id,title,kind,content,status,tags,created_at,source,spec,content_hash) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, agent_id, title, kind, content, "raw",
                         json.dumps(tags or [], ensure_ascii=False), now, source, spec, h),
                    )
                    conn.commit()
                    target_id = pid
            finally:
                conn.close()
        return self.get(target_id)

    def list(self, agent_id: Optional[str] = None,
              status: Optional[str] = None,
              include_test: bool = False,
              limit: Optional[int] = None,
              offset: int = 0,
              include_archived: bool = False) -> List[Proposal]:
        """列出提案（默认按 created_at DESC）。

        - limit=None（默认）不分页：cli/gateway/mcp/diagnostics 等内部调用者行为不变。
        - 仅 WebUI list_proposals 显式传 limit（默认 100，硬上限由调用方控制）。
        - include_archived=False（默认）隐藏已归档项：归档是「退休」语义，从活跃视图移除。
        - 测试/基础设施身份默认隐藏（include_test=False）：过滤下推到 SQL，
          使 LIMIT/OFFSET/COUNT 与返回行一致（否则 Python 侧后过滤会让分页错位）。
        """
        where = ["1=1"]
        params: List[Any] = []
        if agent_id:
            where.append("agent_id=?")
            params.append(agent_id)
        if status:
            where.append("status=?")
            params.append(status)
        if not include_archived:
            where.append("archived_at IS NULL")
        if not include_test:
            pred, pparams = _test_agent_sql(negate=True)
            where.append(pred)
            params.extend(pparams)
        sql = "SELECT * FROM proposals WHERE " + " AND ".join(where) + " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if offset:
                sql += " OFFSET ?"
                params.append(int(offset))
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [self._row_to_proposal(r) for r in rows]

    def count(self, agent_id: Optional[str] = None,
              status: Optional[str] = None,
              include_test: bool = False,
              include_archived: bool = False) -> int:
        """返回与 list() 同等过滤条件下的提案总数（分页 total 用）。"""
        where = ["1=1"]
        params: List[Any] = []
        if agent_id:
            where.append("agent_id=?")
            params.append(agent_id)
        if status:
            where.append("status=?")
            params.append(status)
        if not include_archived:
            where.append("archived_at IS NULL")
        if not include_test:
            pred, pparams = _test_agent_sql(negate=True)
            where.append(pred)
            params.extend(pparams)
        sql = "SELECT COUNT(*) AS n FROM proposals WHERE " + " AND ".join(where)
        with self._lock:
            conn = self._conn()
            try:
                n = conn.execute(sql, params).fetchone()["n"]
            finally:
                conn.close()
        return int(n)

    def get(self, pid: str) -> Optional[Proposal]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
            finally:
                conn.close()
        return self._row_to_proposal(r) if r else None

    def promote(self, pid: str, reviewer: str = "human") -> Proposal:
        """推进一级：raw→candidate→public。到 public 时落盘公用 skill。"""
        p = self.get(pid)
        if p is None:
            raise KeyError(f"提案不存在: {pid}")
        # 子流程提案不走「升格为经验 skill」路径（raw→candidate→public 落盘文档），
        # 它只能由人类在 WebUI「部署」注册到网关（写 NR 子流程实例 + 入 subflow_registry）。
        if p.kind == "subflow":
            raise ValueError("子流程提案不能升格为经验 skill，请在 WebUI 点「部署」注册到网关")
        if p.status == "rejected":
            raise ValueError("已拒绝的提案不能升格")
        if p.status == "public":
            return p  # 已是最高级
        nxt = STATUS_FLOW[p.status]
        decided = _now()
        public_path = None
        if nxt == "public":
            public_path = self._write_public(p)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE proposals SET status=?, decided_at=?, reviewer=?, public_path=? WHERE id=?",
                    (nxt, decided, reviewer, public_path, pid),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(pid)

    def reject(self, pid: str, reviewer: str = "human", reason: str = "") -> Proposal:
        p = self.get(pid)
        if p is None:
            raise KeyError(f"提案不存在: {pid}")
        if p.status == "public":
            raise ValueError("已升格为 public 的提案不能拒绝")
        content = p.content
        if reason:
            content = p.content + f"\n\n> 拒绝理由: {reason}"
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE proposals SET status='rejected', decided_at=?, reviewer=?, content=? WHERE id=?",
                    (_now(), reviewer, content, pid),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(pid)

    def mark_deployed(self, pid: str, flow_id: str) -> Optional[Proposal]:
        """记录提案对应的已部署 NR flow id。"""
        if self.get(pid) is None:
            return None
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE proposals SET deployed_flow_id=? WHERE id=?", (flow_id, pid))
                conn.commit()
            finally:
                conn.close()
        return self.get(pid)

    def clear_deployed(self, pid: str) -> None:
        """撤回后清除已部署标记。"""
        if self.get(pid) is None:
            return
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE proposals SET deployed_flow_id=NULL WHERE id=?", (pid,))
                conn.commit()
            finally:
                conn.close()

    def archive(self, pid: str) -> Optional[Proposal]:
        """标记提案为已归档（archived_at=now）。归档后默认从 list()/count()/诊断计数中隐藏。"""
        if self.get(pid) is None:
            return None
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE proposals SET archived_at=? WHERE id=?", (_now(), pid))
                conn.commit()
            finally:
                conn.close()
        return self.get(pid)

    def unarchive(self, pid: str) -> Optional[Proposal]:
        """取消归档（archived_at=NULL）。"""
        if self.get(pid) is None:
            return None
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE proposals SET archived_at=NULL WHERE id=?", (pid,))
                conn.commit()
            finally:
                conn.close()
        return self.get(pid)

    def delete(self, pid: str) -> bool:
        """物理删除一条提案（含已升格 public 的落盘文档）。返回是否存在并删除。

        注意：仅删提案记录；若提案曾部署到 NR（deployed_flow_id 非空），
        对应 NR flow 不会被自动撤回，需到「已部署」面板手动 undeploy，避免孤儿 flow。"""
        p = self.get(pid)
        if p is None:
            return False
        # 清理已升格 public 的落盘文档
        if p.public_path and os.path.exists(p.public_path):
            try:
                os.remove(p.public_path)
            except OSError:
                pass
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM proposals WHERE id=?", (pid,))
                conn.commit()
            finally:
                conn.close()
        return True

    def purge_test_proposals(self, dry_run: bool = True) -> Dict[str, Any]:
        """安全清理：仅删除 test/infra 身份的提案。

        这些身份在用户面 list() 默认隐藏（见 _is_test_agent），属测试/评测噪声，
        非真实 agent 留下的经验。真实 agent 提案（agt_* 等）绝不触碰。
        返回 {dry_run, count, ids}；dry_run=True 只报告不删。
        """
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT id, agent_id FROM proposals").fetchall()
            finally:
                conn.close()
        targets = [r["id"] for r in rows if _is_test_agent(r["agent_id"])]
        if not dry_run and targets:
            with self._lock:
                conn = self._conn()
                try:
                    conn.executemany(
                        "DELETE FROM proposals WHERE id=?", [(t,) for t in targets]
                    )
                    conn.commit()
                finally:
                    conn.close()
        return {"dry_run": dry_run, "count": len(targets), "ids": targets}

    def auto_archive(self, dry_run: bool = True, decided_days: int = 90) -> Dict[str, Any]:
        """批量归档「已决策且距今 > decided_days 天」的提案。

        raw（decided_at 为空）永不归档；仅动已 promote/reject 的。归档后默认从
        list()/count()/诊断计数隐藏，相当于「退休」。
        默认 dry_run=True 只报告候选、不写库；PM 确认分布后再 dry_run=False 落盘。
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=decided_days)).isoformat()
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id FROM proposals "
                    "WHERE archived_at IS NULL AND decided_at IS NOT NULL AND decided_at < ?",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()
        ids = [r["id"] for r in rows]
        if not dry_run and ids:
            with self._lock:
                conn = self._conn()
                try:
                    now = _now()
                    conn.executemany(
                        "UPDATE proposals SET archived_at=? WHERE id=?",
                        [(now, i) for i in ids],
                    )
                    conn.commit()
                finally:
                    conn.close()
        return {"dry_run": dry_run, "decided_days": decided_days,
                "count": len(ids), "ids": ids}

    def analyze_raw_noise(self, dry_run: bool = True) -> Dict[str, Any]:
        """对 raw 提案做噪声分布统计（仅报告，绝不删）。

        分类：
        - test 身份 raw（默认在用户面隐藏的测试/基础设施身份）
        - 重复 content_hash 的「多余副本」数（每组保留最早一条）
        - 空 content_hash 数（无法去重判定）
        - 按 agent_id 的 top-N 分布
        返回分布供 PM 决策是否清理。dry_run 保留以与 purge 接口对称。
        """
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, agent_id, content_hash FROM proposals WHERE status='raw'"
                ).fetchall()
            finally:
                conn.close()
        total = len(rows)
        test_ids = [r["id"] for r in rows if _is_test_agent(r["agent_id"])]
        by_hash: Dict[str, List[str]] = {}
        for r in rows:
            h = r["content_hash"]
            if h:
                by_hash.setdefault(h, []).append(r["id"])
        dup_groups = sum(1 for ids in by_hash.values() if len(ids) > 1)
        dup_extra = sum(len(ids) - 1 for ids in by_hash.values() if len(ids) > 1)
        empty_hash = sum(1 for r in rows if not r["content_hash"])
        by_agent: Dict[str, int] = {}
        for r in rows:
            by_agent[r["agent_id"]] = by_agent.get(r["agent_id"], 0) + 1
        top_agents = sorted(by_agent.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "dry_run": dry_run,
            "total_raw": total,
            "test_raw": len(test_ids),
            "non_test_raw": total - len(test_ids),
            "duplicate_groups": dup_groups,
            "duplicate_extra": dup_extra,
            "empty_hash": empty_hash,
            "top_agents": [{"agent_id": a, "count": c} for a, c in top_agents],
            "test_ids_sample": test_ids[:20],
        }

    # ── 升格落盘：公用 skill 文档 ──
    def _write_public(self, p: Proposal) -> str:
        out_dir = os.path.join(self.cfg.data_dir, self.cfg.env, "experience", "public")
        os.makedirs(out_dir, exist_ok=True)
        slug = f"{_slug(p.title)}-{p.id[3:]}"
        path = os.path.join(out_dir, f"{slug}.md")
        kind_label = {
            "skill": "经验 Skill", "convention": "约定", "fix": "缺陷修复", "idea": "想法",
        }.get(p.kind, p.kind)
        front = (
            "---\n"
            f"title: \"{p.title}\"\n"
            f"kind: {p.kind}\n"
            f"source_agent: {p.agent_id}\n"
            f"status: public\n"
            f"created: {p.created_at}\n"
            f"tags: {json.dumps(p.tags, ensure_ascii=False)}\n"
            "---\n\n"
            f"# {p.title}\n\n"
            f"> 类型：{kind_label} ｜ 来源 agent：{p.agent_id} ｜ 升格：{_now()}\n\n"
            f"{p.content}\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(front)
        return path

    @staticmethod
    def _row_to_proposal(r) -> Proposal:
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        return Proposal(
            id=r["id"],
            agent_id=r["agent_id"],
            title=r["title"],
            kind=r["kind"],
            content=r["content"],
            status=r["status"],
            tags=tags,
            created_at=r["created_at"],
            decided_at=r["decided_at"],
            reviewer=r["reviewer"],
            public_path=r["public_path"],
            deployed_flow_id=r["deployed_flow_id"] if "deployed_flow_id" in r.keys() else None,
            source=(r["source"] or "unknown") if "source" in r.keys() else "unknown",
            spec=(r["spec"] or "") if "spec" in r.keys() else "",
            archived_at=r["archived_at"] if "archived_at" in r.keys() else None,
        )
