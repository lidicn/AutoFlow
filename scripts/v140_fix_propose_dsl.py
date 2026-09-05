#!/usr/bin/env python3
"""修复 autoflow_propose_dsl 的 deploy_token 参数"""

MCP = r"E:\NAS\autoflow\src\autoflow_gateway\mcp_server.py"
with open(MCP, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改签名
old_sig = '''async def autoflow_propose_dsl(dsl: Optional[str] = None, expected_postconditions_json: str = "[]",
                               resolved_entities_json: str = "[]", strict: bool = False,
                               require_e2e: bool = False) -> str:'''

new_sig = '''async def autoflow_propose_dsl(dsl: Optional[str] = None, expected_postconditions_json: str = "[]",
                               resolved_entities_json: str = "[]", strict: bool = False,
                               require_e2e: bool = False, deploy_token: str = "") -> str:'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. autoflow_propose_dsl 签名增加 deploy_token")
else:
    print("WARNING: 未找到签名")

# 2. 查找调用 propose_dsl 的位置
import re
matches = list(re.finditer(r'gw\.propose_dsl\(', content))
print(f"找到 {len(matches)} 处 gw.propose_dsl 调用")
for i, m in enumerate(matches):
    print(f"  位置 {i}: {content[m.start():m.start()+200]}")

with open(MCP, "w", encoding="utf-8") as f:
    f.write(content)
