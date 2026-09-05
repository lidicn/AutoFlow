#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照/回滚模块 —— 授权码操作前自动快照，支持一键回滚。

快照类型：
  · full：全量快照，备份整个 tab 的 flow JSON
  · incremental：增量快照，只记录变更的 flow（部署/修改/撤回前）

快照存储：data/<env>/snapshots/<token_id>/<snapshot_id>.json
快照元数据：data/<env>/snapshots/<token_id>/index.json

回滚方式：
  · full_rollback：全量回滚，整个 tab 恢复到指定快照
  · selective_rollback：选择性回滚，只恢复指定 flow
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _gen_snapshot_id() -> str:
    return "snap_" + uuid.uuid4().hex[:16]


class SnapshotManager:
    """快照管理器。"""

    def __init__(self, snapshots_root: str):
        self.snapshots_root = snapshots_root
        os.makedirs(snapshots_root, exist_ok=True)

    def _token_dir(self, token_id: str) -> str:
        d = os.path.join(self.snapshots_root, token_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _index_path(self, token_id: str) -> str:
        return os.path.join(self._token_dir(token_id), "index.json")

    def _load_index(self, token_id: str) -> Dict[str, Any]:
        p = self._index_path(token_id)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"version": 1, "snapshots": [], "token_id": token_id}

    def _save_index(self, token_id: str, index: Dict[str, Any]) -> None:
        p = self._index_path(token_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    def create_full_snapshot(self, token_id: str, tab_id: str,
                              tab_data: Dict[str, Any],
                              *, label: str = "", created_by: str = "system") -> Dict[str, Any]:
        """创建全量快照（授权码生效时调用）。

        tab_data: 整个 tab 的 flow 数据（含 nodes）
        """
        snap_id = _gen_snapshot_id()
        now = _utcnow_iso()

        snap_data = {
            "snapshot_id": snap_id,
            "type": "full",
            "token_id": token_id,
            "tab_id": tab_id,
            "label": label or "授权前全量快照",
            "created_at": now,
            "created_by": created_by,
            "node_count": len(tab_data.get("nodes", [])),
            "data": tab_data,  # 全量数据
        }

        # 写快照文件
        snap_path = os.path.join(self._token_dir(token_id), f"{snap_id}.json")
        tmp = snap_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap_data, f, ensure_ascii=False)
        os.replace(tmp, snap_path)

        # 更新索引
        index = self._load_index(token_id)
        index["snapshots"].append({
            "snapshot_id": snap_id,
            "type": "full",
            "tab_id": tab_id,
            "label": snap_data["label"],
            "created_at": now,
            "created_by": created_by,
            "node_count": snap_data["node_count"],
            "file": f"{snap_id}.json",
        })
        self._save_index(token_id, index)

        return {"snapshot_id": snap_id, "type": "full", "node_count": snap_data["node_count"]}

    def create_incremental_snapshot(self, token_id: str, tab_id: str,
                                      current_tab_data: Dict[str, Any],
                                      affected_flow_ids: List[str],
                                      *, operation: str = "deploy",
                                      label: str = "", created_by: str = "system") -> Dict[str, Any]:
        """创建增量快照（每次操作前调用）。

        只记录受影响的 flow 的当前状态（操作前），用于回滚。
        affected_flow_ids: 本次操作会影响的 flow_id 列表
        """
        snap_id = _gen_snapshot_id()
        now = _utcnow_iso()

        # 提取受影响的节点
        all_nodes = current_tab_data.get("nodes", [])
        affected_nodes = []
        for n in all_nodes:
            # 检查节点是否属于受影响的 flow（通过边界 comment 或 flow_catalog 判断）
            # 增量快照简单记录所有节点，回滚时按 flow_id 筛选
            affected_nodes.append(n)

        snap_data = {
            "snapshot_id": snap_id,
            "type": "incremental",
            "token_id": token_id,
            "tab_id": tab_id,
            "operation": operation,
            "label": label or f"{operation} 前快照",
            "created_at": now,
            "created_by": created_by,
            "affected_flow_ids": affected_flow_ids,
            "node_count": len(all_nodes),
            "data": current_tab_data,  # 操作前的全量数据（增量回滚时用）
        }

        snap_path = os.path.join(self._token_dir(token_id), f"{snap_id}.json")
        tmp = snap_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap_data, f, ensure_ascii=False)
        os.replace(tmp, snap_path)

        index = self._load_index(token_id)
        index["snapshots"].append({
            "snapshot_id": snap_id,
            "type": "incremental",
            "tab_id": tab_id,
            "operation": operation,
            "label": snap_data["label"],
            "created_at": now,
            "created_by": created_by,
            "affected_flow_ids": affected_flow_ids,
            "node_count": snap_data["node_count"],
            "file": f"{snap_id}.json",
        })
        self._save_index(token_id, index)

        return {"snapshot_id": snap_id, "type": "incremental", "node_count": snap_data["node_count"]}

    def list_snapshots(self, token_id: str, *, snapshot_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出授权码的所有快照。"""
        index = self._load_index(token_id)
        snaps = index.get("snapshots", [])
        if snapshot_type:
            snaps = [s for s in snaps if s.get("type") == snapshot_type]
        # 按时间倒序
        snaps.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return snaps

    def get_snapshot(self, token_id: str, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """获取单个快照的完整数据。"""
        snap_path = os.path.join(self._token_dir(token_id), f"{snapshot_id}.json")
        if not os.path.isfile(snap_path):
            return None
        try:
            with open(snap_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def full_rollback(self, token_id: str, snapshot_id: str,
                       nr_client, *, allow_prod: bool = True) -> Dict[str, Any]:
        """全量回滚：整个 tab 恢复到指定快照。

        返回 {ok, snapshot_id, tab_id, node_count_before, node_count_after}
        """
        snap = self.get_snapshot(token_id, snapshot_id)
        if snap is None:
            return {"ok": False, "error": f"快照 {snapshot_id} 不存在"}

        tab_id = snap.get("tab_id")
        snap_data = snap.get("data", {})
        node_count_before = 0

        # 获取当前 tab 状态
        try:
            current = nr_client.get_flow(tab_id)
            node_count_before = len(current.get("nodes", []))
        except Exception:
            current = None

        # 回滚前再做一次快照（防止回滚错了还能再回滚）
        if current:
            self.create_incremental_snapshot(
                token_id, tab_id, current, [],
                operation="rollback", label=f"回滚前快照（回滚到 {snapshot_id}）",
                created_by="rollback_system")

        # 执行回滚
        try:
            nr_client.update_flow_nodes(tab_id, snap_data, force=True, allow_prod=allow_prod)
            node_count_after = len(snap_data.get("nodes", []))
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "tab_id": tab_id,
                "node_count_before": node_count_before,
                "node_count_after": node_count_after,
                "rollback_type": "full",
            }
        except Exception as e:
            return {"ok": False, "error": f"回滚失败: {e}"}

    def selective_rollback(self, token_id: str, snapshot_id: str,
                             flow_ids_to_restore: List[str],
                             nr_client, flow_catalog_getter,
                             *, allow_prod: bool = True) -> Dict[str, Any]:
        """选择性回滚：只恢复指定 flow，保留其他 flow 的当前状态。

        flow_ids_to_restore: 要恢复的 flow_id 列表
        flow_catalog_getter: 函数，返回 flow_catalog（用于查找 flow 的节点 ID）
        """
        snap = self.get_snapshot(token_id, snapshot_id)
        if snap is None:
            return {"ok": False, "error": f"快照 {snapshot_id} 不存在"}

        tab_id = snap.get("tab_id")
        snap_data = snap.get("data", {})
        snap_nodes = {n.get("id"): n for n in snap_data.get("nodes", [])}

        # 获取当前 tab 状态
        try:
            current = nr_client.get_flow(tab_id)
        except Exception as e:
            return {"ok": False, "error": f"无法读取当前 tab: {e}"}

        current_nodes = current.get("nodes", [])

        # 获取 flow_catalog
        try:
            catalog = flow_catalog_getter()
        except Exception:
            catalog = {"flows": {}}

        # 收集要恢复的 flow 的节点 ID
        nodes_to_restore = set()
        boundary_ids = set()
        for fid in flow_ids_to_restore:
            meta = catalog.get("flows", {}).get(fid, {})
            for nid in meta.get("deployed_node_ids", []):
                nodes_to_restore.add(nid)
            for nid in meta.get("boundary_comment_ids", []):
                boundary_ids.add(nid)
                nodes_to_restore.add(nid)

        # 构建回滚后的节点列表
        # 1. 保留当前 tab 中不属于要恢复的 flow 的节点
        # 2. 用快照中的节点替换要恢复的 flow 的节点
        tab_node = next((n for n in current_nodes if n.get("type") == "tab"), None)
        other_nodes = [n for n in current_nodes
                        if n.get("id") not in nodes_to_restore and n.get("type") != "tab"]

        restored_nodes = []
        for nid in nodes_to_restore:
            if nid in snap_nodes:
                restored_nodes.append(snap_nodes[nid])

        merged_nodes = []
        if tab_node:
            merged_nodes.append(tab_node)
        merged_nodes.extend(other_nodes)
        merged_nodes.extend(restored_nodes)

        # 回滚前再做一次快照
        self.create_incremental_snapshot(
            token_id, tab_id, current, flow_ids_to_restore,
            operation="selective_rollback",
            label=f"选择性回滚前快照（恢复 {len(flow_ids_to_restore)} 个 flow）",
            created_by="rollback_system")

        # 执行回滚
        merged_flow = dict(current)
        merged_flow["nodes"] = merged_nodes
        try:
            nr_client.update_flow_nodes(tab_id, merged_flow, force=True, allow_prod=allow_prod)
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "tab_id": tab_id,
                "restored_flow_count": len(flow_ids_to_restore),
                "restored_node_count": len(restored_nodes),
                "rollback_type": "selective",
            }
        except Exception as e:
            return {"ok": False, "error": f"选择性回滚失败: {e}"}

    def diff_snapshots(self, token_id: str, snapshot_id_1: str,
                         snapshot_id_2: str) -> Dict[str, Any]:
        """对比两个快照的差异，返回节点级和字段级差异详情。"""
        snap1 = self.get_snapshot(token_id, snapshot_id_1)
        snap2 = self.get_snapshot(token_id, snapshot_id_2)
        if snap1 is None or snap2 is None:
            return {"ok": False, "error": "快照不存在"}

        nodes1 = {n.get("id"): n for n in snap1.get("data", {}).get("nodes", [])}
        nodes2 = {n.get("id"): n for n in snap2.get("data", {}).get("nodes", [])}

        added = [nid for nid in nodes2 if nid not in nodes1]
        removed = [nid for nid in nodes1 if nid not in nodes2]
        changed = []
        for nid in nodes1:
            if nid in nodes2 and nodes1[nid] != nodes2[nid]:
                changed.append(nid)

        # 构建详细 diff 数据，包含完整节点信息
        added_nodes = [nodes2[nid] for nid in added[:50]]
        removed_nodes = [nodes1[nid] for nid in removed[:50]]
        changed_details = []
        for nid in changed[:50]:
            n1 = nodes1[nid]
            n2 = nodes2[nid]
            # 计算字段级差异
            all_keys = set(list(n1.keys()) + list(n2.keys()))
            field_diffs = []
            for k in all_keys:
                v1 = n1.get(k)
                v2 = n2.get(k)
                if v1 != v2:
                    field_diffs.append({"field": k, "old": v1, "new": v2})
            changed_details.append({
                "node_id": nid,
                "type": n1.get("type", ""),
                "name": n1.get("name", ""),
                "field_diffs": field_diffs
            })

        return {
            "ok": True,
            "snapshot_1": snapshot_id_1,
            "snapshot_2": snapshot_id_2,
            "snap1_label": snap1.get("label", ""),
            "snap2_label": snap2.get("label", ""),
            "snap1_time": snap1.get("created_at", ""),
            "snap2_time": snap2.get("created_at", ""),
            "node_count_1": len(nodes1),
            "node_count_2": len(nodes2),
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "added_nodes": added_nodes,
            "removed_nodes": removed_nodes,
            "changed_details": changed_details,
        }

    def cleanup_old_snapshots(self, token_id: str, *, retain_days: int = 30) -> int:
        """清理过期快照（默认保留 30 天）。"""
        index = self._load_index(token_id)
        snaps = index.get("snapshots", [])
        now = _utcnow()
        removed = 0

        kept = []
        for s in snaps:
            try:
                created = datetime.fromisoformat(s.get("created_at", ""))
                if (now - created).days > retain_days:
                    # 删除快照文件
                    snap_path = os.path.join(self._token_dir(token_id), s.get("file", ""))
                    if os.path.isfile(snap_path):
                        try:
                            os.remove(snap_path)
                        except Exception:
                            pass
                    removed += 1
                    continue
            except Exception:
                pass
            kept.append(s)

        index["snapshots"] = kept
        self._save_index(token_id, index)
        return removed
