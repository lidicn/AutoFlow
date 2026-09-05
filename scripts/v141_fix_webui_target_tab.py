#!/usr/bin/env python3
"""修复 webui.py: deploy_token_create target_tab 改为可选"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    async def deploy_token_create(request: Request):
        """创建授权码。"""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        target_tab = (b.get("target_tab") or "").strip()
        if not name:
            return _js({"ok": False, "error": "名称不能为空"}, 400)
        if not target_tab:
            return _js({"ok": False, "error": "目标 tab 不能为空"}, 400)

        try:
            token = _token_store().create_token(
                name=name,
                target_tab=target_tab,'''

new = '''    async def deploy_token_create(request: Request):
        """创建授权码。"""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        target_tab = (b.get("target_tab") or "").strip() or None
        if not name:
            return _js({"ok": False, "error": "名称不能为空"}, 400)
        # target_tab 可选：留空表示不绑定 tab，走 per_flow 模式（每个 flow 独立 tab）

        try:
            token = _token_store().create_token(
                name=name,
                target_tab=target_tab,'''

if old in content:
    content = content.replace(old, new, 1)
    with open(WU, "w", encoding="utf-8") as f:
        f.write(content)
    print("webui.py deploy_token_create target_tab 改为可选")
else:
    print("ERROR: 未找到目标代码")
