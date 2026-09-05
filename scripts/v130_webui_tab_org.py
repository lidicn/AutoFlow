#!/usr/bin/env python3
"""修改 webui.py: /config 增加 tab_org_mode，增加设置 API"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

# 1. /config API 增加 tab_org_mode 字段
old_config = '''    async def config_view(request: Request):
        return _js({
            "env": cfg.env,
            "nr_url": cfg.nr_url,
            "hass_server": cfg.hass_server,
            "mcp": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}",
            "mcp_white": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_white_path}",
            "mcp_admin": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_admin_path}",
            "blast_radius_max_flows": cfg.blast_radius_max_flows,
            "elevated_domains": sorted(cfg.elevated_domains),
            "safe_domains": sorted(cfg.safe_domains),
            "task_pool_enabled": is_task_pool_enabled(cfg),
            "raw_node_escape_enabled": is_raw_node_escape_enabled(cfg),
            "deploy_policy": get_deploy_policy(cfg),
            "selfheal_budget": load_feature_flags(cfg).get("selfheal_budget", 3),
        })'''

new_config = '''    async def config_view(request: Request):
        from . import tab_organizer as tab_org
        return _js({
            "env": cfg.env,
            "nr_url": cfg.nr_url,
            "hass_server": cfg.hass_server,
            "mcp": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}",
            "mcp_white": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_white_path}",
            "mcp_admin": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_admin_path}",
            "blast_radius_max_flows": cfg.blast_radius_max_flows,
            "elevated_domains": sorted(cfg.elevated_domains),
            "safe_domains": sorted(cfg.safe_domains),
            "task_pool_enabled": is_task_pool_enabled(cfg),
            "raw_node_escape_enabled": is_raw_node_escape_enabled(cfg),
            "deploy_policy": get_deploy_policy(cfg),
            "selfheal_budget": load_feature_flags(cfg).get("selfheal_budget", 3),
            "tab_org_mode": tab_org.get_tab_org_mode(),
        })'''

if old_config in content:
    content = content.replace(old_config, new_config, 1)
    print("1. /config API 增加 tab_org_mode 字段")
else:
    print("WARNING: /config API 未找到")

# 2. 在 settings_update 函数中增加 tab_org_mode 设置
old_settings = '''    async def settings_update(request: Request):
        """运行时开关：DSL 验证任务池 / 原生节点逃逸 / 部署策略（免重启落盘）。"""
        b = await _body(request)
        tp = b.get("task_pool_enabled")
        if tp is not None:
            if not isinstance(tp, bool):
                return _js({"ok": False, "error": "task_pool_enabled 必须是布尔值"}, 400)
            set_feature_flag(cfg, "task_pool_enabled", tp)'''

new_settings = '''    async def settings_update(request: Request):
        """运行时开关：DSL 验证任务池 / 原生节点逃逸 / 部署策略 / Tab组织模式（免重启落盘）。"""
        b = await _body(request)
        tp = b.get("task_pool_enabled")
        if tp is not None:
            if not isinstance(tp, bool):
                return _js({"ok": False, "error": "task_pool_enabled 必须是布尔值"}, 400)
            set_feature_flag(cfg, "task_pool_enabled", tp)
        tom = b.get("tab_org_mode")
        if tom is not None:
            if tom not in ("per_flow", "single_tab"):
                return _js({"ok": False, "error": "tab_org_mode 必须是 per_flow 或 single_tab"}, 400)
            set_feature_flag(cfg, "tab_org_mode", tom)'''

if old_settings in content:
    content = content.replace(old_settings, new_settings, 1)
    print("2. settings_update 增加 tab_org_mode 设置")
else:
    print("WARNING: settings_update 未找到")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

print("\nwebui.py 修改完成")
