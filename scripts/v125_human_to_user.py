#!/usr/bin/env python3
"""v1.2.5: 人类→用户文案修正 + README核心版简介"""
import re

# ═══════════════════════════════════════════════════════════
# 1. 前端 app.js: 人类→用户（2处）
# ═══════════════════════════════════════════════════════════
APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    js = f.read()

js = js.replace("都需人类在 WebUI 点 Deploy 后部署", "都需用户在 WebUI 点 Deploy 后部署")
js = js.replace("且始终由人类在 WebUI 触发——绝不无人值守部署", "且始终由用户在 WebUI 触发——绝不无人值守部署")

with open(APP, "w", encoding="utf-8") as f:
    f.write(js)
print("app.js: 人类→用户 (2处)")

# ═══════════════════════════════════════════════════════════
# 2. mcp_server.py: 工具描述中的人类→用户（AI会模仿用词）
# ═══════════════════════════════════════════════════════════
MCP = r"E:\NAS\autoflow\src\autoflow_gateway\mcp_server.py"
with open(MCP, "r", encoding="utf-8") as f:
    mcp = f.read()

# 精准替换用户/AI可见的文案，保留"人类可读"等技术术语
replacements = [
    ("待人类审核", "待用户审核"),
    ("待人类在 WebUI", "待用户在 WebUI"),
    ("人类在 WebUI", "用户在 WebUI"),
    ("人类审核", "用户审核"),
    ("人类批准", "用户批准"),
    ("人类审批", "用户审批"),
    ("人类决策", "用户决策"),
    ("人类拍板", "用户拍板"),
    ("人类选择", "用户选择"),
    ("人类审阅", "用户审阅"),
    ("人类尚未选择", "用户尚未选择"),
    ("等人类", "等用户"),
    ("交由人类", "交由用户"),
    ("人类在", "用户在"),
    ("告诉人类", "告诉用户"),
    ("呈现给人类", "呈现给用户"),
]

count = 0
for old, new in replacements:
    n = mcp.count(old)
    if n > 0:
        mcp = mcp.replace(old, new)
        count += n
        print(f"  mcp_server.py: '{old}' → '{new}' ({n}处)")

with open(MCP, "w", encoding="utf-8") as f:
    f.write(mcp)
print(f"mcp_server.py: 共替换 {count} 处")

# ═══════════════════════════════════════════════════════════
# 3. gateway.py: 工具描述中的人类→用户
# ═══════════════════════════════════════════════════════════
GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    gw = f.read()

gw_count = 0
for old, new in replacements:
    n = gw.count(old)
    if n > 0:
        gw = gw.replace(old, new)
        gw_count += n

with open(GW, "w", encoding="utf-8") as f:
    f.write(gw)
print(f"gateway.py: 共替换 {gw_count} 处")

# ═══════════════════════════════════════════════════════════
# 4. proposals.py / confirm.py / decision_store.py
# ═══════════════════════════════════════════════════════════
for fname in ["proposals.py", "confirm.py", "decision_store.py"]:
    path = rf"E:\NAS\autoflow\src\autoflow_gateway\{fname}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        c = 0
        for old, new in replacements:
            n = content.count(old)
            if n > 0:
                content = content.replace(old, new)
                c += n
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"{fname}: 共替换 {c} 处")
    except FileNotFoundError:
        print(f"{fname}: 不存在，跳过")

print("\n文案修改完成！")
