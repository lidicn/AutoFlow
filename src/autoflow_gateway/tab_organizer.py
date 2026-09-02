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
