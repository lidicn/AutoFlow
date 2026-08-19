"use strict";

// ── 基础工具 ──
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function readCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}
function uiToken() { return localStorage.getItem("af_ui_token") || readCookie("af_ui_token") || ""; }
function qs(token) { return token ? (token.startsWith("?") ? token : "?token=" + token) : ""; }

async function api(method, path, body, opts = {}) {
  const token = uiToken();
  const url = "/api" + path + (method === "GET" && token ? (path.includes("?") ? "&" : "?") + "token=" + token : "");
  const ctrl = new AbortController();
  const timeoutMs = opts.timeout || 25000;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const fetchOpts = { method, headers: {}, signal: ctrl.signal };
  if (token && method !== "GET") fetchOpts.headers["Authorization"] = "Bearer " + token;
  if (body !== undefined) {
    fetchOpts.headers["Content-Type"] = "application/json";
    fetchOpts.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(url, fetchOpts);
    if (res.status === 403) throw new Error("访问被拒绝：需要 WebUI 令牌（点右上角 🔑 设置）");
    let data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error(`请求超时（${timeoutMs / 1000}s）：网关可能正忙或网络不通，请重试`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// 错误占位 + 重试按钮
let _retryFn = null;
function errBox(msg, retryFn) {
  _retryFn = retryFn || null;
  const btn = retryFn ? `<button class="btn sm" id="retryBtn">🔄 重试</button>` : "";
  const html = `<div class="empty err">⚠️ ${esc(msg)}<div style="margin-top:10px">${btn}</div></div>`;
  if (retryFn) {
    setTimeout(() => {
      const b = document.getElementById("retryBtn");
      if (b) b.onclick = () => { if (_retryFn) _retryFn(); };
    }, 0);
  }
  return html;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}
function modal(title, html) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modalMask").hidden = false;
}
function closeModal() { $("#modalMask").hidden = true; }

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function badge(cls, text) { return `<span class="badge ${cls}">${esc(text)}</span>`; }
const MODES = ["black", "white", "dual", "both", "admin"];
function modeLabel(m) {
  return ({
    black: "黑箱（仅DSL，连 /mcp）",
    white: "白箱（可自由部署，连 /mcp-white）",
    dual: "双箱（先白后黑，连 /mcp 或 /mcp-white）",
    both: "不限（旧身份，连 /mcp）",
    admin: "管理员（运维专用，连 /mcp-admin）"
  })[m] || m;
}
// 身份模式 → MCP 端点 path（用于新建后展示正确连接地址）
function endpointForMode(m) {
  return ({ black: "/mcp", white: "/mcp-white", dual: "/mcp-white", both: "/mcp", admin: "/mcp-admin" })[m] || "/mcp";
}
function fmtTime(s) {
  if (!s) return "—";
  try { return new Date(s).toLocaleString("zh-CN", { hour12: false }); } catch { return s; }
}

// ── 导航（C1/C3：工作区 + 版本同步已移除，新增设置管理界面）──
const TABS = ["dashboard", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent"];
function setTab(tab) {
  if (!TABS.includes(tab)) tab = "dashboard";
  $$(".navitem[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  closeMobileSheet();
  TABS.forEach((t) => ($("#view-" + t).hidden = t !== tab));
  if (tab === "dashboard") loadDashboard();
  else if (tab === "agents") loadAgents();
  else if (tab === "safe") loadSafeGate();
  else if (tab === "proposals") loadProposals();
  else if (tab === "deployed") loadDeployed();
  else if (tab === "notes") loadNotes();
  else if (tab === "diagnostics") loadDiagnostics();
  else if (tab === "subflows") loadSubflows();
  else if (tab === "link_apis") loadLinkApis();
  else if (tab === "settings") loadSettings();
  else if (tab === "help") loadHelp();
  else if (tab === "acp_tokens") loadAcpTokens();
  else if (tab === "llm_settings") loadLlmSettings();
  else if (tab === "llm_agent") loadLlmAgent();
}
// ── 帮助（使用手册，内容静态写在 index.html 的 #view-help，仅做进入时滚顶）──
function loadHelp() {
  const v = $("#view-help");
  if (v) v.scrollTop = 0;
}

// ── 概览 ──
async function loadDashboard() {
  const v = $("#view-dashboard");
  v.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const [cfg, ag, pe, pr, no, de] = await Promise.all([
      api("GET", "/config"),
      api("GET", "/agents"),
      api("GET", "/pending"),
      api("GET", "/proposals"),
      api("GET", "/notes"),
      api("GET", "/deployed"),
    ]);
    const c = cfg.data || {};
    const counts = {
      agents: (ag.data?.agents || []).length,
      pending: (pe.data?.pending || []).length,
      raw: (pr.data?.proposals || []).filter((p) => p.status === "raw").length,
      deployed: (de.data?.deployed || []).length,
      notes: (no.data?.notes || []).length,
    };
    v.innerHTML = `
      <div class="view-head"><h2>概览</h2><span class="sub">环境 ${esc(c.env || "")} ｜ 黑箱 ${esc(c.mcp || "")} ｜ 白箱 ${esc(c.mcp_white || "")} ｜ 管理 ${esc(c.mcp_admin || "")}</span></div>
      <div class="grid cols-4">
        <div class="card"><div class="meta">已注册 Agent</div><div class="stat">${counts.agents}</div></div>
        <div class="card"><div class="meta">待确认操作</div><div class="stat">${counts.pending}</div></div>
        <div class="card"><div class="meta">待审提案(raw)</div><div class="stat">${counts.raw}</div></div>
        <div class="card"><div class="meta">已部署 flow</div><div class="stat">${counts.deployed}</div></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>快速上手</h3>
        <div class="desc">
          1. 到 <b>Agents</b> 面板创建 agent（如 deepseek++），复制生成的身份识别码。<br>
          2. 在 agent 的 MCP 配置里填 <code>Authorization: Bearer &lt;身份码&gt;</code>，指向 <code>${esc(c.mcp || "")}</code>。<br>
          3. agent 提交场景 DSL 后，到 <b>提案</b> 面板查看闸门结果，点「部署到 NR」直接部署。<br>
          4. 部署后在 <b>已部署</b> 面板可随时安全撤回（只删网关自己部署的 flow）。<br>
          5. <b>笔记</b> 记录你那些暂时落不了地的智能家居想法。
        </div>
      </div>`;
  } catch (e) {
    v.innerHTML = errBox(e.message || "加载失败", loadDashboard);
  }
}

// ── 工作区（总体/当前/最近完成 + 待确认合并展示）──
async function loadWorkspace() {
  const v = $("#view-workspace");
  v.innerHTML = `<div class="view-head"><h2>🗺️ 工作区</h2><span class="sub">总体计划 · 当前进度 · 最近完成</span>
      <button class="btn sm" id="ws-refresh" style="margin-left:auto">刷新</button></div>
    <div id="ws-body"><div class="empty">加载中…</div></div>`;
  $("#ws-refresh").onclick = loadWorkspace;
  try {
    const [pl, pc, pd] = await Promise.all([
      api("GET", "/plan"),
      api("GET", "/commands"),
      api("GET", "/decisions"),
    ]);
    const plan = pl.data || { overall: "", current: "", completed: [] };
    const cmds = pc.data?.commands || [];
    const decs = pd.data?.decisions || [];
    const completed = (plan.completed || []).slice(0, 12);
    const ws = $("#ws-body");
    ws.innerHTML = `
      <div class="card ws-cmd" style="margin-top:14px">
        <h3>💬 直达 DeepSeek 指令</h3>
        <p class="desc">用大白话给常驻的 DeepSeek 下达任务（点火/查询/编排等）。它用 autoflow 工具执行，完成后经 Bark 回报。给我（WorkBuddy）的指令请仍走远程会话。</p>
        <textarea id="ws-cmd-text" style="width:100%;min-height:72px;border:1px solid var(--border);border-radius:6px;padding:10px;font-size:14px;background:var(--bg);color:var(--text)" placeholder="例如：把书房 H5 场景重新点火一遍；或：查询书房所有灯的当前状态"></textarea>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <button class="btn primary" id="ws-cmd-send" style="flex:0 0 auto">发送给 DeepSeek</button>
          <span class="meta" id="ws-cmd-hint">Ctrl/⌘ + Enter 发送</span>
        </div>
        <div id="ws-cmd-hist" style="margin-top:12px">${cmds.length ? "" : `<div class="empty">还没有下达过指令。</div>`}</div>
      </div>
      <div class="card ws-decide" style="margin-top:14px">
        <h3>🗳️ 待你决策${decs.length ? `（${decs.length}）` : ""}</h3>
        <p class="desc">DeepSeek 抛出的选择题，点选即可；选择会自动回灌给它继续。</p>
        <div id="ws-decisions">${decs.length ? "" : `<div class="empty">暂无待决策项 🎉</div>`}</div>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <div class="card">
          <h3>🎯 总体计划</h3>
          <p class="desc">长期目标 / 里程碑（你在此编辑，agent 一般不动）。</p>
          <textarea id="ws-overall" style="width:100%;min-height:90px;border:1px solid var(--border);border-radius:6px;padding:8px;font-size:13px;background:var(--bg);color:var(--text)" placeholder="例如：M2 完成后跑 H1–H10 评测，验证编译器修复拉升黑箱通过率">${esc(plan.overall || "")}</textarea>
          <button class="btn primary sm" id="ws-save-overall" style="margin-top:8px">保存总体计划</button>
        </div>
        <div class="card">
          <h3>⚙️ 当前进行</h3>
          <p class="desc">agent 实时同步的当前任务。</p>
          <div class="desc" style="font-size:15px;font-weight:600;min-height:24px;white-space:pre-wrap">${esc(plan.current || "（无）")}</div>
          <div class="meta">最近更新：${fmtTime(plan.updated_at)}</div>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>✅ 最近完成</h3>
        ${completed.length ? `<div class="ws-completed">` + completed.map((c) =>
          `<div class="ws-completed-item"><span class="ws-dot">●</span><span class="ws-text">${esc(c.text)}</span><span class="meta">${fmtTime(c.ts)}</span></div>`
        ).join("") + `</div>` : `<div class="empty">还没有完成记录。</div>`}
      </div>`;
    $("#ws-save-overall").onclick = async () => {
      const r = await api("PUT", "/plan", { overall: $("#ws-overall").value });
      toast(r.ok ? "已保存总体计划" : "失败：" + (r.data?.error || r.status));
      if (r.ok) loadWorkspace();
    };
    // 指令框：发送给 deepseek
    const cmdSend = async () => {
      const ta = $("#ws-cmd-text");
      const text = (ta.value || "").trim();
      if (!text) { toast("请输入指令内容"); return; }
      const btn = $("#ws-cmd-send");
      btn.disabled = true; btn.textContent = "投递中…";
      try {
        const r = await api("POST", "/commands", { text });
        if (r.ok) { ta.value = ""; toast("已投递给 DeepSeek，完成后 Bark 回报"); }
        else toast("失败：" + (r.data?.error || r.status));
      } catch (e) { toast(e.message || "投递失败"); }
      btn.disabled = false; btn.textContent = "发送给 DeepSeek";
      await refreshCmdHist();
    };
    $("#ws-cmd-send").onclick = cmdSend;
    $("#ws-cmd-text").addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); cmdSend(); }
    });
    renderCmdHist(cmds);
    renderDecisions(decs);
    startWsAutoRefresh();
  } catch (e) {
    $("#ws-body").innerHTML = errBox(e.message || "加载失败", loadWorkspace);
  }
}

// 指令历史渲染 + 状态徽标
function cmdStatusBadge(s) {
  const map = {
    queued: ["排队中", "risk-low"],
    dispatching: ["投递中", "risk-medium"],
    dispatched: ["已送达", "risk-low"],
    failed: ["投递失败", "risk-high"],
  };
  const [label, cls] = map[s] || [s || "?", "risk-low"];
  return badge(cls, label);
}
function renderCmdHist(cmds) {
  const box = $("#ws-cmd-hist");
  if (!box) return;
  if (!cmds || !cmds.length) { box.innerHTML = `<div class="empty">还没有下达过指令。</div>`; return; }
  box.innerHTML = cmds.slice(0, 10).map((c) => `
    <div class="item">
      <div class="row">
        <div class="title" style="white-space:pre-wrap">${esc(c.text)}</div>
        ${cmdStatusBadge(c.status)}
      </div>
      <div class="meta">${fmtTime(c.created_at)}${c.status === "failed" && c.result ? " ｜ " + esc((c.result || "").slice(0, 80)) : ""}</div>
    </div>`).join("");
}
async function refreshCmdHist() {
  try {
    const r = await api("GET", "/commands");
    renderCmdHist(r.data?.commands || []);
  } catch (e) { /* 静默：历史刷新失败不打断主流程 */ }
}

// ── 待你决策（多选项请示）──
function renderDecisions(list) {
  const box = $("#ws-decisions");
  if (!box) return;
  if (!list || !list.length) { box.innerHTML = `<div class="empty">暂无待决策项 🎉</div>`; return; }
  box.innerHTML = list.map((d) => `
    <div class="item" data-dec="${esc(d.id)}">
      <div class="title">${esc(d.question)}</div>
      <div class="meta">${esc(d.source || "deepseek")} ｜ ${fmtTime(d.created_at)}${d.status !== "pending" ? " ｜ 已选：" + esc(d.chosen_text || "") : ""}</div>
      <div class="ws-opts">
        ${d.options.map((o, i) => `<button class="btn sm${d.status !== "pending" ? " ghost" : ""}" data-dec="${esc(d.id)}" data-idx="${i}">${esc(o)}</button>`).join("")}
      </div>
    </div>`).join("");
  $$("#ws-decisions [data-idx]").forEach((b) => {
    b.onclick = () => chooseDecision(b.dataset.dec, parseInt(b.dataset.idx, 10), b);
  });
}
async function chooseDecision(id, idx, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api("PUT", "/decisions/" + id, { chosen: idx });
    if (r.ok) toast("已选择：" + (r.data?.decision?.chosen_text || ("选项" + (idx + 1))));
    else toast("失败：" + (r.data?.error || r.status));
  } catch (e) { toast(e.message || "操作失败"); }
  await refreshDecisions();
}
async function refreshDecisions() {
  try {
    const r = await api("GET", "/decisions");
    renderDecisions(r.data?.decisions || []);
  } catch (e) { /* 静默 */ }
}
// 工作区自动刷新：进入 tab 后每 6s 轻量刷新指令历史 + 决策（不触碰指令输入框）
let wsTimer = null;
function startWsAutoRefresh() {
  stopWsAutoRefresh();
  wsTimer = setInterval(() => { refreshCmdHist(); refreshDecisions(); }, 6000);
}
function stopWsAutoRefresh() {
  if (wsTimer) { clearInterval(wsTimer); wsTimer = null; }
}

