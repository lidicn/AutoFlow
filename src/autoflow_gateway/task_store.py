#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — DSL 验证任务池（分布式多 agent 协作的单一真相源）

设计（使用者视角）：
  · tasks 表        —— 任务定义，发布后只读。每条 = 一个场景(scene) + 已注入的真实实体 hint
                       + 需用到的子流程 hint + 期望后置条件。
  · task_claims 表  —— 每个 agent 对同一任务的「一次领用尝试」。主键 (task_id, agent_id)，
                       故多名 agent 可各自独立把同一任务做一遍（拿到多样化的 DSL 写法），
                       彼此不抢占；单个 agent 断点续传靠它自己的 claim 行。

发布（管理员，autoflow_publish_tasks）：把 prompt_bank 场景 + 策划的实体映射灌入 tasks，
并即时用 device_catalog.json 把 entity_id 富化成 {friendly_name, domain, area, possible_states}。

领用（agent，autoflow_claim_task）：优先返回「本 agent 已领用但未提交」的任务（断点续传）；
否则挑一条本 agent 尚无 claim 行的新任务，标记为 claimed 返回。

提交（agent，autoflow_submit_result）：网关即时校验 DSL（编译+lint+可选 staging 闸门），
把结果(result_kind / error_msg / node_count / gate_passed) 写回该 agent 的 claim 行。

统计（管理员，autoflow_pool_stats）：按 tier / result_kind 聚合，给 DSL 引擎迭代指路。

所有写操作落 SQLite autoflow.db，跨进程/重启持久。
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .config import get_config

# 域名 → 可能状态（与 gateway._DOMAIN_POSSIBLE_STATES 保持一致；task_store 离线复用，不依赖 Gateway 实例）
_DOMAIN_POSSIBLE_STATES = {
    "light": ["on", "off"],
    "switch": ["on", "off"],
    "input_boolean": ["on", "off"],
    "fan": ["on", "off"],
    "cover": ["open", "closed"],
    "lock": ["locked", "unlocked"],
    "climate": ["heat", "cool", "off", "auto", "dry", "fan_only"],
    "media_player": ["playing", "paused", "idle", "off"],
    "vacuum": ["cleaning", "docked", "idle", "paused"],
    "binary_sensor": ["on", "off"],
}

