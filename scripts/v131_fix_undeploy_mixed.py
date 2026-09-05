#!/usr/bin/env python3
"""修改 gateway.py 撤回逻辑支持 mixed 模式"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # Tab 组织模式：单 tab 模式下读取 AutoFlow tab，否则读取 flow_id 对应的 tab
        from . import tab_organizer as tab_org
        flow_tab_org_mode = meta.get("tab_org_mode", "per_flow")
        target_tab_id = meta.get("tab_id", flow_id) if flow_tab_org_mode == "single_tab" else flow_id'''

new = '''        # Tab 组织模式：single_tab/mixed 模式下读取对应 tab，否则读取 flow_id 对应的 tab
        from . import tab_organizer as tab_org
        flow_tab_org_mode = meta.get("tab_org_mode", "per_flow")
        target_tab_id = meta.get("tab_id", flow_id) if flow_tab_org_mode in ("single_tab", "mixed") else flow_id'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("撤回逻辑已支持 mixed 模式")
else:
    print("WARNING: 未找到目标代码")
