#!/usr/bin/env python3
"""新建 /api/nr/tabs API，修改前端 _loadNRTabs 调用正确的 API"""

import re

# 1. 在 webui.py 中添加 /api/nr/tabs API
WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 在 tab_org_status 函数之前添加 nr_tabs 函数
old_tab_org = '''    # ── Tab 组织模式：迁移状态 + 执行迁移（P2）──
    async def tab_org_status(request: Request):'''

new_tab_org = '''    # ── Node-RED tab 列表（用于授权码/部署时的目标 tab 选择器）──
    async def nr_tabs(request: Request):
        """返回 Node-RED 中所有 tab 列表（id, label, node_count）。"""
        try:
            flows = gw.nr.list_flows()
            if isinstance(flows, dict):
                flows = flows.get("flows", [])
            flows = flows or []
            tabs = []
            for f in flows:
                if not isinstance(f, dict):
                    continue
                if f.get("type") not in (None, "", "tab"):
                    continue
                if f.get("type") == "subflow":
                    continue
                tabs.append({
                    "id": f.get("id", ""),
                    "label": f.get("label") or f.get("id") or "(未命名)",
                    "node_count": len(f.get("nodes", [])) if isinstance(f.get("nodes"), list) else 0,
                })
            return _js({"ok": True, "tabs": tabs})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "tabs": []}, 500)

    # ── Tab 组织模式：迁移状态 + 执行迁移（P2）──
    async def tab_org_status(request: Request):'''

if old_tab_org in content:
    content = content.replace(old_tab_org, new_tab_org, 1)
    print("1. 添加 nr_tabs 函数: OK")
else:
    print("1. 添加 nr_tabs 函数: NOT FOUND")

# 添加路由
old_route = '''        Route("/api/tab-org/status", tab_org_status, methods=["GET"]),'''
new_route = '''        Route("/api/nr/tabs", nr_tabs, methods=["GET"]),
        Route("/api/tab-org/status", tab_org_status, methods=["GET"]),'''

if old_route in content:
    content = content.replace(old_route, new_route, 1)
    print("2. 添加 /api/nr/tabs 路由: OK")
else:
    print("2. 添加 /api/nr/tabs 路由: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

# 3. 修改前端 _loadNRTabs 函数
APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    js_content = f.read()

old_load = '''async function _loadNRTabs() {
  try {
    const r = await api("GET", "/catalog");
    if (r.ok && r.data) {
      const flows = r.data.flows || r.data.nr_flows || [];
      return flows.filter(f => f.type !== "subflow").map(f => ({
        id: f.id,
        label: f.label || f.id,
        node_count: (f.nodes || []).length
      }));
    }
  } catch (e) {}
  return [];
}'''

new_load = '''async function _loadNRTabs() {
  try {
    const r = await api("GET", "/nr/tabs");
    if (r.ok && r.data && r.data.tabs) {
      return r.data.tabs;
    }
  } catch (e) {}
  return [];
}'''

if old_load in js_content:
    js_content = js_content.replace(old_load, new_load, 1)
    print("3. 修改 _loadNRTabs: OK")
else:
    print("3. 修改 _loadNRTabs: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(js_content)

print("Done")
