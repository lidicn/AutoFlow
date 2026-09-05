#!/usr/bin/env python3
"""后端 target_tab 增加 URL 解析容错：从完整 URL 中提取 tab id"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''            # 确定目标 tab：授权码绑定的 target_tab 优先级最高
            effective_target_tab = target_tab_from_token or target_tab

            # 部署前做快照
            try:
                if effective_target_tab:
                    # 查找目标 tab 的 flow_id
                    all_flows = self.nr.list_flows()
                    target_flow = None
                    for f in all_flows:
                        if f.get("label") == effective_target_tab or f.get("id") == effective_target_tab:
                            target_flow = f
                            break'''

new = '''            # 确定目标 tab：授权码绑定的 target_tab 优先级最高
            effective_target_tab = target_tab_from_token or target_tab

            # URL 容错：如果用户粘贴了完整 NR URL（如 http://host:1880/#flow/abc123），
            # 从中提取 tab id（#flow/ 后面的部分），避免匹配失败。
            if effective_target_tab and "#flow/" in effective_target_tab:
                effective_target_tab = effective_target_tab.split("#flow/")[-1].split("/")[0].split("?")[0].strip()

            # 部署前做快照
            try:
                if effective_target_tab:
                    # 查找目标 tab 的 flow_id
                    all_flows = self.nr.list_flows()
                    target_flow = None
                    for f in all_flows:
                        if f.get("label") == effective_target_tab or f.get("id") == effective_target_tab:
                            target_flow = f
                            break'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("target_tab URL 容错: OK")
else:
    print("target_tab URL 容错: NOT FOUND")
