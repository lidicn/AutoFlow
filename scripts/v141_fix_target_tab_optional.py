#!/usr/bin/env python3
"""修复 deploy_tokens.py: target_tab 改为可选"""

DT = r"E:\NAS\autoflow\src\autoflow_gateway\deploy_tokens.py"
with open(DT, "r", encoding="utf-8") as f:
    content = f.read()

# 1. create_token 签名：target_tab 改为可选
old_sig = '''    def create_token(self, *, name: str, target_tab: str,
                     expires_in_hours: float = DEFAULT_TOKEN_TTL_HOURS,'''
new_sig = '''    def create_token(self, *, name: str, target_tab: Optional[str] = None,
                     expires_in_hours: float = DEFAULT_TOKEN_TTL_HOURS,'''
if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. create_token target_tab 改为可选")
else:
    print("WARNING: 未找到 create_token 签名")

# 2. token_data 中 target_tab 允许为 None
old_data = '''            "name": name,
            "target_tab": target_tab,'''
new_data = '''            "name": name,
            "target_tab": target_tab or None,  # None=不绑定tab，走 per_flow 模式'''
if old_data in content:
    content = content.replace(old_data, new_data, 1)
    print("2. token_data target_tab 允许 None")
else:
    print("WARNING: 未找到 token_data target_tab")

with open(DT, "w", encoding="utf-8") as f:
    f.write(content)
print("\ndeploy_tokens.py 修复完成")
