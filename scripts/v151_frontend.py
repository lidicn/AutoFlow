#!/usr/bin/env python3
"""v1.5.1: app.js 增加 API Key 管理页面"""

APPJS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APPJS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 TABS 数组中添加 api_keys
old_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
new_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
if old_tabs in content:
    content = content.replace(old_tabs, new_tabs, 1)
    print("1. TABS 添加 api_keys: OK")
else:
    print("1. TABS 添加 api_keys: NOT FOUND")

# 2. 在 setTab 中添加路由
old_route = '  else if (tab === "deploy_tokens") loadDeployTokens();'
new_route = '  else if (tab === "deploy_tokens") loadDeployTokens();\n  else if (tab === "api_keys") loadApiKeys();'
if old_route in content:
    content = content.replace(old_route, new_route, 1)
    print("2. setTab 路由添加: OK")
else:
    print("2. setTab 路由添加: NOT FOUND")

# 3. 在 loadDeployTokens 函数之前插入 API Key 管理函数
insert_marker = 'let _allDeployTokens = [];'

api_key_js = '''// ── API Key 管理（v1.5.1）──
let _allApiKeys = [];

async function loadApiKeys() {
  const v = $("#view-api_keys");
  v.innerHTML = `
    <div class="view-head">
      <h2>API Key 管理</h2>
      <span class="sub">AutoFlow Pro：给 Agent 发放 API Key，通过网关 REST API 操作 Flow</span>
      <button class="btn primary" id="ak-create-btn" style="margin-left:auto">➕ 创建 API Key</button>
    </div>
    <div class="card" style="background:var(--bg-soft);border-left:3px solid var(--primary);margin-bottom:16px">
      <div style="font-weight:600;margin-bottom:8px">📖 API Key 使用方法</div>
      <ol style="margin:0;padding-left:20px;line-height:1.8;font-size:13px">
        <li><b>创建 API Key</b>：点击右上角「创建 API Key」，设置名称、Agent ID、授权 tab 和权限。</li>
        <li><b>复制 API Key</b>：创建成功后立即复制 Key 字符串（只显示一次，丢失后需重新创建）。</li>
        <li><b>配置 Agent</b>：在 Agent 的环境变量或配置中设置 <code>AF_GATEWAY_URL</code> 和 <code>AF_API_KEY</code>。</li>
        <li><b>Agent 调用网关</b>：Agent 使用 <code>nr_client.py --gateway propose-dsl "..."</code> 提交 DSL，网关验证 API Key 后自动编译部署。</li>
        <li><b>安全边界</b>：每个 API Key 绑定 Agent ID 和授权 tab 列表，越界操作会被拒绝；权限分级（只读/可部署/可修改）。</li>
        <li><b>可追溯</b>：每次 API 调用记录审计日志，可在 Key 详情中查看使用记录。</li>
      </ol>
    </div>
    <div class="card" style="background:#fef3c7;border-left:3px solid #f59e0b;margin-bottom:16px">
      <div style="font-size:13px;line-height:1.7">
        <b>⚠️ 与授权码的区别</b>：API Key 是 Agent 的<b>身份凭证</b>（长期有效，可吊销），授权码是<b>临时部署权限</b>（有过期时间）。
        AutoFlow Pro 模式下，Agent 用 API Key 认证身份，在授权范围内对话即部署，不需要额外授权码。
      </div>
    </div>
    <div id="ak-list"><div class="empty">加载中…</div></div>`;

  $("#ak-create-btn").onclick = () => showCreateApiKeyModal();
  await refreshApiKeys();
}

async function refreshApiKeys() {
  const list = $("#ak-list");
  try {
    const r = await api("GET", "/keys");
    if (!r.ok) throw new Error(r.data?.error || r.status);
    _allApiKeys = r.data?.keys || [];
    renderApiKeyList();
  } catch (e) {
    list.innerHTML = errBox(e.message, refreshApiKeys);
  }
}

function renderApiKeyList() {
  const list = $("#ak-list");
  if (!_allApiKeys.length) {
    list.innerHTML = `<div class="empty">暂无 API Key。点击右上角「创建 API Key」为 Agent 发放网关访问权限。</div>`;
    return;
  }
  list.innerHTML = _allApiKeys.map(k => {
    const isActive = !k.revoked;
    const statusBadge = isActive
      ? `<span class="badge" style="background:#dcfce7;color:#166534">有效</span>`
      : `<span class="badge" style="background:#fee2e2;color:#991b1b">已吊销</span>`;
    const tabsText = (k.authorized_tabs && k.authorized_tabs.length)
      ? k.authorized_tabs.length + " 个 tab"
      : "全部 tab";
    const permsText = (k.permissions || []).join(", ");
    return `
    <div class="card deploy-token-card" data-key-id="${esc(k.key_id)}">
      <div style="display:flex;justify-content:space-between;align-items:start;gap:12px">
        <div style="flex:1">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
            <h3 style="margin:0">${esc(k.name)}</h3>
            ${statusBadge}
          </div>
          <div class="meta" style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
            Agent: <b>${esc(k.agent_id)}</b> ·
            授权: <b>${esc(tabsText)}</b> ·
            权限: ${esc(permsText)}
          </div>
          <div style="display:flex;gap:16px;font-size:12px;color:var(--text-muted);flex-wrap:wrap">
            <span>调用: <b>${k.use_count || 0}</b></span>
            <span>创建: ${esc((k.created_at || "").slice(0, 19).replace("T", " "))}</span>
            <span>上次使用: ${esc((k.last_used_at || "从未").slice(0, 19).replace("T", " "))}</span>
            ${k.expires_at ? `<span>到期: ${esc((k.expires_at || "").slice(0, 19).replace("T", " "))}</span>` : ""}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <button class="btn sm" data-ak-edit="${esc(k.key_id)}">✏️ 编辑</button>
          <button class="btn sm" data-ak-logs="${esc(k.key_id)}">📋 日志</button>
          ${isActive ? `<button class="btn sm danger" data-ak-revoke="${esc(k.key_id)}">🚫 吊销</button>` : ""}
        </div>
      </div>
    </div>`;
  }).join("");

  $$("[data-ak-edit]").forEach(b => b.onclick = () => editApiKey(b.dataset.akEdit));
  $$("[data-ak-logs]").forEach(b => b.onclick = () => showApiKeyLogs(b.dataset.akLogs));
  $$("[data-ak-revoke]").forEach(b => b.onclick = () => revokeApiKey(b.dataset.akRevoke));
}

function showCreateApiKeyModal() {
  modal("创建 API Key", `
    <div class="card" style="background:var(--bg-soft);border-left:3px solid var(--primary);margin-bottom:16px;padding:10px 14px">
      <div style="font-size:12px;line-height:1.6">
        <b>API Key 作用</b>：给 Agent 发放后，Agent 可通过网关 REST API（/api/core/*）操作 Flow，
        支持 DSL 优先、编译闸门、快照回滚。创建后请立即复制 Key 并配置到 Agent 环境变量。
      </div>
    </div>
    <div class="field">
      <label>名称 *</label>
      <input type="text" id="ak-name" class="input" placeholder="如：豆包管家">
    </div>
    <div class="field">
      <label>Agent ID *</label>
      <input type="text" id="ak-agent-id" class="input" placeholder="如：doubao-butler" value="pro-agent">
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">用于审计日志标识 Agent 身份，建议用有意义的名称。</div>
    </div>
    <div class="field">
      <label>授权 tab（可多选，不选=全部）</label>
      <div id="ak-tab-list" style="max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:6px 10px">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>
    </div>
    <div class="field">
      <label>权限</label>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-perm-read" checked style="margin:0"> 只读（查询实体/快照）
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-perm-deploy" checked style="margin:0"> 可部署（propose-dsl）
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-perm-modify" style="margin:0"> 可修改（deploy-raw/回滚）
        </label>
      </div>
    </div>
    <div class="field">
      <label>有效期（可选，留空=永久）</label>
      <input type="datetime-local" id="ak-expires" class="input">
    </div>
  `, async () => {
    const name = $("#ak-name").value.trim();
    const agentId = $("#ak-agent-id").value.trim();
    if (!name) { toast("请填写名称", "error"); return false; }
    if (!agentId) { toast("请填写 Agent ID", "error"); return false; }

    const tabs = [];
    $$("#ak-tab-list input[type=checkbox]:checked").forEach(cb => tabs.push(cb.value));
    const perms = [];
    if ($("#ak-perm-read").checked) perms.push("read");
    if ($("#ak-perm-deploy").checked) perms.push("deploy");
    if ($("#ak-perm-modify").checked) perms.push("modify");

    const expires = $("#ak-expires").value ? new Date($("#ak-expires").value).toISOString() : null;

    try {
      const r = await api("POST", "/keys", {
        name, agent_id: agentId,
        authorized_tabs: tabs,
        permissions: perms,
        expires_at: expires,
      });
      if (!r.ok) throw new Error(r.data?.error || r.status);
      showApiKeyCreated(r.data);
      return true;
    } catch (e) {
      toast("创建失败: " + e.message, "error");
      return false;
    }
  });

  // 加载 tab 列表
  loadNRTabsForApiKey();
}

async function loadNRTabsForApiKey() {
  try {
    const r = await api("GET", "/nr/tabs");
    const tabs = r.data?.tabs || [];
    const container = $("#ak-tab-list");
    if (!tabs.length) {
      container.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:4px 0">暂无 tab，不勾选即授权全部</div>`;
      return;
    }
    container.innerHTML = tabs.map(t => `
      <label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;border-bottom:1px solid var(--border)">
        <input type="checkbox" value="${esc(t.id)}" style="margin:0">
        <span style="flex:1;font-size:13px">${esc(t.label || t.id)}</span>
        <span style="font-size:11px;color:var(--text-muted)">${t.node_count || 0} 节点</span>
      </label>
    `).join("");
  } catch (e) {
    $("#ak-tab-list").innerHTML = `<div style="color:#dc2626;font-size:12px">加载 tab 失败: ${esc(e.message)}</div>`;
  }
}

function showApiKeyCreated(data) {
  modal("API Key 创建成功", `
    <div class="card" style="background:#dcfce7;border-left:3px solid #16a34a;margin-bottom:16px;padding:14px">
      <div style="font-weight:600;margin-bottom:8px">✅ 请立即复制以下 API Key（只显示一次）</div>
      <div style="background:#fff;padding:10px;border-radius:6px;font-family:monospace;font-size:13px;word-break:break-all;margin-bottom:10px">${esc(data.key)}</div>
      <button class="btn sm primary" id="ak-copy-btn">📋 复制</button>
    </div>
    <div style="font-size:13px;line-height:1.8">
      <b>下一步：告知 Agent</b><br>
      在 Agent 的环境变量中设置：<br>
      <code style="background:var(--bg-soft);padding:2px 6px;border-radius:4px">AF_GATEWAY_URL=http://你的网关地址:8000</code><br>
      <code style="background:var(--bg-soft);padding:2px 6px;border-radius:4px">AF_API_KEY=${esc(data.key)}</code><br>
      <code style="background:var(--bg-soft);padding:2px 6px;border-radius:4px">AF_AGENT_ID=${esc(data.agent_id)}</code>
    </div>
  `, null, "关闭");
  setTimeout(() => {
    const btn = $("#ak-copy-btn");
    if (btn) btn.onclick = () => {
      navigator.clipboard.writeText(data.key).then(() => toast("已复制", "success"));
    };
  }, 100);
}

async function editApiKey(keyId) {
  const k = _allApiKeys.find(x => x.key_id === keyId);
  if (!k) return;
  modal("编辑 API Key - " + k.name, `
    <div class="field">
      <label>名称</label>
      <input type="text" id="ak-edit-name" class="input" value="${esc(k.name)}">
    </div>
    <div class="field">
      <label>授权 tab（可多选，不选=全部）</label>
      <div id="ak-edit-tab-list" style="max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:6px 10px">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>
    </div>
    <div class="field">
      <label>权限</label>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-edit-perm-read" ${(k.permissions||[]).includes("read")?"checked":""} style="margin:0"> 只读
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-edit-perm-deploy" ${(k.permissions||[]).includes("deploy")?"checked":""} style="margin:0"> 可部署
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ak-edit-perm-modify" ${(k.permissions||[]).includes("modify")?"checked":""} style="margin:0"> 可修改
        </label>
      </div>
    </div>
  `, async () => {
    const name = $("#ak-edit-name").value.trim();
    const tabs = [];
    $$("#ak-edit-tab-list input[type=checkbox]:checked").forEach(cb => tabs.push(cb.value));
    const perms = [];
    if ($("#ak-edit-perm-read").checked) perms.push("read");
    if ($("#ak-edit-perm-deploy").checked) perms.push("deploy");
    if ($("#ak-edit-perm-modify").checked) perms.push("modify");
    try {
      const r = await api("PUT", "/keys/" + keyId, { name, authorized_tabs: tabs, permissions: perms });
      if (!r.ok) throw new Error(r.data?.error || r.status);
      toast("已更新", "success");
      refreshApiKeys();
      return true;
    } catch (e) {
      toast("更新失败: " + e.message, "error");
      return false;
    }
  });
  // 加载 tab 列表并勾选已有授权
  setTimeout(async () => {
    try {
      const r = await api("GET", "/nr/tabs");
      const tabs = r.data?.tabs || [];
      const container = $("#ak-edit-tab-list");
      container.innerHTML = tabs.map(t => `
        <label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;border-bottom:1px solid var(--border)">
          <input type="checkbox" value="${esc(t.id)}" ${(k.authorized_tabs||[]).includes(t.id)?"checked":""} style="margin:0">
          <span style="flex:1;font-size:13px">${esc(t.label || t.id)}</span>
          <span style="font-size:11px;color:var(--text-muted)">${t.node_count || 0} 节点</span>
        </label>
      `).join("");
    } catch (e) {
      $("#ak-edit-tab-list").innerHTML = `<div style="color:#dc2626;font-size:12px">加载失败</div>`;
    }
  }, 100);
}

async function revokeApiKey(keyId) {
  if (!confirm("确定吊销此 API Key？吊销后立即失效，Agent 将无法调用网关。")) return;
  try {
    const r = await api("POST", "/keys/" + keyId + "/revoke");
    if (!r.ok) throw new Error(r.data?.error || r.status);
    toast("已吊销", "success");
    refreshApiKeys();
  } catch (e) {
    toast("吊销失败: " + e.message, "error");
  }
}

async function showApiKeyLogs(keyId) {
  try {
    const r = await api("GET", "/keys/logs?limit=50");
    const allLogs = r.data?.logs || [];
    const logs = allLogs.filter(l => l.key_id === keyId);
    const k = _allApiKeys.find(x => x.key_id === keyId);
    modal("API Key 日志 - " + (k?.name || keyId), `
      <div style="max-height:400px;overflow-y:auto">
        ${logs.length ? logs.map(l => `
          <div style="padding:8px 0;border-bottom:1px solid var(--border);font-size:12px">
            <span style="color:var(--text-muted)">${esc((l.ts||"").slice(0,19).replace("T"," "))}</span>
            <span style="margin-left:8px;font-weight:600">${esc(l.action)}</span>
            <span style="margin-left:8px;color:${l.success?'#16a34a':'#dc2626'}">${l.success?'✅':'❌'}</span>
            ${l.detail ? `<span style="margin-left:8px;color:var(--text-muted)">${esc(l.detail)}</span>` : ""}
          </div>
        `).join("") : '<div class="empty">暂无日志</div>'}
      </div>
    `, null, "关闭");
  } catch (e) {
    toast("加载日志失败: " + e.message, "error");
  }
}

'''

if insert_marker in content:
    content = content.replace(insert_marker, api_key_js + insert_marker, 1)
    print("3. API Key JS 函数插入: OK")
else:
    print("3. API Key JS 函数插入: NOT FOUND")

with open(APPJS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
