#!/usr/bin/env python3
"""v1.5.4: 修复 propose-dsl json 未导入 + PUT 支持 revoked 吊销"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 确保 import json 存在
if "import json" not in content.split("import asyncio")[0]:
    content = content.replace("import os\nimport asyncio", "import os\nimport json\nimport asyncio", 1)
    print("1. import json added: OK")
else:
    print("1. import json already exists")

# 2. 让 PUT 支持 revoked=true 吊销
old = '''    async def api_keys_update(request: Request):
        """更新 API Key 授权范围。"""
        try:
            key_id = request.path_params.get("key_id", "")
            b = await _body(request)
            store = _api_key_store()
            result = store.update_key(
                key_id=key_id,
                name=b.get("name"),
                authorized_tabs=b.get("authorized_tabs"),
                permissions=b.get("permissions"),
            )
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:'''

new = '''    async def api_keys_update(request: Request):
        """更新 API Key 授权范围。支持 revoked=true 吊销。"""
        try:
            key_id = request.path_params.get("key_id", "")
            b = await _body(request)
            store = _api_key_store()
            if b.get("revoked") is True:
                result = store.revoke_key(key_id)
            else:
                result = store.update_key(
                    key_id=key_id,
                    name=b.get("name"),
                    authorized_tabs=b.get("authorized_tabs"),
                    permissions=b.get("permissions"),
                )
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:'''

if old in content:
    content = content.replace(old, new, 1)
    print("2. PUT revoked support: OK")
else:
    print("2. PUT revoked support: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
