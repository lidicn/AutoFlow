#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledge_evo —— 错误知识库自进化闭环（P1 主链路回读 / P2 效果度量 / P3 失效标记）。

把 error_knowledge.py 的「采集✅ / 沉淀✅ / 回读⚠️仅竞技场+WebUI / 度量❌ / 失效❌」
半截闭环补成完整闭环，并严格遵守三条铁律：

  · C1 永不删除：失效 = 标记 resolved_*，绝不 del / pop / 截断存储；
  · C2 生产文件兼容：error_knowledge.py 零改动（只继承），gateway.py 只留
    解耦的接线缝（把本模块的 attach / try_close_loop 钩子接到 propose_dsl）；
  · C3 fail-safe：attach_prior_art / try_close_loop_success 任何异常都不得
    影响调用方主路径，异常时原样返回或静默跳过。

三目标：
  P1 attach_prior_art()：任一 ok:False 失败结果注入 prior_art（建议+相似案例+recurring）。
  P2 get_effect_funnel()：回读效果漏斗（再犯 / 恢复率 / by_error_type 聚合）。
  P3 mark_resolved() + lazy_expire()：失效标记（fixed|obsolete|false_positive）
     与版本维度懒过期（resolved_version < VERSION 自动改写 obsolete）。

仅标准库（re / datetime / typing / json / os）+ 同包 error_knowledge，与 error_knowledge.py 一致。

设计要点（与 mimo 初稿对齐，并砍掉其 scope creep）：
  · 本模块是**存储驱动、调用方解耦**的：store 由接线方（gateway / webui / 竞技场）持有并
    传入，模块本身不假设任何特定进程/入口——这正是它能安全复用于多入口的原因。
  · attach_prior_art 的顺序**直接透传** get_suggestion 的「失效下沉」语义，不做任何未请求的
    DSL 相关性重排（mimo 初稿的 _order_cases dsl 重排已砍，避免隐含顺序假设）。
  · dsl 形参保留为 reserved 透传位（供将来相似度排序扩展），当前不参与排序。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:  # 包内导入（正常 pytest / 生产形态）
    from .error_knowledge import ErrorKnowledgeStore
except ImportError:  # pragma: no cover - 直接以脚本/顶层模块方式加载时
    from error_knowledge import ErrorKnowledgeStore


# ── 常量（均为可调常量，便于演进） ───────────────────────────────────────────
VERSION = "1.0.0"                        # 知识库 schema 版本；用于懒过期判断
RECURRING_THRESHOLD = 3                   # 同一 agent 近 N 次同类错误 -> 标记 recurring
RECURRING_WINDOW_DAYS = 7                 # recurring 统计窗口（天）
REPEAT_WINDOW_HOURS = 24                  # 漏斗「同类型再犯」相邻间隔上限（小时）
SIMILAR_CASE_LIMIT = 5                    # prior_art 透传给调用方的相似案例上限
RESOLUTIONS = ("fixed", "obsolete", "false_positive")


# ── 小工具 ───────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    """宽容解析 ISO 时间戳；失败返回 None（不计窗口）。"""
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _version_tuple(version: str) -> Tuple[int, int, int]:
    """'1.0.0' -> (1,0,0)；无法解析的段按 0（宽容比较，宁可判旧）。"""
    parts: List[int] = []
    for chunk in str(version or "").strip().split("."):
        digits = re.sub(r"\D", "", chunk) or "0"
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def _is_resolved(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("resolution"))


def _is_recovered(entry: Dict[str, Any]) -> bool:
    outs = entry.get("outcomes")
    if not isinstance(outs, list) or not outs:
        return False
    last = outs[-1]
    return bool(last.get("recovered")) if isinstance(last, dict) else False


