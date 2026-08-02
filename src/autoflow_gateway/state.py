#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 共享知识层（L1 单一真相源）

所有 agent 先读它、再动手、最后回写。文件持久化在 data/<env>/state/。
支持原子写入（临时文件 + 重命名），避免并发写坏。
"""
import json
import os
import shutil
import tempfile
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from .config import get_config

logger = logging.getLogger(__name__)


class SharedState:
    """单一真相源：device_catalog / flow_catalog / entity_mapping / intent_log。"""

    _lock = threading.RLock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.base = os.path.join(self.cfg.data_dir, self.cfg.env_subdir(), "state")
        os.makedirs(self.base, exist_ok=True)
        self._files = {
            "device_catalog": os.path.join(self.base, "device_catalog.json"),
            "flow_catalog": os.path.join(self.base, "flow_catalog.json"),
            "entity_mapping": os.path.join(self.base, "entity_mapping.json"),
            "intent_log": os.path.join(self.base, "intent_log.json"),
        }

    # ── 原子读写 ──
    def _load(self, name: str, default=None) -> Dict[str, Any]:
        path = self._files.get(name)
        if not path or not os.path.exists(path):
            return default if default is not None else {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # 损坏/不可读：先备份损坏文件再回退默认，避免静默丢数据且无迹可查
            # （正是 2026-07-29 02:18 崩溃致 flow_catalog 损坏→静默变空→不可恢复的根因）
            try:
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    corrupt = f"{path}.corrupt.{stamp}.bak"
                    shutil.copy(path, corrupt)
                    logger.error("共享态[%s] 读取损坏，已备份损坏文件到 %s: %s",
                                 name, corrupt, e)
                else:
                    logger.error("共享态[%s] 读取失败（文件缺失或为空）: %s", name, e)
            except Exception as be:
                logger.error("共享态[%s] 读取失败且无法备份损坏文件: %s | 原错误: %s",
                             name, be, e)
            return default if default is not None else {}

    def _save(self, name: str, data: Dict[str, Any]) -> None:
        path = self._files.get(name)
        if not path:
            raise KeyError(f"未知共享态: {name}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 写前保留上一版备份（与 device_catalog/entity_mapping 对齐），防写坏/写空不可恢复；
        # 备份失败绝不影响主写入。
        try:
            if os.path.exists(path):
                shutil.copy(path, path + ".bak")
        except Exception:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)  # 原子替换

    # ── device_catalog ──
    def get_device_catalog(self) -> Dict[str, Any]:
        return self._load("device_catalog", {"version": 1, "freshness": "", "entities": {}})

    def upsert_device(self, entity_id: str, meta: Dict[str, Any]) -> None:
        with self._lock:
            cat = self.get_device_catalog()
            cat.setdefault("entities", {})[entity_id] = meta
            self._save("device_catalog", cat)

    def set_device_catalog(self, cat: Dict[str, Any]) -> None:
        with self._lock:
            cat.setdefault("version", 1)
            cat.setdefault("freshness", datetime.now(timezone.utc).isoformat())
            self._save("device_catalog", cat)

    def is_entity_known(self, entity_id: str) -> bool:
        return entity_id in self.get_device_catalog().get("entities", {})

    def set_entity_detail(self, entity_id: str, detail: Dict[str, Any]) -> None:
        """懒缓存某实体的完整 attributes（Tier1 device_detail）。"""
        with self._lock:
            cat = self.get_device_catalog()
            e = cat.get("entities", {}).get(entity_id)
            if e is None:
                return
            e["detail"] = detail
            e["detail_cached"] = True
            e["indexed_at"] = datetime.now(timezone.utc).isoformat()
            self._save("device_catalog", cat)

    # ── entity_mapping（含语义桥 + 区域索引 + 房间别名）──
    def get_entity_mapping(self) -> Dict[str, Any]:
        m = self._load("entity_mapping", {"version": 1, "freshness": "", "mappings": {}})
        m.setdefault("version", 1)
        m.setdefault("mappings", {})        # 概念/中文别名 -> entity_id
        m.setdefault("areas", {})           # {area_id: name} 来自 HA（area_id 是 hass-cli 的坑，索引只存 name）
        m.setdefault("room_aliases", {})    # {中文房间词: area_name | "__all__"}
        return m

    def save_entity_mapping(self, m: Dict[str, Any]) -> None:
        with self._lock:
            m.setdefault("version", 1)
            m.setdefault("freshness", datetime.now(timezone.utc).isoformat())
            self._save("entity_mapping", m)

    def add_mapping(self, concept: str, entity_id: str) -> None:
        with self._lock:
            m = self.get_entity_mapping()
            m.setdefault("mappings", {})[concept] = entity_id
            self._save("entity_mapping", m)

    def get_room_aliases(self) -> Dict[str, str]:
        return self.get_entity_mapping().get("room_aliases", {})

    def get_area_index(self) -> Dict[str, str]:
        return self.get_entity_mapping().get("areas", {})

    def resolve(self, concept: str) -> Optional[str]:
        """中文/别名 → entity_id；本体也可直接是 entity_id。"""
        m = self.get_entity_mapping().get("mappings", {})
        if concept in m:
            return m[concept]
        if concept in self.get_device_catalog().get("entities", {}):
            return concept
        return None

    # ── flow_catalog ──
    def get_flow_catalog(self) -> Dict[str, Any]:
        return self._load("flow_catalog", {"version": 1, "flows": {}})

    def upsert_flow(self, flow_id: str, meta: Dict[str, Any]) -> None:
        with self._lock:
            cat = self.get_flow_catalog()
            cat.setdefault("flows", {})[flow_id] = meta
            self._save("flow_catalog", cat)

    def get_flow_meta(self, flow_id: str) -> Optional[Dict[str, Any]]:
        return self.get_flow_catalog().get("flows", {}).get(flow_id)

    def remove_flow(self, flow_id: str) -> None:
        with self._lock:
            cat = self.get_flow_catalog()
            cat.get("flows", {}).pop(flow_id, None)
            self._save("flow_catalog", cat)

    # ── intent_log ──
    def append_intent(self, intent: Dict[str, Any]) -> None:
        with self._lock:
            log = self._load("intent_log", {"intents": []})
            log.setdefault("intents", []).append(intent)
            self._save("intent_log", log)

    def list_intents(self, agent_id: Optional[str] = None) -> list:
        intents = self._load("intent_log", {"intents": []}).get("intents", [])
        if agent_id:
            intents = [i for i in intents if i.get("agent_id") == agent_id]
        return intents
