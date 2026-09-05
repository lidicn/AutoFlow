#!/usr/bin/env python3
"""修复 v1.3.1 测试发现的问题：
1. tab-org API 用闭包变量 gw 而非 request.app.state.gateway
2. 增加 try-except 防止遗留记录缺字段导致 500
3. 前端保存设置 POST -> PUT
4. 前端 status 失败时显式提示
"""

# 1. 修复 webui.py
WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

# 修复 tab_org_status 中的 gateway 访问
old_status = '''    async def tab_org_status(request: Request):
        """获取当前 Tab 组织模式状态和迁移统计。"""
        from . import tab_organizer as tab_org
        gw = request.app.state.gateway
        status = tab_org.get_migration_status(gw.state)'''

new_status = '''    async def tab_org_status(request: Request):
        """获取当前 Tab 组织模式状态和迁移统计。"""
        from . import tab_organizer as tab_org
        try:
            status = tab_org.get_migration_status(gw.state)
        except Exception as e:
            return _js({"ok": False, "error": f"获取状态失败: {e}",
                        "current_mode": tab_org.get_tab_org_mode(),
                        "per_flow_count": 0, "single_tab_count": 0,
                        "total_flows": 0, "warning": None}, 500)'''

if old_status in content:
    content = content.replace(old_status, new_status, 1)
    print("1. 修复 tab_org_status gateway 访问 + 异常处理")
else:
    print("WARNING: 未找到 tab_org_status")

# 修复 tab_org_migrate 中的 gateway 访问
old_migrate = '''    async def tab_org_migrate(request: Request):
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
            return _js({"ok": False, "error": f"迁移失败: {e}"}, 500)'''

new_migrate = '''    async def tab_org_migrate(request: Request):
        """执行 Tab 组织模式迁移。

        Body: {target_mode: "single_tab" | "per_flow"}
        """
        from . import tab_organizer as tab_org
        b = await _body(request)
        target_mode = b.get("target_mode")
        if target_mode not in ("single_tab", "per_flow"):
            return _js({"ok": False, "error": "target_mode 必须是 single_tab 或 per_flow"}, 400)

        try:
            if target_mode == "single_tab":
                result = tab_org.migrate_per_flow_to_single_tab(gw.nr, gw.state, allow_prod=True)
            else:
                result = tab_org.migrate_single_tab_to_per_flow(gw.nr, gw.state, allow_prod=True)
            return _js(result)
        except Exception as e:
            import traceback
            return _js({"ok": False, "error": f"迁移失败: {e}",
                        "traceback": traceback.format_exc()[-500:]}, 500)'''

if old_migrate in content:
    content = content.replace(old_migrate, new_migrate, 1)
    print("2. 修复 tab_org_migrate gateway 访问 + 异常处理")
else:
    print("WARNING: 未找到 tab_org_migrate")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

# 2. 修复 tab_organizer.py 中的向后兼容问题
TO = r"E:\NAS\autoflow\src\autoflow_gateway\tab_organizer.py"
with open(TO, "r", encoding="utf-8") as f:
    content = f.read()

# get_migration_status 已经用了 .get()，但增加更健壮的处理
old_gms = '''def get_migration_status(state) -> Dict[str, Any]:
    """获取当前迁移状态统计。"""
    catalog = state.get_flow_catalog()
    flows = catalog.get("flows", {})
    per_flow_count = sum(1 for m in flows.values()
                          if m.get("tab_org_mode", "per_flow") == "per_flow")
    single_tab_count = sum(1 for m in flows.values()
                           if m.get("tab_org_mode") == "single_tab")
    return {
        "current_mode": get_tab_org_mode(),
        "per_flow_count": per_flow_count,
        "single_tab_count": single_tab_count,
        "total_flows": len(flows),
        "can_migrate_to_single": per_flow_count > 0,
        "can_migrate_to_per_flow": single_tab_count > 0,
    }'''

