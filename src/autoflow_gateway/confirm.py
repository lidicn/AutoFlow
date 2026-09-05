#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 人工确认闸（写必人工确认）

所有写操作（NR 流变更 / HA call_service）不立即执行，而是进入待确认队列，
由人类（或受限 elevated 通道）批准后才真正落地。这是零信任的最后一道闸。
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from .config import get_config


class ConfirmationError(RuntimeError):
    pass


class PendingOp:
    def __init__(self, operation: str, agent_id: str, risk_level: str,
                 summary: str, blast_radius: int, payload: Dict[str, Any],
                 target: str = "", owner_flow: str = ""):
        self.id = "op_" + uuid.uuid4().hex[:12]
        self.operation = operation
        self.agent_id = agent_id
        self.risk_level = risk_level
        self.summary = summary
        self.blast_radius = blast_radius
        self.payload = payload          # 真正执行所需的参数
        self.target = target
        self.owner_flow = owner_flow
        self.status = "pending"         # pending | approved | rejected
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.decided_at = None
        self.reviewer = None
        self.reason = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PendingOp":
        p = cls.__new__(cls)
        p.__dict__.update(d)
        return p


class ConfirmationGate:
    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.base = os.path.join(self.cfg.data_dir, self.cfg.env_subdir(), "pending")
        os.makedirs(self.base, exist_ok=True)

    def _path(self, op_id: str) -> str:
        return os.path.join(self.base, f"{op_id}.json")

    def request(self, op: PendingOp) -> PendingOp:
        # 速率熔断：单 agent 待确认上限
        pending = self.list_pending(agent_id=op.agent_id, statuses=("pending",))
        if len(pending) >= self.cfg.max_pending_per_agent:
            raise ConfirmationError(
                f"agent '{op.agent_id}' 待确认数已达上限 {self.cfg.max_pending_per_agent}，"
                f"请先处理积压或等待批准。"
            )
        with open(self._path(op.id), "w", encoding="utf-8") as f:
            json.dump(op.to_dict(), f, indent=2, ensure_ascii=False)
        return op

    def get(self, op_id: str) -> Optional[PendingOp]:
        p = self._path(op_id)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return PendingOp.from_dict(json.load(f))

    def _decide(self, op_id: str, status: str, reviewer: str, reason: Optional[str]) -> PendingOp:
        op = self.get(op_id)
        if op is None:
            raise ConfirmationError(f"待确认项不存在: {op_id}")
        if op.status != "pending":
            raise ConfirmationError(f"待确认项 {op_id} 已 {op.status}，不能重复决定。")
        op.status = status
        op.decided_at = datetime.now(timezone.utc).isoformat()
        op.reviewer = reviewer
        op.reason = reason
        with open(self._path(op_id), "w", encoding="utf-8") as f:
            json.dump(op.to_dict(), f, indent=2, ensure_ascii=False)
        return op

    def approve(self, op_id: str, reviewer: str = "human") -> PendingOp:
        return self._decide(op_id, "approved", reviewer, None)

    def reject(self, op_id: str, reviewer: str = "human", reason: Optional[str] = None) -> PendingOp:
        return self._decide(op_id, "rejected", reviewer, reason)

    def list_pending(self, agent_id: Optional[str] = None,
                     statuses: tuple = ("pending",)) -> List[PendingOp]:
        out = []
        if not os.path.isdir(self.base):
            return out
        for fn in os.listdir(self.base):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(self.base, fn), "r", encoding="utf-8") as f:
                op = PendingOp.from_dict(json.load(f))
            if op.status not in statuses:
                continue
            if agent_id and op.agent_id != agent_id:
                continue
            out.append(op)
        return sorted(out, key=lambda o: o.created_at)
