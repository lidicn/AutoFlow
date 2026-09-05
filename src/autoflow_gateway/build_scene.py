#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — 场景构建器（L3 共享代码，幂等）

把 SceneIntent 转成符合约定的 NR 流 JSON（带坐标/命名/安全默认值）。
复用 nr_client 的节点构建器保证 v6/v3 schema 正确。
幂等：stable scene_id → 同一 flow；reconcile 更新而非复制。
"""
import uuid
from typing import Dict, Any, List

from .schemas import SceneIntent


def _nid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


def build_api_call_service(node_id: str, flow_id: str, action, server: str = "") -> Dict:
    """api-call-service 节点（nr_client 未内置，此处补齐）。"""
    return {
        "id": node_id,
        "type": "api-call-service",
        "z": flow_id,
        "name": f"🔧 {action.domain}.{action.service} {action.entity_id or ''}".strip(),
        "server": server,
        "version": 5,
        "debugenabled": False,
        "domain": action.domain,
        "service": action.service,
        "entityId": action.entity_id or "",
        "data": action.data or {},
        "dataType": "jsonata",
        "mergecontext": "",
        "output_location": "",
        "output_location_type": "none",
        "mustacheAltTags": False,
        "x": 400,
        "y": 120,
        "wires": [[]],
    }


def build_scene(intent: SceneIntent, nr_layer=None, flow_id: str = None) -> Dict[str, Any]:
    """从意图生成 NR flow 结构。返回 {id, type:'tab', label, nodes:[...]}。

    nr_layer 用于获取默认 HA server 节点 id（可选）。flow_id 稳定时用于幂等 reconcile。
    """
    flow_id = flow_id or ("scn_" + intent.intent_id[4:])
    server = ""
    if nr_layer is not None:
        try:
            server = nr_layer.client._get_default_server()
        except Exception:
            server = ""

    nodes: List[Dict] = []
    prev_ids: List[str] = []

    # 触发器
    for t in intent.trigger:
        if t.type == "state_changed" and t.entity_id:
            nid = _nid("trig_")
            node = nr_layer.build(
                "server_state_changed", nid, flow_id, t.entity_id,
                name=f"📡 {t.entity_id} → {t.state or '任意'}", server=server,
                wires=[[]],
            ) if nr_layer else {
                "id": nid, "type": "server-state-changed", "z": flow_id,
                "name": f"📡 {t.entity_id}", "server": server, "version": 6,
                "entities": {"entity": [t.entity_id], "substring": [], "regex": []},
                "wires": [[]],
            }
            # 若声明了触发态，则接一个 switch 过滤（简化：直接在 state_changed 上用 ifState）
            node["ifState"] = t.state or ""
            node["ifStateType"] = "str"
            node["ifStateOperator"] = "is"
            node["outputs"] = 1
            nodes.append(node)
            prev_ids.append(nid)
        elif t.type in ("time", "inject"):
            nid = _nid("trig_")
            node = nr_layer.build(
                "inject", nid, flow_id,
                name=t.payload.get("name", "⏰ 定时") if t.payload else "⏰ 定时",
                crontab=t.payload.get("crontab", "") if t.payload else "",
                wires=[[]],
            ) if nr_layer else {
                "id": nid, "type": "inject", "z": flow_id, "name": "⏰ 定时",
                "props": [{"p": "payload"}, {"p": "topic", "vt": "str"}],
                "repeat": "", "crontab": t.payload.get("crontab", "") if t.payload else "",
                "once": False, "onceDelay": 0.1, "topic": "", "payload": "",
                "payloadType": "date", "wires": [[]],
            }
            nodes.append(node)
            prev_ids.append(nid)
        else:
            # event 等：暂以 inject 占位
            nid = _nid("trig_")
            nodes.append({
                "id": nid, "type": "inject", "z": flow_id, "name": f"⚡ {t.type}",
                "props": [{"p": "payload"}], "repeat": "", "crontab": "",
                "once": False, "onceDelay": 0.1, "topic": "", "payload": "",
                "payloadType": "date", "wires": [[]],
            })
            prev_ids.append(nid)

    # 动作
    action_ids: List[str] = []
    for a in intent.action:
        nid = _nid("act_")
        node = build_api_call_service(nid, flow_id, a, server=server)
        nodes.append(node)
        action_ids.append(nid)

    # 连线：每个触发器 → 每个动作
    for pid in prev_ids:
        for aid in action_ids:
            found = next((n for n in nodes if n["id"] == pid), None)
            if found:
                if not found.get("wires"):
                    found["wires"] = [[]]
                if isinstance(found["wires"][0], list):
                    found["wires"][0].append(aid)
                else:
                    found["wires"] = [[aid]]

    # tab
    flow = {
        "id": flow_id,
        "type": "tab",
        "label": f"🤖 {intent.name}",
        "disabled": False,
        "info": intent.description,
        "nodes": nodes,
    }
    return flow
