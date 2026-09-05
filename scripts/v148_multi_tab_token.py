#!/usr/bin/env python3
"""deploy_tokens.py 支持多 tab 授权"""

DT = r"E:\NAS\autoflow\src\autoflow_gateway\deploy_tokens.py"
with open(DT, "r", encoding="utf-8") as f:
    content = f.read()

# 1. create_token 增加 target_tabs 参数
old_sig = '''    def create_token(self, *, name: str, target_tab: Optional[str] = None,
                     expires_in_hours: float = DEFAULT_TOKEN_TTL_HOURS,'''

new_sig = '''    def create_token(self, *, name: str, target_tab: Optional[str] = None,
                     target_tabs: Optional[List[str]] = None,
                     expires_in_hours: float = DEFAULT_TOKEN_TTL_HOURS,'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. create_token 增加 target_tabs 参数: OK")
else:
    print("1. create_token 增加 target_tabs 参数: NOT FOUND")

# 2. 存储时用 target_tabs 列表
old_store = '''            "target_tab": target_tab or None,  # None=不绑定tab，走 per_flow 模式'''

new_store = '''            # target_tabs: 允许部署的 tab id 列表；空列表/None=不绑定，走 per_flow 模式
            # 兼容旧字段 target_tab（单 tab），自动转为 target_tabs
            "target_tab": target_tab or None,
            "target_tabs": target_tabs if target_tabs else ([target_tab] if target_tab else None),'''

if old_store in content:
    content = content.replace(old_store, new_store, 1)
    print("2. 存储 target_tabs 列表: OK")
else:
    print("2. 存储 target_tabs 列表: NOT FOUND")

# 3. validate_token 增加 target_tab 参数
old_val_sig = '''    def validate_token(self, token_plaintext: str, *, operation: str,
                       agent_id: Optional[str] = None,
                       nr_instance: Optional[str] = None,
                       node_count: int = 0) -> Dict[str, Any]:'''

new_val_sig = '''    def validate_token(self, token_plaintext: str, *, operation: str,
                       agent_id: Optional[str] = None,
                       nr_instance: Optional[str] = None,
                       node_count: int = 0,
                       target_tab: Optional[str] = None) -> Dict[str, Any]:'''

if old_val_sig in content:
    content = content.replace(old_val_sig, new_val_sig, 1)
    print("3. validate_token 增加 target_tab 参数: OK")
else:
    print("3. validate_token 增加 target_tab 参数: NOT FOUND")

# 4. 在 validate_token 中增加 target_tab 检查（在 bound_agent 检查之后）
old_bound_check = '''        # 检查绑定的 agent
        if token_data.get("bound_agent") and agent_id and token_data["bound_agent"] != agent_id:
            return {"ok": False, "error": f"授权码绑定的 agent 是 {token_data['bound_agent']}，当前 agent 是 {agent_id}",
                    "token_id": token_id}'''

new_bound_check = '''        # 检查绑定的 agent
        if token_data.get("bound_agent") and agent_id and token_data["bound_agent"] != agent_id:
            return {"ok": False, "error": f"授权码绑定的 agent 是 {token_data['bound_agent']}，当前 agent 是 {agent_id}",
                    "token_id": token_id}

        # 检查目标 tab 是否在允许列表中
        allowed_tabs = token_data.get("target_tabs") or ([token_data["target_tab"]] if token_data.get("target_tab") else None)
        if allowed_tabs and target_tab:
            # 兼容 URL 格式：提取 #flow/ 后面的 tab id
            check_tab = target_tab
            if "#flow/" in check_tab:
                check_tab = check_tab.split("#flow/")[-1].split("/")[0].split("?")[0]
            if check_tab not in allowed_tabs:
                return {"ok": False, "error": f"授权码只允许在指定 tab 操作，当前 tab 不在允许列表中",
                        "token_id": token_id, "allowed_tabs": allowed_tabs}'''

if old_bound_check in content:
    content = content.replace(old_bound_check, new_bound_check, 1)
    print("4. validate_token 增加 target_tab 检查: OK")
else:
    print("4. validate_token 增加 target_tab 检查: NOT FOUND")

with open(DT, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
