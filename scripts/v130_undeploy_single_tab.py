#!/usr/bin/env python3
"""修改 gateway.py 撤回逻辑，支持单 tab 模式"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 修改点1：读取活 flow 时，单 tab 模式下读取 AutoFlow tab
old_read = '''        # 读活 flow（可能用户已手动改/加节点）
        live = None
        nr_unreachable = False
        nr_err = None
        try:
            live = self.nr.get_flow(flow_id)'''

new_read = '''        # Tab 组织模式：单 tab 模式下读取 AutoFlow tab，否则读取 flow_id 对应的 tab
        from . import tab_organizer as tab_org
        flow_tab_org_mode = meta.get("tab_org_mode", "per_flow")
        target_tab_id = meta.get("tab_id", flow_id) if flow_tab_org_mode == "single_tab" else flow_id

        # 读活 flow（可能用户已手动改/加节点）
        live = None
        nr_unreachable = False
        nr_err = None
        try:
            live = self.nr.get_flow(target_tab_id)'''

if old_read in content:
    content = content.replace(old_read, new_read, 1)
    print("修改点1: 读取活 flow 时支持单 tab 模式")
else:
    print("WARNING: 修改点1 未找到")

# 修改点2：404 时的处理，单 tab 模式下不应该因为 flow_id 找不到就清账本
old_404 = '''            if "404" in nr_err or "not found" in nr_err.lower():
                live = None
                nr_unreachable = False
            else:'''

new_404 = '''            if "404" in nr_err or "not found" in nr_err.lower():
                if flow_tab_org_mode == "single_tab":
                    # 单 tab 模式下 AutoFlow tab 不存在，视为已全部撤回
                    live = None
                    nr_unreachable = False
                else:
                    live = None
                    nr_unreachable = False
            else:'''

if old_404 in content:
    content = content.replace(old_404, new_404, 1)
    print("修改点2: 404 处理支持单 tab 模式")
else:
    print("WARNING: 修改点2 未找到")

# 修改点3：删除整个 tab 时，单 tab 模式下不删除整个 tab，只移除节点
old_delete_tab = '''        if u_preserved == 0:
            # tab 已空 → 删除整个 tab（clean）
            try:
                self.nr.delete_flow(flow_id, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 删除失败: {e}"
            action = "deleted_tab"
        else:'''

new_delete_tab = '''        if u_preserved == 0 and flow_tab_org_mode != "single_tab":
            # tab 已空 → 删除整个 tab（clean），仅 per_flow 模式
            try:
                self.nr.delete_flow(flow_id, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 删除失败: {e}"
            action = "deleted_tab"
        else:'''

if old_delete_tab in content:
    content = content.replace(old_delete_tab, new_delete_tab, 1)
    print("修改点3: 删除 tab 时单 tab 模式不删除整个 tab")
else:
    print("WARNING: 修改点3 未找到")

# 修改点4：仅移除网关节点时，单 tab 模式下更新 AutoFlow tab
old_trim = '''            # 仅移除网关节点，保留 tab + 用户节点（手术式）
            reduced = dict(live)  # 保留 label/configs 等所有原始字段
            reduced["nodes"] = ([tab_node] if tab_node else []) + user_nodes
            try:
                self.nr.update_flow_nodes(flow_id, reduced, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 更新失败: {e}"
            action = "trimmed_tab"'''

new_trim = '''            # 仅移除网关节点，保留 tab + 用户节点（手术式）
            # 单 tab 模式下更新 AutoFlow tab，per_flow 模式下更新 flow_id tab
            reduced = dict(live)
            reduced["nodes"] = ([tab_node] if tab_node else []) + user_nodes
            try:
                self.nr.update_flow_nodes(target_tab_id, reduced, force=True, allow_prod=True)
            except Exception as e:
                nr_ok = False
                nr_err = f"NR 更新失败: {e}"
            action = "trimmed_tab" if flow_tab_org_mode != "single_tab" else "trimmed_single_tab"'''

if old_trim in content:
    content = content.replace(old_trim, new_trim, 1)
    print("修改点4: 移除节点时单 tab 模式更新 AutoFlow tab")
else:
    print("WARNING: 修改点4 未找到")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

print("\ngateway.py: 撤回逻辑已修改，支持单 tab 模式")
