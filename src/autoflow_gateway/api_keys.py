#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API Key 管理模块 —— AutoFlow Pro 的 Agent 身份认证。

设计原则：
  · Agent 身份 + 授权范围双层模型：API Key 解决"你是谁"，Tab 授权解决"你能做什么"
  · 不存明文：只存 SHA-256 hash，创建时只显示一次明文
  · 可吊销：用户可随时吊销，立即生效
  · 可追溯：每次使用记录审计日志
  · 细粒度：每个 Agent 可授权特定 tab 和权限级别

存储：data/<env>/api_keys.json
日志：data/<env>/api_key_logs.jsonl
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# 权限级别
PERM_READ = "read"           # 只读：查询实体、快照
PERM_DEPLOY = "deploy"       # 可部署：propose-dsl、deploy-proposal
PERM_MODIFY = "modify"       # 可修改：deploy-raw、rollback
ALL_PERMS = [PERM_READ, PERM_DEPLOY, PERM_MODIFY]

# 默认权限
DEFAULT_PERMS = [PERM_READ, PERM_DEPLOY]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _hash_key(key: str) -> str:
    """对 API Key 做 SHA-256 哈希存储（不存明文）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _generate_key() -> Tuple[str, str]:
    """生成 API Key，返回 (key_id, key_plaintext)。

    key 格式：af_pro_<32位hex>
    key_id：akid_<16位hex>（用于日志，不含密钥）
    """
    key_plaintext = "af_pro_" + uuid.uuid4().hex
    key_id = "akid_" + uuid.uuid4().hex[:16]
    return key_id, key_plaintext


class APIKeyStore:
    """API Key 存储管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.keys_file = os.path.join(data_dir, "api_keys.json")
        self.logs_file = os.path.join(data_dir, "api_key_logs.jsonl")
        os.makedirs(data_dir, exist_ok=True)

    def _load_keys(self) -> Dict[str, Any]:
        if os.path.isfile(self.keys_file):
            try:
                with open(self.keys_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"keys": []}
        return {"keys": []}

    def _save_keys(self, data: Dict[str, Any]) -> None:
        tmp = self.keys_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.keys_file)

    def _log(self, key_id: str, agent_id: str, action: str,
             detail: str = "", success: bool = True) -> None:
        try:
            entry = {
                "ts": _utcnow_iso(),
                "key_id": key_id,
                "agent_id": agent_id,
                "action": action,
                "detail": detail,
                "success": success,
            }
            with open(self.logs_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── CRUD ─────────────────────────────────────────

    def create_key(self, name: str, agent_id: str,
                   authorized_tabs: Optional[List[str]] = None,
                   permissions: Optional[List[str]] = None,
                   expires_at: Optional[str] = None) -> Dict[str, Any]:
        """创建新的 API Key。返回包含明文 key 的字典（只此一次可见）。"""
        key_id, key_plaintext = _generate_key()
        key_hash = _hash_key(key_plaintext)
        now = _utcnow_iso()

        entry = {
            "key_id": key_id,
            "key_hash": key_hash,
            "name": name,
            "agent_id": agent_id,
            "authorized_tabs": authorized_tabs or [],  # 空列表 = 全部 tab
            "permissions": permissions or DEFAULT_PERMS,
            "created_at": now,
            "last_used_at": None,
            "expires_at": expires_at,
            "revoked": False,
            "use_count": 0,
        }

        data = self._load_keys()
        data["keys"].append(entry)
        self._save_keys(data)
        self._log(key_id, agent_id, "create", f"name={name}")

        return {
            "ok": True,
            "key_id": key_id,
            "key": key_plaintext,  # 明文，只此一次
            "name": name,
            "agent_id": agent_id,
            "authorized_tabs": entry["authorized_tabs"],
            "permissions": entry["permissions"],
            "created_at": now,
            "expires_at": expires_at,
        }

    def list_keys(self, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """列出所有 API Key（不含明文和 hash）。"""
        data = self._load_keys()
        result = []
        for k in data["keys"]:
            if k.get("revoked") and not include_revoked:
                continue
            result.append({
                "key_id": k["key_id"],
                "name": k["name"],
                "agent_id": k["agent_id"],
                "authorized_tabs": k.get("authorized_tabs", []),
                "permissions": k.get("permissions", DEFAULT_PERMS),
                "created_at": k.get("created_at"),
                "last_used_at": k.get("last_used_at"),
                "expires_at": k.get("expires_at"),
                "revoked": k.get("revoked", False),
                "use_count": k.get("use_count", 0),
            })
        return result

    def get_key(self, key_id: str) -> Optional[Dict[str, Any]]:
        """按 key_id 获取 key 信息（不含明文和 hash）。"""
        data = self._load_keys()
        for k in data["keys"]:
            if k["key_id"] == key_id:
                return {
                    "key_id": k["key_id"],
                    "name": k["name"],
                    "agent_id": k["agent_id"],
                    "authorized_tabs": k.get("authorized_tabs", []),
                    "permissions": k.get("permissions", DEFAULT_PERMS),
                    "created_at": k.get("created_at"),
                    "last_used_at": k.get("last_used_at"),
                    "expires_at": k.get("expires_at"),
                    "revoked": k.get("revoked", False),
                    "use_count": k.get("use_count", 0),
                }
        return None

    def revoke_key(self, key_id: str) -> Dict[str, Any]:
        """吊销 API Key。"""
        data = self._load_keys()
        for k in data["keys"]:
            if k["key_id"] == key_id:
                k["revoked"] = True
                k["revoked_at"] = _utcnow_iso()
                self._save_keys(data)
                self._log(key_id, k["agent_id"], "revoke")
                return {"ok": True, "key_id": key_id, "revoked": True}
        return {"ok": False, "error": "key_id 不存在"}

    def update_key(self, key_id: str, name: Optional[str] = None,
                   authorized_tabs: Optional[List[str]] = None,
                   permissions: Optional[List[str]] = None) -> Dict[str, Any]:
        """更新 API Key 的授权范围。"""
        data = self._load_keys()
        for k in data["keys"]:
            if k["key_id"] == key_id:
                if name is not None:
                    k["name"] = name
                if authorized_tabs is not None:
                    k["authorized_tabs"] = authorized_tabs
                if permissions is not None:
                    k["permissions"] = permissions
                self._save_keys(data)
                self._log(key_id, k["agent_id"], "update",
                          f"tabs={authorized_tabs}, perms={permissions}")
                return {"ok": True, "key_id": key_id}
        return {"ok": False, "error": "key_id 不存在"}

    # ── 验证 ─────────────────────────────────────────

    def validate_key(self, key_plaintext: str,
                     required_perm: Optional[str] = None,
                     target_tab: Optional[str] = None) -> Dict[str, Any]:
        """验证 API Key。返回 {ok, agent_id, key_id, authorized_tabs, permissions}。

        Args:
            key_plaintext: 明文 API Key
            required_perm: 需要的权限（None 表示不检查权限）
            target_tab: 目标 tab（None 表示不检查 tab 授权）
        """
        if not key_plaintext:
            return {"ok": False, "error": "API Key 为空", "status": 401}

        key_hash = _hash_key(key_plaintext)
        data = self._load_keys()

        for k in data["keys"]:
            if k["key_hash"] != key_hash:
                continue

            # 检查吊销
            if k.get("revoked"):
                self._log(k["key_id"], k["agent_id"], "validate",
                          "已吊销", success=False)
                return {"ok": False, "error": "API Key 已吊销", "status": 403}

            # 检查过期（P0 修复：fail-closed，naive datetime 不再静默放行）
            if k.get("expires_at"):
                exp_str = k["expires_at"]
                try:
                    exp = datetime.fromisoformat(exp_str)
                except (ValueError, TypeError, OverflowError):
                    exp = None
                if exp is None:
                    # 无法解析 → fail-closed：拒绝（避免静默放行造成永不失效）
                    self._log(k["key_id"], k["agent_id"], "validate",
                              f"expires_at 格式无效: {exp_str}", success=False)
                    return {"ok": False, "error": "API Key 已过期", "status": 401}
                if exp.tzinfo is None:
                    # naive 时间无时区，无法与 aware _utcnow() 可靠比较 → 拒绝
                    self._log(k["key_id"], k["agent_id"], "validate",
                              "expires_at 为 naive 格式(缺时区)，拒绝", success=False)
                    return {"ok": False, "error": "API Key 已过期", "status": 401}
                if _utcnow() > exp:
                    self._log(k["key_id"], k["agent_id"], "validate",
                              "已过期", success=False)
                    return {"ok": False, "error": "API Key 已过期", "status": 403}

            # 检查权限
            perms = k.get("permissions", DEFAULT_PERMS)
            if required_perm and required_perm not in perms:
                self._log(k["key_id"], k["agent_id"], "validate",
                          f"权限不足: need={required_perm}, have={perms}", success=False)
                return {"ok": False, "error": f"权限不足，需要 {required_perm}", "status": 403}

            # 检查 tab 授权
            auth_tabs = k.get("authorized_tabs", [])
            if target_tab and auth_tabs and target_tab not in auth_tabs:
                self._log(k["key_id"], k["agent_id"], "validate",
                          f"tab 越界: target={target_tab}, allowed={auth_tabs}", success=False)
                return {"ok": False, "error": f"tab 不在授权范围内: {target_tab}",
                        "status": 403, "authorized_tabs": auth_tabs}

            # 更新使用统计
            k["last_used_at"] = _utcnow_iso()
            k["use_count"] = k.get("use_count", 0) + 1
            self._save_keys(data)
            self._log(k["key_id"], k["agent_id"], "validate", "成功")

            return {
                "ok": True,
                "key_id": k["key_id"],
                "agent_id": k["agent_id"],
                "name": k["name"],
                "authorized_tabs": auth_tabs,
                "permissions": perms,
            }

        # 未找到匹配的 key
        self._log("unknown", "unknown", "validate", "key 不存在", success=False)
        return {"ok": False, "error": "API Key 无效", "status": 401}

    def get_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近的审计日志。"""
        if not os.path.isfile(self.logs_file):
            return []
        try:
            with open(self.logs_file, encoding="utf-8") as f:
                lines = f.readlines()
            logs = []
            for line in lines[-limit:]:
                try:
                    logs.append(json.loads(line))
                except Exception:
                    pass
            return logs
        except Exception:
            return []
