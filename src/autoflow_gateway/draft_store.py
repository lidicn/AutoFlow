# -*- coding: utf-8 -*-
"""C20 — 草稿暂存（mimo 亮点①：IR 不回传全量，以 ref 流转）。

设计意图
--------
`propose_dsl` 解析/编译出完整 flow 后，把 (dsl, flow, gate, expected, scene_name,
proposal_id, agent_id) 暂存到本 store，返回一个短 ref（``af:xxxxxxxx``）。
后续 `verify_flow(ref=...)` 可凭 ref 取回 flow，**免去 agent 把整份 IR 原样重传回去**——
这正是 mimo 那套「ref 化、IR 永不回传模型」在 AutoFlow 的最小可行落地。

约束与取舍
----------
- **会话级 + TTL**：网关单进程，覆盖「一次提案→校验」对话窗口足矣；默认 1h。
- **ref 由内容哈希派生**：相同 flow 幂等得到同一 ref（重提交不污染 store）；
  8 位十六进制约 4e9 空间，单次会话碰撞可忽略。
- **不是缓存、不是持久提案**：提案落档仍走 `ProposalStore`；这里只解决「IR 往返」的 token 浪费。
- 多 worker（uvicorn Option B）下各 worker 独立暂存，足够覆盖单次会话窗口；
  若未来要跨 worker，改为按 agent_id 的共享后端即可，接口不变。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional

_DEFAULT_TTL = 3600.0


class DraftStore:
    """进程内、带 TTL 的草稿暂存表。"""

    def __init__(self, ttl: float = _DEFAULT_TTL):
        self._ttl = ttl
        self._store: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _ref_of(payload: Dict[str, Any]) -> str:
        seed = json.dumps(payload.get("flow") or {}, sort_keys=True, ensure_ascii=False)
        seed += "\x00" + (payload.get("dsl") or "")
        return "af:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]

    def put(self, payload: Dict[str, Any]) -> str:
        ref = self._ref_of(payload)
        self._store[ref] = {"payload": payload, "ts": time.time()}
        self._gc()
        return ref

    def get(self, ref: str) -> Optional[Dict[str, Any]]:
        entry = self._store.get(ref)
        if not entry:
            return None
        if time.time() - entry["ts"] > self._ttl:
            self._store.pop(ref, None)
            return None
        return entry["payload"]

    def _gc(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)


# 进程级单例（网关单进程；多 worker 时各 worker 独立暂存，接口不变）
_STORE = DraftStore()


def stage_draft(payload: Dict[str, Any]) -> str:
    """暂存一份草稿，返回短 ref。"""
    return _STORE.put(payload)


def get_draft(ref: str) -> Optional[Dict[str, Any]]:
    """凭 ref 取回草稿 payload；过期/不存在返回 None。"""
    return _STORE.get(ref)


__all__ = ["DraftStore", "stage_draft", "get_draft"]