// ── Agents ──
async function loadAgents() {
  const v = $("#view-agents");
  v.innerHTML = `<div class="view-head"><h2>Agents</h2></div>
    <div class="card form-card">
      <h3>新建 Agent</h3>
      <div class="field"><label>名称（如 deepseek++）</label><input id="a-name" placeholder="deepseek++"></div>
      <div class="field"><label>权限级别</label>
        <select id="a-tier"><option value="staging">staging（练手/虚拟HA）</option><option value="prod">prod（真实环境）</option><option value="sandbox">sandbox（受限）</option></select></div>
      <div class="field"><label>身份模式</label>
        <select id="a-mode">${MODES.map((m) => `<option value="${m}">${modeLabel(m)}</option>`).join("")}</select></div>
      <div class="field"><label>备注</label><textarea id="a-notes" placeholder="可选"></textarea></div>
      <button class="btn primary" id="a-create">生成身份识别码</button>
    </div>
    <div id="a-list" style="margin-top:14px"><div class="empty">加载中…</div></div>`;
  $("#a-create").onclick = createAgent;
  try {
    const r = await api("GET", "/agents");
    const list = $("#a-list");
    const agents = (r.data?.agents || []).slice()
      .sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh-Hans-CN"));
    if (!agents.length) { list.innerHTML = `<div class="empty">还没有 agent，先在上方创建。</div>`; return; }
    list.innerHTML = agents.map((a) => `
      <div class="item">
        <div class="row">
          <div><span class="title">${esc(a.name)}</span> <span class="meta">(${esc(a.agent_id)})</span></div>
          <div>${badge("tier-" + a.tier, a.tier)} ${badge("status-" + a.status, a.status)}</div>
        </div>
        <div class="desc">创建：${fmtTime(a.created_at)} ｜ 最近连接：${fmtTime(a.last_seen)}</div>
        ${badge("mode-" + (a.mode || "both"), modeLabel(a.mode || "both"))}
        ${a.notes ? `<div class="desc">备注：${esc(a.notes)}</div>` : ""}
        <div class="actions">
          <button class="btn sm" data-edit="${esc(a.agent_id)}">编辑</button>
          <button class="btn sm" data-regen="${esc(a.agent_id)}">重置身份码</button>
          <button class="btn sm danger" data-revoke="${esc(a.agent_id)}">吊销</button>
          <button class="btn sm danger" data-del="${esc(a.agent_id)}">删除</button>
        </div>
      </div>`).join("");
    $$("[data-edit]").forEach((b) => (b.onclick = () => editAgent(b.dataset.edit)));
    $$("[data-regen]").forEach((b) => (b.onclick = () => regenAgent(b.dataset.regen)));
    $$("[data-revoke]").forEach((b) => (b.onclick = () => revokeAgent(b.dataset.revoke)));
    $$("[data-del]").forEach((b) => (b.onclick = () => deleteAgent(b.dataset.del)));
  } catch (e) { $("#a-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
async function createAgent() {
  const name = $("#a-name").value.trim();
  if (!name) return toast("请填名称");
  const r = await api("POST", "/agents", { name, tier: $("#a-tier").value, mode: $("#a-mode").value, notes: $("#a-notes").value });
  if (!r.ok) return toast("创建失败：" + (r.data?.error || r.status));
  const a = r.data.agent;
  const ep = endpointForMode(a.mode);
  const fullUrl = `${location.protocol}//${location.host}${ep}`;
  modal("身份识别码已生成（仅此一次）",
    `<p>请复制下方身份码，填入 <b>${esc(a.name)}</b> 的 MCP 配置：</p>
     <div class="code-box">${esc(a.identity_code)}</div>
     <p class="desc">MCP 服务器地址：<code>${esc(fullUrl)}</code><br>请求头：<code>Authorization: Bearer ${esc(a.identity_code)}</code></p>`);
  loadAgents();
}
async function regenAgent(id) {
  if (!confirm("重置后旧身份码立即失效，确定？")) return;
  const r = await api("POST", `/agents/${id}/regen`);
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  modal("新身份识别码（仅此一次）", `<div class="code-box">${esc(r.data.identity_code)}</div>`);
  loadAgents();
}
async function revokeAgent(id) {
  if (!confirm("吊销后该 agent 无法再连接网关（行仍保留，可恢复），确定？")) return;
  const r = await api("POST", `/agents/${id}/revoke`);
  toast(r.ok ? "已吊销" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadAgents();
}
async function deleteAgent(id) {
  if (!confirm("⚠️ 真删除：该 agent 将从身份库彻底移除（含身份码哈希），不可恢复。确定？")) return;
  const r = await api("DELETE", `/agents/${id}`);
  toast(r.ok ? "已彻底删除" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadAgents();
}
async function editAgent(id) {
  const r = await api("GET", "/agents");
  const a = (r.data?.agents || []).find((x) => x.agent_id === id);
  if (!a) return toast("找不到该 agent");
  const tiers = ["staging", "prod", "sandbox"];
  modal("编辑 Agent", `
    <p class="desc">身份码不可改（需重置请点卡片上「重置身份码」）。身份模式（黑/白/双箱）用下方下拉框设置，无需再写 notes 魔法串。</p>
    <div class="field"><label>名称</label><input id="e-name" value="${esc(a.name)}"></div>
    <div class="field"><label>权限级别</label>
      <select id="e-tier">${tiers.map((t) => `<option value="${t}" ${t === a.tier ? "selected" : ""}>${t}</option>`).join("")}</select></div>
    <div class="field"><label>身份模式</label>
      <select id="e-mode">${MODES.map((m) => `<option value="${m}" ${m === (a.mode || "both") ? "selected" : ""}>${modeLabel(m)}</option>`).join("")}</select></div>
    <div class="field"><label>状态</label>
      <select id="e-status"><option value="active" ${a.status === "active" ? "selected" : ""}>active</option><option value="revoked" ${a.status === "revoked" ? "selected" : ""}>revoked</option></select></div>
    <div class="field"><label>备注（其他说明）</label><textarea id="e-notes" placeholder="可选">${esc(a.notes || "")}</textarea></div>
    <div class="row" style="gap:8px;margin-top:12px">
      <button class="btn primary" id="e-save">保存</button>
      <button class="btn" id="e-cancel">取消</button>
    </div>`);
  $("#e-cancel").onclick = closeModal;
  $("#e-save").onclick = () => saveAgent(id);
}
async function saveAgent(id) {
  const r = await api("PUT", `/agents/${id}`, {
    name: $("#e-name").value.trim(),
    tier: $("#e-tier").value,
    mode: $("#e-mode").value,
    status: $("#e-status").value,
    notes: $("#e-notes").value,
  });
  if (!r.ok) return toast("保存失败：" + (r.data?.error || r.status));
  toast("已保存");
  closeModal();
  loadAgents();
}



// ── 场景提案（部署候选） ──
// 注：经验沉淀(P5)已推迟——DSL 是自顶向下编码经验，与自底向上提取 skill 方向相反。
//       本页面只处理 agent 提交的 DSL 部署候选：raw → 确认闸 → NR。

let _allProposals = [];  // 全量数据，供搜索过滤

function _renderProposals(items) {
  const list = $("#p-list");
  if (!items.length) { list.innerHTML = `<div class="empty">没有匹配的提案。</div>`; return; }
  list.innerHTML = items.map((p) => {
      // DSL 提案：content 里存的是 {dsl, gate, node_count} JSON
      // raw_flow 提案：content 里存的是 {type:"raw_flow", flow, node_count, blocking_rules, lint_*, logic}
      let dslMeta = null;
      try { dslMeta = JSON.parse(p.content || "{}"); } catch(e) {}
      const isDsl = !!dslMeta?.dsl;
      const isRaw = dslMeta?.type === "raw_flow";
      const isSubflow = p.kind === "subflow" || dslMeta?.type === "subflow";
      const dslText = isDsl ? dslMeta.dsl : "";
      const gate = isDsl ? (dslMeta.gate || {}) : (p.gate_result || {});
      const gatePassed = gate.passed === true;
      const nodeCount = isDsl ? dslMeta.node_count : (isRaw ? dslMeta.node_count : null);
      const kindBadge = isRaw ? "raw" : (isDsl ? "dsl" : p.kind);
      // 注意：已部署的提案也允许重新部署（用户可能已手动在 NR 删掉 tab，需要自愈入口）
      // 子流程提案：人审通过后「注册」（写 NR 子流程 + 登记注册表），同样需部署按钮
      const canDeploy = p.status !== "rejected" && (dslText || isRaw || isSubflow);
      // P4-B：来源徽章 + 策略感知（需审/可信，由后端按 deploy_policy+source 算好）
      const srcBadgeCls = p.source === "compiler" ? "ok"
                        : (p.source === "raw" ? "st-candidate" : "kind-idea");
      const srcBadgeTxt = p.source === "compiler" ? "编译产物·可信"
                         : (p.source === "raw" ? "手写·需审" : "未知来源");
      const reviewBadgeCls = p.requires_review ? "st-candidate" : "ok";
      const reviewBadgeTxt = p.requires_review ? "需人工审核" : "可信·可自动部署";
      let rawMeta = "";
      if (isRaw) {
        const le = dslMeta.lint_error_count || 0, lw = dslMeta.lint_warning_count || 0;
        const br = (dslMeta.blocking_rules || []).join(",");
        const logicBad = (dslMeta.logic?.unreachable_actions || []).length > 0;
        rawMeta = `
          <div class="desc">原生 flow：${nodeCount ?? "?"} 节点 ｜ 静态检查 ${le} error / ${lw} warning
            ${br ? ` ｜ <span style="color:#c0392b">硬伤(${esc(br)})</span>` : ""}
            ${logicBad ? ` ｜ <span style="color:#c0392b">逻辑不可达</span>` : ""}</div>
          <div class="desc">部署前需人工审核此 flow（不自动落 NR）。</div>`;
      }
      let subflowMeta = "";
      if (isSubflow) {
        const sn = dslMeta?.dsl_name || dslMeta?.name || "(未命名)";
        const sfId = dslMeta?.definition_id || "";
        const sfNc = dslMeta?.node_count ?? "?";
        subflowMeta = `
          <div class="desc">子流程提案：DSL 调用名 <b>${esc(sn)}</b> ｜ NR 子流程 id <code>${esc(sfId || "?")}</code> ｜ ${esc(String(sfNc))} 节点</div>
          <div class="desc">人类点「注册子流程」后：写 NR 子流程实例 + 登记子流程注册表，agent 即可经 MCP 调用。</div>`;
      }
      return `
      <div class="item">
        <div class="row">
          <div><span class="title">${esc(p.title)}</span> ${p.id ? `<span class="meta">(${esc(p.id.slice(0,12))}…)</span>` : ""}</div>
          <div>${badge("kind-" + kindBadge, kindBadge)} ${badge("st-" + p.status, p.status)}
            ${gatePassed ? badge("ok", "闸门 PASS") : (gate.passed === false ? badge("danger", "闸门 FAIL") : "")}
            ${badge(srcBadgeCls, srcBadgeTxt)} ${badge(reviewBadgeCls, reviewBadgeTxt)}
            ${p.archived_at ? badge("kind-idea", "已归档") : ""}
          </div>
        </div>
        <div class="desc">来源：${esc(p.agent_id)} ｜ ${fmtTime(p.created_at)}</div>
        ${!isDsl && p.spec ? `<div class="desc">规格：${esc(p.spec)}</div>` : ""}
        ${dslText ? `<pre class="code-box" style="font-size:12px;max-height:120px;overflow:auto">${esc(dslText)}</pre>` : (!isSubflow && !isDsl && !isRaw ? `<div class="desc">${esc(p.content)}</div>` : "")}
        ${isRaw ? rawMeta : ""}${isSubflow ? subflowMeta : ""}
        ${gate.assertions?.length ? `
        <div style="margin:6px 0;font-size:12px">
          <b>闸门断言</b>：
          ${gate.assertions.map((a) => `<span style="margin-right:8px">${a.ok ? "✅" : "❌"} ${esc(a.entity_id || a.check)}=${esc(a.expected)}</span>`).join("")}
        </div>` : ""}
        ${nodeCount && isDsl ? `<div class="desc">编译产物：${nodeCount} 节点 ｜ 无 Function：✅</div>` : ""}
        <div class="actions">
          ${canDeploy ? `<button class="btn sm primary" data-dep="${esc(p.id)}">${isSubflow ? "注册子流程" : (p.deployed_flow_id ? "重新部署到 NR" : "部署到 NR")}</button>` : ""}
          ${p.status !== "rejected" ? `<button class="btn sm danger" data-prej="${esc(p.id)}">拒绝</button>` : ""}
          <button class="btn sm" data-del="${esc(p.id)}">删除</button>
          ${p.archived_at ? `<button class="btn sm" data-unarch="${esc(p.id)}">取消归档</button>` : `<button class="btn sm" data-arch="${esc(p.id)}">归档</button>`}
          ${p.deployed_flow_id && !isSubflow ? `<button class="btn sm danger" data-undep="${esc(p.deployed_flow_id)}">撤回</button>
            <span class="badge ok" style="margin-left:4px">已部署: ${esc(p.deployed_flow_id.slice(0,12))}…</span>` : ""}
          ${p.deployed_flow_id && isSubflow ? `<span class="badge ok" style="margin-left:4px">已注册: ${esc(p.deployed_flow_id.slice(0,12))}…</span>` : ""}
        </div>
      </div>`;
    }).join("");
    $$("[data-dep]").forEach((b) => (b.onclick = () => deployProposal(b.dataset.dep)));
    $$("[data-prej]").forEach((b) => (b.onclick = () => rejectProposal(b.dataset.prej)));
    $$("[data-del]").forEach((b) => (b.onclick = () => deleteProposal(b.dataset.del)));
    $$("[data-undep]").forEach((b) => (b.onclick = () => undeployProposal(b.dataset.undep)));
    $$("[data-arch]").forEach((b) => (b.onclick = () => archiveProposal(b.dataset.arch)));
    $$("[data-unarch]").forEach((b) => (b.onclick = () => unarchiveProposal(b.dataset.unarch)));
}

function _filterProposals(q) {
  const t = (q || "").trim().toLowerCase();
  if (!t) return _renderProposals(_allProposals);
  const filtered = _allProposals.filter((p) => {
    const title = (p.title || "").toLowerCase();
    const id = (p.id || "").toLowerCase();
    let dslMeta = null;
    try { dslMeta = JSON.parse(p.content || "{}"); } catch(e) {}
    const dsl = (dslMeta?.dsl || "").toLowerCase();
    return title.includes(t) || id.includes(t) || dsl.includes(t);
  });
  _renderProposals(filtered);
  // 更新计数
  const cnt = $("#p-filter-count");
  if (cnt) cnt.textContent = `${filtered.length} / ${_allProposals.length}`;
}

// 提案分页状态（W2-5b 服务端分页：按页拉取，避免一次性加载上千条撑爆事件循环）
let _propOffset = 0;
let _propLimit = 100;
let _propIncludeArchived = false;
let _propTotal = 0;

async function loadProposals() {
  const v = $("#view-proposals");
  v.innerHTML = `<div class="view-head"><h2>场景提案</h2><span class="sub">agent 提交的 DSL 场景，经闸门验证后可部署到 Node-RED</span></div>
    <div class="search-bar" style="margin:10px 0;display:flex;gap:8px;align-items:center">
      <input id="p-search" type="text" placeholder="🔍 搜索当前页提案（标题 / ID / DSL 内容）…" style="flex:1;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--bg);color:var(--text)">
      <span id="p-filter-count" style="font-size:12px;color:var(--text-dim);white-space:nowrap"></span>
    </div>
    <div id="p-pager" class="pager"></div>
    <div id="p-list" style="margin-top:14px"><div class="empty">加载中…</div></div>`;
  // 绑定搜索（仅绑定一次，翻页不重复绑定）
  const sb = $("#p-search");
  if (sb) {
    sb.addEventListener("input", () => _filterProposals(sb.value));
    sb.addEventListener("keydown", (e) => { if (e.key === "Enter") _filterProposals(sb.value); });
  }
  await _loadProposalPage();
}

async function _loadProposalPage() {
  // 归档/删除后总数可能减少，当前 offset 越界则回退到最后一页
  if (_propTotal && _propOffset >= _propTotal) {
    _propOffset = Math.max(0, Math.floor((_propTotal - 1) / _propLimit) * _propLimit);
  }
  try {
    const inc = _propIncludeArchived ? 1 : 0;
    const r = await api("GET", `/proposals?limit=${_propLimit}&offset=${_propOffset}&include_archived=${inc}`);
    _allProposals = r.data?.proposals || [];
    _propTotal = r.data?.total ?? _allProposals.length;
    _renderProposalPager();
    _filterProposals("");
  } catch (e) { $("#p-list").innerHTML = errBox(e.message || "加载失败", loadProposals); }
}

function _renderProposalPager() {
  const el = $("#p-pager");
  if (!el) return;
  const total = _propTotal, limit = _propLimit;
  const pages = Math.max(1, Math.ceil(total / limit));
  const cur = Math.floor(_propOffset / limit) + 1;
  el.innerHTML = `
    <button class="btn sm" id="p-prev" ${_propOffset <= 0 ? "disabled" : ""}>‹ 上一页</button>
    <span style="font-size:12px;color:var(--text-dim)">第 ${cur} / ${pages} 页 ｜ 共 ${total} 条</span>
    <button class="btn sm" id="p-next" ${cur >= pages ? "disabled" : ""}>下一页 ›</button>
    <span style="font-size:12px;color:var(--text-dim)">跳到</span>
    <input id="p-jump" type="number" min="1" max="${pages}" value="${cur}" style="width:64px;padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)">
    <button class="btn sm" id="p-goto">跳转</button>
    <label style="font-size:12px;color:var(--text-dim);display:flex;align-items:center;gap:4px;margin-left:8px;cursor:pointer">
      <input type="checkbox" id="p-arch" ${_propIncludeArchived ? "checked" : ""}> 显示已归档
    </label>`;
  const prev = $("#p-prev"), next = $("#p-next"), goto = $("#p-goto"), jump = $("#p-jump"), arch = $("#p-arch");
  if (prev) prev.onclick = () => { if (_propOffset >= limit) { _propOffset -= limit; _loadProposalPage(); } };
  if (next) next.onclick = () => { if (_propOffset + limit < total) { _propOffset += limit; _loadProposalPage(); } };
  if (goto) goto.onclick = () => {
    let n = parseInt(jump.value, 10) || 1;
    n = Math.min(Math.max(1, n), pages);
    _propOffset = (n - 1) * limit;
    _loadProposalPage();
  };
  if (arch) arch.onclick = () => { _propIncludeArchived = arch.checked; _propOffset = 0; _loadProposalPage(); };
}
async function rejectProposal(id) {
  const reason = prompt("拒绝理由（可选）：") || "";
  const r = await api("POST", `/proposals/${id}/reject`, { reason });
  toast(r.ok ? "已拒绝" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadProposals();
}
async function deleteProposal(id) {
  if (!confirm("删除该提案？（不可恢复；若已部署到 NR，对应 flow 不会被自动撤回，需到「已部署」面板手动撤回）")) return;
  const r = await api("DELETE", `/proposals/${id}/delete`);
  toast(r.ok ? "已删除" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadProposals();
}
async function archiveProposal(id) {
  if (!confirm("归档该提案？（退休语义：默认从活跃列表隐藏，仍可经「显示已归档」查看与恢复）")) return;
  const r = await api("POST", `/proposals/${id}/archive`);
  toast(r.ok ? "已归档" : "失败：" + (r.data?.error || r.status));
  if (r.ok) _loadProposalPage();
}
async function unarchiveProposal(id) {
  const r = await api("POST", `/proposals/${id}/unarchive`);
  toast(r.ok ? "已取消归档" : "失败：" + (r.data?.error || r.status));
  if (r.ok) _loadProposalPage();
}
async function deployProposal(id) {
  const p = _allProposals.find((x) => x.id === id);
  let isSub = false;
  try { isSub = !!(p && (p.kind === "subflow" || JSON.parse(p.content || "{}").type === "subflow")); } catch (e) {}
  const msg = isSub
    ? "确定注册该子流程到网关？\n（写 NR 子流程实例 + 登记子流程注册表，注册后 agent 可经 MCP 调用。冲突或失败不会动 NR。）"
    : "确定部署该场景到 Node-RED？部署后可在「已部署」面板随时安全撤回。";
  if (!confirm(msg)) return;
  const r = await api("POST", `/proposals/${id}/deploy`, { target: "prod" });
  if (!r.ok) {
    if (r.data?.conflict) return toast("冲突：" + (r.data.error || "同名子流程已存在，可改名或 force 重建"));
    // 安全闸 / staging 闸门拦截：常驻对话框，必须点「确定」才关闭（不自动消失）
    const errText = String(r.data?.error || r.status || "未知错误");
    const isGate = r.data?.stage === "gate" || /闸门|受保护对象|安全闸/.test(errText);
    if (isGate) {
      const ent = r.data?.protected_entity
        ? `<p style="color:#c0392b">受保护实体：${esc(r.data.protected_entity)}</p>` : "";
      const detail = r.data?.gate
        ? `<pre style="white-space:pre-wrap;max-height:240px;overflow:auto;background:#f6f6f6;padding:8px;border-radius:6px">${esc(JSON.stringify(r.data.gate, null, 2))}</pre>`
        : "";
      modal("安全闸 / staging 闸门拦截",
        `<p style="line-height:1.7">${esc(errText)}</p>` +
        detail + ent +
        `<p style="color:#888">未部署。请检查 DSL 与预期后置条件是否一致，或调整安全闸规则后重试。</p>` +
        `<div style="margin-top:14px;text-align:right"><button class="btn" onclick="closeModal()">确定</button></div>`);
      return;
    }
    return toast((isSub ? "注册失败：" : "部署失败：") + errText);
  }
  toast((isSub ? "注册成功：" : "部署成功：") + (r.data.label || r.data.flow_id || r.data.subflow_id));
  loadProposals();
  if (!isSub) loadDeployed();
}

// ── 已部署 ──
async function loadDeployed() {
  const v = $("#view-deployed");
  v.innerHTML = `<div class="view-head"><h2>已部署</h2><span class="sub">本网关部署到 Node-RED 的 flow，可随时安全撤回</span></div>
    <div id="d-list"><div class="empty">加载中…</div></div>`;
  try {
    const r = await api("GET", "/deployed");
    const list = $("#d-list");
    const items = r.data?.deployed || [];
    if (!items.length) { list.innerHTML = `<div class="empty">还没有通过网关部署的 flow。</div>`; return; }
    list.innerHTML = items.map((d) => `
      <div class="item${d.stale ? " stale" : ""}">
        <div class="row">
          <div><span class="title">${esc(d.label)}</span> <span class="meta">(${esc((d.flow_id||'').slice(0,12))}…)</span></div>
          <div>${d.stale ? badge("stale", "注册表漂移") : badge("ok", "已部署")}</div>
        </div>
        <div class="desc">来源 agent：${esc(d.owner_agent)} ｜ 节点数：${esc(d.node_count ?? "?")} ｜ ${fmtTime(d.deployed_at)}</div>
        ${d.server_resolved === false ? `<div class="desc" style="color:#c0392b">⚠ 触发器未绑定 HA server（部署时未能解析到），flow 不会自动触发，需在 NR 中手动绑定 server。</div>` : ""}
        ${d.stale ? `<div class="desc" style="color:#c0392b">⚠ 注册表↔NR 分叉：注册表记此 flow 已部署，但 Node-RED 实例里已无该 flow_id（可能已被手动删除、重命名或切换了 NR 实例）。撤回将仅清理注册表记录，不会触碰 NR。</div>` : ""}
        <div class="actions">
          <button class="btn sm" data-trg="${esc(d.flow_id)}">▶ 触发</button>
          <button class="btn sm danger" data-und="${esc(d.flow_id)}">撤回</button>
        </div>
      </div>`).join("");
    $$("[data-und]").forEach((b) => (b.onclick = () => undeployProposal(b.dataset.und)));
    $$("[data-trg]").forEach((b) => (b.onclick = () => triggerFlow(b.dataset.trg)));
  } catch (e) { $("#d-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
async function undeployProposal(id) {
  if (!confirm("确定撤回该网关部署？\n若你在此 tab 中另有自己的节点，将被保留，仅移除网关写入的节点。")) return;
  let r = await api("POST", `/deployed/${id}/undeploy`, {});
  if (!r.ok && r.data?.code === "nr_unreachable") {
    if (confirm("NR 当前不可达，无法确认 flow 状态。\n若你确认已在 NR 手动删除该 flow，是否只清理网关注册表？")) {
      r = await api("POST", `/deployed/${id}/undeploy`, { force: true });
    }
  }
  if (r.ok) {
    const d = r.data || {};
    if (d.action === "trimmed_tab") {
      toast(`已移除网关节点 ${d.gateway_nodes_removed} 个，保留你的 ${d.user_nodes_preserved} 个节点`);
    } else if (d.action === "deleted_tab") {
      toast(`已撤回并删除 tab「${d.label || id}」`);
    } else {
      toast("已撤回：" + (d.label || id));
    }
    loadDeployed();
    loadProposals();  // 提案面板可能也在展示该 flow 的「撤回」入口，同步刷新
    return;
  }
  toast("撤回失败：" + (r.data?.error || r.status));
}

async function triggerFlow(id) {
  const r = await api("POST", `/flows/${id}/trigger`, {});
  if (!r.ok) { toast("触发失败：" + (r.data?.error || r.status)); return; }
  const d = r.data || {};
  const n = (d.triggered || []).length;
  const errs = (d.errors || []).length;
  if (d.warning) { toast("已触发，但 " + d.warning); return; }
  toast(`已触发 ${n} 个 inject${errs ? `，失败 ${errs}` : ""}。可到「诊断」页做 debug 回看`);
}

// ── 笔记 ──
let noteSearch = "", noteTag = "";
async function loadNotes() {
  const v = $("#view-notes");
  v.innerHTML = `<div class="view-head"><h2>笔记</h2><span class="sub">关于智能家居系统的想法（未必马上落地）</span></div>
    <div class="card form-card">
      <h3>新建笔记</h3>
      <div class="field"><label>标题</label><input id="n-title" placeholder="想法标题"></div>
      <div class="field"><label>内容</label><textarea id="n-body" placeholder="记录你的想法…"></textarea></div>
      <div class="field"><label>标签（逗号分隔）</label><input id="n-tags" placeholder="照明, 安全"></div>
      <button class="btn primary" id="n-create">保存</button>
    </div>
    <div class="field" style="max-width:560px;margin-top:14px">
      <input id="n-search" placeholder="搜索标题/内容…" value="${esc(noteSearch)}">
    </div>
    <div id="n-list"><div class="empty">加载中…</div></div>`;
  $("#n-create").onclick = createNote;
  $("#n-search").oninput = (e) => { noteSearch = e.target.value; loadNotesList(); };
  loadNotesList();
}
async function loadNotesList() {
  const list = $("#n-list");
  if (!list) return;
  try {
    const q = noteSearch ? `?q=${encodeURIComponent(noteSearch)}` : (noteTag ? `?tag=${encodeURIComponent(noteTag)}` : "");
    const r = await api("GET", "/notes" + q);
    const notes = r.data?.notes || [];
    if (!notes.length) { list.innerHTML = `<div class="empty">还没有笔记。</div>`; return; }
    list.innerHTML = notes.map((n) => `
      <div class="item">
        <div class="row">
          <div><span class="title">${esc(n.title || "(无标题)")}</span></div>
          <div class="meta">${fmtTime(n.updated_at)}</div>
        </div>
        <div class="desc">${esc(n.body)}</div>
        ${n.tags?.length ? `<div>${n.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
        <div class="actions">
          <button class="btn sm" data-edit="${esc(n.id)}">编辑</button>
          <button class="btn sm danger" data-del="${esc(n.id)}">删除</button>
        </div>
      </div>`).join("");
    $$("[data-edit]").forEach((b) => (b.onclick = () => editNote(b.dataset.edit)));
    $$("[data-del]").forEach((b) => (b.onclick = () => deleteNote(b.dataset.del)));
  } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
async function createNote() {
  const r = await api("POST", "/notes", {
    title: $("#n-title").value, body: $("#n-body").value,
    tags: $("#n-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
  });
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  toast("已保存");
  loadNotes();
}
async function editNote(id) {
  const r = await api("GET", "/notes");
  const n = (r.data?.notes || []).find((x) => x.id === id);
  if (!n) return;
  modal("编辑笔记",
    `<div class="field"><label>标题</label><input id="m-title" value="${esc(n.title)}"></div>
     <div class="field"><label>内容</label><textarea id="m-body">${esc(n.body)}</textarea></div>
     <div class="field"><label>标签</label><input id="m-tags" value="${esc((n.tags || []).join(", "))}"></div>
     <button class="btn primary" id="m-save">保存</button>`);
  $("#m-save").onclick = async () => {
    const ur = await api("PUT", `/notes/${id}`, {
      title: $("#m-title").value, body: $("#m-body").value,
      tags: $("#m-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
    });
    if (ur.ok) { closeModal(); toast("已更新"); loadNotesList(); }
    else toast("失败：" + (ur.data?.error || ur.status));
  };
}
async function deleteNote(id) {
  if (!confirm("删除该笔记？")) return;
  const r = await api("DELETE", `/notes/${id}`);
  toast(r.ok ? "已删除" : "失败");
  if (r.ok) loadNotesList();
}

// ── 版本同步（dev -> prod）──
async function loadSync() {
  const v = $("#view-sync");
  v.innerHTML = `
    <div class="view-head">
      <div>
        <h2>🔄 版本同步</h2>
        <span class="sub">dev → prod，按 stage 标签驱动</span>
      </div>
      <div class="sync-actions">
        <button class="btn sm" id="sync-preview">预览推送</button>
        <button class="btn sm" id="sync-refresh">刷新</button>
        <button class="btn sm ok" id="sync-push-release">推送 release → 1880</button>
      </div>
    </div>
    <div class="sync-legend">
      <span class="badge stage-release" style="background:var(--ok-weak);color:var(--ok)">release</span> 每日 02:00 自动推送 + 可手动推送
      <span class="badge stage-dev" style="background:var(--primary-weak);color:var(--primary)">dev</span> 仅留开发，绝不推送
      <span class="badge stage-agent" style="background:var(--warn-weak);color:var(--warn)">agent</span> 练手版，绝不推送
    </div>
    <div id="sync-list"><div class="empty">加载中…</div></div>
    <style>
      .sync-actions { display:flex; gap:6px; flex-wrap:wrap; }
      .sync-legend { margin: 10px 0 14px; font-size: 13px; color: var(--text-dim); display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
      .sync-table { width:100%; border-collapse: collapse; background: var(--surface); border-radius:10px; overflow:hidden; box-shadow: 0 1px 4px rgba(0,0,0,.05); }
      .sync-table th, .sync-table td { padding: 10px 12px; text-align:left; border-bottom: 1px solid var(--border, #eee); font-size: 14px; vertical-align: middle; }
      .sync-table th { background: var(--surface-2); font-weight: 600; font-size: 13px; color: var(--text-dim); }
      .sync-table tr:last-child td { border-bottom: none; }
      .sync-table .mono { font-family: monospace; }
      .sync-table .actions { display:flex; gap:4px; flex-wrap:wrap; white-space:nowrap; }
      .sync-table .title { font-weight: 600; }
      .sync-table .meta { color: var(--text-dim); font-size: 12px; }
    </style>`;
  $("#sync-refresh").onclick = loadSync;
  $("#sync-preview").onclick = previewPush;
  $("#sync-push-release").onclick = pushReleaseManual;
  await renderSyncList();
}

async function renderSyncList() {
  const list = $("#sync-list");
  if (!list) return;
  try {
    const r = await api("GET", "/sync/scan");
    const flows = r.data?.flows || [];
    if (!flows.length) { list.innerHTML = `<div class="empty">NR 中没有 tab。</div>`; return; }
    list.innerHTML = `
      <table class="sync-table">
        <thead><tr>
          <th>Flow</th><th>Stage</th><th>版本</th><th>将推送</th><th>操作</th>
        </tr></thead>
        <tbody>
          ${flows.map((f) => `
            <tr data-id="${esc(f.id)}">
              <td><span class="title">${esc(f.label)}</span><div class="meta">${(esc(f.id)||"").slice(0,12)}…</div></td>
              <td>${stageBadge(f.stage)}</td>
              <td class="mono">${esc(f.version)}</td>
              <td>${f.would_push ? badge("ok","是") : badge("","否")}</td>
              <td class="actions">
                <button class="btn sm" data-set="release">标 release</button>
                <button class="btn sm" data-set="dev">标 dev</button>
                <button class="btn sm" data-set="agent">标 agent</button>
              </td>
            </tr>`).join("")}
        </tbody>
      </table>`;
    $$("#sync-list [data-set]").forEach((b) =>
      (b.onclick = () => setStageFor(b.closest("tr").dataset.id, b.dataset.set)));
  } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function stageBadge(stage) {
  const map = {
    release: { c: "var(--ok)", bg: "var(--ok-weak)" },
    dev: { c: "var(--primary)", bg: "var(--primary-weak)" },
    agent: { c: "var(--warn)", bg: "var(--warn-weak)" },
  };
  const s = map[stage] || { c: "var(--text-dim)", bg: "var(--surface-2)" };
  return `<span class="badge" style="background:${s.bg};color:${s.c}">${esc(stage)}</span>`;
}

async function setStageFor(id, stage) {
  const ver = prompt(`设置 stage=${stage}。版本号（留空沿用现有，缺省 1.0.0）：`, "");
  const body = { id, stage };
  if (ver && ver.trim()) body.version = ver.trim();
  const r = await api("POST", "/sync/set-stage", body);
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  toast(`已将 ${r.data.label || id} 标为 ${stage} ${r.data.version || ""}`);
  loadSync();
}

async function previewPush() {
  const r = await api("POST", "/sync/push-release", { dry_run: true });
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  const pushed = r.data?.pushed || [];
  if (!pushed.length) return modal("预览推送", `<div class="empty">${esc(r.data?.message || "没有需要推送的 release flow")}</div>`);
  modal("预览推送（dry-run）",
    `<div class="desc" style="margin-bottom:10px">以下 ${pushed.length} 个 flow 将被推送并自动启用：</div>` +
    pushed.map((p) => `<div class="item"><div class="desc">🔸 ${esc(p.label || p.id)} <span class="mono">(v${esc(p.version || "")})</span></div></div>`).join(""));
}

async function pushReleaseManual() {
  if (!confirm("确认将 NR 中所有 release 且版本号大于已推送版本的 flow 推送到 prod？\n（dev/agent 不会推送；推送后 flow 在 prod 自动启用）")) return;
  const r = await api("POST", "/sync/push-release", { dry_run: false });
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  const pushed = r.data?.pushed || [];
  const okList = pushed.filter((p) => p.ok);
  if (!okList.length) return toast(r.data?.message || "没有需要推送的 flow");
  modal("推送结果",
    `<div style="margin-bottom:10px">${esc(r.data?.message || "")}</div>` +
    okList.map((p) => `<div class="item"><div class="desc">✅ ${esc(p.label || p.id)} <span class="mono">(v${esc(p.version || "")})</span> → 已启用推送</div></div>`).join("") +
    pushed.filter((p) => !p.ok).map((p) => `<div class="item"><div class="desc" style="color:var(--danger)">❌ ${esc(p.label || p.id)}: ${esc(p.error || "")}</div></div>`).join(""));
  loadSync();
}

// （C2-fix: 清 Agent Lab 残留注释）
const LAB_AGENTS = [
  { id: "deepseek++", icon: "🤖", color: "#4a90d9" },
  { id: "trae", icon: "🎨", color: "#8b5cf6" },
  { id: "solo", icon: "🔧", color: "#059669" },
  { id: "hand", icon: "✋", color: "#d97706" },
];
let labActiveAgent = LAB_AGENTS[0].id;
// 每个 agent tab 记住上次部署的 flow id，实现「同一 flow 迭代更新」而非每次新建
let labFlowIds = {};

function renderLab() {
  const v = $("#view-lab");
  const agent = LAB_AGENTS.find(a => a.id === labActiveAgent) || LAB_AGENTS[0];

  const tabs = LAB_AGENTS.map(a =>
    `<button class="lab-tab ${a.id === labActiveAgent ? 'active' : ''}" data-agent="${a.id}"
             style="--ac:${a.color}">${a.icon} ${a.id}</button>`
  ).join("");

  v.innerHTML = `
    <div class="lab-header">
      <h2>🧪 白盒部署</h2>
      <p class="desc">投喂提示词给各 Agent → 粘贴产出 flow JSON → 校验/部署到 NR。收集失败模式反哺编译器。</p>
    </div>
    <div class="lab-tabs">${tabs}</div>
    <div class="lab-workspace">
      <div class="lab-panel lab-prompt">
        <h3>📋 提示词</h3>
        <p class="hint">从 <code>docs/test_prompts.md</code> 复制 L1-L7 提示词，粘贴到下方后发给 ${agent.id}。</p>
        <textarea id="labPrompt" placeholder="在此粘贴提示词（供参考，不影响部署）..."></textarea>
        <div class="lab-actions">
          <button class="btn primary" id="labValidate">仅校验（dry-run）</button>
          <button class="btn" id="labDeploy">部署到 NR</button>
        </div>
      </div>
      <div class="lab-panel lab-flow">
        <h3>📦 Flow JSON <span class="badge">${agent.id}</span></h3>
        <p class="hint">粘贴 Agent 产出的 nodes 数组或完整 flow JSON。支持纯 nodes 数组或含 id/label 的完整 flow。</p>
        <textarea id="labFlowJson" placeholder='["id":"n1","type":"inject",...] 或 {"id":"flow1","label":"...","nodes":[...]}'></textarea>
      </div>
    </div>
    <div class="lab-result" id="labResult" hidden></div>
    <div class="lab-history" id="labHistory"><h3>📜 部署历史</h3><div id="labHistoryList">加载中…</div></div>

    <style>
      .lab-header h2 { margin: 0 0 4px; }
      .lab-header .desc { margin: 0 0 16px; opacity: .7; font-size: .9em; }
      .lab-tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
      .lab-tab { padding: 8px 16px; border: 2px solid transparent; border-radius: 8px;
                 background: var(--bg2, #f5f5f5); cursor: pointer; font-size: .9em; transition: .15s; }
      .lab-tab.active { border-color: var(--ac, #4a90d9); background: #fff; box-shadow: 0 1px 6px rgba(0,0,0,.08); }
      .lab-tab:hover:not(.active) { background: #eee; }
      .lab-workspace { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
      @media (max-width: 768px) { .lab-workspace { grid-template-columns: 1fr; } }
      .lab-panel { background: var(--bg2, #f9f9f9); border-radius: 10px; padding: 16px; }
      .lab-panel h3 { margin: 0 0 8px; font-size: 1em; }
      .lab-panel .hint { margin: 0 0 8px; font-size: .82em; opacity: .6; }
      .lab-panel textarea { width: 100%; min-height: 180px; border: 1px solid #ddd; border-radius: 6px;
                           padding: 10px; font-family: monospace; font-size: .85em; resize: vertical;
                           box-sizing: border-box; }
      .lab-actions { display: flex; gap: 8px; margin-top: 10px; }
      .lab-result { margin-top: 16px; padding: 16px; border-radius: 10px; }
      .lab-result.ok { background: #ecfdf5; border: 1px solid #a7f3d0; }
      .lab-result.err { background: #fef2f2; border: 1px solid #fecaca; }
      .lab-history { margin-top: 20px; }
      .lab-history h3 { font-size: 1em; margin-bottom: 8px; }
      .lab-entry { padding: 8px 12px; border-radius: 6px; margin-bottom: 6px; font-size: .85em;
                   display: flex; justify-content: space-between; align-items: center; }
      .lab-entry.ok { background: #f0fdf4; }
      .lab-entry.fail { background: #fef2f2; }
    </style>
  `;

  // Tab switching
  $$(".lab-tab").forEach(btn => {
    btn.onclick = () => { labActiveAgent = btn.dataset.agent; renderLab(); };
  });

  // Buttons
  $("#labValidate").onclick = () => handleLabAction("validate");
  $("#labDeploy").onclick = () => handleLabAction("deploy");

  // Load history
  loadLabHistory();
}

async function handleLabAction(mode) {
  const raw = $("#labFlowJson").value.trim();
  if (!raw) { toast("请先粘贴 Flow JSON"); return; }

  let flowData;
  try {
    const parsed = JSON.parse(raw);
    // Accept both bare nodes array and full flow object
    flowData = Array.isArray(parsed) ? { id: `lab-${Date.now}`, label: `${labActiveAgent}-flow`, nodes: parsed } : parsed;
  } catch (e) {
    toast("JSON 解析失败: " + e.message); return;
  }

  const resultEl = $("#labResult");
  resultEl.hidden = false;

  if (mode === "validate") {
    resultEl.className = "lab-result";
    const r = await api("POST", "/lab/validate", { flow_json: flowData });
    if (r.data?.ok) {
      resultEl.classList.add("ok");
      resultEl.innerHTML = `<strong>✅ 校验通过</strong>${r.data.warnings.length ?
        `<br><small>⚠️ ${r.data.warnings.length} 个警告：` +
        r.data.warnings.map(w => `<br>• [${w.node_id}] ${w.message}`).join("") + `</small>` : ""}` +
        `<br><small>${r.data.node_count} 个节点 | ${r.data.total_issues} 个问题</small>`;
    } else {
      resultEl.classList.add("err");
      resultEl.innerHTML = `<strong>❌ 校验未通过</strong><br>` +
        (r.data.errors || []).map(e => `• [${e.node_id}] ${e.message}<br>`).join("");
    }
  } else {
    // Deploy
    resultEl.className = "lab-result";
    const r = await api("POST", "/lab/deploy", {
      flow_json: flowData,
      agent_id: labActiveAgent,
      label: `${labActiveAgent}-${Date.now()}`,
      target: "staging",
      target_flow_id: labFlowIds[labActiveAgent] || null,
    });

    if (r.data?.ok) {
      labFlowIds[labActiveAgent] = r.data.flow_id;  // 记住 id 供下次迭代更新
      resultEl.classList.add("ok");
      resultEl.innerHTML = `<strong>🚀 部署成功！</strong><br>
        <table class="info-table">
          <tr><td>flow_id</td><td><code>${esc(r.data.flow_id)}</code></td></tr>
          <tr><td>节点数</td><td>${r.data.node_count}</td></tr>
          <tr><td>校验</td><td>${(r.data.validation || []).length} 条</td></tr>
          <tr><td>时间</td><td>${fmtTime(r.data.deployed_at)}</td></tr>
        </table>`;
      toast(`已部署到 NR: ${r.data.flow_id}`);
      setTimeout(loadLabHistory, 500);
    } else {
      resultEl.classList.add("err");
      const errs = r.data.validation || [];
      resultEl.innerHTML = `<strong>❌ 部署失败</strong><br>
        <code>${esc(r.data.error || "未知错误")}</code>` +
        (errs.length ? `<br><br><strong>校验详情：</strong><br>` +
          errs.map(e => `[${e.level}] ${e.node_id}: ${e.message}`).join("<br>") : "");
    }
  }
}

async function loadLabHistory() {
  const list = $("#labHistoryList");
  try {
    const r = await api("GET", "/lab/deploys");
    const entries = r.data?.deploys || [];
    if (!entries.length) { list.innerHTML = "<p class='empty'>暂无部署记录</p>"; return; }
    list.innerHTML = entries.map(e =>
      `<div class="lab-entry ${e.status === 'DEPLOY_OK' ? 'ok' : 'fail'}">
        <span><b>${esc(e.agent)}</b> · ${esc(e.label)} · ${fmtTime(e.ts).split(' ')[1]}</span>
        <span>${e.status === 'DEPLOY_OK' ? badge("ok","✅ "+e.detail) :
          badge("fail","❌ "+e.errors+"err/"+e.warnings+"warn")}</span>
      </div>`
    ).join("");
  } catch { list.innerHTML = "<p class='empty'>加载失败</p>"; }
}

async function loadLab() { renderLab(); }

// ── 人工抽查（合并三家白箱提交 → 单 tab，触发器禁用）──
async function loadSpotcheck() {
  const v = $("#view-spotcheck");
  v.innerHTML = `
    <div class="view-head"><h2>🔎 人工抽查</h2><span class="sub">挑选同一任务的 3 家白箱提交，合并为一份白盒提案（触发器默认禁用，不会自动点火；审核后部署到 NR）</span></div>
    <div class="card form-card">
      <div class="field">
        <label>任务</label>
        <select id="sc-task"><option>加载中…</option></select>
        <span class="meta" id="sc-task-meta"></span>
      </div>
      <div class="field">
        <label>Tab 标签（部署到 NR 的名称）</label>
        <input id="sc-label" placeholder="抽查·wb_xxx">
      </div>
      <div class="actions">
        <button class="btn" id="sc-dry">预览合并（dry-run）</button>
        <button class="btn primary" id="sc-deploy">提交为提案（待审核）</button>
      </div>
    </div>
    <div class="sc-result lab-result" id="scResult" hidden></div>`;
  try {
    const r = await api("GET", "/spotcheck/ready");
    const roster = r.data?.roster || [];
    const tasks = r.data?.tasks || [];
    const sel = $("#sc-task");
    if (!tasks.length) { sel.innerHTML = `<option>没有 wb_* 任务</option>`; }
    else {
      sel.innerHTML = tasks.map((t) => {
        const n = Object.values(t.submissions).filter((s) => s.has_dsl).length;
        const tag = t.complete ? "✅ 三家齐" : `${n}/3`;
        return `<option value="${esc(t.task_id)}">${esc(t.task_id)} · ${esc(t.title || "")} · [${tag}]</option>`;
      }).join("");
    }
    sel.onchange = () => {
      const t = tasks.find((x) => x.task_id === sel.value);
      const meta = $("#sc-task-meta");
      if (t) {
        meta.innerHTML = roster.map((r0) => {
          const s = t.submissions[r0.agent_id];
          return `${esc(r0.name)}: ${s && s.has_dsl ? badge("ok", "有") : badge("", "无")}`;
        }).join("  ");
        $("#sc-label").value = "抽查·" + t.task_id;
      }
    };
    if (tasks[0]) sel.onchange();
  } catch (e) { toast(e.message || "加载失败"); }
  $("#sc-dry").onclick = () => scRun(true);
  $("#sc-deploy").onclick = () => scRun(false);
}

async function scRun(dry) {
  const taskId = $("#sc-task").value;
  if (!taskId || taskId === "加载中…") return toast("请先选择任务");
  if (!dry && !confirm("确定把 3 家提交合并为一份「白盒提案」？\n（待你在「场景提案」面板审核后一键部署到 NR；触发器已禁用，不会自动触发）")) return;
  const r = await api("POST", "/spotcheck", {
    task_id: taskId,
    label: $("#sc-label").value.trim(),
    dry_run: dry,
  });
  const el = $("#scResult");
  el.hidden = false;
  if (!r.ok || !r.data?.ok) {
    el.className = "lab-result err";
    el.innerHTML = `<strong>❌ ${esc(dry ? "预览" : "部署")}失败</strong><br><code>${esc(r.data?.error || "未知错误")}</code>`;
    return;
  }
  const d = r.data;
  el.className = "lab-result ok";
  const rosterRows = (d.roster_info || []).map((ri) =>
    `<tr><td>${esc(ri.name || ri.agent_id)}</td><td>${ri.ok ? badge("ok", "合并 " + (ri.node_count || 0) + " 节点") : badge("danger", ri.reason || "失败")}</td></tr>`
  ).join("");
  const lintE = d.lint_error_count || 0, lintW = d.lint_warning_count || 0;
  const isProposal = !!d.proposal_id;
  el.innerHTML = `
    <strong>✅ ${esc(isProposal ? (dry ? "预览就绪" : "已提交为提案") : (dry ? "预览就绪" : "已部署"))}</strong>
    ${dry ? `<span class="badge">dry-run 不落 NR</span>` : (isProposal ? `<span class="badge">待人工审核后部署</span>` : ``)}
    <table class="info-table">
      <tr><td>tab 标签</td><td>${esc(d.label || "")}</td></tr>
      <tr><td>${isProposal ? "proposal_id" : "flow_id"}</td><td><code>${esc(d.proposal_id || d.flow_id || "")}</code></td></tr>
      <tr><td>合并节点数</td><td>${esc(d.node_count || 0)}</td></tr>
      <tr><td>静态检查</td><td>${lintE} error / ${lintW} warning${d.would_block_on_lint ? ` ｜ <span style="color:#c0392b">真部署将被硬伤规则拦截(${(d.would_block_rules || []).join(",")})</span>` : ""}</td></tr>
    </table>
    <details><summary class="meta">各家合并情况</summary>
      <table class="info-table"><tr><th>Agent</th><th>结果</th></tr>${rosterRows}</table>
    </details>
    ${dry ? `<div class="desc">确认无误后点「提交为提案（待审核）」。</div>` : (isProposal ? `<div class="desc">已生成提案，请在「场景提案」面板审核后一键部署到 NR。</div>` : ``)}`;
  if (isProposal) toast("已提交提案：" + (d.proposal_id || ""));
  else if (!dry) toast("已部署到 NR：" + (d.flow_id || ""));
}

// ── 诊断查看器（P4-C，只读）──
async function loadDiagnostics() {
  const v = $("#view-diagnostics");
  v.innerHTML = `<div class="view-head"><h2>🩺 诊断</h2><span class="sub">网关瞬时健康与最近活动（trace 重启即丢，属正常）</span>
      <button class="btn sm" id="dx-refresh" style="margin-left:auto">刷新</button></div>
    <div id="dx-body"><div class="empty">加载中…</div></div>
    <style>
      .dx-scroll { max-height: 340px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; margin-top: 8px; }
      .dx-trace th, .dx-trace td { padding: 6px 10px; font-size: 12px; text-align: left; border-bottom: 1px solid var(--border); }
      .dx-trace tr:last-child td { border-bottom: none; }
      .dx-ctx { max-width: 380px; word-break: break-all; }
      .dx-ctx code { font-size: 12px; color: var(--text-dim); }
    </style>`;
  $("#dx-refresh").onclick = loadDiagnostics;
  try {
    const r = await api("GET", "/diagnostics");
    const d = r.data || {};
    const c = d.counts || {};
    const byStatus = c.proposals_by_status || {};
    const statusHtml = Object.keys(byStatus).map((k) => `<span class="badge">${esc(k)}: ${byStatus[k]}</span>`).join(" ") || "—";
    const traces = d.traces || [];
    const traceRows = traces.length ? traces.slice().reverse().map((t) => {
      const t0 = t.ts ? new Date(t.ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
      const ctx = {};
      for (const k of Object.keys(t)) { if (!["ts", "trace_id", "stage"].includes(k)) ctx[k] = t[k]; }
      let ctxStr = Object.keys(ctx).length ? JSON.stringify(ctx) : "";
      if (ctxStr.length > 400) ctxStr = ctxStr.slice(0, 400) + "…";
      return `<tr>
        <td class="mono">${esc(t0)}</td>
        <td class="mono">${esc((t.trace_id || "").slice(0, 8))}</td>
        <td><span class="badge">${esc(t.stage || "")}</span></td>
        <td class="dx-ctx"><code>${esc(ctxStr)}</code></td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="empty">暂无 trace（网关重启后缓冲清空，活跃后自会累积）</td></tr>`;
    const jobs = d.golden_jobs || [];
    const jobRows = jobs.length ? jobs.map((j) => {
      const stCls = j.status === "done" ? "ok" : (j.status === "error" ? "danger" : "st-candidate");
      const okCls = j.ok === true ? "ok" : (j.ok === false ? "danger" : "");
      const started = j.started_at ? new Date(j.started_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
      const meta = [j.scenario ? ("场景 " + esc(j.scenario)) : "", j.mode ? esc(j.mode) : "", j.backend ? esc(j.backend) : ""].filter(Boolean).join(" / ");
      return `<tr>
        <td class="mono">${esc((j.job_id || "").slice(0, 10))}</td>
        <td>${badge("kind-idea", j.kind || "?")}</td>
        <td>${esc(meta)}</td>
        <td>${badge(stCls, j.status || "?")} ${j.ok != null ? badge(okCls, j.ok ? "PASS" : "FAIL") : ""}</td>
        <td class="mono">${esc(started)}</td>
        <td>${j.n_events != null ? esc(j.n_events) : "—"}</td>
        <td class="dx-ctx"><code>${esc(j.summary || "")}</code></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7" class="empty">暂无评测任务（golden / 验收）</td></tr>`;
    const body = $("#dx-body");
    body.innerHTML = `
      <div class="grid cols-4">
        <div class="card"><div class="meta">已注册 Agent</div><div class="stat">${c.agents ?? 0}</div></div>
        <div class="card"><div class="meta">待确认操作</div><div class="stat">${c.pending_ops ?? 0}</div></div>
        <div class="card"><div class="meta">已部署 flow</div><div class="stat">${c.deployed_flows ?? 0}</div></div>
        <div class="card"><div class="meta">提案总数</div><div class="stat">${c.proposals_total ?? 0}</div></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>环境 / 健康</h3>
        <div class="desc">环境 <b>${esc(d.env || "?")}</b> ｜ 部署策略 <b>${esc(d.deploy_policy || "review_all")}</b></div>
        <div class="desc">NR: <code>${esc(d.nr_url || "")}</code> ｜ HA: <code>${esc(d.hass_server || "")}</code></div>
        <div class="desc">黑箱 <code>${esc(d.mcp || "")}</code> ｜ 白箱 <code>${esc(d.mcp_white || "")}</code> ｜ 管理 <code>${esc(d.mcp_admin || "")}</code></div>
        <div class="desc" style="margin-top:6px">提案状态分布：${statusHtml} ｜ 已落地部署 ${c.proposals_deployed ?? 0}</div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>最近结构化 trace（${traces.length} 条）</h3>
        <div class="dx-scroll">
          <table class="info-table dx-trace">
            <thead><tr><th>时间</th><th>trace</th><th>stage</th><th>上下文</th></tr></thead>
            <tbody>${traceRows}</tbody>
          </table>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>最近评测任务（golden / 验收）</h3>
        <div class="dx-scroll">
          <table class="info-table">
            <thead><tr><th>job</th><th>类型</th><th>场景/模式/后端</th><th>状态</th><th>开始</th><th>事件</th><th>摘要</th></tr></thead>
            <tbody>${jobRows}</tbody>
          </table>
        </div>
      </div>`;
  } catch (e) {
    $("#dx-body").innerHTML = errBox(e.message || "加载失败", loadDiagnostics);
  }
}

// ── 令牌设置 ──
$("#tokenBtn").onclick = () => {
  const cur = uiToken();
  modal("WebUI 访问令牌",
    `<p class="desc">若网关启动时设了 <code>AF_WEBUI_TOKEN</code>，所有 /api 请求需带此令牌。留空表示无需令牌（仅本机）。</p>
     <div class="field"><label>当前</label><input id="tk" value="${esc(cur)}" placeholder="留空=无"></div>
     <button class="btn primary" id="tk-save">保存</button>`);
  $("#tk-save").onclick = () => {
    const v = $("#tk").value.trim();
    localStorage.setItem("af_ui_token", v);
    if (v) document.cookie = "af_ui_token=" + encodeURIComponent(v) + "; path=/; max-age=31536000; SameSite=Lax";
    else document.cookie = "af_ui_token=; path=/; max-age=0";
    closeModal(); toast("已保存");
  };
};

// ── DSL 验证任务池开关 ──
$("#tpBtn").onclick = async () => {
  let enabled = true;
  try { const c = await api("GET", "/config"); enabled = !!(c.data && c.data.task_pool_enabled); } catch {}
  modal("DSL 验证任务池开关",
    `<p class="desc">关闭后，agent 调用任务池相关工具（autoflow_list_tasks / claim_task / submit_result / publish_tasks / reset_pool / pool_stats）会立即被拒绝，避免误用。开启则恢复正常。（保存即时生效，无需重启网关）</p>
     <div class="field"><label>任务池</label>
       <div class="seg" id="tpSeg">
         <button class="seg-btn" data-v="on">开启</button>
         <button class="seg-btn" data-v="off">关闭</button>
       </div>
     </div>
     <button class="btn primary" id="tp-save">保存</button>`);
  const seg = $("#tpSeg");
  seg.querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.v === (enabled ? "on" : "off"));
    b.onclick = () => {
      seg.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    };
  });
  $("#tp-save").onclick = async () => {
    const on = seg.querySelector(".seg-btn.active")?.dataset.v === "on";
    const r = await api("PUT", "/settings", { task_pool_enabled: on });
    if (r.data?.ok) { closeModal(); toast(on ? "任务池已开启" : "任务池已关闭（agent 调用将被拒绝）"); }
    else toast("保存失败: " + (r.data?.error || r.status));
  };
};

// ── 原生节点逃逸开关（Phase 4，中风险逃生舱）──
$("#rnBtn").onclick = async () => {
  let enabled = false;
  try { const c = await api("GET", "/config"); enabled = !!(c.data && c.data.raw_node_escape_enabled); } catch {}
  modal("原生节点逃逸开关",
    `<p class="desc">开启后，黑箱 DSL 可用 <code>原生节点: {"type":"..."}</code> 直接嵌手写 Node-RED 节点，兜 DSL 表达不了的 20%（如复合 AND/OR 条件、特殊 contrib 节点）。<b>中风险</b>：绕过编译器，故默认关闭，可随时关闭使其立即失效（再提交含原生节点的 DSL 会被拒绝）。允许节点类型白名单已永久禁止 function / exec。</p>
     <div class="field"><label>原生节点逃逸</label>
       <div class="seg" id="rnSeg">
         <button class="seg-btn" data-v="on">开启</button>
         <button class="seg-btn" data-v="off">关闭</button>
       </div>
     </div>
     <button class="btn primary" id="rn-save">保存</button>`);
  const seg = $("#rnSeg");
  seg.querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.v === (enabled ? "on" : "off"));
    b.onclick = () => {
      seg.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    };
  });
  $("#rn-save").onclick = async () => {
    const on = seg.querySelector(".seg-btn.active")?.dataset.v === "on";
    const r = await api("PUT", "/settings", { raw_node_escape_enabled: on });
    if (r.data?.ok) { closeModal(); toast(on ? "原生节点逃逸已开启（可用 原生节点: 嵌手写节点）" : "原生节点逃逸已关闭（再提交将被拒绝）"); }
    else toast("保存失败: " + (r.data?.error || r.status));
  };
};
// ── 部署策略（按提案来源分流部署）──
$("#dpBtn").onclick = async () => {
  let cur = "review_all";
  try { const c = await api("GET", "/config"); cur = c.data?.deploy_policy || "review_all"; } catch {}
  modal("部署策略（按来源分流）",
    `<p class="desc">决定编译器产物是否需要人类审核后再部署：<br>
     • <b>review_all</b>：所有提案（含编译器产物）都需人类在 WebUI 点 Deploy 后部署（默认，最稳）。<br>
     • <b>compiler_auto</b>：编译器产物标「可信」徽章、可自动部署；原生手写(raw)永远需人审。<br>
     无论哪种策略，实际部署都仍过 staging 闸门(validate/lint/E2E)，且始终由人类在 WebUI 触发——绝不无人值守部署。</p>
     <div class="field"><label>部署策略</label>
       <div class="seg" id="dpSeg">
         <button class="seg-btn" data-v="review_all">全审核 (review_all)</button>
         <button class="seg-btn" data-v="compiler_auto">编译自动 (compiler_auto)</button>
       </div>
     </div>
     <button class="btn primary" id="dp-save">保存</button>`);
  const seg = $("#dpSeg");
  seg.querySelectorAll(".seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.v === cur);
    b.onclick = () => {
      seg.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    };
  });
  $("#dp-save").onclick = async () => {
    const v = seg.querySelector(".seg-btn.active")?.dataset.v || "review_all";
    const r = await api("PUT", "/settings", { deploy_policy: v });
    if (r.data?.ok) { closeModal(); toast("部署策略已设为 " + v + "（即时生效，无需重启）"); }
    else toast("保存失败: " + (r.data?.error || r.status));
  };
};
// ── 评测工作台 ──
let evalJobId = null;
let evalTimer = null;

function startEvalPoll() {
  if (evalTimer) return;
  evalTimer = setInterval(refreshEvalMonitor, 1500);
}
function stopEvalPoll() {
  if (evalTimer) { clearInterval(evalTimer); evalTimer = null; }
}
function segWire(containerId, onPick) {
  const c = $("#" + containerId);
  if (!c) return;
  c.querySelectorAll(".seg-btn").forEach((b) => {
    b.onclick = () => {
      c.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      if (onPick) onPick(b.dataset.v);
    };
  });
}
function segVal(containerId) {
  const c = $("#" + containerId);
  const a = c && c.querySelector(".seg-btn.active");
  return a ? a.dataset.v : "";
}

function loadEval() {
  const v = $("#view-eval");
  v.innerHTML = `
    <div class="view-head"><h2>评测工作台</h2><span class="sub" id="eval-cfg-sub">加载中…</span></div>
    <div class="grid cols-2">
      <div class="card form-card">
        <h3>运行评测</h3>
        <div class="field">
          <label>后端</label>
          <div class="seg" id="eval-backend">
            <button class="seg-btn active" data-v="ds_bridge">DeepSeek++（默认）</button>
          </div>
        </div>
        <div class="field">
          <label>类型</label>
          <div class="seg" id="eval-type">
            <button class="seg-btn active" data-v="golden">Golden 场景</button>
            <button class="seg-btn" data-v="acceptance">验收提示词</button>
          </div>
        </div>
        <div class="field" id="eval-scenario-field">
          <label>场景编号</label>
          <input id="eval-scenario" value="1" placeholder="1 / 2 / 3 …">
        </div>
        <div class="field" id="eval-prompt-field" hidden>
          <label>验收提示词</label>
          <textarea id="eval-prompt" placeholder="例如：请把书房的主灯关掉。先 autoflow_resolve_entity 解析真实设备，再 autoflow_propose_dsl 提交。"></textarea>
        </div>
        <div class="field">
          <label>模式</label>
          <div class="seg" id="eval-mode">
            <button class="seg-btn active" data-v="black">黑箱</button>
            <button class="seg-btn" data-v="white">白箱</button>
          </div>
        </div>
        <div class="grid cols-2">
          <div class="field"><label>总超时(s)</label><input id="eval-timeout" value="240"></div>
          <div class="field" id="eval-ct-field"><label>单轮超时(s)</label><input id="eval-ct" value="600"></div>
        </div>
        <button class="btn primary" id="eval-run">▶ 运行</button>
        <span id="eval-run-hint" class="meta" style="margin-left:8px"></span>
        <p class="desc" style="margin-top:10px;color:var(--text-dim);font-size:12px">
          后端固定为 DeepSeek++（Chrome 驱动，看不到内部但能看点火/等待）；NIM 对照后端已移除。
        </p>
      </div>
      <div class="card">
        <h3>任务监控</h3>
        <div id="eval-monitor"><div class="empty">尚未运行任务</div></div>
      </div>
    </div>
  `;
  const sub = $("#eval-cfg-sub");
  if (sub) sub.textContent = "后端固定 DeepSeek++（NIM 对照后端已移除）";

  segWire("eval-backend");
  segWire("eval-type", (val) => {
    const sf = $("#eval-scenario-field"), pf = $("#eval-prompt-field");
    if (sf) sf.hidden = val !== "golden";
    if (pf) pf.hidden = val !== "acceptance";
  });
  segWire("eval-mode");
  const runBtn = $("#eval-run");
  if (runBtn) runBtn.onclick = onEvalRun;

  if (evalJobId) refreshEvalMonitor();
}

async function onEvalRun() {
  const type = segVal("eval-type");
  const backend = segVal("eval-backend");
  const mode = segVal("eval-mode");
  const timeout = parseInt($("#eval-timeout").value, 10) || 240;
  const call_timeout = parseInt($("#eval-ct").value, 10) || 300;
  const body = { type, backend, mode, timeout, call_timeout };
  if (type === "acceptance") {
    const prompt = ($("#eval-prompt").value || "").trim();
    if (!prompt) { toast("验收提示词不能为空"); return; }
    body.prompt = prompt;
  } else {
    body.scenario = ($("#eval-scenario").value || "1").trim();
  }
  const hint = $("#eval-run-hint");
  if (hint) hint.textContent = "启动中…";
  try {
    const r = await api("POST", "/eval/run", body);
    if (!r.ok || !r.data || !r.data.job_id) {
      toast("启动失败: " + ((r.data && r.data.error) || r.status));
      if (hint) hint.textContent = "";
      return;
    }
    evalJobId = r.data.job_id;
    toast("已启动评测 · job " + evalJobId);
    if (hint) hint.textContent = "";
    refreshEvalMonitor();
    startEvalPoll();
  } catch (e) {
    toast("启动异常: " + e.message);
    if (hint) hint.textContent = "";
  }
}

async function refreshEvalMonitor() {
  const box = $("#eval-monitor");
  if (!box) return;
  if (!evalJobId) { box.innerHTML = `<div class="empty">尚未运行任务</div>`; return; }
  let job;
  try {
    const r = await api("GET", "/jobs/" + encodeURIComponent(evalJobId));
    if (!r.ok || !r.data) { box.innerHTML = `<div class="empty err">${esc((r.data && r.data.error) || "查询失败")}</div>`; return; }
    job = r.data;
  } catch (e) {
    box.innerHTML = `<div class="empty err">${esc(e.message)}</div>`; return;
  }
  const st = job.status || "starting";
  const stCls = st === "running" ? "st-running" : (st === "done" ? "st-done" : (st === "error" ? "st-error" : ""));
  const started = job.started_at ? job.started_at : 0;
  const finished = job.finished_at;
  const elapsed = finished ? (finished - started) : (Date.now() / 1000 - started);
  const head = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
      ${badge(stCls, st === "running" ? "运行中" : (st === "done" ? "成功" : (st === "error" ? "失败" : "等待中")))}
      <span class="meta">后端 ${esc(job.backend || "?")}</span>
      <span class="meta">模式 ${esc(job.mode || "?")}</span>
      <span class="meta">已用时 ${Math.max(0, Math.round(elapsed))}s</span>
      <span class="meta">job ${esc(evalJobId)}</span>
    </div>`;
  let summary = "";
  const res = job.result;
  if (res) {
    if (res.acceptance) summary += `<div class="meta" style="margin:6px 0">验收结果：<b>${esc(res.acceptance)}</b> ｜ 提案 ${res.n_proposals != null ? res.n_proposals : "?"} 条</div>`;
    if (res.ok === false && res.error) summary += `<div class="meta" style="color:var(--danger)">错误：${esc(res.error)}</div>`;
  }
  const events = job.events || [];
  const evHtml = events.map((e) => {
    const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
    const okCls = e.ok === true ? "ok" : (e.ok === false ? "err" : "");
    return `<div class="evt ${okCls}"><span class="evt-time">${esc(t)}</span><span class="evt-phase">${esc(e.phase)}</span><span class="evt-msg">${esc(e.msg)}</span></div>`;
  }).join("");
  const detail = res ? `<details style="margin-top:10px"><summary class="meta">展开完整结果 JSON</summary><div class="code-box">${esc(JSON.stringify(res, null, 2))}</div></details>` : "";
  box.innerHTML = head + summary + `<div class="evt-wrap" id="evt-wrap">${evHtml}</div>` + detail;
  const wrap = $("#evt-wrap");
  if (wrap) wrap.scrollTop = wrap.scrollHeight;
  if (st === "running") startEvalPoll();
  else stopEvalPoll();
}

// ── 自愈闭环：重试次数（WebUI 可配，0=禁用自主重试，1~20=单 (agent, flow) 滑动窗口内最多自主重试）──
$("#shBtn").onclick = async () => {
  let cur = 3;
  try { const c = await api("GET", "/config"); cur = (c.data && c.data.selfheal_budget) || 3; } catch {}
  modal("自愈重试次数（自愈闭环）",
    `<p class="desc">agent 自主触发 → 回读 → 修正已部署 flow 时，同一 (agent, flow) 在 10 分钟窗口内最多重试 N 次；耗尽即停止并转报告，防止自动修复死循环。<br>
     • <b>0</b>：不自主重试（一次失败即停，等同纯人审时代行为但无闸）。<br>
     • <b>1~20</b>：单 (agent, flow) 滑动窗口内最多自主重试次数（默认 3）。<br>
     改完<b>即时生效，无需重启网关</b>。</p>
     <div class="field"><label>自愈重试次数</label>
       <input type="number" id="shInput" min="0" max="20" value="${cur}" style="width:90px">
     </div>
     <button class="btn primary" id="sh-save">保存</button>`);
  $("#sh-save").onclick = async () => {
    const n = parseInt($("#shInput").value, 10);
    if (Number.isNaN(n) || n < 0 || n > 20) {
      return toast("自愈重试次数必须是 0~20 之间的整数");
    }
    const r = await api("PUT", "/settings", { selfheal_budget: n });
    if (r.data?.ok) { closeModal(); toast(`自愈重试次数已设为 ${n}（即时生效，无需重启）`); }
    else toast("保存失败: " + (r.data?.error || r.status));
  };
};

$("#modalClose").onclick = closeModal;
$("#modalMask").onclick = (e) => { if (e.target.id === "modalMask") closeModal(); };

// ── 移动端「更多」抽屉 ──
function openMobileSheet() { $("#mobileSheet")?.removeAttribute("hidden"); }
function closeMobileSheet() { $("#mobileSheet")?.setAttribute("hidden", ""); }
$("#bottomnavMore")?.addEventListener("click", (e) => { e.stopPropagation(); openMobileSheet(); });
$("#mobileSheet")?.addEventListener("click", (e) => {
  const target = e.target.closest("[data-action='close-sheet']");
  if (target) closeMobileSheet();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMobileSheet(); });

// ── 导航 ──
$$(".navitem[data-tab]").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));

// ── 子流程注册表（#575/#579/#580）──
let _sfList = [];
let _sfView = "subflow";  // A1：当前子流程类视图（subflow=子流程 Tab | link_api=Link API Tab）
async function loadSubflows() {
  _sfView = "subflow";
  const v = $("#view-subflows");
  v.innerHTML = `<div class="view-head"><h2>子流程</h2>
    <button class="btn primary" id="sf-import-btn">＋ 导入 NR 子流程</button></div>
    <div class="row" style="gap:8px;margin:8px 0">
      <select id="sf-filter"><option value="">全部</option><option value="managed">网关预置</option><option value="imported">用户导入</option></select>
      <span class="meta" id="sf-count"></span>
    </div>
    <div id="sf-list"><div class="empty">加载中…</div></div>`;
  $("#sf-import-btn").onclick = showImportSubflow;
  $("#sf-filter").onchange = () => renderSfList($("#sf-filter").value);
  await refreshSfList();
}
async function refreshSfList() {
  try {
    const r = await api("GET", "/subflows");
    _sfList = r.data?.subflows || [];
    renderSfList($("#sf-filter") ? $("#sf-filter").value : "");
  } catch (e) {
    const el = $("#sf-list"); if (el) el.innerHTML = errBox(e.message, refreshSfList);
  }
}
function renderSfList(filter) {
  const el = $("#sf-list"); if (!el) return;
  let rows = _sfList.slice();
  // A1：按当前 Tab 视图过滤 kind——子流程 Tab 只显 subflow，Link API Tab 只显 link_out/http_api
  if (_sfView === "subflow") rows = rows.filter((s) => (s.kind || "subflow") === "subflow");
  else if (_sfView === "link_api") rows = rows.filter((s) => ["link_out", "http_api"].includes(s.kind || ""));
  if (filter) rows = rows.filter((s) => s.source_type === filter);
  const cnt = $("#sf-count"); if (cnt) cnt.textContent = `共 ${rows.length} 条`;
  if (!rows.length) { el.innerHTML = `<div class="empty">暂无子流程（点右上「＋ 导入」从 NR 自省导入）。</div>`; return; }
  el.innerHTML = rows.map((s) => {
    const ins = Array.isArray(s.input_schema) ? s.input_schema.length : 0;
    const envs = Array.isArray(s.env_requirements) ? s.env_requirements.length : 0;
    const isManaged = s.source_type === "managed";
    const kind = s.kind || "subflow";
    const kindBadge = kind === "link_out"
      ? badge("kind-link_out", "link API")
      : badge("kind-subflow", "subflow");
    const idLine = kind === "link_out"
      ? `link out 入口（entry_link_id）：${esc(s.entry_link_id || "—")}`
      : `NR 子流程 id：${esc(s.nr_subflow_id || "—")}`;
    return `<div class="item" data-key="${esc(s.key)}">
      <div class="row">
        <div><span class="title">${esc(s.title || s.key)}</span> <span class="meta">${esc(s.key)}</span></div>
        <div>${badge("tier-" + (isManaged ? "managed" : "imported"), isManaged ? "网关预置" : "用户导入")} ${kindBadge} ${badge("status-" + s.status, s.status)}</div>
      </div>
      <div class="desc">DSL 调用：<code>调用子流程: ${esc(s.key)}(...)</code></div>
      <div class="desc">前置参数 ${ins} 项 ｜ 需配置 env ${envs} 项 ｜ ${idLine}</div>
      <div class="actions"><button class="btn sm" data-detail="${esc(s.key)}">查看前置参数</button>${sfEnsureAction(s)}${sfBarkAction(s)}${sfStatusActions(s)}${sfDeleteAction(s)}</div>
    </div>`;
  }).join("");
  $$("[data-detail]").forEach((b) => (b.onclick = () => showSfDetail(b.dataset.detail)));
  $$("[data-sf-status]").forEach((b) => (b.onclick = () => setSfStatus(b.dataset.key, b.dataset.sfStatus)));
  $$("[data-sf-del]").forEach((b) => (b.onclick = () => deleteSubflow(b.dataset.sfDel)));
  $$("[data-sf-ensure]").forEach((b) => (b.onclick = () => ensureSubflow(b.dataset.sfEnsure)));
  $$("[data-bark-install]").forEach((b) => (b.onclick = () => installBarkSubflow(b.dataset.barkInstall)));
}

// ── A5(#171)：Bark 安装前置参数 ──
// 未配 BARK_SERVER/BARK_KEY 时按钮禁用并提示；配置后调 POST /api/subflows/bark/install。
function sfBarkAction(s) {
  if (s.key !== "bark_push") return "";
  const ready = !!s.bark_ready;
  const title = ready
    ? "幂等安装 Bark 子流程到 Node-RED（已存在则跳过）"
    : "请先在「设置 → 连接配置 → Bark」填写 BARK_SERVER 与 BARK_KEY";
  const status = ready
    ? '<span class="meta" style="color:#28a745;margin-right:6px">✅ Bark 已配置</span>'
    : '<span class="meta" style="color:#dc3545;margin-right:6px">⚠️ Bark 未配置</span>';
  const disabledAttr = ready ? "" : "disabled";
  const disabledStyle = ready ? "" : "opacity:0.5;cursor:not-allowed";
  return ` ${status}<button class="btn sm" data-bark-install="${esc(s.key)}" ${disabledAttr} style="${disabledStyle}" title="${title}">安装 Bark 子流程</button>`;
}

async function installBarkSubflow(key) {
  try {
    const r = await api("POST", "/subflows/bark/install", {});
    if (!r.ok) return toast("安装失败：" + (r.data?.error || r.status));
    const d = r.data || {};
    toast(d.exists ? "Bark 子流程已存在，无需安装：" + key
      : d.created ? "已安装 Bark 子流程到 NR：" + key : "安装完成：" + key);
    await refreshSfList();
  } catch (e) { toast("安装失败：" + e.message); }
}
// ── A1：Link API Tab（网关 HTTP 桥接：link_out / http_api）─────────────
// A2：配置表单 / A3：一键安装「AutoFlow API」tab 到 NR（按节点 id 增量合并，不删旧节点）。
async function loadLinkApis() {
  _sfView = "link_api";
  const v = $("#view-link_apis");
  v.innerHTML = `<div class="view-head"><h2>Link API（网关 HTTP 桥接）</h2><button class="btn" id="la-install-tab" title="把已配置好的 Link API 增量合并到 NR 的 AutoFlow API tab">📦 安装到 Node-RED</button></div>
    <div class="row" style="gap:8px;margin:8px 0">
      <span class="meta" id="la-count"></span>
    </div>
    <div id="la-list"><div class="empty">加载中…</div></div>`;
  $("#la-install-tab").onclick = installLinkApiTab;
  await refreshLinkApis();
}

async function installLinkApiTab() {
  try {
    const r = await api("POST", "/link-apis/install-tab", {});
    if (!r.ok) {
      const d = r.data || {};
      if (Array.isArray(d.missing) && d.missing.length) {
        const info = d.missing.map((m) => `${esc(m.title || m.name)}：缺少 ${m.missing.join("、")}`).join("；");
        return toast("安装失败：" + (d.error || "配置不完整") + " — " + info);
      }
      return toast("安装失败：" + (d.error || r.status));
    }
    const d = r.data || {};
    // #177：tab_id 是 NR 实际分配的真实 id（不是种子 af_api_tab），据此判断有无重名 tab
    let msg = d.skipped
      ? `AutoFlow API tab 已是最新，无需改动（tab ${d.tab_id || "?"}）。`
      : `已${d.tab_created ? "创建" : "更新"} AutoFlow API tab（${d.tab_id || "?"}）：`
        + `新增 ${d.nodes_added || 0} 个、刷新 ${d.nodes_updated || 0} 个，总计 ${d.nodes_total || 0} 个节点。`
        + `包含 ${(d.specs || []).join(", ")}`;
    if (Array.isArray(d.duplicate_tabs) && d.duplicate_tabs.length > 1) {
      msg += ` ⚠️ NR 上有 ${d.duplicate_tabs.length} 个同名 tab，请手动清理多余的：${d.duplicate_tabs.join(", ")}`;
    }
    toast(msg);
  } catch (e) { toast("安装失败：" + e.message); }
}
async function refreshLinkApis() {
  try {
    const r = await api("GET", "/subflows");
    _sfList = r.data?.subflows || [];
    _sfView = "link_api";
    renderLinkApis();
  } catch (e) {
    const el = $("#la-list"); if (el) el.innerHTML = errBox(e.message, refreshLinkApis);
  }
}
function renderLinkApis() {
  const el = $("#la-list"); if (!el) return;
  let rows = _sfList.filter((s) => ["link_out", "http_api"].includes(s.kind || ""));
  const cnt = $("#la-count"); if (cnt) cnt.textContent = `共 ${rows.length} 条`;
  if (!rows.length) { el.innerHTML = `<div class="empty">暂无 Link API（网关桥接类能力）。</div>`; return; }
  el.innerHTML = rows.map((s) => {
    const ins = Array.isArray(s.input_schema) ? s.input_schema.length : 0;
    const kindBadge = badge("kind-link_out", "link API");
    const idLine = s.kind === "link_out"
      ? `link out 入口：${esc(s.entry_link_id || "—")}`
      : "网关内联（不生成 NR 节点）";
    return `<div class="item" data-key="${esc(s.key)}">
      <div class="row">
        <div><span class="title">${esc(s.title || s.key)}</span> <span class="meta">${esc(s.key)}</span></div>
        <div>${badge("tier-" + (s.source_type === "managed" ? "managed" : "imported"), s.source_type === "managed" ? "网关预置" : "用户导入")} ${kindBadge} ${badge("status-" + s.status, s.status)}</div>
      </div>
      <div class="desc">DSL 调用：<code>调用子流程: ${esc(s.key)}(...)</code></div>
      <div class="desc">前置参数 ${ins} 项 ｜ ${idLine}</div>
      <div class="actions">
        <button class="btn sm" data-la-cfg="${esc(s.key)}" title="填写 token / 坐标等运行时参数">⚙️ 配置</button>
        <button class="btn sm danger" data-la-del="${esc(s.key)}" title="清空本机配置并移除 AutoFlow API tab 里由它派生的节点">🗑️ 删除</button>
      </div>
    </div>`;
  }).join("");
  $$("[data-la-cfg]").forEach((b) => (b.onclick = () => showLinkApiConfig(b.dataset.laCfg)));
  $$("[data-la-del]").forEach((b) => (b.onclick = () => deleteLinkApi(b.dataset.laDel)));
}

// ── #182：删除（卸载）Link API ──
// 语义要跟用户讲清楚：删的是「你的配置 + NR 里那条链」，能力声明本身留在网关，
// 日后想用还能重新配置+安装。不说清楚，用户会以为能力被永久拆了而不敢点。
async function deleteLinkApi(key) {
  const s = _sfList.find((x) => x.key === key);
  const title = s ? (s.title || key) : key;
  if (!confirm(
    `确定删除 Link API「${title}」？\n`
    + "\n将清空它的本机配置（token 等），并移除 Node-RED「AutoFlow API」tab 中"
    + "由它派生的节点。\n其它 Link API 的链路与你自己的流程不受影响；"
    + "能力声明保留在网关，之后可重新配置并安装回去。")) return;
  try {
    const r = await api("DELETE", "/link-apis/" + encodeURIComponent(key));
    if (!r.ok) return toast("删除失败：" + (r.data?.error || r.status));
    const d = r.data || {};
    const nrPart = d.nodes_removed
      ? `，已从 tab ${d.tab_id || "?"} 移除 ${d.nodes_removed} 个节点`
      : "（NR 上无派生节点，无需改动）";
    toast(`已删除 ${title}${nrPart}。`);
    await refreshLinkApis();
  } catch (e) { toast("删除失败：" + e.message); }
}

// ── A2：Link API 配置表单（方案 B：api_configs 表持久化）──
async function showLinkApiConfig(key) {
  const s = _sfList.find((x) => x.key === key);
  const title = s ? (s.title || key) : key;
  let data;
  try {
    const r = await api("GET", "/link-apis/" + encodeURIComponent(key) + "/config");
    if (!r.ok) return toast("读取配置失败：" + (r.data?.error || r.status));
    data = r.data;
  } catch (e) { return toast("读取配置失败：" + e.message); }

  const fields = data.config_fields || [];
  const cfg = data.config || {};
  if (!fields.length) {
    modal("配置 Link API · " + esc(title),
      `<div class="empty">该 Link API 无需运行时配置（spec 中无 &lt;ENV&gt; 占位符）。</div>`);
    return;
  }
  // 密钥类字段（含 TOKEN/KEY/SECRET/PASSWORD/API）→ password 不回显；坐标等明文。
  const isSecret = (n) => /TOKEN|KEY|SECRET|PASSWORD|API/i.test(n);
  const rows = fields.map((n) => {
    const val = cfg[n];
    const set = val !== undefined && val !== null && val !== "";
    if (isSecret(n)) {
      const state = set
        ? `<span class="conn-src">· 已设置（${String(val).length} 字符）</span>`
        : `<span class="conn-src">· 未设置</span>`;
      return `<div class="field">
        <label>${esc(n)} <span class="badge kind-link_out">密钥</span> ${state}</label>
        <input type="password" data-la-k="${esc(n)}" data-secret="1" autocomplete="new-password"
               placeholder="${set ? "留空表示不修改" : "请输入 " + esc(n)}">
      </div>`;
    }
    return `<div class="field">
      <label>${esc(n)}</label>
      <input data-la-k="${esc(n)}" data-secret="0" value="${esc(val || "")}" placeholder="请输入 ${esc(n)}">
    </div>`;
  }).join("");
  modal("配置 Link API · " + esc(title), `
    <p class="desc">填写此 Link API 的运行参数。密钥仅本机存储于 <code>api_configs</code> 表，不进 git；保存后立即生效。</p>
    ${rows}
    <div class="conn-result" id="la-cfg-result"></div>
    <button class="btn primary" id="la-cfg-save">保存</button>`);
  $("#la-cfg-save").onclick = () => saveLinkApiConfig(key);
}

async function saveLinkApiConfig(key) {
  const patch = {};
  $$("#modalBody [data-la-k]").forEach((el) => {
    const n = el.dataset.laK;
    const v = el.value;                 // 密钥不 trim（保留首尾空格意图）；坐标允许 trim
    if (el.dataset.secret === "1") {
      if (v) patch[n] = v;              // 空 = 保留已有
    } else {
      patch[n] = v.trim();
    }
  });
  const out = $("#la-cfg-result");
  if (!Object.keys(patch).length) { out.textContent = "没有改动。"; return; }
  out.textContent = "保存中…";
  const btn = $("#la-cfg-save"); if (btn) btn.disabled = true;
  try {
    const r = await api("PUT", "/link-apis/" + encodeURIComponent(key) + "/config", { config: patch });
    if (!r.ok) { out.textContent = "保存失败：" + (r.data?.error || r.status); return; }
    toast("已保存：" + key);
    closeModal();
    await refreshLinkApis();
  } catch (e) {
    out.textContent = "保存失败：" + e.message;
  } finally { if (btn) btn.disabled = false; }
}
// history_* 是 DSL 内置原语（编译器语法的一部分），只能禁用不能删除
const SF_HISTORY_KEYS = ["history_state_at", "history_occurred", "history_duration", "history_aggregate"];
const isHistorySf = (s) => SF_HISTORY_KEYS.includes(s.key);

function sfDeleteAction(s) {
  // #711：历史子流程 → 无删除（只能禁用）；其余一律给删除按钮。
  // 删除的 NR 侧语义按「谁建的谁负责」：managed=网关自建→连 NR 实例一起删；
  // imported=用户自己在 NR 建的→只取消登记，NR 上原样保留。
  if (isHistorySf(s)) return "";
  const kind = s.kind || "subflow";
  const isManaged = s.source_type === "managed";
  const nrNote = kind !== "subflow" ? "（无 NR 实例）"
    : isManaged ? "（网关自建，将一并删除 NR 子流程实例）"
                : "（仅取消登记，NR 上的子流程保留）";
  return ` <button class="btn sm danger" data-sf-del="${esc(s.key)}" title="删除${nrNote}">删除</button>`;
}
async function deleteSubflow(key) {
  const s = _sfList.find((x) => x.key === key) || {};
  const kind = s.kind || "subflow";
  const tail = kind !== "subflow"
    ? "（该能力无 NR 子流程实例）"
    : s.source_type === "managed"
      ? "\n⚠️ 这是网关自建的子流程，NR 上的子流程实例会被一并删除。"
      : "\n（只从网关注册表取消登记，Node-RED 上的子流程保持原样，不会被删除。）";
  if (!confirm(`确定删除子流程「${key}」？${tail}\n此操作不可撤销，已引用它的 flow 将失效。`)) return;
  try {
    const r = await api("DELETE", "/subflows/" + encodeURIComponent(key));
    if (!r.ok) return toast("删除失败：" + (r.data?.error || r.status));
    toast("已删除：" + key + (r.data?.nr_removed ? "（NR 实例已删除）"
      : r.data?.nr_kept ? "（NR 子流程已保留）" : ""));
    await refreshSfList();
  } catch (e) { toast("删除失败：" + e.message); }
}
function sfEnsureAction(s) {
  // #711：历史子流程「安装到 NR」—— 不部署也能提前装 / NR 侧被手删后一键修复
  if (!isHistorySf(s)) return "";
  return ` <button class="btn sm" data-sf-ensure="${esc(s.key)}" title="幂等安装 4 个 af_hist_* 子流程到 Node-RED（已存在则跳过）">安装到 NR</button>`;
}
async function ensureSubflow(key) {
  try {
    const r = await api("POST", "/subflows/" + encodeURIComponent(key) + "/ensure", {});
    if (!r.ok) return toast("安装失败：" + (r.data?.error || r.status));
    const d = r.data || {};
    toast(d.exists ? "已存在，无需安装：" + key
      : d.created ? "已安装到 NR：" + key : "安装完成：" + key);
    await refreshSfList();
  } catch (e) { toast("安装失败：" + e.message); }
}
function sfStatusActions(s) {
  // #711：managed 子流程（含 history_*）此前被一刀切隐藏启停按钮，导致这类条目
  // 在 WebUI 上完全不可操作。历史子流程不允许删除，「禁用」是它唯一的治理手段，
  // 故一律按状态展示启停按钮。
  if (s.status === "pending_review")
    return ` <button class="btn sm ok" data-sf-status="active" data-key="${esc(s.key)}">通过审核</button><button class="btn sm" data-sf-status="disabled" data-key="${esc(s.key)}">禁用</button>`;
  if (s.status === "active")
    return ` <button class="btn sm" data-sf-status="disabled" data-key="${esc(s.key)}">禁用</button>`;
  if (s.status === "disabled")
    return ` <button class="btn sm ok" data-sf-status="active" data-key="${esc(s.key)}">启用</button>`;
  return "";
}
async function setSfStatus(key, status) {
  try {
    const r = await api("PATCH", "/subflows/" + encodeURIComponent(key) + "/status", { status });
    if (!r.ok) return toast("状态变更失败：" + (r.data?.error || r.status));
    toast((status === "active" ? "已启用/通过：" : status === "disabled" ? "已禁用：" : "已置为 " + status + "：") + key);
    await refreshSfList();
  } catch (e) { toast("状态变更失败：" + e.message); }
}
function showSfDetail(key) {
  const s = _sfList.find((x) => x.key === key); if (!s) return;
  const ins = Array.isArray(s.input_schema) ? s.input_schema : [];
  const envs = Array.isArray(s.env_requirements) ? s.env_requirements : [];
  const kind = s.kind || "subflow";
  const kindLabel = kind === "link_out" ? "link API（网关发 change + link out）" : "subflow 实例（NR 子流程）";
  const idLabel = kind === "link_out"
    ? `link out 入口：${esc(s.entry_link_id || "—")}`
    : `NR 子流程 id：${esc(s.nr_subflow_id || "—")}`;
  const inRows = ins.length
    ? ins.map((p) => `<tr><td><code>${esc(p.name)}</code></td><td>${esc(p.type || "str")}</td><td>${p.required ? "<b>必填</b>" : "可选"}</td><td>${esc(p.desc || "")}</td></tr>`).join("")
    : `<tr><td colspan="4" class="desc">（无，或未自省到 msg 读取）</td></tr>`;
  const envRows = envs.length
    ? envs.map((e) => `<li><code>${esc(typeof e === "string" ? e : e.name)}</code></li>`).join("")
    : `<li class="desc">（无）</li>`;
  modal("子流程前置参数 · " + esc(s.title || s.key), `
    <div class="desc">DSL 调用：<code>调用子流程: ${esc(s.key)}(...)</code></div>
    <div class="desc">类型：${esc(s.source_type === "managed" ? "网关预置" : "用户导入")} ｜ 形态：${esc(kindLabel)} ｜ 状态：${esc(s.status)} ｜ ${idLabel}</div>
    <h3>前置参数（调用方需传入）</h3>
    <table class="tbl"><thead><tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr></thead><tbody>${inRows}</tbody></table>
    <h3>需配置的 env 变量（owner 侧）</h3>
    <ul class="kv">${envRows}</ul>
    <details><summary class="meta">完整 JSON</summary><div class="code-box">${esc(JSON.stringify(s, null, 2))}</div></details>`);
}
function showImportSubflow() {
  modal("导入 NR 子流程（自省前置参数）", `
    <div class="field"><label>NR 子流程 id（在 NR 子流程属性里复制）</label><input id="sf-nr" placeholder="如 b0bbc86abb2172a5"></div>
    <div class="field"><label>DSL 调用名 key（唯一，勿与内置撞名）</label><input id="sf-key" placeholder="如 my_custom_push"></div>
    <div class="field"><label>标题（可选）</label><input id="sf-title" placeholder="我的子流程"></div>
    <div class="field"><label>owner（可选）</label><input id="sf-owner" placeholder="webui"></div>
    <div class="field"><label>状态</label><select id="sf-status"><option value="active">active（立即可用）</option><option value="pending_review">pending_review（待审核）</option><option value="disabled">disabled（禁用）</option></select></div>
    <p class="desc">提交后网关会读取该 NR 子流程的 in 端口与 env，自动抽取「前置参数」填入注册表，无需手填。</p>
    <button class="btn primary" id="sf-do-import">自省并导入</button>`);
  $("#sf-do-import").onclick = doImportSubflow;
}
async function doImportSubflow() {
  const nr_subflow_id = $("#sf-nr").value.trim();
  const key = $("#sf-key").value.trim();
  if (!nr_subflow_id) return toast("请填 NR 子流程 id");
  if (!key) return toast("请填 DSL 调用名 key");
  const body = {
    nr_subflow_id, key,
    title: $("#sf-title").value.trim(),
    owner: $("#sf-owner").value.trim() || "webui",
    status: $("#sf-status").value,
  };
  const btn = $("#sf-do-import"); btn.disabled = true; btn.textContent = "导入中…";
  try {
    const r = await api("POST", "/subflows/import", body);
    if (!r.ok) return toast("导入失败：" + (r.data?.error || r.status));
    toast("导入成功：" + key);
    closeModal();
    await refreshSfList();
  } catch (e) {
    toast("导入失败：" + e.message);
  } finally { btn.disabled = false; btn.textContent = "自省并导入"; }
}

// ── 设置管理界面（C3/C21/C25）──
async function loadSettings() {
  const v = $("#view-settings");
  v.innerHTML = `
    <div class="view-head"><h2>⚙️ 设置</h2><span class="sub">连接配置 · 审计日志</span></div>
    <div class="tabs sub" id="settings-tabs">
      <button class="stab active" data-s="conn">连接配置</button>
      <button class="stab" data-s="audit">审计日志</button>
    </div>
    <div id="settings-body"><div class="empty">加载中…</div></div>`;
  $$("#settings-tabs .stab").forEach((b) => (b.onclick = () => {
    $$("#settings-tabs .stab").forEach((x) => x.classList.toggle("active", x === b));
    settingsShow(b.dataset.s);
  }));
  settingsShow("conn");
}
function settingsShow(s) {
  if (s === "conn") return loadConnection();
  if (s === "audit") return loadAudit();
}
// 连接设置（#45）：HA / Node-RED / Bark 的地址与凭据。
// 密钥只在本机落盘（data/<env>/connections.json，已 gitignore），界面永远只回显掩码。
const CONN_SOURCE_LABEL = { ui: "界面设置", env: "环境变量", default: "默认值", unset: "未设置" };

async function loadConnection() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="empty">加载中…</div>`;
  let data;
  try {
    const r = await api("GET", "/settings/connections");
    if (!r.ok) throw new Error(r.data?.error || r.status);
    data = r.data;
  } catch (e) { body.innerHTML = errBox(e.message, loadConnection); return; }

  const groups = data.groups || [];
  body.innerHTML = groups.map((g) => `
    <div class="card conn-card" data-group="${esc(g.id)}">
      <h3>${esc(g.label)}</h3>
      <p class="conn-desc">${esc(g.desc || "")}</p>
      ${(g.fields || []).map((f) => connFieldHtml(f)).join("")}
      <div class="conn-actions">
        <button class="btn primary sm" data-save="${esc(g.id)}">保存</button>
        <button class="btn sm" data-test="${esc(g.id)}">测试连接</button>
        ${g.id === "ha" ? `<button class="btn sm" data-import="${esc(g.id)}">导入全部设备</button>` : ""}
        ${g.id === "bark" ? `<label><input type="checkbox" id="bark-send"> 发送测试推送</label>` : ""}
      </div>
      <div class="conn-result" data-result="${esc(g.id)}"></div>
    </div>`).join("") + `
    <div class="card conn-card">
      <p class="conn-desc">🔒 凭据保存在本机 <span class="conn-path">${esc(data.path || "")}</span>（该目录已在 .gitignore 中，不会随仓库泄漏），
      保存后立即生效、无需重启网关。界面设置的优先级高于环境变量与 .env。</p>
    </div>`;

  groups.forEach((g) => {
    $(`[data-save="${g.id}"]`).onclick = () => saveConnGroup(g);
    $(`[data-test="${g.id}"]`).onclick = () => testConnGroup(g.id);
    const imp = $(`[data-import="${g.id}"]`);
    if (imp) imp.onclick = () => importCatalog(imp, g.id);
    (g.fields || []).forEach((f) => {
      const clr = $(`[data-clear="${f.key}"]`);
      if (clr) clr.onclick = () => clearConnField(g, f);
    });
  });
}

function connFieldHtml(f) {
  const src = `<span class="conn-src">· ${esc(CONN_SOURCE_LABEL[f.source] || f.source)}</span>`;
  if (f.kind === "secret") {
    const state = f.configured
      ? `<span class="conn-src">· 已设置 ${esc(f.masked || "")}（${f.length || 0} 字符）</span> ${src}`
      : `<span class="conn-src">· 未设置</span>`;
    return `<div class="field">
      <label>${esc(f.label)} ${state}</label>
      <div class="conn-row">
        <input type="password" data-k="${esc(f.key)}" data-kind="secret" autocomplete="new-password"
               placeholder="${f.configured ? "留空表示不修改" : esc(f.placeholder)}">
        ${f.configured ? `<button class="btn sm danger" data-clear="${esc(f.key)}">清除</button>` : ""}
      </div>
      ${f.hint ? `<p class="conn-hint">${esc(f.hint)}</p>` : ""}
    </div>`;
  }
  return `<div class="field">
    <label>${esc(f.label)} ${src}</label>
    <input data-k="${esc(f.key)}" data-kind="${esc(f.kind)}" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder)}">
    ${f.hint ? `<p class="conn-hint">${esc(f.hint)}</p>` : ""}
  </div>`;
}

async function saveConnGroup(g) {
  const patch = {};
  let dirty = false;
  (g.fields || []).forEach((f) => {
    const el = $(`[data-k="${f.key}"]`);
    if (!el) return;
    const v = el.value.trim();
    if (f.kind === "secret") {
      if (v) { patch[f.key] = v; dirty = true; }      // 空=不改（界面上只有掩码）
    } else if (v !== (f.value || "")) {
      patch[f.key] = v;                                // 空串=清除，回退 env/默认
      dirty = true;
    }
  });
  const out = $(`[data-result="${g.id}"]`);
  if (!dirty) { out.textContent = "没有改动。"; return; }
  out.textContent = "保存中…";
  try {
    const r = await api("PUT", "/settings/connections", patch);
    if (!r.ok) throw new Error(r.data?.error || r.status);
    const notices = r.data?.notices || [];
    toast("已保存并生效");
    await loadConnection();
    if (notices.length) {
      const box = $(`[data-result="${g.id}"]`);
      if (box) box.innerHTML = notices.map((n) => `⚠️ ${esc(n)}`).join("<br>");
    }
  } catch (e) { out.textContent = "保存失败：" + e.message; }
}

async function clearConnField(g, f) {
  if (!confirm(`确定清除「${f.label}」？清除后将回退到环境变量或默认值。`)) return;
  try {
    const r = await api("PUT", "/settings/connections", { [f.key]: null });
    if (!r.ok) throw new Error(r.data?.error || r.status);
    toast("已清除");
    await loadConnection();
  } catch (e) { toast("清除失败：" + e.message); }
}

async function testConnGroup(id) {
  const out = $(`[data-result="${id}"]`);
  out.textContent = "测试中…";
  const sendEl = $("#bark-send");
  try {
    const r = await api("POST", "/settings/connections/test",
      { targets: [id], send_bark: id === "bark" && !!(sendEl && sendEl.checked) });
    if (!r.ok) throw new Error(r.data?.error || r.status);
    const d = (r.data?.results || {})[id] || {};
    out.innerHTML = d.ok
      ? `✅ ${esc(d.detail || "连接正常")}`
      : `❌ ${esc(d.error || "连接失败")}`;
  } catch (e) { out.textContent = "测试失败：" + e.message; }
}
async function importCatalog(btn, gid) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = "导入中…";
  try {
    const r = await api("POST", "/catalog/import");
    if (r.ok) toast(`已导入 ${r.data?.total} 个实体`);
    else toast("导入失败：" + (r.data?.error || r.status));
  } catch (e) { toast(e.message || "导入失败"); }
  btn.disabled = false; btn.textContent = old;
}
async function loadSafeGate() {
  const v = $("#view-safe");
  v.innerHTML = `<div class="view-head"><h2>🛡️ 安全闸</h2><span class="sub">设备保护：先导入全屋设备目录，再勾选需保护的实体</span></div>
    <div class="card" style="margin-top:14px">
      <h3>设备目录</h3>
      <div id="sg-catalog"><div class="empty">加载中…</div></div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">
        <button class="btn primary sm" id="sg-import">导入全部设备</button>
        <span class="meta">从 Home Assistant / Node-RED 拉取全屋实体（仅显式触发，不随测试连接自动跑）</span>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>添加保护</h3>
      <p class="desc">用中文/英文搜索设备（按 friendly_name / area / entity_id），点选即加入保护。Tier-0 触及需人工确认；Tier-1 放行但记审计。</p>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <input id="sg-search" placeholder="如「书房灯」「客厅」「office」" style="flex:1;min-width:200px">
        <select id="sg-tier"><option value="0">Tier-0（需确认）</option><option value="1">Tier-1（放行+审计）</option></select>
      </div>
      <div id="sg-results" style="margin-top:8px"><div class="empty">输入关键词搜索设备</div></div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>已保护实体</h3>
      <div id="sg-list"><div class="empty">加载中…</div></div>
    </div>`;

  const refreshCatalog = async () => {
    const r = await api("GET", "/catalog");
    const c = $("#sg-catalog");
    if (!r.ok) { c.innerHTML = `<div class="empty">目录不可用：${esc(r.data?.error || r.status)}</div>`; return; }
    const d = r.data || {};
    c.innerHTML = `<div class="desc">共 <b>${d.total}</b> 个实体 ｜ 最近导入：${esc(d.freshness || "从未")}</div>`;
  };
  await refreshCatalog();

  $("#sg-import").onclick = async () => {
    const btn = $("#sg-import"); btn.disabled = true; btn.textContent = "导入中…";
    try {
      const r = await api("POST", "/catalog/import");
      if (r.ok) { toast(`已导入 ${r.data?.total} 个实体`); await refreshCatalog(); await refreshList(); }
      else toast("导入失败：" + (r.data?.error || r.status));
    } catch (e) { toast(e.message || "导入失败"); }
    btn.disabled = false; btn.textContent = "导入全部设备";
  };

  let searchTimer;
  $("#sg-search").addEventListener("input", (e) => {
    const kw = e.target.value.trim();
    clearTimeout(searchTimer);
    if (!kw) { $("#sg-results").innerHTML = `<div class="empty">输入关键词搜索设备</div>`; return; }
    searchTimer = setTimeout(async () => {
      const r = await api("GET", "/entities?keyword=" + encodeURIComponent(kw) + "&limit=20");
      const box = $("#sg-results");
      const list = r.data?.entities || [];
      if (!list.length) { box.innerHTML = `<div class="empty">没有匹配「${esc(kw)}」的设备</div>`; return; }
      box.innerHTML = `<div class="list">` + list.map((en) => `
        <div class="item">
          <div class="row"><div><span class="title">${esc(en.friendly_name || en.entity_id)}</span> <span class="meta">${esc(en.entity_id)}</span></div>
          <div class="meta">${esc((en.area ? en.area + " · " : "") + (en.domain || ""))}</div></div>
          <div class="actions"><button class="btn sm primary" data-add="${esc(en.entity_id)}">保护</button></div>
        </div>`).join("") + `</div>`;
      $$("#sg-results [data-add]").forEach((b) => (b.onclick = () => addProtection(b.dataset.add)));
    }, 250);
  });

  const tier = () => parseInt($("#sg-tier").value, 10);
  const addProtection = async (entityId) => {
    const r = await api("POST", "/device-guard", { match: { type: "entity", value: entityId }, tier: tier() });
    if (r.ok) { toast(`已保护 ${entityId}`); await refreshList(); }
    else toast("添加失败：" + (r.data?.error || r.status));
  };

  const refreshList = async () => {
    const r = await api("GET", "/device-guard");
    const box = $("#sg-list");
    const rules = r.data?.rules || [];
    if (!rules.length) { box.innerHTML = `<div class="empty">还没有保护规则。</div>`; return; }
    box.innerHTML = `<div class="list">` + rules.map((x) => `<div class="item">
      <div class="row"><div><span class="title">${esc(x.match.value)}</span> <span class="meta">${esc(x.match.type)}</span></div>
      <div>${badge("tier-" + x.tier, x.tier === 0 ? "Tier-0 需确认" : "Tier-1 放行")} <button class="btn sm danger" data-del="${esc(x.id)}">删除</button></div></div>
    </div>`).join("") + `</div>`;
    $$("#sg-list [data-del]").forEach((b) => (b.onclick = async () => {
      const r = await api("DELETE", "/device-guard/" + b.dataset.del);
      if (r.ok) refreshList(); else toast("删除失败");
    }));
  };
  await refreshList();
}
async function loadAudit() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="card"><h3>审计日志</h3><p class="desc">去人审后唯一可追溯性来源（部署 / 应用修正均留痕）。</p><div id="audit-list"><div class="empty">加载中…</div></div></div>`;
  try {
    const r = await api("GET", "/audit?limit=100");
    const items = r.data?.audit || [];
    if (!items.length) { $("#audit-list").innerHTML = `<div class="empty">暂无审计记录。</div>`; return; }
    $("#audit-list").innerHTML = `<table class="tbl"><thead><tr><th>时间</th><th>类型</th><th>详情</th></tr></thead><tbody>` +
      items.map((a) => `<tr><td class="mono">${esc((a.ts || a.time || "").toString().slice(0, 19))}</td><td>${esc(a.type || "")}</td><td class="desc">${esc(JSON.stringify(a.data !== undefined ? a.data : a))}</td></tr>`).join("") +
      `</tbody></table>`;
  } catch (e) { $("#audit-list").innerHTML = errBox(e.message, loadAudit); }
}

// 首次运行免责（C11/C21）
function showFirstRun() {
  const mask = $("#firstRunMask");
  if (!mask) return;
  mask.hidden = false;
  const ack = $("#firstRunAck");
  const accept = $("#firstRunAccept");
  ack.onchange = () => { accept.disabled = !ack.checked; };
  accept.onclick = async () => {
    try {
      const r = await api("POST", "/first-run", {});
      if (r.ok) mask.hidden = true;
      else toast("确认失败：" + (r.data?.error || r.status));
    } catch (e) { toast("确认失败：" + e.message); }
  };
}

// ── ACP 对等令牌管理（DEV-acp-webui，对接 webui.py 的 /api/acp/tokens）──
async function loadAcpTokens() {
  const v = $("#view-acp_tokens");
  v.innerHTML = `<div class="view-head"><h2>ACP 对等令牌</h2>
    <span class="sub">拓扑 X 对等（autoflow ↔ memory-worker）· 明文仅签发时显示一次</span>
    <button class="btn primary" id="acpCreateBtn">＋ 新建令牌</button></div>
    <div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:12px">
      <div style="flex:1;min-width:0">
        <div class="meta">ACP 功能开关</div>
        <div class="sub">关闭后 /acp 停止服务、delegate / ask_llm 工具返回禁用提示（免重启生效）。</div>
      </div>
      <label style="display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap">
        <input type="checkbox" id="acpEnabled" style="width:18px;height:18px" />
        <span class="sub" id="acpEnabledState">读取中…</span>
      </label>
    </div>
    <div id="acpOutbound" class="card" style="margin:14px 0">加载出向配置…</div>
    <div id="acpList" class="empty">加载中…</div>`;
  const btn = $("#acpCreateBtn");
  if (btn) btn.onclick = showCreateAcpToken;
  const sw = $("#acpEnabled");
  if (sw) {
    try {
      const er = await api("GET", "/acp/enabled");
      sw.checked = !!(er.data && er.data.enabled);
    } catch (e) { sw.checked = true; }
    const st = $("#acpEnabledState");
    if (st) st.textContent = sw.checked ? "已启用" : "已关闭";
    sw.onchange = async () => {
      try {
        const r = await api("PUT", "/acp/enabled", { enabled: sw.checked });
        if (!r.ok) throw new Error(r.data?.error || "切换失败");
        toast(sw.checked ? "ACP 已启用" : "ACP 已关闭");
        if (st) st.textContent = sw.checked ? "已启用" : "已关闭";
      } catch (e) {
        toast(e.message || "切换失败");
        sw.checked = !sw.checked; // 回滚 UI
        if (st) st.textContent = sw.checked ? "已启用" : "已关闭";
      }
    };
  }
  try {
    const r = await api("GET", "/acp/tokens");
    if (!r.ok) throw new Error(r.data?.error || "加载失败");
    renderAcpTokens(r.data.tokens || []);
  } catch (e) {
    const box = $("#acpList");
    if (box) box.innerHTML = errBox(e.message || "加载失败", loadAcpTokens);
  }
  renderAcpOutbound();
}

async function renderAcpOutbound() {
  const box = $("#acpOutbound");
  if (!box) return;
  try {
    const r = await api("GET", "/settings/connections");
    if (!r.ok) throw new Error(r.data?.error || "加载失败");
    const g = (r.data?.groups || []).find((x) => x.id === "memory");
    if (!g) {
      box.innerHTML = `<div class="empty">当前网关未启用 Memory-Agent 连接字段，请确认后端版本 ≥ 本改动。</div>`;
      return;
    }
    const urlF = g.fields.find((f) => f.key === "MEMORY_WORKER_ACP_URL");
    const tokF = g.fields.find((f) => f.key === "MEMORY_WORKER_ACP_TOKEN");
    box.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <div>
        <div class="meta">出向委派配置（autoflow → memory-agent）</div>
        <div class="sub">网关调用对端 /acp 所需的地址与令牌，保存后立即热生效。</div>
      </div>
    </div>
    <div class="field">
      <label>${esc(urlF.label)} ${urlF.configured
        ? `<span class="conn-src">· 已设置</span>`
        : `<span class="conn-src">· 未设置</span>`}</label>
      <input id="acpOutUrl" value="${esc(urlF.value || "")}"
             placeholder="${esc(urlF.placeholder || "http://host:port/acp")}">
      ${urlF.hint ? `<p class="conn-hint">${esc(urlF.hint)}</p>` : ""}
    </div>
    <div class="field" style="margin-top:10px">
      <label>${esc(tokF.label)} ${tokF.configured
        ? `<span class="conn-src">· 已设置 ${tokF.length || 0} 字符</span>`
        : `<span class="conn-src">· 未设置</span>`}</label>
      <input type="password" id="acpOutToken" autocomplete="new-password"
             placeholder="${tokF.configured ? "留空表示不修改" : "acp_..."}">
      ${tokF.hint ? `<p class="conn-hint">${esc(tokF.hint)}</p>` : ""}
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap">
      <button class="btn primary sm" id="acpOutSave">保存</button>
      <button class="btn sm" id="acpOutTest">测试连接</button>
      <span class="sub" id="acpOutResult"></span>
    </div>`;
    $("#acpOutSave").onclick = async () => {
      const patch = {};
      const u = $("#acpOutUrl").value.trim();
      const t = $("#acpOutToken").value.trim();
      if (u) patch.MEMORY_WORKER_ACP_URL = u;
      if (t) patch.MEMORY_WORKER_ACP_TOKEN = t;
      const out = $("#acpOutResult");
      out.textContent = "保存中…";
      try {
        const sr = await api("PUT", "/settings/connections", patch);
        if (!sr.ok) throw new Error(sr.data?.error || sr.status);
        toast("已保存并生效");
        await renderAcpOutbound();
      } catch (e) { out.textContent = "保存失败：" + e.message; }
    };
    $("#acpOutTest").onclick = async () => {
      const out = $("#acpOutResult");
      out.textContent = "测试中…";
      try {
        const tr = await api("POST", "/settings/connections/test", { targets: ["memory"] });
        if (!tr.ok) throw new Error(tr.data?.error || tr.status);
        const d = (tr.data?.results || {}).memory || {};
        out.innerHTML = d.ok
          ? `✅ ${esc(d.detail || "连接正常")}`
          : `❌ ${esc(d.error || "连接失败")}`;
      } catch (e) { out.textContent = "测试失败：" + e.message; }
    };
  } catch (e) {
    box.innerHTML = `<div class="empty">${errBox(e.message || "加载失败", renderAcpOutbound)}</div>`;
  }
}

function renderAcpTokens(tokens) {
  const box = $("#acpList");
  if (!box) return;
  if (!tokens.length) {
    box.innerHTML = `<div class="empty">暂无 ACP 令牌。点「＋ 新建令牌」生成第一个（明文仅显示一次）。</div>`;
    return;
  }
  const rows = tokens.map((t) => {
    const last4 = t.token_id ? t.token_id.slice(-4) : "";
    const masked = "acp_****" + last4;
    const st = t.status === "active" ? badge("ok", "活跃") : badge("warn", "已吊销");
    const last = t.last_seen ? fmtTime(t.last_seen) : "从未使用";
    const revBtn = t.status === "active"
      ? `<button class="btn sm" data-rev="${esc(t.token_id)}">吊销</button>` : `<span class="sub">已吊销</span>`;
    const delBtn = t.status === "active"
      ? `<button class="btn sm danger" data-del="${esc(t.token_id)}">删除</button>`
      : `<button class="btn sm" disabled>已删除</button>`;
    return `<div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:10px">
      <div style="flex:1;min-width:0">
        <div class="meta">${esc(t.name)}</div>
        <div class="sub">${masked} ｜ 创建 ${fmtTime(t.created_at)} ｜ 最近使用 ${last}</div>
        ${t.notes ? `<div class="desc" style="margin-top:4px">${esc(t.notes)}</div>` : ""}
      </div>
      <div style="display:flex;gap:8px;align-items:center;white-space:nowrap">${st}${revBtn}${delBtn}</div>
    </div>`;
  }).join("");
  box.innerHTML = `<div>${rows}</div>`;
  $$('[data-rev]').forEach((b) => (b.onclick = () => revokeAcpToken(b.dataset.rev)));
  $$('[data-del]').forEach((b) => (b.onclick = () => deleteAcpToken(b.dataset.del)));
}

function showCreateAcpToken() {
  modal("新建 ACP 令牌", `
    <div class="desc">生成用于拓扑 X 对等（memory-worker）的 acp_ 令牌。明文仅显示这一次，请立即写入对端 <code>AUTOFLOW_ACP_TOKEN</code>。</div>
    <div class="form" style="margin-top:10px">
      <label class="lbl">名称（必填）</label>
      <input id="acpName" class="inp" placeholder="如 memory-worker" />
      <label class="lbl" style="margin-top:8px">备注（可选）</label>
      <textarea id="acpNotes" class="inp" rows="2" placeholder="用途说明"></textarea>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" id="acpCancel">取消</button>
      <button class="btn primary" id="acpSubmit">生成</button>
    </div>`);
  $("#acpCancel").onclick = closeModal;
  $("#acpSubmit").onclick = async () => {
    const name = $("#acpName").value.trim();
    if (!name) { toast("名称必填"); return; }
    const notes = $("#acpNotes").value.trim();
    try {
      const r = await api("POST", "/acp/tokens", { name, notes });
      if (!r.ok) throw new Error(r.data?.error || "创建失败");
      showAcpTokenOnce(r.data.token);
    } catch (e) { toast(e.message || "创建失败"); }
  };
}

function showAcpTokenOnce(tok) {
  const plain = tok && tok.acp_token ? tok.acp_token : "";
  modal("ACP 令牌已生成（仅此一次）", `
    <div class="desc">请立即复制并写入对端（memory-worker）的 <code>AUTOFLOW_ACP_TOKEN</code> 环境变量。关闭后不可再查看。</div>
    <div class="card" style="word-break:break-all;font-family:monospace;padding:10px;margin-top:8px">${esc(plain)}</div>
    <div class="modal-foot">
      <button class="btn ghost" id="acpClose">我已保存，关闭</button>
      <button class="btn primary" id="acpCopy">复制</button>
    </div>`);
  $("#acpCopy").onclick = () => {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(plain).then(() => toast("已复制"), () => toast("复制失败，请手动选择"));
    } else { toast("复制失败，请手动选择"); }
  };
  $("#acpClose").onclick = () => { closeModal(); loadAcpTokens(); };
}

async function revokeAcpToken(id) {
  if (!confirm("吊销该 ACP 令牌？吊销后该对端无法再调 /acp（哈希保留用于审计）。")) return;
  try {
    const r = await api("POST", `/acp/tokens/${id}/revoke`);
    if (!r.ok) throw new Error(r.data?.error || "吊销失败");
    toast("已吊销");
    loadAcpTokens();
  } catch (e) { toast(e.message || "吊销失败"); }
}

async function deleteAcpToken(id) {
  if (!confirm("⚠️ 物理删除该 ACP 令牌？含哈希一并抹除，不可恢复。确定？")) return;
  try {
    const r = await api("DELETE", `/acp/tokens/${id}`);
    if (!r.ok) throw new Error(r.data?.error || "删除失败");
    toast("已删除");
    loadAcpTokens();
  } catch (e) { toast(e.message || "删除失败"); }
}

// ── LLM 设置（DEV-llm-ui-pool-test：可视化账号池 + 测试按钮）──
async function loadLlmSettings() {
  const v = $("#view-llm_settings");
  v.innerHTML = `<div class="view-head"><h2>LLM 设置</h2>
    <span class="sub">OpenAI 兼容 /chat/completions · 配置落本地文件，密钥不回显</span></div>
    <div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:12px">
      <div style="flex:1;min-width:0">
        <div class="meta">启用 LLM 助手</div>
        <div class="sub">关闭后「LLM 助手」页不可用（免重启生效）。</div>
      </div>
      <label style="display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap">
        <input type="checkbox" id="llmEnabled" style="width:18px;height:18px" />
        <span class="sub" id="llmEnabledState">读取中…</span>
      </label>
    </div>
    <div class="card account-pool" style="margin-bottom:12px">
      <div class="meta" style="font-size:15px;font-weight:600">内置大模型代理池</div>
      <div class="sub" style="margin:2px 0 10px">按列表顺序依次尝试，遇限流 / 超时 / 鉴权失败自动切换下一个。优先级 = 列表顺序（可上移 / 下移）。</div>
      <div class="conn-actions" style="margin-bottom:10px">
        <button class="btn sm primary" id="llmAddBackend">+ 新增后端</button>
        <button class="btn sm" id="llmTestAll">测试全部</button>
        <span class="sub" id="llmTestAllState"></span>
      </div>
      <div id="llmPool"></div>
      <details style="margin-top:8px">
        <summary class="sub" style="cursor:pointer">向后兼容：单后端（回落单，可选）</summary>
        <div class="form" style="margin-top:8px">
          <label class="lbl">Base URL</label>
          <input id="llmUrl" class="inp" placeholder="https://api.openai.com/v1" />
          <label class="lbl" style="margin-top:8px">API Key</label>
          <input id="llmKey" class="inp" type="password" placeholder="留空 = 不修改（已保存密钥保留）" autocomplete="off" />
          <label class="lbl" style="margin-top:8px">Model</label>
          <input id="llmModel" class="inp" placeholder="gpt-4o-mini" />
        </div>
      </details>
      <div style="margin-top:12px;display:flex;gap:10px">
        <button class="btn primary" id="llmSave">保存</button>
        <span class="sub" id="llmSaveState"></span>
      </div>
    </div>
    <div id="llmCfgState" class="empty">读取中…</div>`;

  const sw = $("#llmEnabled"), st = $("#llmEnabledState");
  const saveBtn = $("#llmSave"), saveState = $("#llmSaveState");
  const pool = $("#llmPool"), addBtn = $("#llmAddBackend");
  const testAllBtn = $("#llmTestAll"), testAllState = $("#llmTestAllState");
  const urlI = $("#llmUrl"), keyI = $("#llmKey"), modelI = $("#llmModel");
  let backends = [];           // 内存模型（来自服务端，密钥为脱敏值）
  let singleKeyTouched = false;

  function renderPool() {
    if (!backends.length) {
      pool.innerHTML = `<div class="sub" style="padding:6px 0">尚未添加后端。点「+ 新增后端」开始；或填写下方单后端。</div>`;
      return;
    }
    pool.innerHTML = backends.map((b, i) => `
      <div class="backend-card" data-i="${i}">
        <div class="bc-head">
          <span class="bc-idx">${i + 1}</span>
          <input class="inp bc-name" value="${esc(b.name || "")}" placeholder="显示名（如 主用 agnes）" />
          <label class="switch-inline"><input type="checkbox" class="bc-enabled" ${b.enabled !== false ? "checked" : ""}/> 启用</label>
          <div class="bc-ops">
            <button class="btn sm ghost bc-up" ${i === 0 ? "disabled" : ""} title="上移">↑</button>
            <button class="btn sm ghost bc-down" ${i === backends.length - 1 ? "disabled" : ""} title="下移">↓</button>
            <button class="btn sm ghost bc-del" title="删除">✕</button>
          </div>
        </div>
        <div class="form bc-form">
          <label class="lbl">模型 Model</label>
          <input class="inp bc-model" value="${esc(b.model || "")}" placeholder="如 agnes-2.0-flash" />
          <label class="lbl">API 地址</label>
          <input class="inp bc-url" value="${esc(b.url || "")}" placeholder="https://api.xxx/v1" />
          <label class="lbl">API Key</label>
          <div class="conn-row">
            <input class="inp bc-key" type="password" placeholder="留空 = 保留已保存密钥" autocomplete="off" />
            <button class="btn sm ghost bc-show" type="button" title="显示/隐藏">👁</button>
          </div>
          <div class="bc-params">
            <div><label class="lbl">温度</label><input class="inp bc-temp" type="number" step="0.1" value="${b.temperature ?? 0.7}" /></div>
            <div><label class="lbl">最大 tokens</label><input class="inp bc-maxtok" type="number" value="${b.max_tokens ?? 4096}" /></div>
            <div><label class="lbl">超时(秒)</label><input class="inp bc-timeout" type="number" value="${b.timeout ?? 120}" /></div>
          </div>
          <div class="conn-actions"><button class="btn sm bc-test" type="button">测试这条</button><span class="bc-test-res sub"></span></div>
        </div>
      </div>`).join("");
    pool.querySelectorAll(".backend-card").forEach((card) => {
      const i = parseInt(card.dataset.i, 10);
      const b = backends[i];
      card.querySelector(".bc-name").addEventListener("input", (e) => { b.name = e.target.value; });
      card.querySelector(".bc-model").addEventListener("input", (e) => { b.model = e.target.value.trim(); });
      card.querySelector(".bc-url").addEventListener("input", (e) => { b.url = e.target.value.trim(); });
      card.querySelector(".bc-enabled").addEventListener("change", (e) => { b.enabled = e.target.checked; });
      card.querySelector(".bc-temp").addEventListener("input", (e) => { b.temperature = parseFloat(e.target.value) || 0.7; });
      card.querySelector(".bc-maxtok").addEventListener("input", (e) => { b.max_tokens = parseInt(e.target.value, 10) || 4096; });
      card.querySelector(".bc-timeout").addEventListener("input", (e) => { b.timeout = parseInt(e.target.value, 10) || 120; });
      const keyInput = card.querySelector(".bc-key");
      keyInput.addEventListener("input", (e) => { b.api_key = e.target.value; b._keyTouched = true; });
      card.querySelector(".bc-show").addEventListener("click", () => {
        keyInput.type = keyInput.type === "password" ? "text" : "password";
      });
      card.querySelector(".bc-up").addEventListener("click", () => { if (i > 0) { [backends[i - 1], backends[i]] = [backends[i], backends[i - 1]]; renderPool(); } });
      card.querySelector(".bc-down").addEventListener("click", () => { if (i < backends.length - 1) { [backends[i + 1], backends[i]] = [backends[i], backends[i + 1]]; renderPool(); } });
      card.querySelector(".bc-del").addEventListener("click", () => { backends.splice(i, 1); renderPool(); });
      card.querySelector(".bc-test").addEventListener("click", () => testOne(i, card));
    });
  }

  function buildBackendPayload(i) {
    const b = backends[i];
    const p = {
      name: (b.name || "").trim(),
      model: (b.model || "").trim(),
      url: (b.url || "").trim(),
      enabled: b.enabled !== false,
      temperature: b.temperature ?? 0.7,
      max_tokens: b.max_tokens ?? 4096,
      timeout: b.timeout ?? 120,
    };
    if (b._keyTouched) p.api_key = b.api_key;  // 用户新填（含显式清空）→ 覆盖/清空
    return p;
  }

  async function testOne(i, card) {
    const b = backends[i];
    const resEl = card.querySelector(".bc-test-res");
    resEl.textContent = "测试中…";
    const payload = {
      name: (b.name || "").trim(),
      model: (b.model || "").trim(),
      url: (b.url || "").trim(),
      // 未改动密钥 → 回传掩码让服务端找回真实 key；改动过 → 用新值
      api_key: b._keyTouched ? (b.api_key || "") : (b.api_key && b.api_key.indexOf("****") >= 0 ? b.api_key : ""),
    };
    if (!payload.url || !payload.model) { resEl.textContent = "⚠ 需填 URL 与 Model"; return; }
    try {
      const r = await api("POST", "/llm/test", { scope: "backend", backend: payload });
      const res = (r.data && r.data.backends && r.data.backends[0]) || {};
      if (res.connected) resEl.textContent = "✓ 连通";
      else resEl.textContent = "✕ " + (res.error || "失败");
    } catch (e) {
      resEl.textContent = "✕ " + (e.message || "测试失败");
    }
  }

  async function testAll() {
    testAllState.textContent = "测试中…";
    try {
      const r = await api("POST", "/llm/test", { scope: "all" });
      if (!r.ok) throw new Error(r.data?.error || "测试失败");
      const backs = (r.data && r.data.backends) || [];
      testAllState.textContent = r.data.message || "";
      backs.forEach((hit) => {
        const card = pool.querySelector(`.backend-card[data-i="${backends.findIndex((x) => (x.url || "").trim() === hit.endpoint || (x.name || "").trim() === hit.name)}"]`);
        if (card) {
          const resEl = card.querySelector(".bc-test-res");
          resEl.textContent = hit.connected ? "✓ 连通" : ("✕ " + (hit.error || "失败"));
        }
      });
    } catch (e) {
      testAllState.textContent = "✕ " + (e.message || "测试失败");
    }
  }

  try {
    const r = await api("GET", "/llm/config");
    if (!r.ok) throw new Error(r.data?.error || "加载失败");
    const c = r.data || {};
    sw.checked = !!c.enabled;
    st.textContent = c.enabled ? "已启用" : "已关闭";
    backends = (c.backends && c.backends.length) ? c.backends.map((b) => ({
      name: b.name || "", model: b.model || "", url: b.url || "",
      enabled: b.enabled !== false, api_key: b.api_key || "",
      temperature: 0.7, max_tokens: 4096, timeout: 120,
    })) : [];
    urlI.value = c.api_url || "";
    modelI.value = c.model || "";
    renderPool();
    $("#llmCfgState").innerHTML = c.configured
      ? `<div class="desc">✅ 后端已配置（${esc(c.model || (backends[0] && backends[0].model) || "")}）。保存后免重启生效。</div>`
      : `<div class="desc">⚠️ 尚未配置后端。填好地址 / API Key / Model 后点保存。</div>`;
  } catch (e) {
    $("#llmCfgState").innerHTML = errBox(e.message || "加载失败", loadLlmSettings);
    sw.checked = false; st.textContent = "读取失败";
  }

  if (sw) sw.onchange = async () => {
    try {
      const r = await api("PUT", "/llm/config", { enabled: sw.checked });
      if (!r.ok) throw new Error(r.data?.error || "切换失败");
      st.textContent = sw.checked ? "已启用" : "已关闭";
      toast(sw.checked ? "LLM 已启用" : "LLM 已关闭");
    } catch (e) {
      toast(e.message || "切换失败");
      sw.checked = !sw.checked;
      st.textContent = sw.checked ? "已启用" : "已关闭";
    }
  };
  if (addBtn) addBtn.onclick = () => {
    backends.push({ name: "", model: "", url: "", enabled: true, api_key: "", temperature: 0.7, max_tokens: 4096, timeout: 120 });
    renderPool();
  };
  if (testAllBtn) testAllBtn.onclick = testAll;
  if (keyI) keyI.addEventListener("input", () => { singleKeyTouched = true; });
  if (saveBtn) saveBtn.onclick = async () => {
    saveState.textContent = "保存中…";
    const body = {
      enabled: sw.checked,
      backends: backends.map((_, i) => buildBackendPayload(i)),
      api_url: urlI.value.trim(),
      model: modelI.value.trim(),
    };
    if (singleKeyTouched) body.api_key = keyI.value;  // 改动过才传；否则服务端保留单后端旧 key
    try {
      const r = await api("PUT", "/llm/config", body);
      if (!r.ok) throw new Error(r.data?.error || "保存失败");
      saveState.textContent = "✅ 已保存";
      toast("LLM 配置已保存");
      keyI.value = ""; singleKeyTouched = false;
      backends.forEach((b) => { b._keyTouched = false; });
      const cs = $("#llmCfgState");
      if (cs) cs.innerHTML = `<div class="desc">✅ 后端已配置。保存后免重启生效。</div>`;
    } catch (e) {
      saveState.textContent = "❌ " + (e.message || "保存失败");
      toast(e.message || "保存失败");
    }
  };
}

// ── LLM 助手（DEV-llm-ui-pool-test：气泡对话 + localStorage 持久化）──
const LLM_CHAT_KEY = 'af.llm_agent.messages';
let _llmHistory = null;  // null = 尚未从 localStorage 恢复（切 tab 不再清空）
function _loadLlmChat() {
  try { const s = localStorage.getItem(LLM_CHAT_KEY); const a = s ? JSON.parse(s) : []; return Array.isArray(a) ? a.slice(-50) : []; }
  catch { return []; }
}
function _saveLlmChat() {
  try { localStorage.setItem(LLM_CHAT_KEY, JSON.stringify((_llmHistory || []).slice(-50))); } catch {}
}

async function loadLlmAgent() {
  const v = $("#view-llm_agent");
  let r;
  try {
    r = await api("GET", "/llm/config");
    if (!r.ok) throw new Error(r.data?.error || "加载失败");
    if (!r.data?.enabled) {
      v.innerHTML = `<div class="view-head"><h2>LLM 助手</h2></div>
        <div class="empty">LLM 未启用。请先到「LLM 设置」页开启并配置后端。<button class="btn sm" id="llmGoSettings" style="margin-left:10px">去设置</button></div>`;
      const g = $("#llmGoSettings");
      if (g) g.onclick = () => setTab("llm_settings");
      return;
    }
  } catch (e) {
    v.innerHTML = `<div class="empty">${errBox(e.message || "加载失败", loadLlmAgent)}</div>`;
    return;
  }
  if (_llmHistory === null) _llmHistory = _loadLlmChat();
  // 后端列表用于「选择模型」下拉
  const llmBackends = ((r.data && r.data.backends) || []).map((b, i) => ({
    i, name: b.name || b.model || `后端 ${i + 1}`, model: b.model || "", enabled: b.enabled !== false,
  })).filter((b) => b.enabled);
  const modelOptions = llmBackends.length
    ? `<option value="-1">默认模型</option>` + llmBackends.map((b) => `<option value="${b.i}">${esc(b.name)} (${esc(b.model)})</option>`).join("")
    : `<option value="-1">默认模型</option>`;
  v.innerHTML = `<div class="view-head"><h2>LLM 助手</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span class="badge env">内置大模型代理池</span>
      <button class="btn sm ghost" id="llmClear">清空对话</button>
    </div></div>
    <div id="llmChat" class="chat"></div>
    <div class="chat-input-bar" id="llmInputBar">
      <div class="input-tools">
        <select class="tool-select" id="llmBuild" title="构建/路由模式">
          <option value="autoflow">Autoflow</option>
          <option value="acp">memory-agent ACP</option>
        </select>
        <select class="tool-select" id="llmModel" title="选择模型">${modelOptions}</select>
      </div>
      <textarea id="llmInput" rows="2" placeholder="随便问点什么，/ 可查看命令，@ 可添加上下文..."></textarea>
      <button class="btn primary send-btn" id="llmSend">➤</button>
    </div>`;
  const chat = $("#llmChat"), input = $("#llmInput"), send = $("#llmSend");
  const clear = $("#llmClear");
  const buildSel = $("#llmBuild"), modelSel = $("#llmModel");
  // 模式切换时同步提示与可用状态
  function refreshLlmInputState() {
    const isAcp = buildSel && buildSel.value === "acp";
    if (modelSel) modelSel.disabled = isAcp;
    input.placeholder = isAcp
      ? "已选择 memory-agent ACP，消息将直接委派给 memory-agent..."
      : "随便问点什么，/ 可查看命令，@ 可添加上下文...";
  }
  if (buildSel) buildSel.addEventListener("change", refreshLlmInputState);
  refreshLlmInputState();
  // 输入框随内容自动增高（最多 160px 后内部滚动）
  function autoGrow(){ input.style.height="auto"; input.style.height=Math.min(input.scrollHeight,160)+"px"; }
  input.addEventListener("input", autoGrow); autoGrow();

  function renderBubble(msg) {
    if (msg.role === "error") {
      const row = document.createElement("div");
      row.className = "chat-row error";
      row.innerHTML = `<div class="chat-bubble error">${esc(msg.error || "出错了")}</div>`;
      chat.appendChild(row); chat.scrollTop = chat.scrollHeight; return;
    }
    const row = document.createElement("div");
    row.className = "chat-row " + (msg.role === "user" ? "user" : "ai");
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble " + (msg.role === "user" ? "user" : "ai");
    if (msg.role === "ai") {
      const avatar = document.createElement("div");
      avatar.className = "chat-avatar"; avatar.textContent = "🤖";
      const tools = (msg.tool_calls && msg.tool_calls.length) ? msg.tool_calls : null;
      let html = "";
      if (tools) {
        const stepHtml = tools.map((s) => {
          const args = typeof s.args === "string" ? s.args : JSON.stringify(s.args || {});
          return `<div class="step"><code>🔧 ${esc(s.tool)}</code> <span class="sub">${esc(args)}</span>
            <div class="desc" style="margin-top:4px;white-space:pre-wrap">${esc(s.result || "")}</div></div>`;
        }).join("");
        html += `<details class="chat-tool-calls" style="margin-bottom:6px"><summary class="sub">调用了 ${tools.length} 个工具</summary>${stepHtml}</details>`;
      }
      html += `<div class="chat-text">${esc(msg.content || "")}</div>`;
      bubble.innerHTML = html;
      row.appendChild(avatar); row.appendChild(bubble);
    } else {
      bubble.innerHTML = `<div class="chat-text">${esc(msg.content || "")}</div>`;
      row.appendChild(bubble);
    }
    chat.appendChild(row); chat.scrollTop = chat.scrollHeight;
  }

  if (!_llmHistory.length) {
    chat.innerHTML = `<div class="empty" style="padding:24px 10px">和内置助手聊聊吧——它能调用网关工具查询与控制你的智能家居。</div>`;
  } else {
    _llmHistory.forEach(renderBubble);
  }

  if (clear) clear.onclick = () => {
    _llmHistory = []; _saveLlmChat();
    chat.innerHTML = `<div class="empty" style="padding:24px 10px">对话已清空。</div>`;
  };

  async function sendMsg() {
    const text = input.value.trim();
    if (!text) return;
    const mode = buildSel ? buildSel.value : "autoflow";
    const backendIndex = modelSel ? parseInt(modelSel.value, 10) : -1;
    input.value = "";
    renderBubble({ role: "user", content: text });
    _llmHistory.push({ role: "user", content: text }); _saveLlmChat();
    send.disabled = true; send.textContent = mode === "acp" ? "委派中…" : "思考中…";
    try {
      const body = { message: text, history: _llmHistory.slice(0, -1), mode };
      if (mode === "autoflow" && backendIndex >= 0) body.backend_index = backendIndex;
      const r = await api("POST", "/llm/chat", body, { timeout: 300000 });
      if (!r.ok) throw new Error(r.data?.error || "调用失败");
      const steps = (r.data && r.data.steps) || [];
      const aiMsg = { role: "assistant", content: r.data.text || "", tool_calls: steps };
      renderBubble(aiMsg);
      _llmHistory.push(aiMsg); _saveLlmChat();
    } catch (e) {
      const errMsg = { role: "error", error: e.message || "调用失败" };
      renderBubble(errMsg);
      _llmHistory.push(errMsg); _saveLlmChat();
    } finally {
      send.disabled = false; send.textContent = "发送";
    }
  }
  if (send) send.onclick = sendMsg;
  if (input) input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
}

// 启动
(async () => {
  try {
    const c = await api("GET", "/config");
    $("#envBadge").textContent = "env: " + (c.data?.env || "?");
  } catch { $("#envBadge").textContent = "env: ?"; }
  setTab("dashboard");
  try {
    const fr = await api("GET", "/first-run");
    if (!fr.data?.accepted) showFirstRun();
  } catch {}
})();
