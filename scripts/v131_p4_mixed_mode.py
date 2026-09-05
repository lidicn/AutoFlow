#!/usr/bin/env python3
"""修改 gateway.py: deploy_proposal 增加 target_tab 参数（P4 混合模式）"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 函数签名增加 target_tab 参数
old_sig = '''    def deploy_proposal(self, pid: str, agent_id: str = "human",
                        target_flow_id: Optional[str] = None,
                        target: str = "prod", force: bool = False,
                        validate: bool = True, allow_prod: bool = True,
                        vhass_store=None,
                        dry_run: bool = False,
                        require_e2e: Optional[bool] = None) -> Dict[str, Any]:'''

new_sig = '''    def deploy_proposal(self, pid: str, agent_id: str = "human",
                        target_flow_id: Optional[str] = None,
                        target: str = "prod", force: bool = False,
                        validate: bool = True, allow_prod: bool = True,
                        vhass_store=None,
                        dry_run: bool = False,
                        require_e2e: Optional[bool] = None,
                        target_tab: Optional[str] = None) -> Dict[str, Any]:'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. 函数签名增加 target_tab 参数")
else:
    print("WARNING: 未找到函数签名")

# 2. 在 Tab 组织模式判断处增加 mixed 模式
old_mode = '''        # ── Tab 组织模式：per_flow（默认）或 single_tab ──
        from . import tab_organizer as tab_org
        use_single_tab = tab_org.is_single_tab_mode()

        if use_single_tab:'''

new_mode = '''        # ── Tab 组织模式：per_flow（默认）/ single_tab / mixed（P4）──
        from . import tab_organizer as tab_org
        use_single_tab = tab_org.is_single_tab_mode()
        use_mixed_tab = target_tab is not None  # P4: 手动指定目标 tab

        if use_mixed_tab:
            # P4 混合模式：部署到用户指定的 tab
            try:
                # 查找或创建目标 tab
                target_tab_id = None
                target_tab_label = target_tab
                all_flows = self.nr.list_flows()
                for f in all_flows:
                    if f.get("id") == target_tab or f.get("label") == target_tab:
                        target_tab_id = f.get("id")
                        target_tab_label = f.get("label", target_tab)
                        break
                if target_tab_id is None:
                    # 创建新 tab
                    new_tab = {
                        "id": target_tab,
                        "label": target_tab_label,
                        "nodes": [{"id": target_tab, "type": "tab",
                                   "label": target_tab_label, "disabled": False}],
                        "configs": {},
                    }
                    self.nr.create_or_update_flow(target_tab, new_tab, force=True,
                                                   allow_prod=allow_prod)
                    target_tab_id = target_tab

                # 分配 y 坐标（在目标 tab 中）
                catalog = self.state.get_flow_catalog()
                existing_in_tab = [m for m in catalog.get("flows", {}).values()
                                    if m.get("tab_id") == target_tab_id]
                y_offset = tab_org.assign_y_offset(existing_in_tab)

                # 生成边界 comment
                flow_label = flow.get("label", "")
                start_c, end_c, start_id, end_id = tab_org.make_boundary_comments(
                    deploy_id, flow_label, y_offset)
                start_c["z"] = target_tab_id
                end_c["z"] = target_tab_id

                # 平移节点
                shifted_nodes = tab_org.shift_flow_nodes(
                    flow.get("nodes", []), y_offset + 100, target_tab_id)

                # 合并到目标 tab
                target_tab_data = self.nr.get_flow(target_tab_id)
                existing_nodes = target_tab_data.get("nodes", [])
                other_nodes = [n for n in existing_nodes if n.get("type") != "tab"]
                tab_node = next((n for n in existing_nodes if n.get("type") == "tab"), None)
                if tab_node is None:
                    tab_node = {"id": target_tab_id, "type": "tab", "label": target_tab_label}
                merged_nodes = [tab_node] + other_nodes + [start_c, end_c] + shifted_nodes
                merged_flow = dict(target_tab_data)
                merged_flow["nodes"] = merged_nodes
                merged_flow["id"] = target_tab_id
                self.nr.update_flow(target_tab_id, merged_flow, force=True, allow_prod=allow_prod)

                fid = deploy_id
                created = True
                gateway_node_ids = [n.get("id") for n in shifted_nodes if n.get("id")]
                gateway_node_ids.extend([start_id, end_id])
                tab_org_mode = "mixed"
                boundary_comment_ids = [start_id, end_id]
                flow_y_offset = y_offset
                tab_id = target_tab_id

            except Exception as e:
                return {"ok": False, "error": f"混合模式部署失败（target_tab={target_tab}）: {e}"}
        elif use_single_tab:'''

if old_mode in content:
    content = content.replace(old_mode, new_mode, 1)
    print("2. 增加 mixed 混合模式部署逻辑")
else:
    print("WARNING: 未找到 Tab 组织模式判断处")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

print("\ngateway.py 修改完成")
