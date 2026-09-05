#!/usr/bin/env python3
"""v1.5.7: app.js 增加 Token 统计 + 错误知识库页面"""

APPJS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APPJS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. TABS 增加 token_stats 和 error_kb
old_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "templates", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
new_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "templates", "token_stats", "error_kb", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
if old_tabs in content:
    content = content.replace(old_tabs, new_tabs, 1)
    print("1. TABS 添加: OK")
else:
    print("1. TABS 添加: NOT FOUND")

# 2. setTab 增加路由
old_route = '  else if (tab === "templates") loadTemplates();'
new_route = '  else if (tab === "templates") loadTemplates();\n  else if (tab === "token_stats") loadTokenStats();\n  else if (tab === "error_kb") loadErrorKB();'
if old_route in content:
    content = content.replace(old_route, new_route, 1)
    print("2. setTab 路由添加: OK")
else:
    print("2. setTab 路由添加: NOT FOUND")

# 3. 在模板库函数之后插入 Token 统计和错误知识库页面
insert_marker = '// ── API Key 管理（v1.5.1）──'

new_pages = '''// ── Token 统计（v1.5.7）──
async function loadTokenStats() {
  const v = $("#view-token_stats");
  v.innerHTML = `
    <div class="view-head">
      <h2>Token 消耗统计</h2>
      <span class="sub">AutoFlow Pro 模式下的 Token 消耗趋势与分析</span>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:16px">
      <select id="ts-days" class="input" style="width:auto">
        <option value="7">最近 7 天</option>
        <option value="14">最近 14 天</option>
        <option value="30">最近 30 天</option>
      </select>
      <button class="btn" id="ts-refresh">刷新</button>
    </div>
    <div id="ts-summary" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px"></div>
    <div class="card" style="margin-bottom:16px">
      <h3 style="margin:0 0 12px">每日趋势</h3>
      <div id="ts-daily"></div>
    </div>
    <div class="card">
      <h3 style="margin:0 0 12px">按 Agent 统计</h3>
      <div id="ts-by-agent"></div>
    </div>`;

  $("#ts-days").onchange = () => refreshTokenStats();
  $("#ts-refresh").onclick = () => refreshTokenStats();
  await refreshTokenStats();
}

async function refreshTokenStats() {
  const days = $("#ts-days").value;
  try {
    const r = await api("GET", "/core/token-stats?days=" + days);
    if (!r.ok) throw new Error(r.data?.error || r.status);
    const s = r.data.stats;

    // 汇总卡片
    $("#ts-summary").innerHTML = `
      <div class="card" style="background:var(--bg-soft);text-align:center;padding:16px">
        <div style="font-size:24px;font-weight:700">${s.total_calls || 0}</div>
        <div style="font-size:12px;color:var(--text-muted)">总调用次数</div>
      </div>
      <div class="card" style="background:var(--bg-soft);text-align:center;padding:16px">
        <div style="font-size:24px;font-weight:700">${(s.estimated_tokens || 0).toLocaleString()}</div>
        <div style="font-size:12px;color:var(--text-muted)">估算 Token 消耗</div>
      </div>
      <div class="card" style="background:var(--bg-soft);text-align:center;padding:16px">
        <div style="font-size:24px;font-weight:700">${s.avg_tokens_per_call || 0}</div>
        <div style="font-size:12px;color:var(--text-muted)">平均每次 Token</div>
      </div>
      <div class="card" style="background:var(--bg-soft);text-align:center;padding:16px">
        <div style="font-size:24px;font-weight:700">${Object.keys(s.by_mode || {}).length}</div>
        <div style="font-size:12px;color:var(--text-muted)">使用模式数</div>
      </div>`;

    // 每日趋势（简单柱状图，用 div 实现）
    const daily = (s.daily || []).slice().reverse();
    const maxTokens = Math.max(...daily.map(d => d.estimated_tokens || 0), 1);
    $("#ts-daily").innerHTML = daily.length ? `
      <div style="display:flex;align-items:flex-end;gap:4px;height:120px;padding:8px 0">
        ${daily.map(d => `
          <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
            <div style="width:100%;background:var(--primary);opacity:0.7;border-radius:4px 4px 0 0;height:${Math.max(2, (d.estimated_tokens / maxTokens) * 100)}%" title="${d.estimated_tokens} tokens"></div>
            <span style="font-size:10px;color:var(--text-muted)">${d.date.slice(5)}</span>
          </div>
        `).join("")}
      </div>
    ` : '<div class="empty">暂无数据</div>';

    // 按 Agent 统计
    const agents = Object.entries(s.by_agent || {}).sort((a, b) => (b[1].calls || 0) - (a[1].calls || 0));
    $("#ts-by-agent").innerHTML = agents.length ? `
      <table style="width:100%;font-size:13px">
        <thead><tr style="text-align:left;color:var(--text-muted)">
          <th style="padding:8px">Agent ID</th>
          <th style="padding:8px">调用次数</th>
          <th style="padding:8px">估算 Token</th>
          <th style="padding:8px">平均每次</th>
        </tr></thead>
        <tbody>
          ${agents.map(([id, st]) => `
            <tr style="border-top:1px solid var(--border)">
              <td style="padding:8px"><code>${esc(id)}</code></td>
              <td style="padding:8px">${st.calls || 0}</td>
              <td style="padding:8px">${((st.input_chars || 0) + (st.output_chars || 0)) / 4 | 0}</td>
              <td style="padding:8px">${st.calls ? (((st.input_chars || 0) + (st.output_chars || 0)) / 4 / st.calls | 0) : 0}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    ` : '<div class="empty">暂无 Agent 数据</div>';
  } catch (e) {
    $("#ts-summary").innerHTML = errBox(e.message, refreshTokenStats);
  }
}

// ── 错误知识库（v1.5.7）──
async function loadErrorKB() {
  const v = $("#view-error_kb");
  v.innerHTML = `
    <div class="view-head">
      <h2>错误知识库</h2>
      <span class="sub">自动记录 propose-dsl 编译失败案例，按类型分类，辅助排查</span>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <select id="ek-type" class="input" style="width:auto">
        <option value="">全部类型</option>
        <option value="unknown_entity">未知实体</option>
        <option value="syntax_error">语法错误</option>
        <option value="lint_error">Lint 警告</option>
        <option value="gate_failed">闸门拦截</option>
        <option value="e2e_failed">E2E 失败</option>
        <option value="deploy_failed">部署失败</option>
        <option value="other">其他</option>
      </select>
      <input type="text" id="ek-keyword" class="input" placeholder="搜索 DSL 或错误信息..." style="flex:1;min-width:200px">
      <button class="btn" id="ek-refresh">刷新</button>
    </div>
    <div id="ek-stats" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px"></div>
    <div id="ek-list"><div class="empty">加载中…</div></div>`;

  $("#ek-type").onchange = () => refreshErrorKB();
  $("#ek-keyword").oninput = () => refreshErrorKB();
  $("#ek-refresh").onclick = () => refreshErrorKB();
  await refreshErrorKB();
}

async function refreshErrorKB() {
  const type = $("#ek-type").value;
  const kw = $("#ek-keyword").value;
  try {
    let url = "/errors?limit=50";
    if (type) url += "&error_type=" + encodeURIComponent(type);
    if (kw) url += "&keyword=" + encodeURIComponent(kw);
    const r = await api("GET", url);
    if (!r.ok) throw new Error(r.data?.error || r.status);

    // 统计标签
    const stats = r.data.stats || {};
    const typeNames = {
      unknown_entity: "未知实体", syntax_error: "语法错误", lint_error: "Lint 警告",
      gate_failed: "闸门拦截", e2e_failed: "E2E 失败", deploy_failed: "部署失败",
      empty_dsl: "空 DSL", other: "其他"
    };
    $("#ek-stats").innerHTML = Object.entries(stats)
      .filter(([k]) => k !== "_total")
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `
        <span class="badge" style="background:var(--bg-soft);font-size:12px;padding:6px 12px">
          ${typeNames[k] || k}: <b>${v}</b>
        </span>
      `).join("") || '<span style="color:var(--text-muted);font-size:13px">暂无错误记录</span>';

    // 错误列表
    const errors = r.data.errors || [];
    $("#ek-list").innerHTML = errors.length ? errors.map(e => `
      <div class="card" style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px">
          <span class="badge" style="background:#fee2e2;color:#991b1b;font-size:11px">${typeNames[e.error_type] || e.error_type}</span>
          <span style="font-size:11px;color:var(--text-muted)">${esc((e.timestamp || "").slice(0, 19).replace("T", " "))}</span>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">
          Agent: <code>${esc(e.agent_id || "unknown")}</code>
          ${e.stage ? `· 阶段: ${esc(e.stage)}` : ""}
        </div>
        <div style="background:var(--bg-soft);padding:8px;border-radius:6px;font-size:12px;white-space:pre-wrap;max-height:80px;overflow:auto;margin-bottom:6px">${esc(e.dsl || "")}</div>
        <div style="font-size:12px;color:#dc2626">❌ ${esc(e.error || "")}</div>
      </div>
    `).join("") : '<div class="empty">暂无错误记录</div>';
  } catch (e) {
    $("#ek-list").innerHTML = errBox(e.message, refreshErrorKB);
  }
}

'''

if insert_marker in content:
    content = content.replace(insert_marker, new_pages + insert_marker, 1)
    print("3. Token 统计 + 错误知识库页面: OK")
else:
    print("3. Token 统计 + 错误知识库页面: NOT FOUND")

with open(APPJS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
