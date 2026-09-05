#!/usr/bin/env python3
"""webui.py 创建授权码 API 支持 target_tabs"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    async def deploy_token_create(request: Request):
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
                target_tab=target_tab,
                expires_in_hours=float(b.get("expires_in_hours", 4)),
                permissions=b.get("permissions") or ["deploy"],
                node_threshold=int(b.get("node_threshold", 50)),
                max_nodes=int(b.get("max_nodes", 500)),
                max_flows=int(b.get("max_flows", 20)),
                rate_limit_per_min=int(b.get("rate_limit_per_min", 10)),
                require_confirm_dangerous=bool(b.get("require_confirm_dangerous", True)),
                bound_agent=b.get("bound_agent") or None,
                created_by="webui",
            )
            return _js({"ok": True, "token": token})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 400)'''

new = '''    async def deploy_token_create(request: Request):
        """创建授权码。支持 target_tab（单 tab）和 target_tabs（多 tab 列表）。"""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        if not name:
            return _js({"ok": False, "error": "名称不能为空"}, 400)

        # 解析目标 tab：支持单 tab 字符串和多 tab 列表
        # 兼容 URL 格式：从 http://host:1880/#flow/abc123 中提取 abc123
        def _normalize_tab(t):
            t = (t or "").strip()
            if not t:
                return None
            if "#flow/" in t:
                t = t.split("#flow/")[-1].split("/")[0].split("?")[0]
            return t or None

        target_tabs = []
        # 优先使用 target_tabs 列表
        raw_tabs = b.get("target_tabs")
        if isinstance(raw_tabs, list):
            for t in raw_tabs:
                nt = _normalize_tab(t)
                if nt:
                    target_tabs.append(nt)
        # 兼容旧字段 target_tab（单 tab 字符串）
        raw_single = b.get("target_tab")
        if raw_single:
            nt = _normalize_tab(raw_single)
            if nt and nt not in target_tabs:
                target_tabs.append(nt)

        # target_tab 用于向后兼容（取第一个）
        target_tab = target_tabs[0] if target_tabs else None

        try:
            token = _token_store().create_token(
                name=name,
                target_tab=target_tab,
                target_tabs=target_tabs if target_tabs else None,
                expires_in_hours=float(b.get("expires_in_hours", 4)),
                permissions=b.get("permissions") or ["deploy"],
                node_threshold=int(b.get("node_threshold", 50)),
                max_nodes=int(b.get("max_nodes", 500)),
                max_flows=int(b.get("max_flows", 20)),
                rate_limit_per_min=int(b.get("rate_limit_per_min", 10)),
                require_confirm_dangerous=bool(b.get("require_confirm_dangerous", True)),
                bound_agent=b.get("bound_agent") or None,
                created_by="webui",
            )
            return _js({"ok": True, "token": token})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 400)'''

if old in content:
    content = content.replace(old, new, 1)
    print("webui.py deploy_token_create 支持 target_tabs: OK")
else:
    print("webui.py deploy_token_create 支持 target_tabs: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