# 域名 → 常用 HA 服务（与 _DOMAIN_POSSIBLE_STATES 同位；供 hint 附带「目标 service」，
# 让 agent 只填空不猜调用，消除「漏填 entity_id / 调错服务」头号失败源，见 #P2-4）。
_DOMAIN_SERVICES = {
    "light": ["light.turn_on", "light.turn_off", "light.toggle"],
    "switch": ["switch.turn_on", "switch.turn_off", "switch.toggle"],
    "input_boolean": ["input_boolean.turn_on", "input_boolean.turn_off", "input_boolean.toggle"],
    "fan": ["fan.turn_on", "fan.turn_off", "fan.set_percentage", "fan.toggle"],
    "cover": ["cover.open_cover", "cover.close_cover", "cover.stop_cover", "cover.toggle"],
    "lock": ["lock.lock", "lock.unlock"],
    "climate": ["climate.set_temperature", "climate.set_hvac_mode", "climate.turn_on", "climate.turn_off"],
    "media_player": ["media_player.turn_on", "media_player.turn_off", "media_player.play_media", "media_player.media_play"],
    "vacuum": ["vacuum.start", "vacuum.pause", "vacuum.return_to_base", "vacuum.stop"],
    "humidifier": ["humidifier.turn_on", "humidifier.turn_off", "humidifier.set_humidity"],
    "scene": ["scene.turn_on"],
    "script": ["script.turn_on"],
    "automation": ["automation.turn_on", "automation.turn_off", "automation.trigger"],
    "group": ["group.turn_on", "group.turn_off", "group.toggle"],
    "input_number": ["input_number.set_value"],
    "input_select": ["input_select.select_option"],
    "input_text": ["input_text.set_value"],
    "number": ["number.set_value"],
    "select": ["select.select_option"],
    "button": ["button.press"],
    "timer": ["timer.start", "timer.cancel", "timer.finish"],
    "alarm_control_panel": ["alarm_control_panel.alarm_arm_away", "alarm_control_panel.alarm_disarm"],
    # 只读域：无写入服务
    "sensor": [], "binary_sensor": [], "person": [], "device_tracker": [],
    "zone": [], "sun": [], "weather": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _possible_states(domain: Optional[str]) -> List[str]:
    return _DOMAIN_POSSIBLE_STATES.get(domain or "", [])


def _domain_services(domain: Optional[str]) -> List[str]:
    """#P2-4：返回该域常用的 HA 服务清单，供任务 hint 直接附带「目标 service」。"""
    return _DOMAIN_SERVICES.get(domain or "", [])


def load_catalog(catalog_path: str) -> dict:
    """读取 device_catalog.json → {entity_id: meta}。文件缺失返回空 dict。"""
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        ents = d.get("entities", {})
        if isinstance(ents, list):
            # 兼容列表形态
            return {e.get("entity_id"): e for e in ents if isinstance(e, dict)}
        return ents or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


class TaskStore:
    _lock = threading.Lock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.cfg.data_dir, "autoflow.db")
        self._init_db()

    # ───────────── DB ─────────────
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS tasks (
                        id            TEXT PRIMARY KEY,
                        tier          TEXT NOT NULL DEFAULT '',
                        scene_name    TEXT NOT NULL DEFAULT '',
                        task_text     TEXT NOT NULL,
                        entity_hint   TEXT NOT NULL DEFAULT '[]',
                        subflow_hint  TEXT NOT NULL DEFAULT '[]',
                        expected      TEXT NOT NULL DEFAULT '[]',
                        created_at    TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS task_claims (
                        task_id      TEXT NOT NULL,
                        agent_id     TEXT NOT NULL,
                        status       TEXT NOT NULL DEFAULT 'claimed',
                        submitted_dsl TEXT,
                        result_kind  TEXT,
                        error_msg    TEXT,
                        node_count   INTEGER,
                        gate_passed  INTEGER,
                        retries      INTEGER NOT NULL DEFAULT 0,
                        claimed_at   TEXT NOT NULL,
                        updated_at   TEXT NOT NULL,
                        PRIMARY KEY (task_id, agent_id)
                    )"""
                )
                # 缺陷/建议上报表（agent → 人类 backlog）：与 task_claims 平级，跨进程/重启持久。
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS issues (
                        issue_id   TEXT PRIMARY KEY,
                        agent_id   TEXT NOT NULL,
                        task_id    TEXT,
                        severity   TEXT NOT NULL DEFAULT 'medium',
                        category   TEXT NOT NULL DEFAULT 'defect',
                        title      TEXT NOT NULL,
                        body       TEXT NOT NULL,
                        status     TEXT NOT NULL DEFAULT 'open',
                        created_at TEXT NOT NULL
                    )"""
                )
                # 子流程注册表（#575）：网关子流程 link API 的单一真相源。
                # managed=网关预置(bark_push/history_* 等)；imported=用户从 NR 自省导入。
                # 编译器 / WebUI / MCP 三方均查此表，取代硬编码特判（flow_uses_*）。
                # key=DSL 调用名（调用子流程 <key>），主键；status: active|pending_review|disabled。
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS subflow_registry (
                        key              TEXT PRIMARY KEY,
                        title            TEXT NOT NULL DEFAULT '',
                        nr_subflow_id    TEXT,
                        source_type      TEXT NOT NULL DEFAULT 'imported',
                        input_schema     TEXT NOT NULL DEFAULT '[]',
                        env_requirements TEXT NOT NULL DEFAULT '[]',
                        owner            TEXT NOT NULL DEFAULT '',
                        registered_at    TEXT NOT NULL,
                        status           TEXT NOT NULL DEFAULT 'active',
                        spec_ref         TEXT,
                        kind             TEXT NOT NULL DEFAULT 'subflow',
                        entry_link_id    TEXT
                    )"""
                )
                # 加固字段（#272）：任务是否要求条件分支（如果…才…）。
                # 默认 0=不强制；置 1 后 verify_task_dsl 会拦截『无分支节点』的提交。
                try:
                    conn.execute(
                        "ALTER TABLE tasks ADD COLUMN requires_branch "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                except Exception:
                    pass  # 列已存在则忽略（SQLite 不支持 IF NOT EXISTS）
                # 子流程注册表扩展（#585）：支持 link_out 型能力（fire-and-forget，
                # 网关只发 link out 到 entry_link_id，无 NR 子流程实例）。
                try:
                    conn.execute(
                        "ALTER TABLE subflow_registry ADD COLUMN kind "
                        "TEXT NOT NULL DEFAULT 'subflow'"
                    )
                except Exception:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE subflow_registry ADD COLUMN entry_link_id TEXT"
                    )
                except Exception:
                    pass
                conn.commit()
            finally:
                conn.close()

    # ───────────── 实体 hint 富化 ─────────────
    def _enrich_entities(self, entity_ids: List[str], catalog: dict) -> List[dict]:
        """把 entity_id 列表富化成 hint（含 friendly_name/domain/area/possible_states）。
        目录里找不到的 ID 仍保留 entity_id，并标 missing=True，提醒发布方核查。"""
        out = []
        for eid in entity_ids or []:
            if not eid:
                continue
            meta = catalog.get(eid)
            if meta:
                dom = meta.get("domain")
                out.append({
                    "entity_id": eid,
                    "friendly_name": meta.get("friendly_name"),
                    "domain": dom,
                    "area": meta.get("area") or "",
                    "possible_states": _possible_states(dom),
                    # #P2-4（2026-07-24）：hint 附带「目标 service」，
                    # 让 agent 只填空不猜调用（消除漏填 entity_id / 调错服务 头号失败源）。
                    "target_service": _domain_services(dom),
                })
            else:
                out.append({"entity_id": eid, "missing": True, "target_service": []})
        return out

    # ───────────── 发布 ─────────────
    def publish(self, scenes: List[dict], catalog_path: Optional[str] = None) -> dict:
        """灌入任务池。scenes 元素字段：
          {id, tier, scene_name?, task, entities?:[entity_id...],
           subflows?:[name...], expected?:[{entity_id,state}|{subflow}]}
        entity_hint 在发布时即时从 catalog 富化。返回 {inserted, skipped(已存在), errors}。"""
        if catalog_path is None:
            catalog_path = os.path.join(self.cfg.data_dir, "staging", "state", "device_catalog.json")
        catalog = load_catalog(catalog_path)

        inserted, skipped, errors = 0, 0, []
        with self._lock:
            conn = self._conn()
            try:
                for sc in scenes:
                    tid = sc.get("id") or sc.get("key")
                    if not tid:
                        errors.append({"error": "场景缺 id", "scene": sc})
                        continue
                    tier = sc.get("tier") or (tid.split("_")[0] if "_" in tid else "")
                    task_text = sc.get("task") or sc.get("task_text") or ""
                    entities = sc.get("entities") or []
                    subflows = sc.get("subflows") or []
                    expected = sc.get("expected") or []
                    rb = int(sc.get("requires_branch", 0) or 0)
                    ent_hint = self._enrich_entities(entities, catalog)
                    now = _now()
                    conn.execute(
                        """INSERT INTO tasks (id, tier, scene_name, task_text, entity_hint,
                                              subflow_hint, expected, requires_branch, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET
                             tier=excluded.tier,
                             scene_name=excluded.scene_name,
                             task_text=excluded.task_text,
                             entity_hint=excluded.entity_hint,
                             subflow_hint=excluded.subflow_hint,
                             expected=excluded.expected,
                             requires_branch=excluded.requires_branch,
                             created_at=excluded.created_at""",
                        (tid, tier, sc.get("scene_name", "") or tid,
                         task_text, json.dumps(ent_hint, ensure_ascii=False),
                         json.dumps(subflows, ensure_ascii=False),
                         json.dumps(expected, ensure_ascii=False), rb, now),
                    )
                    inserted += 1
                conn.commit()
            finally:
                conn.close()
        return {"ok": True, "inserted": inserted, "skipped": skipped, "errors": errors}

    # ───────────── 领用 ─────────────
    def claim(self, agent_id: str, prefer_mine: bool = True,
               tier: Optional[str] = None) -> Optional[dict]:
        """领用一条任务。
          · prefer_mine=True 时先返回『本 agent 已领用但未提交』的任务（断点续传）；
          · 否则/都没有时，挑一条本 agent 尚无 claim 行的新任务，标记 claimed 返回；
          · tier 指定时只在对应 tier 内挑（黑箱 auto / 白箱 auto_wb），实现身份隔离，
            避免黑箱 agent 误领白箱专属的原生节点任务；
          · 全部任务本 agent 都做过了 → 返回 None（让 agent 知道可以收工或换身份）。"""
        with self._lock:
            conn = self._conn()
            try:
                if prefer_mine:
                    sql = ("SELECT task_id FROM task_claims "
                           "WHERE agent_id=? AND status='claimed'")
                    args = [agent_id]
                    if tier:
                        sql += " AND task_id IN (SELECT id FROM tasks WHERE tier=?)"
                        args.append(tier)
                    sql += " ORDER BY updated_at LIMIT 1"
                    r = conn.execute(sql, tuple(args)).fetchone()
                    if r:
                        return self._get_task(r["task_id"], agent_id, conn)
                # 挑本 agent 尚无 claim 行的任务（按 tier,id 稳定顺序）
                sql = ("SELECT t.id FROM tasks t "
                       "LEFT JOIN task_claims c ON c.task_id=t.id AND c.agent_id=? "
                       "WHERE c.task_id IS NULL")
                args = [agent_id]
                if tier:
                    sql += " AND t.tier=?"
                    args.append(tier)
                sql += " ORDER BY t.tier, t.id LIMIT 1"
                r = conn.execute(sql, tuple(args)).fetchone()
                if r:
                    now = _now()
                    conn.execute(
                        """INSERT INTO task_claims (task_id, agent_id, status, retries,
                                                    claimed_at, updated_at)
                           VALUES (?,?, 'claimed', 0, ?, ?)
                           ON CONFLICT(task_id, agent_id) DO UPDATE SET
                             status='claimed', updated_at=excluded.updated_at""",
                        (r["id"], agent_id, now, now),
                    )
                    conn.commit()
                    return self._get_task(r["id"], agent_id, conn)
                return None
            finally:
                conn.close()

    def claim_specific(self, agent_id: str, task_id: str,
                       allowed_tiers: Optional[list] = None) -> Optional[dict]:
        """直领指定 task_id（供 agent 认领已知新任务，避免随机轮询）。
        - allowed_tiers：调用方按身份模式算出的允许 tier 列表；任务 tier 不在其中→返回 None（防串味/越权）。
        - 已 claimed/submitted 的幂等返回该任务（便于断点续传）。
        - 任务不存在或 tier 不允许 → 返回 None。"""
        with self._lock:
            conn = self._conn()
            try:
                t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if t is None:
                    return None
                if allowed_tiers and t["tier"] not in allowed_tiers:
                    return None
                now = _now()
                conn.execute(
                    """INSERT INTO task_claims (task_id, agent_id, status, retries,
                                                claimed_at, updated_at)
                       VALUES (?,?, 'claimed', 0, ?, ?)
                       ON CONFLICT(task_id, agent_id) DO UPDATE SET
                         status='claimed', updated_at=excluded.updated_at""",
                    (task_id, agent_id, now, now),
                )
                conn.commit()
                return self._get_task(task_id, agent_id, conn)
            finally:
                conn.close()

    # ───────────── 提交 ─────────────
    def submit(self, task_id: str, agent_id: str, dsl: str,
               result_kind: str, error_msg: str = "", node_count: int = 0,
               gate_passed: Optional[int] = None) -> dict:
        """记录某 agent 对某任务的提交结果。upsert 到该 agent 的 claim 行。"""
        with self._lock:
            conn = self._conn()
            try:
                now = _now()
                conn.execute(
                    """INSERT INTO task_claims (task_id, agent_id, status, submitted_dsl,
                                                result_kind, error_msg, node_count,
                                                gate_passed, retries, claimed_at, updated_at)
                       VALUES (?,?, 'submitted', ?,?,?,?,?, 0, ?, ?)
                       ON CONFLICT(task_id, agent_id) DO UPDATE SET
                         status='submitted',
                         submitted_dsl=excluded.submitted_dsl,
                         result_kind=excluded.result_kind,
                         error_msg=excluded.error_msg,
                         node_count=excluded.node_count,
                         gate_passed=excluded.gate_passed,
                         retries=excluded.retries + 1,
                         updated_at=excluded.updated_at""",
                    (task_id, agent_id, dsl, result_kind, error_msg, node_count,
                     gate_passed, now, now),
                )
                conn.commit()
                r = conn.execute(
                    "SELECT * FROM task_claims WHERE task_id=? AND agent_id=?",
                    (task_id, agent_id),
                ).fetchone()
                return self._row_to_dict(r)
            finally:
                conn.close()

    # ───────────── 缺陷/建议上报（agent → 人类 backlog）─────────────
    _ISSUE_SEVERITIES = ("low", "medium", "high", "critical")
    _ISSUE_CATEGORIES = ("defect", "doc", "dsl", "entity", "feature", "other")
    _ISSUE_STATUSES = ("open", "ack", "resolved", "wontfix")

    def report_issue(self, agent_id: str, title: str, body: str,
                     task_id: Optional[str] = None,
                     severity: str = "medium", category: str = "defect") -> dict:
        """登记一条 agent 发现的缺陷/建议。返回 {ok, issue_id} 或 {ok:False, error}。
        - severity：low|medium|high|critical
        - category：defect(缺陷)|doc(文档)|dsl(语法)|entity(实体解析)|feature(新需求)|other"""
        if not agent_id:
            return {"ok": False, "error": "agent_id 必填"}
        if not title or not title.strip():
            return {"ok": False, "error": "title 必填"}
        if not body or not body.strip():
            return {"ok": False, "error": "body 必填"}
        sev = (severity or "medium").lower()
        cat = (category or "defect").lower()
        if sev not in self._ISSUE_SEVERITIES:
            return {"ok": False, "error": f"severity 须为 {self._ISSUE_SEVERITIES}"}
        if cat not in self._ISSUE_CATEGORIES:
            return {"ok": False, "error": f"category 须为 {self._ISSUE_CATEGORIES}"}
        issue_id = "iss_" + uuid.uuid4().hex[:10]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO issues (issue_id, agent_id, task_id, severity,
                                           category, title, body, status, created_at)
                       VALUES (?,?,?,?,?,?,?, 'open', ?)""",
                    (issue_id, agent_id, task_id, sev, cat,
                     title.strip(), body.strip(), now),
                )
                conn.commit()
                return {"ok": True, "issue_id": issue_id}
            finally:
                conn.close()

    def list_issues(self, status: Optional[str] = None,
                    agent_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        """列出 issue（按创建时间倒序）。status 可过滤 open/ack/resolved/wontfix。"""
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM issues"
                where, args = [], []
                if status:
                    where.append("status=?")
                    args.append(status)
                if agent_id:
                    where.append("agent_id=?")
                    args.append(agent_id)
                sql += (" WHERE " + " AND ".join(where)) if where else ""
                sql += " ORDER BY created_at DESC LIMIT ?"
                args.append(int(limit))
                rows = conn.execute(sql, args).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def resolve_issue(self, issue_id: str, status: str) -> dict:
        """更新 issue 状态（人类审阅闭环）。status ∈ open/ack/resolved/wontfix。"""
        if status not in self._ISSUE_STATUSES:
            return {"ok": False, "error": f"status 须为 {self._ISSUE_STATUSES}"}
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE issues SET status=? WHERE issue_id=?",
                    (status, issue_id),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return {"ok": False, "error": f"issue 不存在: {issue_id}"}
                return {"ok": True, "issue_id": issue_id, "status": status}
            finally:
                conn.close()

    # ───────────── 子流程注册表（#575）─────────────
    # 单一真相源：取代 subflows.py 里 flow_uses_bark_subflow / flow_uses_history_subflow
    # 的硬编码特判。编译器 get_subflow、WebUI /subflows、MCP 工具均查此表。
    #   - managed  ：网关预置（bark_push / history_* 等），启动时种子写入。
    #   - imported ：用户从 NR 子流程自省导入（introspect + register）。
    # input_schema：JSON 列表 [{name,required,type,default,enum,desc}]（前置参数，界面可查）。
    # env_requirements：JSON 列表（env 变量名字符串），导入时由 NR subflow env 自省。
    _SUBFLOW_SOURCE_TYPES = ("managed", "imported")
    _SUBFLOW_STATUSES = ("active", "pending_review", "disabled")
    _SUBFLOW_KINDS = ("subflow", "link_out")

    def register_subflow(self, key, title="", nr_subflow_id=None,
                         source_type="imported", input_schema=None,
                         env_requirements=None, owner="",
                         status="active", spec_ref=None,
                         kind="subflow", entry_link_id=None) -> dict:
        """登记 / 覆盖一条子流程元数据（upsert by key）。
        - key：DSL 调用名（调用子流程 <key>），主键，必填。
        - input_schema：参数列表 [{name,required,type,default,enum,desc}]；None/空 → '[]'。
        - env_requirements：env 变量名字符串列表；None/空 → '[]'。
        - source_type：managed（网关预置）| imported（用户导入）。
        - status：active | pending_review | disabled。
        - kind：subflow（NR 子流程实例，需 nr_subflow_id）| link_out（fire-and-forget，
          网关发 link out 到 entry_link_id，无 NR 子流程实例）。
        返回 {ok, key} 或 {ok:False, error}。"""
        if not key or not str(key).strip():
            return {"ok": False, "error": "key 必填"}
        st = (source_type or "imported").lower()
        if st not in self._SUBFLOW_SOURCE_TYPES:
            return {"ok": False, "error": f"source_type 须为 {self._SUBFLOW_SOURCE_TYPES}"}
        stat = (status or "active").lower()
        if stat not in self._SUBFLOW_STATUSES:
            return {"ok": False, "error": f"status 须为 {self._SUBFLOW_STATUSES}"}
        kd = (kind or "subflow").lower()
        if kd not in self._SUBFLOW_KINDS:
            return {"ok": False, "error": f"kind 须为 {self._SUBFLOW_KINDS}"}
        if kd == "subflow" and not nr_subflow_id:
            return {"ok": False, "error": "kind=subflow 必须提供 nr_subflow_id"}
        if kd == "link_out" and not entry_link_id:
            return {"ok": False, "error": "kind=link_out 必须提供 entry_link_id"}
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO subflow_registry
                       (key, title, nr_subflow_id, source_type, input_schema,
                        env_requirements, owner, registered_at, status, spec_ref,
                        kind, entry_link_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         title=excluded.title,
                         nr_subflow_id=excluded.nr_subflow_id,
                         source_type=excluded.source_type,
                         input_schema=excluded.input_schema,
                         env_requirements=excluded.env_requirements,
                         owner=excluded.owner,
                         registered_at=excluded.registered_at,
                         status=excluded.status,
                         spec_ref=excluded.spec_ref,
                         kind=excluded.kind,
                         entry_link_id=excluded.entry_link_id""",
                    (str(key).strip(), title or "", nr_subflow_id,
                     st, json.dumps(input_schema or [], ensure_ascii=False),
                     json.dumps(env_requirements or [], ensure_ascii=False),
                     owner or "", now, stat, spec_ref, kd, entry_link_id or None),
                )
                conn.commit()
                return {"ok": True, "key": str(key).strip()}
            finally:
                conn.close()

    def list_subflows(self, source_type: Optional[str] = None,
                      status: Optional[str] = None) -> List[dict]:
        """列出注册子流程（按 key 升序）。source_type / status 可过滤。"""
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM subflow_registry"
                where, args = [], []
                if source_type:
                    where.append("source_type=?")
                    args.append(source_type)
                if status:
                    where.append("status=?")
                    args.append(status)
                sql += (" WHERE " + " AND ".join(where)) if where else ""
                sql += " ORDER BY key"
                rows = conn.execute(sql, args).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def get_subflow_meta(self, key) -> Optional[dict]:
        """读取单条子流程元数据（含已解析的 input_schema / env_requirements 列表）。"""
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT * FROM subflow_registry WHERE key=?",
                    (str(key),)).fetchone()
                return self._row_to_dict(r) if r else None
            finally:
                conn.close()

    def set_subflow_status(self, key, status: str) -> dict:
        """仅更新某条子流程的 status（active / disabled / pending_review）。

        返回 {ok} 或 {ok:False, error}。managed 预置的状态由 seed 管理，
        调用方（WebUI）应拒绝对其手动变更；本方法仅做存在性与枚举校验。
        """
        if not key:
            return {"ok": False, "error": "key 必填"}
        stat = (status or "").lower()
        if stat not in self._SUBFLOW_STATUSES:
            return {"ok": False, "error": f"status 须为 {self._SUBFLOW_STATUSES}"}
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE subflow_registry SET status=? WHERE key=?",
                    (stat, str(key)))
                conn.commit()
                if cur.rowcount == 0:
                    return {"ok": False, "error": f"子流程不存在: {key}"}
                return {"ok": True}
            finally:
                conn.close()

    def delete_subflow(self, key) -> dict:
        """删除一条注册（谨慎）。managed 预置如需回退用 status='disabled' 而非物理删除。
        返回 {ok, deleted} 或 {ok:False, error}。"""
        if not key:
            return {"ok": False, "error": "key 必填"}
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM subflow_registry WHERE key=?", (str(key),))
                conn.commit()
                if cur.rowcount == 0:
                    return {"ok": False, "error": f"子流程不存在: {key}"}
                return {"ok": True, "deleted": str(key)}
            finally:
                conn.close()

    # ───────────── 查询 ─────────────
    def _get_task(self, task_id: str, agent_id: Optional[str], conn) -> Optional[dict]:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if r is None:
            return None
        t = self._row_to_dict(r)
        if agent_id:
            c = conn.execute(
                "SELECT * FROM task_claims WHERE task_id=? AND agent_id=?",
                (task_id, agent_id),
            ).fetchone()
            if c:
                cc = self._row_to_dict(c)
                t["my_status"] = cc.get("status")
                t["my_result"] = cc.get("result_kind")
                t["my_error"] = cc.get("error_msg")
                t["my_node_count"] = cc.get("node_count")
                t["my_gate_passed"] = cc.get("gate_passed")
        return t

    def list(self, agent_id: Optional[str] = None, status: Optional[str] = None,
             tier: Optional[str] = None, include_dsl: bool = False) -> List[dict]:
        """列出任务。带 agent_id 时附带该 agent 的领用/提交状态。
        status 过滤作用于『该 agent 的 claim 状态』(claimed/submitted/None)。"""
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM tasks"
                where, args = [], []
                if tier:
                    where.append("tier=?")
                    args.append(tier)
                sql = sql + (" WHERE " + " AND ".join(where) if where else "")
                sql += " ORDER BY tier, id"
                rows = conn.execute(sql, args).fetchall()
                out = []
                for r in rows:
                    t = self._row_to_dict(r)
                    if agent_id:
                        c = conn.execute(
                            "SELECT * FROM task_claims WHERE task_id=? AND agent_id=?",
                            (t["id"], agent_id),
                        ).fetchone()
                        my = self._row_to_dict(c) if c else None
                        t["my_status"] = my.get("status") if my else None
                        t["my_result"] = my.get("result_kind") if my else None
                        t["my_error"] = my.get("error_msg") if my else None
                        t["my_gate_passed"] = my.get("gate_passed") if my else None
                        if status and t["my_status"] != status:
                            continue
                    if not include_dsl:
                        t.pop("submitted_dsl", None)
                    out.append(t)
                return out
            finally:
                conn.close()

    def get(self, task_id: str, agent_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            conn = self._conn()
            try:
                return self._get_task(task_id, agent_id, conn)
            finally:
                conn.close()

    def get_submission(self, task_id: str, agent_id: str) -> Optional[dict]:
        """读取指定 (task_id, agent_id) 的提交行（含 submitted_dsl 全文）。

        与 get() 不同：get() 出于性能/体积考虑会剔除 submitted_dsl，本方法
        专门用于人工抽查等需要回看 agent 实际提交 DSL 的场景。无提交返回 None。
        """
        with self._lock:
            conn = self._conn()
            try:
                c = conn.execute(
                    "SELECT * FROM task_claims WHERE task_id=? AND agent_id=?",
                    (task_id, agent_id),
                ).fetchone()
                if c is None:
                    return None
                return self._row_to_dict(c)
            finally:
                conn.close()

    def list_submissions(self, tier: Optional[str] = None,
                         agent_id: Optional[str] = None,
                         task_id: Optional[str] = None,
                         result_kind: Optional[str] = None,
                         limit: int = 200, include_dsl: bool = False) -> List[dict]:
        """列出任务提交(claim)记录，可按 tier / agent_id / task_id / result_kind 过滤。
        用于管理员回归闭环：定向拉取某 agent 的失败提交、或某任务的跨 agent 结果对比。
        - include_dsl=False 时剔除 submitted_dsl（避免大 payload）；抽查时置 True。"""
        with self._lock:
            conn = self._conn()
            try:
                sql = ("SELECT c.*, t.tier AS task_tier "
                       "FROM task_claims c LEFT JOIN tasks t ON t.id=c.task_id")
                where, args = [], []
                if tier:
                    where.append("t.tier=?")
                    args.append(tier)
                if agent_id:
                    where.append("c.agent_id=?")
                    args.append(agent_id)
                if task_id:
                    where.append("c.task_id=?")
                    args.append(task_id)
                if result_kind:
                    where.append("c.result_kind=?")
                    args.append(result_kind)
                sql += (" WHERE " + " AND ".join(where)) if where else ""
                sql += " ORDER BY c.updated_at DESC LIMIT ?"
                args.append(int(limit))
                rows = conn.execute(sql, args).fetchall()
                out = [self._row_to_dict(r) for r in rows]
                if not include_dsl:
                    for o in out:
                        o.pop("submitted_dsl", None)
                return out
            finally:
                conn.close()

    def reset(self) -> dict:
        """清空任务池（tasks + task_claims）。谨慎使用。"""
        with self._lock:
            conn = self._conn()
            try:
                n_t = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                n_c = conn.execute("SELECT COUNT(*) FROM task_claims").fetchone()[0]
                conn.execute("DELETE FROM tasks")
                conn.execute("DELETE FROM task_claims")
                conn.commit()
                return {"ok": True, "deleted_tasks": n_t, "deleted_claims": n_c}
            finally:
                conn.close()

    def clear_tiers(self, tiers: List[str]) -> dict:
        """按 tier 清空任务（tasks + 相关 task_claims）。用于『整批替换某池』（如 auto / auto_wb），
        不影响其他 tier（hist / T1-T8 / complex / medium / cov_* 等保留）。"""
        if not tiers:
            return {"ok": True, "deleted_tasks": 0, "deleted_claims": 0, "tiers": []}
        placeholders = ",".join("?" * len(tiers))
        with self._lock:
            conn = self._conn()
            try:
                n_c = conn.execute(
                    f"DELETE FROM task_claims WHERE task_id IN "
                    f"(SELECT id FROM tasks WHERE tier IN ({placeholders}))",
                    tuple(tiers)).rowcount
                n_t = conn.execute(
                    f"DELETE FROM tasks WHERE tier IN ({placeholders})",
                    tuple(tiers)).rowcount
                conn.commit()
                return {"ok": True, "deleted_tasks": n_t, "deleted_claims": n_c, "tiers": tiers}
            finally:
                conn.close()

    def stats(self) -> dict:
        """聚合统计：任务总数、各 agent 提交数、按 result_kind 分布、按 tier 通过率。"""
        with self._lock:
            conn = self._conn()
            try:
                total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                claims = conn.execute("SELECT * FROM task_claims").fetchall()
                claims = [self._row_to_dict(c) for c in claims]
                by_result = {}
                by_agent = {}
                by_tier = {}
                submitted = 0
                for c in claims:
                    rk = c.get("result_kind") or "unknown"
                    by_result[rk] = by_result.get(rk, 0) + 1
                    a = c.get("agent_id") or "?"
                    by_agent[a] = by_agent.get(a, 0) + 1
                    if c.get("status") == "submitted":
                        submitted += 1
                # 按 tier 聚合（join tasks）
                rows = conn.execute(
                    """SELECT t.tier AS tier, c.result_kind AS rk, COUNT(*) AS n
                       FROM task_claims c JOIN tasks t ON t.id=c.task_id
                       WHERE c.status='submitted'
                       GROUP BY t.tier, c.result_kind"""
                ).fetchall()
                for r in rows:
                    t = r["tier"] or "?"
                    by_tier.setdefault(t, {"submitted": 0, "compiled": 0, "gate_pass": 0})
                    by_tier[t]["submitted"] += r["n"]
                    if r["rk"] in ("compiled", "gate_pass"):
                        by_tier[t]["compiled"] += r["n"]
                    if r["rk"] == "gate_pass":
                        by_tier[t]["gate_pass"] += r["n"]
                return {
                    "ok": True,
                    "total_tasks": total,
                    "total_claims": len(claims),
                    "submitted": submitted,
                    "by_result_kind": by_result,
                    "by_agent": by_agent,
                    "by_tier": by_tier,
                }
            finally:
                conn.close()

    # ───────────── 工具 ─────────────
    @staticmethod
    def _row_to_dict(row) -> dict:
        if row is None:
            return None
        d = dict(row)
        # 把 JSON 文本列解析出来，方便消费方（MCP 工具回传前再 json.dumps）
        for key in ("entity_hint", "subflow_hint", "expected", "submitted_dsl",
                    "input_schema", "env_requirements"):
            v = d.get(key)
            if isinstance(v, str):
                try:
                    d[key] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
