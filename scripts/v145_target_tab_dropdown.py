#!/usr/bin/env python3
"""把创建授权码弹窗的 target_tab 从文本输入框改为下拉菜单"""

import re

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 把 input 改为 select
old_input = '''    <div class="field">
      <label>目标 tab（可选）</label>
      <input type="text" id="dt-target-tab" class="input" placeholder="如：客厅（留空=不绑定，每个 flow 独立 tab）">
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">绑定后 Agent 只能在此 tab 部署；留空则走 per_flow 模式，每个 flow 自动创建独立 tab。</div>
    </div>'''

new_select = '''    <div class="field">
      <label>目标 tab（可选）</label>
      <select id="dt-target-tab" class="input" style="width:100%">
        <option value="">不绑定（每个 flow 独立 tab）</option>
        <option value="__loading__" disabled>加载 tab 列表中…</option>
      </select>
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">绑定后 Agent 只能在此 tab 部署；留空则走 per_flow 模式，每个 flow 自动创建独立 tab。</div>
    </div>'''

if old_input in content:
    content = content.replace(old_input, new_select, 1)
    print("1. target_tab input -> select: OK")
else:
    print("1. target_tab input -> select: NOT FOUND")

# 2. 在 showCreateTokenModal 函数中，modal() 之后添加加载 tab 列表的逻辑
old_confirm = '''  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();'''

new_confirm = '''  // 加载 Node-RED tab 列表填充下拉菜单
  (async () => {
    try {
      const tabs = await _loadNRTabs();
      const sel = $("#dt-target-tab");
      if (sel && tabs && tabs.length) {
        // 移除 loading 选项，添加已有 tab
        const loadingOpt = sel.querySelector('option[value="__loading__"]');
        if (loadingOpt) loadingOpt.remove();
        const optgroup = document.createElement("optgroup");
        optgroup.label = "已有 tab";
        tabs.forEach(t => {
          const opt = document.createElement("option");
          opt.value = t.label;
          opt.textContent = t.label + "（" + (t.node_count || 0) + " 节点）";
          optgroup.appendChild(opt);
        });
        sel.appendChild(optgroup);
      }
    } catch (e) { /* 加载失败保持默认选项 */ }
  })();

  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();'''

if old_confirm in content:
    content = content.replace(old_confirm, new_confirm, 1)
    print("2. 加载 tab 列表逻辑: OK")
else:
    print("2. 加载 tab 列表逻辑: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
