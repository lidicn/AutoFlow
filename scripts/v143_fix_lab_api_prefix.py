#!/usr/bin/env python3
"""修复 /lab/* 路由：增加 /api 前缀"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # Lab 沙盒部署（缺陷D修复）
        Route("/lab/validate", lab_validate, methods=["POST"]),
        Route("/lab/deploy", lab_deploy, methods=["POST"]),
        Route("/lab/deploys", lab_deploys, methods=["GET"]),'''

new = '''        # Lab 沙盒部署（缺陷D修复）
        Route("/api/lab/validate", lab_validate, methods=["POST"]),
        Route("/api/lab/deploy", lab_deploy, methods=["POST"]),
        Route("/api/lab/deploys", lab_deploys, methods=["GET"]),'''

if old in content:
    content = content.replace(old, new, 1)
    with open(WU, "w", encoding="utf-8") as f:
        f.write(content)
    print("/lab/* 路由已增加 /api 前缀")
else:
    print("ERROR: 未找到目标代码")
