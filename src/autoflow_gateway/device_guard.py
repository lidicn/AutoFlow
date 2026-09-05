#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备保护注册表（Device Guard，C25）。

开放源码 MVP 的安全兜底：列出受保护实体（entity_id 精确匹配 / domain 通配 / HA area），
分级处置：
  - Tier-0：任何触及都需走人工确认闸（部署闸拦截点由 WB1 定，本模块只管数据）。
  - Tier-1：默认放行但记入审计。

本模块只负责「注册表数据 + CRUD」，拦截逻辑（在部署闸哪个环节拦、Tier-0 拦截后如何提示）
由 WB1 在 D3 读写分离落地后统一裁定——见最终路线图 B4/D2。
"""
import json
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from .config import get_config


class DeviceGuardStore:
    _lock = threading.Lock()

    def __init__(self, config=None):
        self.cfg = config or get_config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.path = os.path.join(self.cfg.data_dir, "device_guard.json")
        self._ensure()

    def _ensure(self):
        if not os.path.exists(self.path):
            self._save({"rules": []})

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"rules": []}

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._load().get("rules", []))

    def upsert(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        """新增或更新一条保护规则。

        rule 结构：
          { "id": 可选（缺省自动生成）,
            "match": {"type": "entity"|"domain"|"area", "value": "..."},
            "tier": 0|1,
            "note": 可选 }
        返回落库后的完整规则（含 id）。
        """
        m = rule.get("match") or {}
        mtype = (m.get("type") or "").strip().lower()
        if mtype not in ("entity", "domain", "area"):
            raise ValueError("match.type 必须是 entity / domain / area")
        if not (m.get("value") or "").strip():
            raise ValueError("match.value 不能为空")
        tier = rule.get("tier", 1)
        if tier not in (0, 1):
            raise ValueError("tier 必须是 0 或 1")
        rid = (rule.get("id") or "").strip()
        with self._lock:
            data = self._load()
            rules = data.setdefault("rules", [])
            if rid:
                for r in rules:
                    if r.get("id") == rid:
                        r["match"] = m
                        r["tier"] = tier
                        r["note"] = rule.get("note", r.get("note", ""))
                        self._save(data)
                        return r
            rid = "dg_" + uuid.uuid4().hex[:10]
            rec = {"id": rid, "match": m, "tier": tier,
                   "note": rule.get("note", "")}
            rules.append(rec)
            self._save(data)
            return rec

    def delete(self, rid: str) -> bool:
        with self._lock:
            data = self._load()
            rules = data.get("rules", [])
            new = [r for r in rules if r.get("id") != rid]
            if len(new) == len(rules):
                return False
            data["rules"] = new
            self._save(data)
            return True

    def match_tier(self, entity_id: str, domain: str = "",
                   area: str = "") -> Optional[int]:
        """给定实体信息，返回命中的最高保护级别（0 最严）。未命中返回 None。"""
        hit: Optional[int] = None
        for r in self.list():
            m = r.get("match") or {}
            t = m.get("type")
            v = (m.get("value") or "").strip().lower()
            tier = r.get("tier")
            if t == "entity" and entity_id and entity_id.lower() == v:
                hit = 0 if tier == 0 else (hit if hit is not None else 1)
            elif t == "domain" and domain and domain.lower() == v:
                hit = 0 if tier == 0 else (hit if hit is not None else 1)
            elif t == "area" and area and area.lower() == v:
                hit = 0 if tier == 0 else (hit if hit is not None else 1)
        return hit
