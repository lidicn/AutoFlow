#!/usr/bin/env python3
"""v1.5.2: app.js 增加模板库页面"""

APPJS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APPJS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. TABS 增加 templates
old_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
new_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "templates", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
if old_tabs in content:
    content = content.replace(old_tabs, new_tabs, 1)
    print("1. TABS 添加 templates: OK")
else:
    print("1. TABS 添加 templates: NOT FOUND")

# 2. setTab 增加路由
old_route = '  else if (tab === "api_keys") loadApiKeys();'
new_route = '  else if (tab === "api_keys") loadApiKeys();\n  else if (tab === "templates") loadTemplates();'
if old_route in content:
    content = content.replace(old_route, new_route, 1)
    print("2. setTab 路由添加: OK")
else:
    print("2. setTab 路由添加: NOT FOUND")

# 3. 在 API Key 函数之后插入模板库函数
insert_marker = '// ── API Key 管理（v1.5.1）──'

template_js = '''// ── 模板库（v1.5.2）──
let _allTemplates = [];
let _templateCategories = [];

async function loadTemplates() {
  const v = $("#view-templates");
  v.innerHTML = `
    <div class="view-head">
      <h2>模板库</h2>
      <span class="sub">常用 Flow 模板，一键渲染生成 DSL，快速部署</span>
      <button class="btn primary" id="tpl-create-btn" style="margin-left:auto">➕ 创建模板</button>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <select id="tpl-category-filter" class="input" style="width:auto">
        <option value="">全部分类</option>
      </select>
      <input type="text" id="tpl-search" class="input" placeholder="搜索模板..." style="flex:1;min-width:200px">
    </div>
    <div id="tpl-list"><div class="empty">加载中…</div></div>`;

  $("#tpl-create-btn").onclick = () => showCreateTemplateModal();
  $("#tpl-search").oninput = () => renderTemplateList();
  $("#tpl-category-filter").onchange = () => renderTemplateList();
  await refreshTemplates();
}

async function refreshTemplates() {
  try {
    const r = await api("GET", "/templates");
    if (!r.ok) throw new Error(r.data?.error || r.status);
    _allTemplates = r.data?.templates || [];
    _templateCategories = r.data?.categories || [];
    const sel = $("#tpl-category-filter");
    sel.innerHTML = '<option value="">全部分类</option>' +
      _templateCategories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    renderTemplateList();
  } catch (e) {
    $("#tpl-list").innerHTML = errBox(e.message, refreshTemplates);
  }
}

function renderTemplateList() {
  const list = $("#tpl-list");
  const cat = $("#tpl-category-filter")?.value || "";
  const kw = ($("#tpl-search")?.value || "").toLowerCase();
  let templates = _allTemplates;
  if (cat) templates = templates.filter(t => t.category === cat);
  if (kw) templates = templates.filter(t =>
    t.name.toLowerCase().includes(kw) ||
    t.description.toLowerCase().includes(kw) ||
    (t.tags || []).some(tag => tag.toLowerCase().includes(kw))
  );
  if (!templates.length) {
    list.innerHTML = `<div class="empty">暂无匹配的模板</div>`;
    return;
  }
  list.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">` +
    templates.map(t => `
    <div class="card" style="cursor:pointer" data-tpl-id="${esc(t.id)}">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px">
        <h3 style="margin:0;font-size:15px">${esc(t.name)}</h3>
        ${t.builtin ? '<span class="badge" style="background:#dbeafe;color:#1e40af;font-size:10px">内置</span>' : ''}
      </div>
      <p style="font-size:12px;color:var(--text-muted);margin:0 0 8px;line-height:1.5">${esc(t.description || "")}</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        <span class="badge" style="font-size:10px;background:var(--bg-soft)">${esc(t.category)}</span>
        ${(t.tags || []).slice(0, 3).map(tag => `<span class="badge" style="font-size:10px;background:var(--bg-soft)">${esc(tag)}</span>`).join("")}
      </div>
      <div style="font-size:11px;color:var(--text-muted)">
        变量: ${(t.variables || []).length} 个 · 使用: ${t.use_count || 0} 次
      </div>
    </div>
  `).join("") + `</div>`;

  $$("[data-tpl-id]").forEach(card => card.onclick = () => showTemplateDetail(card.dataset.tplId));
}

async function showTemplateDetail(templateId) {
  try {
    const r = await api("GET", "/templates/" + templateId);
    if (!r.ok) throw new Error(r.data?.error || r.status);
    const t = r.data.template;
    const vars = t.variables || [];
    modal("模板详情 - " + t.name, `
      <p style="font-size:13px;color:var(--text-muted);margin:0 0 12px">${esc(t.description || "")}</p>
      <div style="background:var(--bg-soft);padding:12px;border-radius:8px;margin-bottom:12px">
        <div style="font-weight:600;font-size:12px;margin-bottom:8px">DSL 模板</div>
        <pre style="font-size:12px;white-space:pre-wrap;margin:0;font-family:monospace">${esc(t.dsl)}</pre>
      </div>
      ${vars.length ? `
      <div style="margin-bottom:12px">
        <div style="font-weight:600;font-size:12px;margin-bottom:6px">变量 (${vars.length})</div>
        ${vars.map(v => `
          <div style="font-size:12px;margin-bottom:4px">
            <code style="background:var(--bg-soft);padding:1px 4px;border-radius:3px">${esc(v.name)}</code>
            ${v.description ? `<span style="color:var(--text-muted)"> - ${esc(v.description)}</span>` : ''}
            ${v.default ? `<span style="color:var(--text-muted)"> (默认: ${esc(String(v.default))})</span>` : ''}
          </div>
        `).join("")}
      </div>
      ` : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" id="tpl-render-btn">🎨 渲染并使用</button>
        ${!t.builtin ? `<button class="btn danger" id="tpl-delete-btn">🗑️ 删除</button>` : ''}
      </div>
    `, null, "关闭");

    const renderBtn = $("#tpl-render-btn");
    if (renderBtn) renderBtn.onclick = () => showRenderTemplateModal(t);
    const delBtn = $("#tpl-delete-btn");
    if (delBtn) delBtn.onclick = async () => {
      if (!confirm("确定删除此模板？")) return;
      await api("DELETE", "/templates/" + templateId);
      closeModal();
      toast("已删除", "success");
      refreshTemplates();
    };
  } catch (e) {
    toast("加载失败: " + e.message, "error");
  }
}

function showRenderTemplateModal(t) {
  const vars = t.variables || [];
  modal("渲染模板 - " + t.name, `
    <div style="margin-bottom:12px">
      ${vars.map(v => `
        <div class="field">
          <label>${esc(v.name)} ${v.description ? `<span style="color:var(--text-muted);font-weight:normal">(${esc(v.description)})</span>` : ''}</label>
          <input type="text" class="input tpl-var" data-var="${esc(v.name)}"
            placeholder="${v.default ? '默认: ' + esc(String(v.default)) : '请输入'}"
            value="${v.default ? esc(String(v.default)) : ''}">
        </div>
      `).join("")}
    </div>
    <div id="tpl-rendered" style="display:none;margin-bottom:12px">
      <div style="font-weight:600;font-size:12px;margin-bottom:6px">渲染结果</div>
      <pre id="tpl-rendered-dsl" style="background:var(--bg-soft);padding:10px;border-radius:6px;font-size:12px;white-space:pre-wrap;max-height:200px;overflow:auto"></pre>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn primary" id="tpl-do-render">渲染</button>
      <button class="btn" id="tpl-copy-dsl" style="display:none">📋 复制 DSL</button>
    </div>
  `, null, "关闭");

  $("#tpl-do-render").onclick = async () => {
    const variables = {};
    $$(".tpl-var").forEach(inp => {
      if (inp.value) variables[inp.dataset.var] = inp.value;
    });
    try {
      const r = await api("POST", "/templates/" + t.id + "/render", { variables });
      if (!r.ok) {
        if (r.data?.missing_variables) {
          toast("缺少变量: " + r.data.missing_variables.join(", "), "error");
        } else {
          throw new Error(r.data?.error || r.status);
        }
        return;
      }
      $("#tpl-rendered").style.display = "block";
      $("#tpl-rendered-dsl").textContent = r.data.dsl;
      $("#tpl-copy-dsl").style.display = "inline-block";
      $("#tpl-copy-dsl").onclick = () => {
        navigator.clipboard.writeText(r.data.dsl).then(() => toast("已复制 DSL", "success"));
      };
    } catch (e) {
      toast("渲染失败: " + e.message, "error");
    }
  };
}

function showCreateTemplateModal() {
  modal("创建模板", `
    <div class="field">
      <label>名称 *</label>
      <input type="text" id="tpl-new-name" class="input" placeholder="如：夜间自动关灯">
    </div>
    <div class="field">
      <label>分类</label>
      <input type="text" id="tpl-new-category" class="input" placeholder="如：照明" value="未分类">
    </div>
    <div class="field">
      <label>描述</label>
      <input type="text" id="tpl-new-desc" class="input" placeholder="简短描述">
    </div>
    <div class="field">
      <label>DSL 模板 *</label>
      <textarea id="tpl-new-dsl" class="input" rows="6" placeholder="场景: ...\n触发: {{sensor}} on\n动作: light.turn_on({{light}})"></textarea>
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">使用 {{变量名}} 或 {{变量名|默认值}} 定义占位符</div>
    </div>
  `, async () => {
    const name = $("#tpl-new-name").value.trim();
    const dsl = $("#tpl-new-dsl").value.trim();
    if (!name || !dsl) { toast("名称和 DSL 不能为空", "error"); return false; }
    try {
      const r = await api("POST", "/templates", {
        name, dsl,
        description: $("#tpl-new-desc").value.trim(),
        category: $("#tpl-new-category").value.trim() || "未分类",
      });
      if (!r.ok) throw new Error(r.data?.error || r.status);
      toast("模板已创建", "success");
      refreshTemplates();
      return true;
    } catch (e) {
      toast("创建失败: " + e.message, "error");
      return false;
    }
  });
}

'''

if insert_marker in content:
    content = content.replace(insert_marker, template_js + insert_marker, 1)
    print("3. 模板库 JS 函数插入: OK")
else:
    print("3. 模板库 JS 函数插入: NOT FOUND")

with open(APPJS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
