#!/usr/bin/env python3
"""修复空 tab 覆盖逻辑：空 tab 时更新原 tab，不另建副本"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        if existing and existing.get("id") not in self.state.get_flow_catalog().get("flows", {}):
            # 检查是否为空 tab（撤回后残留）：只有 tab 节点，无其他节点
            is_empty_tab = False
            try:
                _existing_flow = self.nr.get_flow(existing["id"])
                _non_tab_nodes = [n for n in _existing_flow.get("nodes", []) if n.get("type") != "tab"]
                is_empty_tab = len(_non_tab_nodes) == 0
            except Exception:
                pass
            if is_empty_tab:
                # 空 tab 是撤回后残留，允许覆盖（视为本网关之前部署的）
                pass
            elif not force:
                return {
                    "ok": False, "conflict": True,
                    "error": f"NR 中已存在同名 flow「{label}」({existing.get('id')})，且非本网关部署，避免覆盖。可改名后重试，或 force=true 以新建副本。",
                    "existing": {"id": existing.get("id"), "label": label},
                }
            # force：改名新建副本，绝不覆盖用户已有 flow
            # 注意后缀避开受保护标签（protected_flow_labels 含 "AutoFlow"，子串匹配会触发拒绝）
            label = f"{label} (网关副本)"
            flow["label"] = label'''

new = '''        if existing and existing.get("id") not in self.state.get_flow_catalog().get("flows", {}):
            # 检查是否为空 tab（撤回后残留）：只有 tab 节点，无其他节点
            is_empty_tab = False
            try:
                _existing_flow = self.nr.get_flow(existing["id"])
                _non_tab_nodes = [n for n in _existing_flow.get("nodes", []) if n.get("type") != "tab"]
                is_empty_tab = len(_non_tab_nodes) == 0
            except Exception:
                pass
            if is_empty_tab:
                # 空 tab 是撤回后残留，直接覆盖原 tab（设置 target_flow_id 走 update_flow 分支）
                target_flow_id = existing["id"]
            elif not force:
                return {
                    "ok": False, "conflict": True,
                    "error": f"NR 中已存在同名 flow「{label}」({existing.get('id')})，且非本网关部署，避免覆盖。可改名后重试，或 force=true 以新建副本。",
                    "existing": {"id": existing.get("id"), "label": label},
                }
            else:
                # force：改名新建副本，绝不覆盖用户已有 flow
                # 注意后缀避开受保护标签（protected_flow_labels 含 "AutoFlow"，子串匹配会触发拒绝）
                label = f"{label} (网关副本)"
                flow["label"] = label'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("空 tab 覆盖逻辑修复：空 tab 时设置 target_flow_id 更新原 tab")
else:
    print("ERROR: 未找到目标代码")
