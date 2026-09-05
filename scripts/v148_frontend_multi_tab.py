#!/usr/bin/env python3
"""前端创建授权码弹窗：目标 tab 改为多选勾选框"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 把 select 改为勾选框列表容器
old_select = '''    <div class="field">
      <label>目标 tab（可选）</label>
      <select id="dt-target-tab" class="input" style="width:100%">
        <option value="">不绑定（每个 flow 独立 tab）</option>
        <option value="__loading__" disabled>加载 tab 列表中…</option>
      </select>
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">绑定后 Agent 只能在此 tab 部署；留空则走 per_flow 模式，每个 flow 自动创建独立 tab。</div>
    </div>'''

new_select = '''    <div class="field">
      <label>目标 tab（可多选，可选）</label>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        <label style="font-weight:600"><input type="checkbox" id="dt-tab-bind" style="margin-right:4px"> 绑定到指定 tab</label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>
      <div id="dt-tab-list" style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:8px;display:none">
        <div style="color:var(--text-muted);font-size:12px">加载 tab 列表中…</div>
      </div>
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">勾选后 Agent 只能在这些 tab 部署/修改，不会越界到其他 tab。</div>
    </div>'''

if old_select in content:
    content = content.replace(old_select, new_select, 1)
    print("1. select 改为勾选框容器: OK")
else:
    print("1. select 改为勾选框容器: NOT FOUND")

# 2. 修改加载 tab 列表的逻辑
old_load = '''  // 加载 Node-RED tab 列表填充下拉菜单
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
  })();'''

new_load = '''  // 绑定 tab 勾选框切换
  const bindCheckbox = $("#dt-tab-bind");
  const tabList = $("#dt-tab-list");
  if (bindCheckbox && tabList) {
    bindCheckbox.onchange = () => {
      tabList.style.display = bindCheckbox.checked ? "block" : "none";
    };
  }
  // 加载 Node-RED tab 列表填充勾选框
  (async () => {
    try {
      const tabs = await _loadNRTabs();
      const list = $("#dt-tab-list");
      if (list && tabs && tabs.length) {
        list.innerHTML = tabs.map(t =>
          `<label style="display:flex;align-items:center;padding:4px 0;cursor:pointer">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="margin-right:8px">
             <span>${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;margin-left:8px">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");
      } else if (list) {
        list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">未找到 tab，请先在 Node-RED 中创建 tab</div>';
      }
    } catch (e) {
      if (tabList) tabList.innerHTML = '<div style="color:var(--danger);font-size:12px">加载 tab 列表失败</div>';
    }
  })();'''

if old_load in content:
    content = content.replace(old_load, new_load, 1)
    print("2. 加载 tab 列表改为勾选框: OK")
else:
    print("2. 加载 tab 列表改为勾选框: NOT FOUND")

# 3. 修改提交逻辑，收集多个勾选的 tab
old_submit = '''  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();
    if (!name) { toast("名称不能为空"); return; }'''

new_submit = '''  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    if (!name) { toast("名称不能为空"); return; }
    // 收集勾选的目标 tab
    const targetTabs = [];
    if ($("#dt-tab-bind")?.checked) {
      document.querySelectorAll(".dt-tab-item:checked").forEach(cb => {
        if (cb.value) targetTabs.push(cb.value);
      });
    }
    const targetTab = targetTabs.length ? targetTabs[0] : "";'''

if old_submit in content:
    content = content.replace(old_submit, new_submit, 1)
    print("3. 提交逻辑收集多 tab: OK")
else:
    print("3. 提交逻辑收集多 tab: NOT FOUND")

# 4. 修改 API 调用，传入 target_tabs
old_api = '''      const r = await api("POST", "/deploy-tokens", {
        name, target_tab: targetTab,'''

new_api = '''      const r = await api("POST", "/deploy-tokens", {
        name, target_tab: targetTab, target_tabs: targetTabs,'''

if old_api in content:
    content = content.replace(old_api, new_api, 1)
    print("4. API 传入 target_tabs: OK")
else:
    print("4. API 传入 target_tabs: NOT FOUND")

# 5. 修改创建成功显示，显示多个 tab
old_success = '''            <p>目标 tab: <b>${esc(token.target_tab)}</b></p>'''

new_success = '''            <p>目标 tab: <b>${esc((token.target_tabs && token.target_tabs.length) ? token.target_tabs.join(", ") : (token.target_tab || "不绑定（per_flow 模式）"))}</b></p>'''

if old_success in content:
    content = content.replace(old_success, new_success, 1)
    print("5. 成功显示多 tab: OK")
else:
    print("5. 成功显示多 tab: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
