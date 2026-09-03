#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tab 组织器 —— 支持单 tab 集中模式和每 flow 独立 tab 模式。

设计原则：
  · 两种模式共享同一套部署/撤回/修改底层逻辑（按节点 ID 精确操作）
  · 单 tab 模式下，所有 AutoFlow 管理的 flow 合并到固定的「AutoFlow」tab
  · 每个 flow 用 comment 节点标记边界（AF_START / AF_END），方便用户搜索定位
  · 每个 flow 分配独立的坐标区域，避免视觉重叠
  · flow_catalog 记录 tab_org_mode / tab_id / boundary_comment_ids / y_offset

模式切换：
  · 配置 AF_TAB_ORG_MODE=per_flow（默认，每个 flow 独立 tab，保持当前行为）
  · 配置 AF_TAB_ORG_MODE=single_tab（单 tab 集中模式）
  · 已部署的 flow 保持原模式，新部署的 flow 按当前模式
  · 提供迁移工具（后续版本）
"""
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

# 单 tab 模式下的固定 tab 名称
SINGLE_TAB_LABEL = "AutoFlow"
SINGLE_TAB_ID = "af_single_tab"

# 每个 flow 占用的坐标区域
FLOW_AREA_WIDTH = 2400
FLOW_AREA_HEIGHT = 1400
FLOW_AREA_GAP_Y = 400  # flow 之间的垂直间距

# 边界 comment 节点的名称前缀
BOUNDARY_START_PREFIX = "AF_START:"
BOUNDARY_END_PREFIX = "AF_END:"


def get_tab_org_mode() -> str:
    """获取当前 tab 组织模式：per_flow（默认）或 single_tab。

    优先级：运行时 feature_flags > 环境变量 AF_TAB_ORG_MODE > 默认 per_flow
    """
    # 优先从运行时 feature flags 读取
    try:
        from .config import load_feature_flags, get_config
        cfg = get_config()
        flags = load_feature_flags(cfg)
        mode = flags.get("tab_org_mode")
        if mode and mode in ("per_flow", "single_tab"):
            return mode
    except Exception:
        pass
    # 回退到环境变量
    return os.environ.get("AF_TAB_ORG_MODE", "per_flow").strip().lower()


def is_single_tab_mode() -> bool:
    """是否为单 tab 集中模式"""
    return get_tab_org_mode() == "single_tab"


def gen_node_id() -> str:
    """生成 Node-RED 节点 ID"""
    return uuid.uuid4().hex[:16]


def make_boundary_comments(flow_id: str, label: str, y_start: int
                           ) -> Tuple[Dict[str, Any], Dict[str, Any], str, str]:
    """生成 flow 的边界 comment 节点。

    返回 (start_comment, end_comment, start_id, end_id)
    """
    start_id = gen_node_id()
    end_id = gen_node_id()
    start_comment = {
        "id": start_id,
        "type": "comment",
        "z": SINGLE_TAB_ID,
        "name": f"{BOUNDARY_START_PREFIX}{flow_id}",
        "info": f"AutoFlow 管理的 flow 开始\nflow_id: {flow_id}\n名称: {label}\n\n请勿删除此 comment，否则撤回时可能无法精确定位。",
        "x": 80,
        "y": y_start,
        "w": 300,
        "h": 80,
    }
    end_comment = {
        "id": end_id,
        "type": "comment",
        "z": SINGLE_TAB_ID,
        "name": f"{BOUNDARY_END_PREFIX}{flow_id}",
        "info": f"AutoFlow 管理的 flow 结束\nflow_id: {flow_id}",
        "x": 80,
        "y": y_start + FLOW_AREA_HEIGHT - 40,
        "w": 300,
        "h": 60,
    }
    return start_comment, end_comment, start_id, end_id


def assign_y_offset(existing_flows: List[Dict[str, Any]]) -> int:
    """为新 flow 分配 y 坐标偏移量。

    existing_flows: 当前单 tab 中已有的 flow meta 列表
    返回新 flow 的 y_start 坐标
    """
    if not existing_flows:
        return 40
    # 找到最大的 y_offset + FLOW_AREA_HEIGHT + GAP
    max_y = 40
    for meta in existing_flows:
        y = meta.get("y_offset", 40)
        if y + FLOW_AREA_HEIGHT > max_y:
            max_y = y + FLOW_AREA_HEIGHT
    return max_y + FLOW_AREA_GAP_Y


def shift_flow_nodes(nodes: List[Dict[str, Any]], y_offset: int,
                     tab_id: str) -> List[Dict[str, Any]]:
    """将 flow 的节点平移到指定坐标区域，并修正 z（所属 tab）。

    nodes: 编译产出的节点列表
    y_offset: y 坐标偏移
    tab_id: 所属 tab 的 ID
    返回平移后的节点列表
    """
    shifted = []
    for n in nodes:
        node = dict(n)
        # 修正 z（所属 tab）
        node["z"] = tab_id
        # 平移坐标（x 保持不变，y 加上偏移）
        if "x" in node:
            node["x"] = int(node.get("x", 0))
        if "y" in node:
            node["y"] = int(node.get("y", 0)) + y_offset
        shifted.append(node)
    return shifted


def find_flow_boundary(tab_nodes: List[Dict[str, Any]], flow_id: str
                        ) -> Tuple[Optional[str], Optional[str]]:
    """在 tab 的节点中查找指定 flow 的边界 comment ID。

    返回 (start_id, end_id)，找不到则为 None
    """
    start_id = None
    end_id = None
    for n in tab_nodes:
        if n.get("type") == "comment":
            name = n.get("name", "")
            if name == f"{BOUNDARY_START_PREFIX}{flow_id}":
                start_id = n.get("id")
            elif name == f"{BOUNDARY_END_PREFIX}{flow_id}":
                end_id = n.get("id")
    return start_id, end_id


def get_single_tab(nr_client) -> Optional[Dict[str, Any]]:
    """获取单 tab 模式下的固定 AutoFlow tab，不存在则返回 None。"""
    try:
        return nr_client.get_flow(SINGLE_TAB_ID)
    except Exception:
        return None


def get_or_create_single_tab(nr_client, allow_prod: bool = True) -> Dict[str, Any]:
    """获取或创建单 tab 模式下的固定 AutoFlow tab。"""
    tab = get_single_tab(nr_client)
    if tab is not None:
        return tab
    # 创建空 tab
    empty_tab = {
        "id": SINGLE_TAB_ID,
        "label": SINGLE_TAB_LABEL,
        "nodes": [
            {
                "id": SINGLE_TAB_ID,
                "type": "tab",
                "label": SINGLE_TAB_LABEL,
                "disabled": False,
                "info": "AutoFlow 集中管理 tab\n\n所有 AutoFlow 部署的 flow 都在此 tab 中，用 comment 节点分隔。\n请勿手动删除 AF_START/AF_END comment 节点。",
            }
        ],
        "configs": {},
    }
    nr_client.create_or_update_flow(SINGLE_TAB_ID, empty_tab, force=True,
                                     allow_prod=allow_prod)
    return nr_client.get_flow(SINGLE_TAB_ID)


def list_single_tab_flows(flow_catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """列出单 tab 模式下已部署的所有 flow meta。"""
    flows = flow_catalog.get("flows", {})
    return [meta for meta in flows.values()
            if meta.get("tab_org_mode") == "single_tab"]


def validate_boundary_integrity(tab_nodes: List[Dict[str, Any]],
                                 flow_id: str,
                                 deployed_node_ids: List[str]) -> Dict[str, Any]:
    """校验 flow 边界完整性。

    返回 {ok, issues: [str], start_id, end_id}
    """
    issues = []
    start_id, end_id = find_flow_boundary(tab_nodes, flow_id)

    if start_id is None:
        issues.append(f"缺少 AF_START 边界 comment (flow_id={flow_id})")
    if end_id is None:
        issues.append(f"缺少 AF_END 边界 comment (flow_id={flow_id})")

    # 检查部署的节点是否都在 tab 中
    tab_node_ids = {n.get("id") for n in tab_nodes}
    missing_nodes = [nid for nid in deployed_node_ids if nid not in tab_node_ids]
    if missing_nodes:
        issues.append(f"{len(missing_nodes)} 个已登记节点在 tab 中找不到（可能已被手动删除）")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "start_id": start_id,
        "end_id": end_id,
    }


# ═══════════════════════════════════════════════════════════════
# P2: 模式迁移（per_flow ↔ single_tab）
# ═══════════════════════════════════════════════════════════════

def migrate_per_flow_to_single_tab(nr_client, state, allow_prod: bool = True
                                    ) -> Dict[str, Any]:
    """将所有 per_flow 模式的 flow 迁移到 single_tab 模式。

    步骤：
      1. 获取所有 tab_org_mode=per_flow 的 flow
      2. 对每个 flow：读取节点 → 分配新坐标 → 生成边界 comment →
         平移节点 → 合并到 AutoFlow tab → 删除原独立 tab → 更新账本
      3. 返回迁移结果

    返回 {ok, migrated: [flow_id], failed: [{flow_id, error}], total}
    """
    catalog = state.get_flow_catalog()
    flows = catalog.get("flows", {})
    per_flow_flows = [
        (fid, meta) for fid, meta in flows.items()
        if meta.get("tab_org_mode", "per_flow") == "per_flow"
    ]

    if not per_flow_flows:
        return {"ok": True, "migrated": [], "failed": [], "total": 0,
                "note": "没有 per_flow 模式的 flow 需要迁移"}

    # 获取或创建 AutoFlow tab
    af_tab = get_or_create_single_tab(nr_client, allow_prod=allow_prod)
    existing_single_flows = list_single_tab_flows(catalog)
    current_y = assign_y_offset(existing_single_flows)

    migrated = []
    failed = []

    for fid, meta in per_flow_flows:
        try:
            # 读取原 flow 的节点
            try:
                orig_flow = nr_client.get_flow(fid)
            except Exception:
                failed.append({"flow_id": fid, "error": "原 flow 不存在或无法读取"})
                continue

            orig_nodes = orig_flow.get("nodes", [])
            # 过滤掉 tab 节点（type=tab）
            flow_nodes = [n for n in orig_nodes if n.get("type") != "tab"]

            if not flow_nodes:
                failed.append({"flow_id": fid, "error": "原 flow 没有节点"})
                continue

            # 分配 y 坐标
            y_offset = current_y
            current_y += FLOW_AREA_HEIGHT + FLOW_AREA_GAP_Y

            # 生成边界 comment
            label = meta.get("label", "")
            start_c, end_c, start_id, end_id = make_boundary_comments(fid, label, y_offset)

            # 平移节点到新坐标，修正 z
            shifted_nodes = shift_flow_nodes(flow_nodes, y_offset + 100, SINGLE_TAB_ID)

            # 合并到 AutoFlow tab
            af_tab = nr_client.get_flow(SINGLE_TAB_ID)
            existing_nodes = af_tab.get("nodes", [])
            other_nodes = [n for n in existing_nodes if n.get("type") != "tab"]
            tab_node = next((n for n in existing_nodes if n.get("type") == "tab"), None)
            if tab_node is None:
                tab_node = {"id": SINGLE_TAB_ID, "type": "tab", "label": SINGLE_TAB_LABEL}

            merged_nodes = [tab_node] + other_nodes + [start_c, end_c] + shifted_nodes
            merged_flow = dict(af_tab)
            merged_flow["nodes"] = merged_nodes
            merged_flow["id"] = SINGLE_TAB_ID
            nr_client.update_flow(SINGLE_TAB_ID, merged_flow, force=True, allow_prod=allow_prod)

            # 删除原独立 tab
            try:
                nr_client.delete_flow(fid, force=True, allow_prod=allow_prod)
            except Exception:
                pass  # 删除失败不影响迁移，原 tab 可能已被手动删除

            # 更新账本
            deployed_ids = [n.get("id") for n in shifted_nodes if n.get("id")]
            deployed_ids.extend([start_id, end_id])
            new_meta = dict(meta)
            new_meta.update({
                "tab_org_mode": "single_tab",
                "tab_id": SINGLE_TAB_ID,
                "boundary_comment_ids": [start_id, end_id],
                "y_offset": y_offset,
                "deployed_node_ids": deployed_ids,
                "migrated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat(),
                "migrated_from": "per_flow",
            })
            state.upsert_flow(fid, new_meta)
            migrated.append(fid)

        except Exception as e:
            failed.append({"flow_id": fid, "error": str(e)[:200]})

    return {
        "ok": len(failed) == 0,
        "migrated": migrated,
        "failed": failed,
        "total": len(per_flow_flows),
    }


def migrate_single_tab_to_per_flow(nr_client, state, allow_prod: bool = True
                                    ) -> Dict[str, Any]:
    """将所有 single_tab 模式的 flow 迁移到 per_flow 模式。

    步骤：
      1. 获取 AutoFlow tab 中所有 tab_org_mode=single_tab 的 flow
      2. 对每个 flow：提取节点 → 创建新独立 tab → 平移节点到新坐标 →
         部署到新 tab → 从 AutoFlow tab 移除节点 → 更新账本
      3. 返回迁移结果

    返回 {ok, migrated: [flow_id], failed: [{flow_id, error}], total}
    """
    catalog = state.get_flow_catalog()
    flows = catalog.get("flows", {})
    single_flows = [
        (fid, meta) for fid, meta in flows.items()
        if meta.get("tab_org_mode") == "single_tab"
    ]

    if not single_flows:
        return {"ok": True, "migrated": [], "failed": [], "total": 0,
                "note": "没有 single_tab 模式的 flow 需要迁移"}

    # 读取 AutoFlow tab
    try:
        af_tab = nr_client.get_flow(SINGLE_TAB_ID)
    except Exception:
        return {"ok": False, "error": "AutoFlow tab 不存在或无法读取",
                "migrated": [], "failed": [], "total": len(single_flows)}

    af_nodes = af_tab.get("nodes", [])
    af_node_ids = {n.get("id") for n in af_nodes}

    migrated = []
    failed = []
    nodes_to_remove = set()  # 累积要从 AutoFlow tab 移除的节点 ID

    for fid, meta in single_flows:
        try:
            deployed_ids = set(meta.get("deployed_node_ids", []))
            boundary_ids = set(meta.get("boundary_comment_ids", []))
            all_ids = deployed_ids | boundary_ids

            # 提取该 flow 的节点
            flow_nodes = [n for n in af_nodes if n.get("id") in all_ids]
            if not flow_nodes:
                failed.append({"flow_id": fid, "error": "在 AutoFlow tab 中找不到该 flow 的节点"})
                continue

            # 计算 y 偏移量（用于重置坐标）
            y_offsets = [n.get("y", 0) for n in flow_nodes if "y" in n]
            min_y = min(y_offsets) if y_offsets else 0

            # 重置节点坐标（y 减去 min_y，z 改为新 flow id）
            reset_nodes = []
            for n in flow_nodes:
                if n.get("type") == "comment":
                    continue  # 边界 comment 不迁移到新 tab
                node = dict(n)
                node["z"] = fid
                if "y" in node:
                    node["y"] = int(node.get("y", 0)) - min_y + 40
                reset_nodes.append(node)

            if not reset_nodes:
                failed.append({"flow_id": fid, "error": "没有可迁移的节点（只有边界 comment）"})
                continue

            # 创建新独立 tab
            tab_node = {"id": fid, "type": "tab", "label": meta.get("label", fid),
                        "disabled": False, "info": meta.get("purpose", "")}
            new_flow = {
                "id": fid,
                "label": meta.get("label", fid),
                "nodes": [tab_node] + reset_nodes,
                "configs": {},
            }
            nr_client.create_or_update_flow(fid, new_flow, force=True, allow_prod=allow_prod)

            # 累积要移除的节点
            nodes_to_remove.update(all_ids)

            # 更新账本
            new_meta = dict(meta)
            new_meta.update({
                "tab_org_mode": "per_flow",
                "tab_id": fid,
                "boundary_comment_ids": [],
                "y_offset": None,
                "deployed_node_ids": [n.get("id") for n in reset_nodes if n.get("id")],
                "migrated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat(),
                "migrated_from": "single_tab",
            })
            state.upsert_flow(fid, new_meta)
            migrated.append(fid)

        except Exception as e:
            failed.append({"flow_id": fid, "error": str(e)[:200]})

    # 从 AutoFlow tab 移除所有已迁移的节点
    if nodes_to_remove:
        try:
            af_tab = nr_client.get_flow(SINGLE_TAB_ID)
            remaining_nodes = [n for n in af_tab.get("nodes", [])
                                if n.get("id") not in nodes_to_remove]
            # 确保 tab 节点存在
            if not any(n.get("type") == "tab" for n in remaining_nodes):
                remaining_nodes.insert(0, {"id": SINGLE_TAB_ID, "type": "tab",
                                            "label": SINGLE_TAB_LABEL})
            updated_flow = dict(af_tab)
            updated_flow["nodes"] = remaining_nodes
            updated_flow["id"] = SINGLE_TAB_ID
            nr_client.update_flow(SINGLE_TAB_ID, updated_flow, force=True, allow_prod=allow_prod)
        except Exception as e:
            # 移除失败不影响迁移结果，但记录警告
            for m in migrated:
                failed.append({"flow_id": m, "error": f"迁移成功但从 AutoFlow tab 移除节点失败: {str(e)[:100]}"})
            migrated = []

    return {
        "ok": len(failed) == 0,
        "migrated": migrated,
        "failed": failed,
        "total": len(single_flows),
    }


def get_migration_status(state) -> Dict[str, Any]:
    """获取当前迁移状态统计。"""
    try:
        catalog = state.get_flow_catalog()
    except Exception:
        catalog = {"flows": {}}
    flows = catalog.get("flows", {}) if isinstance(catalog, dict) else {}
    per_flow_count = 0
    single_tab_count = 0
    mixed_count = 0
    for m in flows.values():
        if not isinstance(m, dict):
            continue
        mode = m.get("tab_org_mode", "per_flow")
        if mode == "single_tab":
            single_tab_count += 1
        elif mode == "mixed":
            mixed_count += 1
        else:
            per_flow_count += 1
    return {
        "current_mode": get_tab_org_mode(),
        "per_flow_count": per_flow_count,
        "single_tab_count": single_tab_count,
        "mixed_count": mixed_count,
        "total_flows": len(flows),
        "can_migrate_to_single": per_flow_count > 0,
        "can_migrate_to_per_flow": single_tab_count > 0,
    }
