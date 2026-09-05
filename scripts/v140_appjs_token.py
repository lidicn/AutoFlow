#!/usr/bin/env python3
"""修改 app.js: 增加授权码管理页面"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# 1. TABS 数组增加 deploy_tokens
old_tabs = '''const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'''

new_tabs = '''const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'''

if old_tabs in content:
    content = content.replace(old_tabs, new_tabs, 1)
    print("1. TABS 数组增加 deploy_tokens")
else:
    print("WARNING: 未找到 TABS 数组")

# 2. setTab 函数增加 loadDeployTokens 调用
old_settab = '''  else if (tab === "agents") loadAgents();
  else if (tab === "safe") loadSafeGate();'''

new_settab = '''  else if (tab === "agents") loadAgents();
  else if (tab === "deploy_tokens") loadDeployTokens();
  else if (tab === "safe") loadSafeGate();'''

if old_settab in content:
    content = content.replace(old_settab, new_settab, 1)
    print("2. setTab 增加 loadDeployTokens 调用")
else:
    print("WARNING: 未找到 setTab agents 位置")

# 3. 在 loadAgents 函数之后增加 loadDeployTokens 函数
# 先找到 loadAgents 函数的结尾
old_agents_end = '''async function loadAgents() {
  const v = $("#view-agents");'''

# 找到 loadAgents 函数的位置，在它之前插入 loadDeployTokens
# 实际上应该在 loadAgents 之后，但为了简单，我们在 loadAgents 之前插入
insert_pos = content.find('async function loadAgents() {')
if insert_pos > 0:
    deploy_tokens_func = '''
// ── 部署授权码管理（P4）──
let _allDeployTokens = [];
let _currentTokenDetail = null;

async function loadDeployTokens() {
  const v = $("#view-deploy_tokens");
  v.innerHTML = `
    <div class="view-head">
      <h2>部署授权码</h2>
      <span class="sub">给受信任 Agent 发放授权码，可在限定范围内自动部署 Flow</span>
      <button class="btn primary" id="dt-create-btn" style="margin-left:auto">➕ 创建授权码</button>
    </div>
    <div id="dt-list"><div class="empty">加载中…</div></div>
    <div id="dt-detail" hidden></div>`;

  $("#dt-create-btn").onclick = () => showCreateTokenModal();
  await refreshDeployTokens();
}

async function refreshDeployTokens() {
  const list = $("#dt-list");
  try {
    const r = await api("GET", "/deploy-tokens");
    if (!r.ok) throw new Error(r.data?.error || r.status);
    _allDeployTokens = r.data?.tokens || [];
    renderDeployTokenList();
  } catch (e) {
    list.innerHTML = errBox(e.message, refreshDeployTokens);
  }
}

function renderDeployTokenList() {
  const list = $("#dt-list");
  if (!_allDeployTokens.length) {
    list.innerHTML = `<div class="empty">暂无授权码。点击右上角「创建授权码」为受信任 Agent 发放自动部署权限。</div>`;
    return;
  }
  list.innerHTML = _allDeployTokens.map(t => {
    const isActive = t.is_active;
    const statusBadge = isActive
      ? `<span class="badge" style="background:#dcfce7;color:#166534">有效</span>`
      : (t.revoked ? `<span class="badge" style="background:#fee2e2;color:#991b1b">已吊销</span>`
         : `<span class="badge" style="background:#fef3c7;color:#92400e">已过期</span>`);
    const stats = t.stats || {};
    return `
    <div class="card deploy-token-card" data-token-id="${esc(t.token_id)}">
      <div style="display:flex;justify-content:space-between;align-items:start;gap:12px">
        <div style="flex:1">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
            <h3 style="margin:0">${esc(t.name)}</h3>
            ${statusBadge}
          </div>
          <div class="meta" style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
            目标 tab: <b>${esc(t.target_tab)}</b> ·
            权限: ${(t.permissions || []).join(", ")} ·
            创建: ${esc((t.created_at || "").slice(0, 19).replace("T", " "))} ·
            到期: ${esc((t.expires_at || "").slice(0, 19).replace("T", " "))}
          </div>
          <div style="display:flex;gap:16px;font-size:12px;color:var(--text-muted)">
            <span>部署: <b>${stats.deploy_count || 0}</b></span>
            <span>修改: <b>${stats.modify_count || 0}</b></span>
            <span>撤回: <b>${stats.undeploy_count || 0}</b></span>
            <span>节点: <b>${stats.nodes_deployed || 0}</b></span>
            <span>失败: <b style="color:${stats.failed_count ? '#dc2626' : 'inherit'}">${stats.failed_count || 0}</b></span>
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <button class="btn sm" data-dt-logs="${esc(t.token_id)}">📋 日志</button>
          <button class="btn sm" data-dt-snaps="${esc(t.token_id)}">📸 快照</button>
          ${isActive ? `<button class="btn sm danger" data-dt-revoke="${esc(t.token_id)}">🚫 吊销</button>` : ""}
        </div>
      </div>
    </div>`;
  }).join("");

  // 绑定事件
  $$("[data-dt-logs]").forEach(b => b.onclick = () => showTokenLogs(b.dataset.dtLogs));
  $$("[data-dt-snaps]").forEach(b => b.onclick = () => showTokenSnapshots(b.dataset.dtSnaps));
  $$("[data-dt-revoke]").forEach(b => b.onclick = () => revokeToken(b.dataset.dtRevoke));
}

function showCreateTokenModal() {
  modal("创建部署授权码", `
    <div class="field">
      <label>名称 *</label>
      <input type="text" id="dt-name" class="input" placeholder="如：客厅自动化 Agent">
    </div>
    <div class="field">
      <label>目标 tab *</label>
      <input type="text" id="dt-target-tab" class="input" placeholder="如：客厅（Agent 只能在此 tab 部署）">
    </div>
    <div class="field">
      <label>有效期（小时）</label>
      <input type="number" id="dt-expires" class="input" value="4" min="1" max="720">
    </div>
    <div class="field">
      <label>权限</label>
      <div style="display:flex;gap:12px">
        <label><input type="checkbox" id="dt-perm-deploy" checked> 部署</label>
        <label><input type="checkbox" id="dt-perm-modify"> 修改</label>
        <label><input type="checkbox" id="dt-perm-undeploy"> 撤回</label>
      </div>
    </div>
    <div class="field">
      <label>节点数阈值（超过需人工审批）</label>
      <input type="number" id="dt-threshold" class="input" value="50" min="1">
    </div>
    <div class="field">
      <label>最大节点数配额</label>
      <input type="number" id="dt-max-nodes" class="input" value="500" min="1">
    </div>
    <div class="field">
      <label>最大 flow 数配额</label>
      <input type="number" id="dt-max-flows" class="input" value="20" min="1">
    </div>
    <div class="field">
      <label>操作频率限制（次/分钟）</label>
      <input type="number" id="dt-rate" class="input" value="10" min="1">
    </div>
    <div style="margin-top:16px;text-align:right;display:flex;gap:8px;justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="dt-create-confirm">创建</button>
    </div>
  `);

  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    const targetTab = $("#dt-target-tab").value.trim();
    if (!name || !targetTab) { toast("名称和目标 tab 不能为空"); return; }

    const permissions = [];
    if ($("#dt-perm-deploy").checked) permissions.push("deploy");
    if ($("#dt-perm-modify").checked) permissions.push("modify");
    if ($("#dt-perm-undeploy").checked) permissions.push("undeploy");
    if (!permissions.length) { toast("至少选择一个权限"); return; }

    const btn = $("#dt-create-confirm");
    btn.disabled = true;
    btn.textContent = "创建中…";
    try {
      const r = await api("POST", "/deploy-tokens", {
        name, target_tab: targetTab,
        expires_in_hours: parseFloat($("#dt-expires").value),
        permissions,
        node_threshold: parseInt($("#dt-threshold").value),
        max_nodes: parseInt($("#dt-max-nodes").value),
        max_flows: parseInt($("#dt-max-flows").value),
        rate_limit_per_min: parseInt($("#dt-rate").value),
        require_confirm_dangerous: true,
      });
      if (r.ok && r.data?.ok) {
        const token = r.data.token;
        closeModal();
        // 显示创建成功和授权码
        modal("授权码创建成功", `
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;margin-bottom:16px">
            <p style="color:#166534;font-weight:600;margin:0 0 8px">✅ 授权码已创建</p>
            <p style="font-size:12px;color:#15803d;margin:0 0 8px">请立即复制并保存，此授权码只显示一次！</p>
            <div style="background:#fff;padding:10px;border-radius:6px;border:1px solid #d1fae5;font-family:monospace;font-size:14px;word-break:break-all">${esc(token.token_plaintext)}</div>
          </div>
          <div class="meta" style="font-size:12px;color:var(--text-muted)">
            <p>目标 tab: <b>${esc(token.target_tab)}</b></p>
            <p>有效期: ${esc((token.expires_at || "").slice(0, 19).replace("T", " "))}</p>
            <p>权限: ${(token.permissions || []).join(", ")}</p>
          </div>
          <div style="margin-top:16px;text-align:right">
            <button class="btn primary" onclick="closeModal();refreshDeployTokens()">我已复制，关闭</button>
          </div>
        `);
      } else {
        toast("创建失败：" + (r.data?.error || r.status));
      }
    } catch (e) {
      toast("创建出错：" + e.message);
    }
    btn.disabled = false;
    btn.textContent = "创建";
  };
}

async function revokeToken(tokenId) {
  if (!confirm("确定吊销此授权码吗？吊销后立即失效，正在使用的 Agent 将无法自动部署。")) return;
  try {
    const r = await api("DELETE", "/deploy-tokens/" + tokenId);
    if (r.ok) {
      toast("授权码已吊销");
      refreshDeployTokens();
    } else {
      toast("吊销失败：" + (r.data?.error || r.status));
    }
  } catch (e) {
    toast("吊销出错：" + e.message);
  }
}

async function showTokenLogs(tokenId) {
  const token = _allDeployTokens.find(t => t.token_id === tokenId);
  try {
    const r = await api("GET", "/deploy-tokens/" + tokenId + "/logs?limit=50");
    const logs = r.data?.logs || [];
    modal("使用日志 - " + (token?.name || tokenId), `
      <div style="max-height:400px;overflow:auto">
        ${logs.length ? logs.map(l => `
          <div style="padding:8px 0;border-bottom:1px solid var(--border);font-size:12px">
            <div style="display:flex;justify-content:space-between">
              <span style="font-weight:600">${esc(l.operation)}</span>
              <span style="color:${l.success ? '#16a34a' : '#dc2626'}">${l.success ? '✅' : '❌'} ${esc(l.error || '')}</span>
            </div>
            <div class="meta" style="color:var(--text-muted)">
              ${esc((l.timestamp || "").slice(0, 19).replace("T", " "))} ·
              Agent: ${esc(l.agent_id || "")} ·
              ${l.flow_label ? 'Flow: ' + esc(l.flow_label) + ' ·' : ''}
              节点: ${l.node_count || 0}
            </div>
          </div>
        `).join("") : '<div class="empty">暂无日志</div>'}
      </div>
      <div style="margin-top:16px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>
    `);
  } catch (e) {
    toast("加载日志失败：" + e.message);
  }
}

async function showTokenSnapshots(tokenId) {
  const token = _allDeployTokens.find(t => t.token_id === tokenId);
  try {
    const r = await api("GET", "/deploy-tokens/" + tokenId + "/snapshots");
    const snaps = r.data?.snapshots || [];
    modal("快照 - " + (token?.name || tokenId), `
      <div style="max-height:400px;overflow:auto">
        ${snaps.length ? snaps.map(s => `
          <div style="padding:10px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
            <div>
              <div style="font-weight:600;font-size:13px">${esc(s.label || s.snapshot_id)}</div>
              <div class="meta" style="font-size:11px;color:var(--text-muted)">
                ${esc((s.created_at || "").slice(0, 19).replace("T", " "))} ·
                类型: ${s.type === 'full' ? '全量' : '增量'} ·
                节点: ${s.node_count || 0}
              </div>
            </div>
            <button class="btn sm danger" data-snap-rollback="${esc(s.snapshot_id)}">回滚到此</button>
          </div>
        `).join("") : '<div class="empty">暂无快照</div>'}
      </div>
      <div style="margin-top:16px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>
    `);
    $$("[data-snap-rollback]").forEach(b => b.onclick = () => rollbackToSnapshot(tokenId, b.dataset.snapRollback));
  } catch (e) {
    toast("加载快照失败：" + e.message);
  }
}

async function rollbackToSnapshot(tokenId, snapshotId) {
  if (!confirm("确定回滚到此快照吗？回滚会恢复 tab 到快照时的状态，当前未保存的变更将丢失。回滚前会自动创建新快照。")) return;
  try {
    const r = await api("POST", "/deploy-tokens/" + tokenId + "/rollback", {
      snapshot_id: snapshotId, type: "full"
    });
    if (r.data?.ok) {
      toast("回滚成功");
      closeModal();
    } else {
      toast("回滚失败：" + (r.data?.error || r.status));
    }
  } catch (e) {
    toast("回滚出错：" + e.message);
  }
}

'''
    content = content[:insert_pos] + deploy_tokens_func + content[insert_pos:]
    print("3. 增加 loadDeployTokens 函数（约 " + str(len(deploy_tokens_func)) + " 字符）")
else:
    print("WARNING: 未找到 loadAgents 函数位置")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)

print("\napp.js 授权码管理页面完成")
