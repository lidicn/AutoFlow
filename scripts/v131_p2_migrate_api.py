#!/usr/bin/env python3
"""修改 webui.py: 增加 Tab 组织模式迁移 API + P3 分流预警 + P4 混合模式"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

# 在 settings_update 函数后面增加迁移 API
old_marker = '''    # ── 诊断查看器（P4-C，只读）──
    async def diagnostics_view(request: Request):'''

new_marker = '''    # ── Tab 组织模式：迁移状态 + 执行迁移（P2）──
    async def tab_org_status(request: Request):
        """获取当前 Tab 组织模式状态和迁移统计。"""
        from . import tab_organizer as tab_org
        gw = request.app.state.gateway
        status = tab_org.get_migration_status(gw.state)
        # P3: 单 tab 节点数预警
        warning = None
        if tab_org.is_single_tab_mode():
            try:
                af_tab = tab_org.get_single_tab(gw.nr)
                if af_tab:
                    node_count = len(af_tab.get("nodes", []))
                    threshold = int(os.environ.get("AF_SINGLE_TAB_WARN_THRESHOLD", "200"))
                    if node_count >= threshold:
                        warning = {
                            "level": "warning",
                            "message": f"AutoFlow tab 已有 {node_count} 个节点（阈值 {threshold}），建议分流或切换回独立 tab 模式",
                            "node_count": node_count,
                            "threshold": threshold,
                        }
            except Exception:
                pass
        status["warning"] = warning
        return _js(status)

    async def tab_org_migrate(request: Request):
        """执行 Tab 组织模式迁移。

        Body: {target_mode: "single_tab" | "per_flow"}
        """
        from . import tab_organizer as tab_org
        b = await _body(request)
        target_mode = b.get("target_mode")
        if target_mode not in ("single_tab", "per_flow"):
            return _js({"ok": False, "error": "target_mode 必须是 single_tab 或 per_flow"}, 400)

        gw = request.app.state.gateway
        try:
            if target_mode == "single_tab":
                result = tab_org.migrate_per_flow_to_single_tab(gw.nr, gw.state, allow_prod=True)
            else:
                result = tab_org.migrate_single_tab_to_per_flow(gw.nr, gw.state, allow_prod=True)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": f"迁移失败: {e}"}, 500)

    # ── 诊断查看器（P4-C，只读）──
    async def diagnostics_view(request: Request):'''

if old_marker in content:
    content = content.replace(old_marker, new_marker, 1)
    print("1. 增加 Tab 组织模式迁移 API")
else:
    print("WARNING: 未找到诊断查看器标记")

# 增加路由注册（在 app.router.add_post("/settings", settings_update) 附近）
old_route = '''    app.router.add_post("/settings", settings_update)'''
new_route = '''    app.router.add_post("/settings", settings_update)
    app.router.add_get("/tab-org/status", tab_org_status)
    app.router.add_post("/tab-org/migrate", tab_org_migrate)'''

if old_route in content:
    content = content.replace(old_route, new_route, 1)
    print("2. 增加迁移 API 路由注册")
else:
    print("WARNING: 未找到 settings 路由注册")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

print("\nwebui.py 修改完成")