def _sanitized_case(entry: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏：只保留 id/error_type/timestamp/dsl[:120]/error[:120]，绝不带 agent 凭证。"""
    return {
        "id": str(entry.get("id", "")),
        "error_type": str(entry.get("error_type", "other")),
        "timestamp": str(entry.get("timestamp", "")),
        "dsl": str(entry.get("dsl") or "")[:120],
        "error": str(entry.get("error") or "")[:120],
    }


def _recent_same_type_count(store: Any, agent_id: str, error_type: str,
                            days: int = RECURRING_WINDOW_DAYS) -> int:
    """近 days 天内、同一 agent、同类型的错误条数（recurring 统计用）。

    内部 fail-safe：统计失败降级为 0（仍注入 prior_art，不影响主路径）。
    """
    try:
        page = store.list_errors(error_type=error_type, limit=1000)
    except Exception:
        return 0
    cutoff = _utcnow() - timedelta(days=days)
    count = 0
    for e in page.get("errors", []):
        if (e.get("agent_id") or "") != (agent_id or ""):
            continue
        ts = _parse_ts(e.get("timestamp", ""))
        if ts is None or ts < cutoff:
            continue
        count += 1
    return count


# ── P1 · 主链路回读（模块级纯函数，store 由调用方传入） ───────────────────────
def attach_prior_art(result: Dict[str, Any], store: Any,
                    agent_id: str = "", dsl: str = "") -> Dict[str, Any]:
    """把错误知识库的 prior-art 注入 propose_dsl 等失败结果。

    行为（详见 P1）：
      · 从 result 取 (error, stage)；若非 dict 或无 error 则 no-op 返回原 result。
      · 调 store.get_suggestion(error, stage) 拿静态建议 + 相似案例（后者已按
        「失效下沉」排好序）。
      · 计算 recurring：同一 agent_id 近 RECURRING_WINDOW_DAYS 天同类错误
        >= RECURRING_THRESHOLD 次 -> recurring=True。
      · 注入 result['prior_art'] = {error_type, suggestion, similar_cases,
        total_same_type, readback_attached_at, recurring, recurring_hint}。
      · 整体 try/except（C3）：任何异常返回原 result（不抛、不缺键）。

    dsl 参数：reserved 透传位，供将来 DSL 相似度排序扩展；当前不参与排序。
    返回：被注入后的 result（原地改 + 返回同一对象，只新增 prior_art 键，C2）。
    """
    try:
        if not isinstance(result, dict):
            return result
        error = result.get("error") or ""
        if not str(error).strip():
            return result  # no-op：无错误信息可回读
        stage = result.get("stage") or ""
        tip = store.get_suggestion(error, stage) or {}
        error_type = tip.get("error_type", "other")
        suggestion = tip.get("suggestion", "") or ""
        total_same_type = tip.get("total_same_type", 0)
        cases = [c for c in (tip.get("similar_cases") or []) if isinstance(c, dict)]
        cases = cases[:SIMILAR_CASE_LIMIT]  # 仅截断展示量，不动存储（C1）
        similar = [_sanitized_case(c) for c in cases]
        n = _recent_same_type_count(store, agent_id, error_type)
        recurring = n >= RECURRING_THRESHOLD
        prior_art = {
            "error_type": error_type,
            "suggestion": suggestion,
            "similar_cases": similar,
            "total_same_type": total_same_type,
            "readback_attached_at": _utcnow_iso(),
            "recurring": bool(recurring),
            "recurring_hint": (
                f"该 agent 近 {RECURRING_WINDOW_DAYS} 天已踩同类 {n} 次，"
                f"建议优先参考上述建议而非重试"
            ) if recurring else "",
        }
        result["prior_art"] = prior_art  # 只新增键，不动既有字段（C2）
        return result
    except Exception:
        return result  # fail-safe（C3）：原样返回，绝不抛


# ── P2/P3 成功闭环钩子（fail-safe 包装，使基类 store 也能安全调用） ──────────
def try_close_loop_success(store: Any, agent_id: str = "",
                           error_type_hint: str = "") -> Dict[str, Any]:
    """成功路径钩子（fail-safe）：store 不具备闭环能力（如基类 ErrorKnowledgeStore）
    或调用出错时静默跳过，绝不影响调用方成功返回（C3）。"""
    try:
        closer = getattr(store, "close_loop_success", None)
        if callable(closer):
            return closer(agent_id, error_type_hint) or {"ok": False, "skipped": "none"}
        return {"ok": False, "skipped": "store_has_no_close_loop"}
    except Exception:
        return {"ok": False, "skipped": "close_loop_error"}


# ── P2/P3 · 扩展存储（继承而非改基类：error_knowledge.py 零改动） ─────────────
class EvoErrorKnowledgeStore(ErrorKnowledgeStore):
    """错误知识库 + 失效标记(P3) + 效果度量(P2) + 回读增强（get_suggestion 下沉）。

    继承而非改基类（D1）：error_knowledge.py 零改动，旧调用方无感。
    数据永不删除（C1）：mark_resolved / lazy_expire 只改写标记字段，并保留历史
    （prev_resolution / expired_at / expired_by_version）。
    """

    def __init__(self, data_dir: str, auto_expire: bool = True) -> None:
        super().__init__(data_dir)
        if auto_expire:
            try:
                self.lazy_expire()  # 懒过期：加载时按 VERSION 自动 obsolete
            except Exception:
                pass  # fail-safe：过期失败不影响存取

    def get_suggestion(self, error_msg: str, stage: str = "") -> Dict[str, Any]:
        """覆盖基类：已 resolved 的案例标记 stale 并下沉到末尾（不删除），
        额外返回 stale_count；未失效的保持「最近 5 条」原序。"""
        base = super().get_suggestion(error_msg, stage) or {}
        window = [dict(c) for c in (base.get("similar_cases") or []) if isinstance(c, dict)]
        for c in window:
            c["stale"] = _is_resolved(c)
        active = [c for c in window if not c["stale"]]
        stale = [c for c in window if c["stale"]]
        out = dict(base)
        out["similar_cases"] = active + stale
        out["stale_count"] = len(stale)
        return out

    def mark_resolved(self, error_id: str, resolution: str,
                      by: str = "system") -> Dict[str, Any]:
        """标记 case 失效（P3，绝不删除）：写 resolved_at/resolution/resolved_by/
        resolved_version；并保留历史 resolution 于 prev_resolution。"""
        if resolution not in RESOLUTIONS:
            return {"ok": False, "error": "invalid_resolution",
                    "allowed": list(RESOLUTIONS)}
        data = self._load()
        matched = 0
        stamp = _utcnow_iso()
        for e in data.get("errors", []):
            if e.get("id") != error_id:
                continue
            if e.get("resolution"):
                e["prev_resolution"] = e.get("resolution")  # 不丢历史（C1）
            e["resolved_at"] = stamp
            e["resolution"] = resolution
            e["resolved_by"] = by
            e["resolved_version"] = VERSION
            matched += 1
        if not matched:
            return {"ok": False, "error": "not_found", "id": error_id}
        self._save(data)
        return {"ok": True, "id": error_id, "resolution": resolution,
                "resolved_at": stamp, "resolved_version": VERSION, "matched": matched}

    def record_outcome(self, error_id: str, recovered: bool,
                       followup_stage: str = "") -> Dict[str, Any]:
        """为 case 追加一次后续结果（P2 漏斗原料）：outcomes 只追加，不改原 entry 主体。"""
        data = self._load()
        stamp = _utcnow_iso()
        matched = 0
        total = 0
        for e in data.get("errors", []):
            if e.get("id") != error_id:
                continue
            outs = e.get("outcomes")
            outs = list(outs) if isinstance(outs, list) else []
            outs.append({"at": stamp, "recovered": bool(recovered),
                         "followup_stage": followup_stage})
            e["outcomes"] = outs
            matched += 1
            total = len(outs)
        if not matched:
            return {"ok": False, "error": "not_found", "id": error_id}
        self._save(data)
        return {"ok": True, "id": error_id, "recovered": bool(recovered),
                "outcomes_count": total, "at": stamp}

    def close_loop_success(self, agent_id: str = "",
                           error_type_hint: str = "") -> Dict[str, Any]:
        """propose_dsl 成功时闭合漏斗（便利封装 -> record_outcome）：取该 agent 最近一条
        未失效、且尚未 recovered 的同类型（无同类则任意）case，追加 recovered=True。
        找不到 -> {ok: False, reason: 'no_pending_error'}。"""
        data = self._load()
        pending = [e for e in data.get("errors", [])
                   if (e.get("agent_id") or "") == (agent_id or "")
                   and not _is_resolved(e)]
        if error_type_hint:
            same = [e for e in pending if e.get("error_type") == error_type_hint]
            if same:
                pending = same
        pending = sorted(pending, key=lambda x: x.get("timestamp", ""), reverse=True)
        target = None
        for e in pending:
            if not _is_recovered(e):
                target = e
                break
        if target is None:
            return {"ok": False, "reason": "no_pending_error", "agent_id": agent_id}
        out = self.record_outcome(target.get("id", ""), recovered=True,
                                  followup_stage="propose_dsl_success")
        out["error_type"] = target.get("error_type", "other")
        return out

    def get_effect_funnel(self, agent_id: Optional[str] = None,
                         days: int = 7) -> Dict[str, Any]:
        """P2 效果度量漏斗。口径：
          · total_errors            —— 窗口内（days）案例数（agent_id 给定时只算该 agent）
          · repeated_same_type_within_24h —— 同 error_type 相邻两条间隔 <=24h 的后来者计数
            （P1 保证每次失败都带 prior_art，故该值近似「回读未被采纳的再犯」）
          · recovered               —— 最新 outcome 为 recovered=True 的案例数
          · recovery_rate           —— recovered / total_errors（0~1，四舍五入 4 位）
          · resolved_count/active_count —— 已标记失效 / 仍活跃
          · by_error_type           —— 按类型聚合 {errors, repeats, recovered, resolved}
        """
        data = self._load()
        cutoff = _utcnow() - timedelta(days=days)
        sel: List[Tuple[datetime, Dict[str, Any]]] = []
        for e in data.get("errors", []):
            if agent_id is not None and (e.get("agent_id") or "") != (agent_id or ""):
                continue
            ts = _parse_ts(e.get("timestamp", ""))
            if ts is None or ts < cutoff:
                continue
            sel.append((ts, e))

        by_type: Dict[str, Dict[str, int]] = {}
        repeats_total = 0
        recovered_total = 0
        resolved_total = 0
        groups: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = {}
        for ts, e in sel:
            groups.setdefault(e.get("error_type") or "other", []).append((ts, e))
        for et, items in groups.items():
            items.sort(key=lambda p: p[0])
            repeats = 0
            recovered = 0
            resolved = 0
            prev: Optional[datetime] = None
            for ts, e in items:
                if prev is not None and (ts - prev) <= timedelta(hours=REPEAT_WINDOW_HOURS):
                    repeats += 1
                prev = ts
                if _is_resolved(e):
                    resolved += 1
                if _is_recovered(e):
                    recovered += 1
            by_type[et] = {"errors": len(items), "repeats": repeats,
                           "recovered": recovered, "resolved": resolved}
            repeats_total += repeats
            recovered_total += recovered
            resolved_total += resolved

        total = len(sel)
        return {
            "ok": True,
            "total_errors": total,
            "repeated_same_type_within_24h": repeats_total,
            "recovered": recovered_total,
            "recovery_rate": round(recovered_total / total, 4) if total else 0.0,
            "resolved_count": resolved_total,
            "active_count": total - resolved_total,
            "by_error_type": by_type,
        }

    def lazy_expire(self) -> Dict[str, Any]:
        """版本漂移懒过期（P3，绝不删除）：已 resolved 且 resolved_version < VERSION
        的案例改写为 obsolete（原值存 prev_resolution），返回改写条数。幂等。"""
        data = self._load()
        now_ver = _version_tuple(VERSION)
        stamp = _utcnow_iso()
        expired: List[str] = []
        for e in data.get("errors", []):
            if not _is_resolved(e) or e.get("resolution") == "obsolete":
                continue
            if _version_tuple(e.get("resolved_version", "")) < now_ver:
                e["prev_resolution"] = e.get("resolution")
                e["resolution"] = "obsolete"
                e["expired_at"] = stamp
                e["expired_by_version"] = VERSION
                expired.append(str(e.get("id", "")))
        if expired:
            self._save(data)
        return {"ok": True, "expired_count": len(expired), "to_version": VERSION,
                "expired_ids": expired}


# ── 使用示例（__main__，仅标准库；默认写到 data/_demo_evo，可用 env 覆盖） ──
if __name__ == "__main__":
    demo_dir = os.environ.get("AF_EVO_DEMO_DIR") or os.path.join("data", "_demo_evo")
    store = EvoErrorKnowledgeStore(demo_dir)
    print("== 1) 沉淀：record 三条同类失败 ==")
    ids = []
    for i in range(3):
        r = store.record(dsl=f"flow: turn on light.x try {i}",
                         error_msg="unknown entity light.x not found", stage="compile",
                         agent_id="agent_demo", proposal_id=f"p{i}")
        ids.append(r["id"])
        print(r)
    print("== 2) 主链路回读：attach_prior_art ==")
    result = {"ok": False, "stage": "compile", "error": "unknown entity light.x not found",
              "compile_error": {"kind": "unknown_entity"}, "result_kind": "compile_error"}
    result = attach_prior_art(result, store, agent_id="agent_demo",
                              dsl="flow: turn on light.x try 3")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("== 3) 失效标记：mark_resolved（不删除）==")
    print(store.mark_resolved(ids[0], "fixed", by="human_review"))
    print("stale_count =", store.get_suggestion("unknown entity light.x not found",
                                                "compile")["stale_count"])
    print("== 4) 成功闭环：close_loop_success ==")
    print(store.close_loop_success("agent_demo", "unknown_entity"))
    print("== 5) 效果漏斗：get_effect_funnel ==")
    print(json.dumps(store.get_effect_funnel(agent_id="agent_demo"),
                     ensure_ascii=False, indent=2))
    print("== 6) 懒过期：lazy_expire ==")
    print(store.lazy_expire())
    print("演示数据目录：", demo_dir)
