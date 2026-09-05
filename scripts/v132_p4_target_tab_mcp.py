#!/usr/bin/env python3
"""P4 修复：deploy_proposal 从提案读取 target_tab + MCP 工具增加 target_tab 参数"""

# 1. 修改 gateway.py: deploy_proposal 从提案 content 读取 target_tab
GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 在 ctype 判断之后、部署逻辑之前，从 content 读取 target_tab
old_ctype = '''        ctype = content.get("type", "dsl")
        if ctype == "subflow":'''

new_ctype = '''        ctype = content.get("type", "dsl")
        # P4: 从提案 content 读取 target_tab（如果调用方未显式指定）
        if target_tab is None:
            target_tab = content.get("target_tab")
        if ctype == "subflow":'''

if old_ctype in content:
    content = content.replace(old_ctype, new_ctype, 1)
    print("1. deploy_proposal 从提案读取 target_tab")
else:
    print("WARNING: 未找到 ctype 判断")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

# 2. 修改 mcp_server.py: autoflow_deploy_raw 增加 target_tab 参数
MCP = r"E:\NAS\autoflow\src\autoflow_gateway\mcp_server.py"
with open(MCP, "r", encoding="utf-8") as f:
    content = f.read()

# 修改函数签名
old_sig = '''def autoflow_deploy_raw(flow_json: str, label: str = "", target: str = "staging",
                        force: bool = False, require_e2e: bool = False) -> str:'''

new_sig = '''def autoflow_deploy_raw(flow_json: str, label: str = "", target: str = "staging",
                        force: bool = False, require_e2e: bool = False,
                        target_tab: str = "") -> str:'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("2. autoflow_deploy_raw 增加 target_tab 参数")
else:
    print("WARNING: 未找到 autoflow_deploy_raw 签名")

# 在文档字符串中增加 target_tab 说明
old_doc = '''    - require_e2e：True 时提案带 e2e 意图，用户在 WebUI 点「部署到 NR」时会真正先跑一次
      实机验证闸（verdict≠通过即拦截部署）。默认 False（沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E）。
      修复 iss_8d3cffaa96：此前该意图被静默吞掉、主部署路径从不调 e2e 闸。'''

new_doc = '''    - require_e2e：True 时提案带 e2e 意图，用户在 WebUI 点「部署到 NR」时会真正先跑一次
      实机验证闸（verdict≠通过即拦截部署）。默认 False（沿用 env AUTOFLLOW_WHITEBOX_REQUIRE_E2E）。
      修复 iss_8d3cffaa96：此前该意图被静默吞掉、主部署路径从不调 e2e 闸。
    - target_tab：【P4 混合模式】指定部署到哪个 Node-RED tab（按 tab id 或 label 匹配，不存在则自动创建）。
      留空（默认）则按当前 Tab 组织模式部署（per_flow=独立tab / single_tab=AutoFlow集中tab）。
      示例：target_tab="客厅" → 该 flow 部署到「客厅」tab 中，与其他 flow 共存。'''

if old_doc in content:
    content = content.replace(old_doc, new_doc, 1)
    print("3. 文档字符串增加 target_tab 说明")
else:
    print("WARNING: 未找到 require_e2e 文档")

# 在提案提交时增加 target_tab 到 content
# 先找到提案提交的位置
old_submit = '''            "require_e2e": require_e2e,
        }'''

# 这个可能不唯一，让我用更精确的匹配
old_submit2 = '''            "node_count": len(flow.get("nodes", [])),
            "require_e2e": require_e2e,
        }'''

new_submit2 = '''            "node_count": len(flow.get("nodes", [])),
            "require_e2e": require_e2e,
            "target_tab": target_tab or None,
        }'''

if old_submit2 in content:
    content = content.replace(old_submit2, new_submit2, 1)
    print("4. 提案 content 增加 target_tab")
else:
    print("WARNING: 未找到提案提交位置（尝试其他匹配）")
    # 尝试其他匹配
    import re
    matches = list(re.finditer(r'"require_e2e":\s*require_e2e', content))
    print(f"  找到 {len(matches)} 处 require_e2e")
    for i, m in enumerate(matches):
        print(f"  位置 {i}: {content[m.start()-50:m.end()+50]}")

with open(MCP, "w", encoding="utf-8") as f:
    f.write(content)

print("\nP4 修复完成")
