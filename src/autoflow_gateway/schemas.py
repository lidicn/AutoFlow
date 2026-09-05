#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 意图契约与校验

SceneIntent：自然语言 → 可验证中间表示。三个 agent 都产出同一种结构。
关键字段：expected_postconditions（成功判据）——没有它就无法判定 flow 是否跑通。
"""
import re
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


@dataclass
class Trigger:
    type: str                      # "state_changed" | "time" | "inject" | "event"
    entity_id: Optional[str] = None
    state: Optional[str] = None    # 触发态，如 "home" / "on"
    payload: Optional[Dict[str, Any]] = None


@dataclass
class Condition:
    type: str                      # "state" | "time" | "template"
    entity_id: Optional[str] = None
    state: Optional[str] = None
    operator: str = "is"           # is / not / gt / lt
    value: Optional[Any] = None


@dataclass
class Action:
    domain: str                    # light / switch / notify / script ...
    service: str                   # turn_on / turn_off / notify ...
    entity_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PostCondition:
    entity_id: str
    attribute: str = "state"       # state / 任意 attribute
    op: str = "equals"             # equals / contains / gt / lt
    value: Any = None


@dataclass
class SceneIntent:
    name: str
    description: str = ""
    agent_id: str = "unknown"
    trigger: List[Trigger] = field(default_factory=list)
    condition: List[Condition] = field(default_factory=list)
    action: List[Action] = field(default_factory=list)
    expected_postconditions: List[PostCondition] = field(default_factory=list)
    environment: str = "staging"
    intent_id: str = field(default_factory=lambda: "scn_" + uuid.uuid4().hex[:12])
    status: str = "draft"          # draft | proposed | pending | deployed | failed
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SceneIntent":
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            agent_id=d.get("agent_id", "unknown"),
            trigger=[Trigger(**t) for t in d.get("trigger", [])],
            condition=[Condition(**c) for c in d.get("condition", [])],
            action=[Action(**a) for a in d.get("action", [])],
            expected_postconditions=[PostCondition(**p) for p in d.get("expected_postconditions", [])],
            environment=d.get("environment", "staging"),
            intent_id=d.get("intent_id") or ("scn_" + uuid.uuid4().hex[:12]),
            status=d.get("status", "draft"),
            created_at=d.get("created_at") or datetime.now(timezone.utc).isoformat(),
        )


class IntentValidationError(ValueError):
    pass


def validate_intent(intent: SceneIntent, catalog: Optional[Dict[str, Any]] = None) -> List[str]:
    """校验意图。返回错误列表（空=通过）。

    catalog 可选：提供 device_catalog 时额外验证 entity_id 是否真实存在。
    """
    errors: List[str] = []

    if not intent.name or not intent.name.strip():
        errors.append("name 不能为空")
    if not intent.action:
        errors.append("至少需要一个 action")
    if not intent.trigger:
        errors.append("至少需要一个 trigger（否则场景无法触发）")
    # 成功判据是硬性要求——否则无法验证 flow 跑通
    if not intent.expected_postconditions:
        errors.append("必须声明 expected_postconditions（成功判据），否则无法验证 flow 是否跑通")

    # action 结构校验
    for i, a in enumerate(intent.action):
        if not a.domain or not a.service:
            errors.append(f"action[{i}] 缺少 domain/service")
        if not a.entity_id and a.domain not in ("notify", "script"):
            errors.append(f"action[{i}] 缺少 entity_id")
        if a.entity_id and not ENTITY_ID_RE.match(a.entity_id):
            errors.append(f"action[{i}] entity_id 格式非法: {a.entity_id!r}")

    # trigger 结构校验
    for i, t in enumerate(intent.trigger):
        if t.type == "state_changed" and not t.entity_id:
            errors.append(f"trigger[{i}] state_changed 缺少 entity_id")
        if t.entity_id and not ENTITY_ID_RE.match(t.entity_id):
            errors.append(f"trigger[{i}] entity_id 格式非法: {t.entity_id!r}")

    # 若提供 catalog，校验实体真实存在（防止造到不存在的设备上）
    if catalog:
        known = set(catalog.get("entities", {}).keys())
        for a in intent.action:
            if a.entity_id and a.entity_id not in known:
                errors.append(f"action 引用了未知实体: {a.entity_id}（不在 device_catalog）")
        for t in intent.trigger:
            if t.entity_id and t.entity_id not in known:
                errors.append(f"trigger 引用了未知实体: {t.entity_id}（不在 device_catalog）")

    return errors


# ── 共享态 JSON schema 概要（运行时以 dataclass/dict 形式，此处仅作文档化常量）──
DEVICE_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "freshness": {"type": "string"},
        "entities": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "domain": {"type": "string"},
                    "friendly_name": {"type": "string"},
                    "area": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "state": {"type": "string"},
                },
            },
        },
    },
}

ENTITY_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "freshness": {"type": "string"},
        "mappings": {
            "type": "object",
            "description": "概念/中文别名 -> entity_id",
            "additionalProperties": {"type": "string"},
        },
    },
}

FLOW_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "flows": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "flow_id": {"type": "string"},
                    "label": {"type": "string"},
                    "owner_agent": {"type": "string"},
                    "purpose": {"type": "string"},
                    "entities_touched": {"type": "array", "items": {"type": "string"}},
                    "node_count": {"type": "integer"},
                    "created_at": {"type": "string"},
                },
            },
        },
    },
}
