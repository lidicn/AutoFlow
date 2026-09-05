#!/usr/bin/env python3
"""修复授权码弹窗：文字换行 + 横向滚动条问题"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修复「绑定到指定 tab」文字换行 - 增加 white-space:nowrap
old_bind = '''      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
        <label style="font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="dt-tab-bind"> 绑定到指定 tab
        </label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>'''

new_bind = '''      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
        <label style="font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap">
          <input type="checkbox" id="dt-tab-bind" style="margin:0"> 绑定到指定 tab
        </label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>'''

if old_bind in content:
    content = content.replace(old_bind, new_bind, 1)
    print("1. 总开关文字换行修复: OK")
else:
    print("1. 总开关文字换行修复: NOT FOUND")

# 2. 修复列表容器横向滚动条 - 增加 overflow-x:hidden
old_list = '''      <div id="dt-tab-list" style="max-height:220px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:6px 10px;display:none">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>'''

new_list = '''      <div id="dt-tab-list" style="max-height:220px;overflow-y:auto;overflow-x:hidden;border:1px solid var(--border);border-radius:8px;padding:6px 10px;display:none">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>'''

if old_list in content:
    content = content.replace(old_list, new_list, 1)
    print("2. 列表横向滚动修复: OK")
else:
    print("2. 列表横向滚动修复: NOT FOUND")

# 3. 修复列表项布局 - 确保宽度适配，文字可见
old_items = '''        list.innerHTML = tabs.map(t =>
          `<label style="display:flex;align-items:center;padding:6px 4px;cursor:pointer;border-bottom:1px solid var(--border);gap:10px">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="flex-shrink:0;margin:0">
             <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;flex-shrink:0;white-space:nowrap">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");'''

new_items = '''        list.innerHTML = tabs.map(t =>
          `<label style="display:flex;align-items:center;padding:6px 4px;cursor:pointer;border-bottom:1px solid var(--border);gap:8px;min-width:0">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="flex-shrink:0;margin:0;width:16px;height:16px">
             <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px">${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;flex-shrink:0;white-space:nowrap">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");'''

if old_items in content:
    content = content.replace(old_items, new_items, 1)
    print("3. 列表项布局修复: OK")
else:
    print("3. 列表项布局修复: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
