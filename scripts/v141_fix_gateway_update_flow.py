#!/usr/bin/env python3
"""修复 gateway.py: self.nr.update_flow → update_flow_nodes（两处）"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old1 = "self.nr.update_flow(target_tab_id, merged_flow, force=True, allow_prod=allow_prod)"
new1 = "self.nr.update_flow_nodes(target_tab_id, merged_flow, force=True, allow_prod=allow_prod)"

old2 = "self.nr.update_flow(tab_id, merged_flow, force=True, allow_prod=allow_prod)"
new2 = "self.nr.update_flow_nodes(tab_id, merged_flow, force=True, allow_prod=allow_prod)"

count1 = content.count(old1)
count2 = content.count(old2)
print(f"找到 target_tab_id 版本: {count1} 处")
print(f"找到 tab_id 版本: {count2} 处")

if count1 > 0:
    content = content.replace(old1, new1)
    print(f"修复 {count1} 处 target_tab_id 版本")
if count2 > 0:
    content = content.replace(old2, new2)
    print(f"修复 {count2} 处 tab_id 版本")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

# 验证没有遗漏
remaining = content.count("self.nr.update_flow(")
print(f"\n剩余 self.nr.update_flow 调用: {remaining} 处（应为0）")
print("gateway.py 修复完成")
