#!/usr/bin/env python3
"""修复授权码创建弹窗的勾选框布局错乱问题"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修复「绑定到指定 tab」总开关的对齐
old_bind = '''      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        <label style="font-weight:600"><input type="checkbox" id="dt-tab-bind" style="margin-right:4px"> 绑定到指定 tab</label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>'''

new_bind = '''      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
        <label style="font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="dt-tab-bind"> 绑定到指定 tab
        </label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>'''

if old_bind in content:
    content = content.replace(old_bind, new_bind, 1)
    print("1. 总开关对齐修复: OK")
else:
    print("1. 总开关对齐修复: NOT FOUND")

# 2. 修复勾选框列表的布局
old_list = '''      <div id="dt-tab-list" style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:8px;display:none">
        <div style="color:var(--text-muted);font-size:12px">加载 tab 列表中…</div>
      </div>'''

new_list = '''      <div id="dt-tab-list" style="max-height:220px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:6px 10px;display:none">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>'''

if old_list in content:
    content = content.replace(old_list, new_list, 1)
    print("2. 列表容器修复: OK")
else:
    print("2. 列表容器修复: NOT FOUND")

# 3. 修复动态生成的勾选框项布局
old_items = '''        list.innerHTML = tabs.map(t =>
          `<label style="display:flex;align-items:center;padding:4px 0;cursor:pointer">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="margin-right:8px">
             <span>${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;margin-left:8px">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");'''

new_items = '''        list.innerHTML = tabs.map(t =>
          `<label style="display:flex;align-items:center;padding:6px 4px;cursor:pointer;border-bottom:1px solid var(--border);gap:10px">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="flex-shrink:0;margin:0">
             <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;flex-shrink:0;white-space:nowrap">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");'''

if old_items in content:
    content = content.replace(old_items, new_items, 1)
    print("3. 勾选框项布局修复: OK")
else:
    print("3. 勾选框项布局修复: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