new_gms = '''def get_migration_status(state) -> Dict[str, Any]:
    """获取当前迁移状态统计。"""
    try:
        catalog = state.get_flow_catalog()
    except Exception:
        catalog = {"flows": {}}
    flows = catalog.get("flows", {}) if isinstance(catalog, dict) else {}
    per_flow_count = 0
    single_tab_count = 0
    mixed_count = 0
    for m in flows.values():
        if not isinstance(m, dict):
            continue
        mode = m.get("tab_org_mode", "per_flow")
        if mode == "single_tab":
            single_tab_count += 1
        elif mode == "mixed":
            mixed_count += 1
        else:
            per_flow_count += 1
    return {
        "current_mode": get_tab_org_mode(),
        "per_flow_count": per_flow_count,
        "single_tab_count": single_tab_count,
        "mixed_count": mixed_count,
        "total_flows": len(flows),
        "can_migrate_to_single": per_flow_count > 0,
        "can_migrate_to_per_flow": single_tab_count > 0,
    }'''

if old_gms in content:
    content = content.replace(old_gms, new_gms, 1)
    print("3. 修复 get_migration_status 向后兼容")
else:
    print("WARNING: 未找到 get_migration_status")

with open(TO, "w", encoding="utf-8") as f:
    f.write(content)

# 3. 修复前端 app.js
APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# 修复保存设置 POST -> PUT
old_save = '''          const r = await api("POST", "/settings", { tab_org_mode: mode });'''
new_save = '''          const r = await api("PUT", "/settings", { tab_org_mode: mode });'''

if old_save in content:
    content = content.replace(old_save, new_save, 1)
    print("4. 修复前端保存设置 POST -> PUT")
else:
    print("WARNING: 未找到保存设置调用")

# 修复前端 status 失败时静默吞错
old_catch = '''    const [cfgR, statusR] = await Promise.all([
      api("GET", "/config"),
      api("GET", "/tab-org/status").catch(() => ({ ok: false, data: {} }))
    ]);'''

new_catch = '''    const [cfgR, statusR] = await Promise.all([
      api("GET", "/config"),
      api("GET", "/tab-org/status").catch((e) => ({ ok: false, data: { error: e.message }, statusError: true }))
    ]);'''

if old_catch in content:
    content = content.replace(old_catch, new_catch, 1)
    print("5. 修复前端 status 失败时保留错误信息")
else:
    print("WARNING: 未找到 status catch")

# 在 body.innerHTML 之前增加错误提示
old_body_start = '''    const cfg = cfgR.data || {};
    const status = statusR.data || {};
    const currentMode = cfg.tab_org_mode || "per_flow";'''

new_body_start = '''    const cfg = cfgR.data || {};
    const status = statusR.data || {};
    const statusError = statusR.statusError || !statusR.ok;
    const currentMode = cfg.tab_org_mode || "per_flow";'''

if old_body_start in content:
    content = content.replace(old_body_start, new_body_start, 1)
    print("6. 增加 statusError 标志")
else:
    print("WARNING: 未找到 body 变量定义")

# 在 warning 卡片之前增加 status 错误提示
old_warning_card = '''      ${warning ? `
      <div class="card" style="border-left:4px solid #f59e0b;background:#fffbeb">'''

new_warning_card = '''      ${statusError ? `
      <div class="card" style="border-left:4px solid #ef4444;background:#fef2f2">
        <h3 style="color:#991b1b">❌ 状态服务不可用</h3>
        <p class="desc" style="color:#7f1d1d">Tab 组织模式状态获取失败：${status.error || "未知错误"}。迁移功能暂不可用，请检查网关日志或稍后重试。</p>
      </div>` : ""}
      ${warning ? `
      <div class="card" style="border-left:4px solid #f59e0b;background:#fffbeb">'''

if old_warning_card in content:
    content = content.replace(old_warning_card, new_warning_card, 1)
    print("7. 增加 status 错误提示卡片")
else:
    print("WARNING: 未找到 warning 卡片")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)

print("\n所有修复完成")
