#!/usr/bin/env python3
"""gateway.py: validate_token 传入 target_tab，支持 target_tabs 列表"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 1. validate_token 调用传入 target_tab
old_val = '''            # 验证授权码
            validation = token_store.validate_token(
                token_plaintext, operation=operation, agent_id=agent_id,
                node_count=node_count)'''

new_val = '''            # 验证授权码（传入目标 tab 做越界检查）
            validation = token_store.validate_token(
                token_plaintext, operation=operation, agent_id=agent_id,
                node_count=node_count, target_tab=target_tab)'''

if old_val in content:
    content = content.replace(old_val, new_val, 1)
    print("1. validate_token 传入 target_tab: OK")
else:
    print("1. validate_token 传入 target_tab: NOT FOUND")

# 2. target_tab_from_token 支持 target_tabs 列表
old_target = '''            token_id = validation["token_id"]
            token_data = token_store.get_token(token_id)
            target_tab_from_token = token_data.get("target_tab") if token_data else None'''

new_target = '''            token_id = validation["token_id"]
            token_data = token_store.get_token(token_id)
            # 优先取 target_tabs 列表的第一个，兼容旧字段 target_tab
            target_tabs_from_token = token_data.get("target_tabs") if token_data else None
            if not target_tabs_from_token and token_data and token_data.get("target_tab"):
                target_tabs_from_token = [token_data["target_tab"]]
            target_tab_from_token = target_tabs_from_token[0] if target_tabs_from_token else None'''

if old_target in content:
    content = content.replace(old_target, new_target, 1)
    print("2. target_tab_from_token 支持列表: OK")
else:
    print("2. target_tab_from_token 支持列表: NOT FOUND")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
