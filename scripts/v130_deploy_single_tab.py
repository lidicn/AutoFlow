#!/usr/bin/env python3
"""修改 gateway.py 部署逻辑，支持单 tab 模式"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old_block = '''        try:
            result = self.nr.create_or_update_flow(deploy_id, flow, force=True,
                                                  allow_prod=allow_prod)
        except Exception as e:
            return {"ok": False, "error": f"NR 部署失败: {e}"}
        fid = result.get("id") or deploy_id
        created = result.get("created", False)

        # 登记 flow_catalog（owner=部署它的 agent）—— 撤回的唯一依据
        # deployed_node_ids：compile 产出的全部节点 id，撤回时只删这些（手术式移除）
        gateway_node_ids = [n.get("id") for n in flow.get("nodes", []) if n.get("id")]
        meta = {
            "flow_id": fid,
            "label": flow.get("label", ""),
            "owner_agent": agent_id,
            "purpose": flow.get("info", ""),
            "entities_touched": self._collect_entities(flow),
            "node_count": len(flow.get("nodes", [])),
            "deployed_node_ids": gateway_node_ids,
            "source_proposal": pid,
            "source": (p.source if p is not None else "compiler"),
            "nr_url": getattr(self.cfg, "nr_url", ""),
            "deployed_at": datetime.now(timezone.utc).isoformat(),
        }'''

new_block = '''        # ── Tab 组织模式：per_flow（默认）或 single_tab ──
        from . import tab_organizer as tab_org
        use_single_tab = tab_org.is_single_tab_mode()

        if use_single_tab:
            # 单 tab 模式：所有 flow 合并到固定的 AutoFlow tab
            try:
                af_tab = tab_org.get_or_create_single_tab(self.nr, allow_prod=allow_prod)
                tab_id = tab_org.SINGLE_TAB_ID
                existing_flows = tab_org.list_single_tab_flows(self.state.get_flow_catalog())
                y_offset = tab_org.assign_y_offset(existing_flows)
                flow_label = flow.get("label", "")
                start_c, end_c, start_id, end_id = tab_org.make_boundary_comments(
                    deploy_id, flow_label, y_offset)
                shifted_nodes = tab_org.shift_flow_nodes(
                    flow.get("nodes", []), y_offset + 100, tab_id)
                existing_nodes = af_tab.get("nodes", [])
                other_nodes = [n for n in existing_nodes if n.get("type") != "tab"]
                tab_node = next((n for n in existing_nodes if n.get("type") == "tab"), None)
                if tab_node is None:
                    tab_node = {"id": tab_id, "type": "tab", "label": tab_org.SINGLE_TAB_LABEL}
                merged_nodes = [tab_node] + other_nodes + [start_c, end_c] + shifted_nodes
                merged_flow = dict(af_tab)
                merged_flow["nodes"] = merged_nodes
                merged_flow["id"] = tab_id
                self.nr.update_flow(tab_id, merged_flow, force=True, allow_prod=allow_prod)
                fid = deploy_id
                created = True
                gateway_node_ids = [n.get("id") for n in shifted_nodes if n.get("id")]
                gateway_node_ids.extend([start_id, end_id])
                tab_org_mode = "single_tab"
                boundary_comment_ids = [start_id, end_id]
                flow_y_offset = y_offset
            except Exception as e:
                return {"ok": False, "error": f"单 tab 模式部署失败: {e}"}
        else:
            try:
                result = self.nr.create_or_update_flow(deploy_id, flow, force=True,
                                                      allow_prod=allow_prod)
            except Exception as e:
                return {"ok": False, "error": f"NR 部署失败: {e}"}
            fid = result.get("id") or deploy_id
            created = result.get("created", False)
            gateway_node_ids = [n.get("id") for n in flow.get("nodes", []) if n.get("id")]
            tab_org_mode = "per_flow"
            boundary_comment_ids = []
            flow_y_offset = None
            tab_id = fid

        # 登记 flow_catalog（owner=部署它的 agent）—— 撤回的唯一依据
        meta = {
            "flow_id": fid,
            "label": flow.get("label", ""),
            "owner_agent": agent_id,
            "purpose": flow.get("info", ""),
            "entities_touched": self._collect_entities(flow),
            "node_count": len(flow.get("nodes", [])),
            "deployed_node_ids": gateway_node_ids,
            "source_proposal": pid,
            "source": (p.source if p is not None else "compiler"),
            "nr_url": getattr(self.cfg, "nr_url", ""),
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "tab_org_mode": tab_org_mode,
            "tab_id": tab_id,
            "boundary_comment_ids": boundary_comment_ids,
            "y_offset": flow_y_offset,
        }'''

if old_block in content:
    content = content.replace(old_block, new_block, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("gateway.py: 部署逻辑已修改，支持单 tab 模式")
else:
    print("ERROR: 未找到目标代码块")
    # 调试：查找附近的代码
    import re
    idx = content.find("create_or_update_flow(deploy_id")
    if idx >= 0:
        print(f"找到 create_or_update_flow 在位置 {idx}")
        print(content[idx-100:idx+200])
