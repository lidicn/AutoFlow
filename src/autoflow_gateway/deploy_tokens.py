#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署授权码模块 —— 允许受信任 agent 在限定范围内自动部署 flow。

设计原则：
  · 不绕过安全闸（lint/e2e/实体白名单仍然生效），只是免人工确认
  · 多重限制：目标 tab、有效期、权限、节点阈值、资源配额、频率限制
  · 可回溯：授权前全量快照、操作前增量快照、审计日志、一键回滚
  · 可吊销：用户可随时吊销，立即生效
  · fail-safe：授权码无效时自动回退到人工审批，不拒绝部署

授权码存储：data/<env>/deploy_tokens.json
使用日志：data/<env>/deploy_token_logs.jsonl
快照：data/<env>/snapshots/<token_id>/
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple


# 权限常量
PERM_DEPLOY = "deploy"
PERM_MODIFY = "modify"
PERM_UNDEPLOY = "undeploy"
ALL_PERMS = [PERM_DEPLOY, PERM_MODIFY, PERM_UNDEPLOY]

# 默认配置
DEFAULT_NODE_THRESHOLD = 50  # 单 flow 超过这个节点数仍需人工审批
DEFAULT_MAX_NODES = 500  # 授权码有效期内最多部署的节点数
DEFAULT_MAX_FLOWS = 20  # 授权码有效期内最多部署的 flow 数
DEFAULT_RATE_LIMIT = 10  # 每分钟最多操作数
DEFAULT_TOKEN_TTL_HOURS = 4  # 默认有效期 4 小时


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _hash_token(token: str) -> str:
    """对 token 做 SHA-256 哈希存储（不存明文）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> Tuple[str, str]:
    """生成授权码，返回 (token_id, token_plaintext)。

    token 格式：dt_<32位hex>
    token_id：dtid_<16位hex>（用于日志和快照目录命名，不含密钥）
    """
    token_plaintext = "dt_" + uuid.uuid4().hex
    token_id = "dtid_" + uuid.uuid4().hex[:16]
    return token_id, token_plaintext


class DeployTokenStore:
    """授权码存储管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.tokens_file = os.path.join(data_dir, "deploy_tokens.json")
        self.logs_file = os.path.join(data_dir, "deploy_token_logs.jsonl")
        self.snapshots_dir = os.path.join(data_dir, "snapshots")
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)
        self._lock_file = os.path.join(data_dir, ".deploy_tokens.lock")

    def _load_tokens(self) -> Dict[str, Any]:
        if os.path.isfile(self.tokens_file):
            try:
                with open(self.tokens_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"version": 1, "tokens": {}}

    def _save_tokens(self, data: Dict[str, Any]) -> None:
        tmp = self.tokens_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.tokens_file)

    def _append_log(self, entry: Dict[str, Any]) -> None:
        with open(self.logs_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def create_token(self, *, name: str, target_tab: Optional[str] = None,
                     expires_in_hours: float = DEFAULT_TOKEN_TTL_HOURS,
                     permissions: Optional[List[str]] = None,
                     node_threshold: int = DEFAULT_NODE_THRESHOLD,
                     max_nodes: int = DEFAULT_MAX_NODES,
                     max_flows: int = DEFAULT_MAX_FLOWS,
                     rate_limit_per_min: int = DEFAULT_RATE_LIMIT,
                     require_confirm_dangerous: bool = True,
                     bound_agent: Optional[str] = None,
                     bound_nr_instance: Optional[str] = None,
                     created_by: str = "user") -> Dict[str, Any]:
        """创建授权码。

        返回 {token_id, token_plaintext, ...配置}
        token_plaintext 只在创建时返回一次，之后只存哈希。
        """
        token_id, token_plaintext = _generate_token()
        now = _utcnow()
        expires_at = now + timedelta(hours=expires_in_hours)

        token_data = {
            "token_id": token_id,
            "token_hash": _hash_token(token_plaintext),
            "name": name,
            "target_tab": target_tab or None,  # None=不绑定tab，走 per_flow 模式
            "permissions": permissions or [PERM_DEPLOY],
            "node_threshold": node_threshold,
            "max_nodes": max_nodes,
            "max_flows": max_flows,
            "rate_limit_per_min": rate_limit_per_min,
            "require_confirm_dangerous": require_confirm_dangerous,
            "bound_agent": bound_agent,
            "bound_nr_instance": bound_nr_instance,
            "created_at": now.isoformat(),
            "created_by": created_by,
            "expires_at": expires_at.isoformat(),
            "revoked": False,
            "revoked_at": None,
            "revoked_by": None,
            # 使用统计
            "stats": {
                "deploy_count": 0,
                "modify_count": 0,
                "undeploy_count": 0,
                "nodes_deployed": 0,
                "flows_deployed": 0,
                "failed_count": 0,
                "last_used_at": None,
                "rate_window_start": None,
                "rate_window_count": 0,
            },
        }

        data = self._load_tokens()
        data["tokens"][token_id] = token_data
        self._save_tokens(data)

        # 返回时不含 token_hash
        result = {k: v for k, v in token_data.items() if k != "token_hash"}
        result["token_plaintext"] = token_plaintext
        return result

    def validate_token(self, token_plaintext: str, *, operation: str,
                       agent_id: Optional[str] = None,
                       nr_instance: Optional[str] = None,
                       node_count: int = 0) -> Dict[str, Any]:
        """验证授权码是否可用于指定操作。

        返回 {ok, token_id?, error?, reason?, needs_manual_approval?}
        """
        if not token_plaintext:
            return {"ok": False, "error": "授权码为空"}

        token_hash = _hash_token(token_plaintext)
        data = self._load_tokens()

        # 查找匹配的 token
        token_data = None
        for tid, t in data.get("tokens", {}).items():
            if t.get("token_hash") == token_hash:
                token_data = t
                break

        if token_data is None:
            return {"ok": False, "error": "授权码无效"}

        token_id = token_data["token_id"]

        # 检查是否被吊销
        if token_data.get("revoked"):
            return {"ok": False, "error": "授权码已被吊销", "token_id": token_id}

        # 检查是否过期
        expires_at = datetime.fromisoformat(token_data["expires_at"])
        if _utcnow() > expires_at:
            return {"ok": False, "error": "授权码已过期", "token_id": token_id}

        # 检查绑定的 agent
        if token_data.get("bound_agent") and agent_id and token_data["bound_agent"] != agent_id:
            return {"ok": False, "error": f"授权码绑定的 agent 是 {token_data['bound_agent']}，当前 agent 是 {agent_id}",
                    "token_id": token_id}

        # 检查绑定的 NR 实例
        if token_data.get("bound_nr_instance") and nr_instance and token_data["bound_nr_instance"] != nr_instance:
            return {"ok": False, "error": "授权码绑定的 NR 实例不匹配", "token_id": token_id}

        # 检查权限
        perms = token_data.get("permissions", [])
        if operation not in perms:
            return {"ok": False, "error": f"授权码没有 {operation} 权限（当前权限：{', '.join(perms)}）",
                    "token_id": token_id}

        # 检查节点数阈值（超过阈值仍需人工审批）
        threshold = token_data.get("node_threshold", DEFAULT_NODE_THRESHOLD)
        if node_count > threshold:
            return {"ok": True, "token_id": token_id, "needs_manual_approval": True,
                    "reason": f"flow 节点数 {node_count} 超过阈值 {threshold}，需人工审批"}

        # 检查资源配额
        stats = token_data.get("stats", {})
        if stats.get("nodes_deployed", 0) + node_count > token_data.get("max_nodes", DEFAULT_MAX_NODES):
            return {"ok": False, "error": f"节点数配额已满（已用 {stats.get('nodes_deployed', 0)}/{token_data.get('max_nodes', DEFAULT_MAX_NODES)}）",
                    "token_id": token_id}
        if stats.get("flows_deployed", 0) >= token_data.get("max_flows", DEFAULT_MAX_FLOWS):
            return {"ok": False, "error": f"flow 数配额已满（已用 {stats.get('flows_deployed', 0)}/{token_data.get('max_flows', DEFAULT_MAX_FLOWS)}）",
                    "token_id": token_id}

        # 检查频率限制
        now = time.time()
        rate_window_start = stats.get("rate_window_start")
        rate_window_count = stats.get("rate_window_count", 0)
        rate_limit = token_data.get("rate_limit_per_min", DEFAULT_RATE_LIMIT)
        if rate_window_start and (now - rate_window_start) < 60:
            if rate_window_count >= rate_limit:
                # 频率超限也记录到日志（不通过 record_usage，因为那是成功/失败部署的日志）
                return {"ok": False, "error": f"操作频率超限（每分钟最多 {rate_limit} 次），请稍后重试",
                        "token_id": token_id}
        else:
            # 重置时间窗口 —— 必须保存，否则 rate_window_start 永远为 null，限流永不生效
            stats["rate_window_start"] = now
            stats["rate_window_count"] = 0
            self._save_tokens(data)

        return {"ok": True, "token_id": token_id, "needs_manual_approval": False}

    def record_usage(self, token_id: str, *, operation: str, agent_id: str,
                     flow_id: Optional[str] = None, flow_label: Optional[str] = None,
                     node_count: int = 0, success: bool = True,
                     error: Optional[str] = None) -> None:
        """记录授权码使用情况。"""
        data = self._load_tokens()
        token_data = data.get("tokens", {}).get(token_id)
        if token_data is None:
            return

        stats = token_data.setdefault("stats", {})
        stats["last_used_at"] = _utcnow_iso()

        if success:
            if operation == PERM_DEPLOY:
                stats["deploy_count"] = stats.get("deploy_count", 0) + 1
                stats["nodes_deployed"] = stats.get("nodes_deployed", 0) + node_count
                stats["flows_deployed"] = stats.get("flows_deployed", 0) + 1
            elif operation == PERM_MODIFY:
                stats["modify_count"] = stats.get("modify_count", 0) + 1
            elif operation == PERM_UNDEPLOY:
                stats["undeploy_count"] = stats.get("undeploy_count", 0) + 1
        else:
            stats["failed_count"] = stats.get("failed_count", 0) + 1
        # 频率计数：成功和失败都计数，防止失败风暴绕过限流
        stats["rate_window_count"] = stats.get("rate_window_count", 0) + 1

        self._save_tokens(data)

        # 写日志
        log_entry = {
            "timestamp": _utcnow_iso(),
            "token_id": token_id,
            "agent_id": agent_id,
            "operation": operation,
            "flow_id": flow_id,
            "flow_label": flow_label,
            "node_count": node_count,
            "success": success,
            "error": error,
        }
        self._append_log(log_entry)

    def revoke_token(self, token_id: str, *, revoked_by: str = "user") -> bool:
        """吊销授权码。"""
        data = self._load_tokens()
        token_data = data.get("tokens", {}).get(token_id)
        if token_data is None:
            return False
        token_data["revoked"] = True
        token_data["revoked_at"] = _utcnow_iso()
        token_data["revoked_by"] = revoked_by
        self._save_tokens(data)
        return True

    def list_tokens(self, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """列出授权码（不含 token_hash 和 token_plaintext）。"""
        data = self._load_tokens()
        tokens = []
        for tid, t in data.get("tokens", {}).items():
            if not include_revoked and t.get("revoked"):
                continue
            # 检查是否过期
            is_expired = False
            try:
                expires_at = datetime.fromisoformat(t["expires_at"])
                is_expired = _utcnow() > expires_at
            except Exception:
                pass
            result = {k: v for k, v in t.items() if k != "token_hash"}
            result["is_expired"] = is_expired
            result["is_active"] = not t.get("revoked") and not is_expired
            tokens.append(result)
        # 按创建时间倒序
        tokens.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return tokens

    def get_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        """获取单个授权码详情（不含 token_hash）。"""
        data = self._load_tokens()
        t = data.get("tokens", {}).get(token_id)
        if t is None:
            return None
        result = {k: v for k, v in t.items() if k != "token_hash"}
        return result

    def get_logs(self, token_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """获取使用日志。"""
        if not os.path.isfile(self.logs_file):
            return []
        logs = []
        with open(self.logs_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if token_id and entry.get("token_id") != token_id:
                    continue
                logs.append(entry)
        logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return logs[:limit]

    def get_snapshot_dir(self, token_id: str) -> str:
        """获取授权码的快照目录。"""
        d = os.path.join(self.snapshots_dir, token_id)
        os.makedirs(d, exist_ok=True)
        return d
