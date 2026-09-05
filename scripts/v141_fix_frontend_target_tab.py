#!/usr/bin/env python3
"""修复 app.js: 创建授权码表单 target_tab 改为可选"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    <div class="field">
      <label>目标 tab *</label>
      <input type="text" id="dt-target-tab" class="input" placeholder="如：客厅（Agent 只能在此 tab 部署）">
    </div>'''

new = '''    <div class="field">
      <label>目标 tab（可选）</label>
      <input type="text" id="dt-target-tab" class="input" placeholder="如：客厅（留空=不绑定，每个 flow 独立 tab）">
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">绑定后 Agent 只能在此 tab 部署；留空则走 per_flow 模式，每个 flow 自动创建独立 tab。</div>
    </div>'''

if old in content:
    content = content.replace(old, new, 1)
    print("1. 表单 target_tab 改为可选")
else:
    print("WARNING: 未找到表单 target_tab")

# 修改创建确认逻辑：target_tab 不再必填
old_check = '''    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();
    if (!name || !targetTab) { toast("名称和目标 tab 不能为空"); return; }'''

new_check = '''    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();
    if (!name) { toast("名称不能为空"); return; }'''

if old_check in content:
    content = content.replace(old_check, new_check, 1)
    print("2. 创建确认逻辑 target_tab 不再必填")
else:
    print("WARNING: 未找到创建确认逻辑")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)
print("\napp.js 修复完成")
