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
  const url = "/api" + path;
  const ctrl = new AbortController();
  const timeoutMs = opts.timeout || 25000;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  // I-9：会话 Cookie 是环境权限，所有写请求带同源自定义头，防 CSRF 跨站调用。
  const fetchOpts = { method, headers: { "X-Requested-With": "autoflow" }, signal: ctrl.signal, credentials: "same-origin" };
  if (body !== undefined) {
    fetchOpts.headers["Content-Type"] = "application/json";
    fetchOpts.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(url, fetchOpts);
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (res.status === 401) {
      // 未登录 / 会话失效：交给账号模块弹登录或注册框（后端区分 401=要登录 / 403=权限不足）
      if (window.__afAuth) window.__afAuth.onUnauthorized();
      throw new Error((data && data.error) || "需要登录");
    }
    if (res.status === 403) throw new Error((data && data.error) || "请求被拒绝");
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error(`请求超时（${timeoutMs / 1000}秒）：网关可能正忙或网络不通，请重试`);
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

function toast(msg, type) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (type ? " " + type : "");
  t.hidden = false;
  clearTimeout(toast._t);
  const dur = type === "error" ? 4000 : type === "warn" ? 3200 : 2600;
  toast._t = setTimeout(() => (t.hidden = true), dur);
}
function modal(title, html, confirmCb, closeLabel) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  // Handle footer with buttons when confirmCb or closeLabel provided
  const footer = $("#modalFoot");
  if (footer) {
    if (confirmCb || closeLabel) {
      footer.hidden = false;
      const closeBtnText = closeLabel || "关闭";
      const confirmBtn = confirmCb
        ? `<button class="btn primary" id="tpl-create-confirm">确认</button>`
        : "";
      footer.innerHTML = `${confirmBtn}<button class="btn ghost" id="modal-close-btn">${esc(closeBtnText)}</button>`;
    } else {
      footer.hidden = true;
      footer.innerHTML = "";
    }
  }
  $("#modalMask").hidden = false;
  if (confirmCb) {
    const confirmBtn = $("#tpl-create-confirm");
    if (confirmBtn) {
      confirmBtn.onclick = async () => {
        const ok = await confirmCb();
        if (ok !== false) closeModal();
      };
    }
  }
  const closeBtn = $("#modal-close-btn");
  if (closeBtn) closeBtn.onclick = closeModal;
}
function closeModal() { $("#modalMask").hidden = true; }

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// BUG-3: HTTP 环境下 navigator.clipboard 不可用，提供 execCommand 降级方案
async function safeCopy(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* ignore */ }
  }
  // fallback: 创建临时 textarea 选中后 execCommand
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(ta);
  return ok;
}
// BUG-2: PWA 独立模式下 confirm/prompt 不可用，使用自定义模态框替代
function confirmDialog(msg) {
  return new Promise(resolve => {
    const html = `<p style="line-height:1.6;margin:0">${esc(msg)}</p>`;
    modal("确认操作", html, async () => resolve(true), "取消");
    // 覆盖 footer 按钮语义
    const foot = $("#modalFoot");
    if (foot) {
      foot.innerHTML = `<button class="btn ghost" id="confirmCancelBtn">取消</button><button class="btn primary" id="confirmOkBtn">确认</button>`;
      $("#confirmCancelBtn").onclick = () => { closeModal(); resolve(false); };
      $("#confirmOkBtn").onclick = () => { closeModal(); resolve(true); };
    }
  });
}
function promptDialog(msg, defaultValue) {
  return new Promise(resolve => {
    const html = `
      <p style="line-height:1.6;margin:0 0 12px">${esc(msg)}</p>
      <div class="field">
        <input type="text" id="promptInput" class="input" value="${esc(defaultValue || '')}" placeholder="输入内容...">
      </div>`;
    modal("请输入", html, async () => {
      const val = $("#promptInput")?.value.trim() || "";
      resolve(val || null);
    }, "取消");
    const foot = $("#modalFoot");
    if (foot) {
      foot.innerHTML = `<button class="btn ghost" id="promptCancelBtn">取消</button><button class="btn primary" id="promptOkBtn">确定</button>`;
      $("#promptCancelBtn").onclick = () => { closeModal(); resolve(null); };
      $("#promptOkBtn").onclick = async () => {
        const val = $("#promptInput")?.value.trim() || "";
        closeModal();
        resolve(val || null);
      };
    }
    setTimeout(() => { const inp = $("#promptInput"); if (inp) { inp.focus(); inp.select(); } }, 50);
  });
}
function badge(cls, text) { return `<span class="badge ${cls}">${esc(text)}</span>`; }
const MODES = ["normal", "expert", "developer"];
function modeLabel(m) {
  return ({
    normal: "标准模式（仅 DSL 提案）",
    expert: "高级模式（可直接部署）",
    developer: "管理员模式（运维/调试）"
  })[m] || m;
}
// 身份模式 → MCP 端点 path（用于新建后展示正确连接地址）
function endpointForMode(m) {
  return ({ normal: "/mcp", expert: "/mcp-white", developer: "/mcp-admin" })[m] || "/mcp";
}
// 新建 Agent 页右侧的身份模式说明面板
function renderAgentModeGuide() {
  return `
    <div class="card guide-panel">
      <h3>身份模式说明</h3>
      <p class="desc">模式决定该 agent 能领什么任务、看到哪些 MCP 工具、能否直接部署。创建后由你在 WebUI 设定，agent 自身不可更改。</p>
      <div class="mode-item">
        <h4>标准模式（normal）：仅提交 DSL，最安全</h4>
        <p>连 <code>/mcp</code>。只能写 DSL 提案，<b>看不到部署刀</b>。所有上线必须经 WebUI 人工批准。默认、最安全，适合公开/不可信的 LLM agent。</p>
      </div>
      <div class="mode-item">
        <h4>高级模式（expert）：可提交 raw flow 并直接部署</h4>
        <p>连 <code>/mcp-white</code>。可手写原生节点、自由部署、使用测试杠杆；双任务池都能领（auto_wb + auto）。适合你信任的内部 agent。</p>
      </div>
      <div class="mode-item">
        <h4>管理员模式（admin）：运维/调试专用</h4>
        <p>连 <code>/mcp-admin</code>。可调用全部工具（含运维刀、测试杠杆、任务池）。仅限网关自身运维身份，普通 agent 不应持有。</p>
      </div>
      <div class="hint">安全模型：模式是部署时由你设定的<b>信任等级</b>，不是 agent 运行时自选——普通模式永远无法绕过 verify+人工批准闸。底层端点隔离（/mcp、/mcp-white、/mcp-admin）保持不变。</div>
    </div>`;
}
function fmtTime(s) {
  if (!s) return "—";
  try { return new Date(s).toLocaleString("zh-CN", { hour12: false }); } catch { return s; }
}

// ── 导航（C1/C3：工作区 + 版本同步已移除，新增设置管理界面）──
const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "deploy_tokens", "api_keys", "templates", "token_stats", "error_kb", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];
function setTab(tab) {
  if (!TABS.includes(tab)) tab = "dashboard";
  $$(".navitem[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  closeMobileSheet();
  TABS.forEach((t) => ($("#view-" + t).hidden = t !== tab));
  if (tab === "dashboard") loadDashboard();
  else if (tab === "tutorials") { if (typeof renderTutorialList === "function") renderTutorialList(); }
  else if (tab === "agents") loadAgents();
  else if (tab === "deploy_tokens") loadDeployTokens();
  else if (tab === "api_keys") loadApiKeys();
  else if (tab === "templates") loadTemplates();
  else if (tab === "token_stats") loadTokenStats();
  else if (tab === "error_kb") loadErrorKB();
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
  else if (tab === "update") loadUpdate();
  // 首次访问引导
  if (typeof checkFirstVisit === "function") checkFirstVisit(tab);
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
      <div class="view-head"><h2>概览</h2><span class="sub">环境 ${esc(c.env || "")} ｜ 普通 ${esc(c.mcp || "")} ｜ 专家 ${esc(c.mcp_white || "")} ｜ 开发者 ${esc(c.mcp_admin || "")}</span></div>
      <div class="grid cols-4">
        <div class="card"><div class="meta">已接入 Agent</div><div class="stat">${counts.agents}</div></div>
        <div class="card"><div class="meta">待确认操作</div><div class="stat">${counts.pending}</div></div>
        <div class="card"><div class="meta">待审原生 flow</div><div class="stat">${counts.raw}</div></div>
        <div class="card"><div class="meta">已部署 flow</div><div class="stat">${counts.deployed}</div></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>快速上手</h3>
        <div class="desc" style="line-height:2">
          AutoFlow 有两种使用方式：<b>Agent 调用 MCP</b>（推荐）或 <b>WebUI 内置 AI 对话</b>。<br>
          1. 到 <b>Agent 管理</b> 创建 Agent，复制接入令牌，配置到 AI 客户端的 MCP 中。<br>
          2. 跟 Agent 说需求（如"晚上 10 点关灯"），它会生成 flow 提案。<br>
          3. 到 <b>提案</b> 查看安全闸结果，确认后点「部署到 NR」。<br>
          4. 部署后在 <b>已部署</b> 可查看状态、安全撤回。flow 不工作时用<b>自动修复</b>。<br>
          5. 不确定怎么操作？打开左侧 <b>教程</b>，有分步图文引导。
        </div>
        <div style="margin-top:10px">
          <button class="btn sm" id="dash-go-tutorial">📚 打开教程</button>
          <button class="btn sm" id="dash-go-agent" style="margin-left:8px">创建 Agent</button>
          <button class="btn sm" id="dash-go-llm" style="margin-left:8px">AI 对话</button>
        </div>
      </div>`;
    const gt = $("#dash-go-tutorial");
    if (gt) gt.onclick = () => setTab("tutorials");
    const ga = $("#dash-go-agent");
    if (ga) ga.onclick = () => setTab("agents");
    const gl = $("#dash-go-llm");
    if (gl) gl.onclick = () => setTab("llm_agent");
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
        <h3>💬 给 Agent 下指令</h3>
        <p class="desc">用大白话给常驻的 DeepSeek 下达任务（点火/查询/编排等）。它用 autoflow 工具执行，完成后经 Bark 回报。给我（WorkBuddy）的指令请仍走远程会话。</p>
        <textarea id="ws-cmd-text" style="width:100%;min-height:72px;border:1px solid var(--border);border-radius:6px;padding:10px;font-size:14px;background:var(--bg);color:var(--text)" placeholder="例如：把书房 H5 场景重新点火一遍；或：查询书房所有灯的当前状态"></textarea>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <button class="btn primary" id="ws-cmd-send" style="flex:0 0 auto">发送</button>
          <span class="meta" id="ws-cmd-hint">Ctrl/⌘ + Enter 发送</span>
        </div>
        <div id="ws-cmd-hist" style="margin-top:12px">${cmds.length ? "" : `<div class="empty">还没有下达过指令。</div>`}</div>
      </div>
      <div class="card ws-decide" style="margin-top:14px">
        <h3>🗳️ 待你决策${decs.length ? `（${decs.length}）` : ""}</h3>
        <p class="desc">Agent 遇到选择题时在此等你决策，选择后自动回灌继续。</p>
        <div id="ws-decisions">${decs.length ? "" : `<div class="empty">暂无待决策项 🎉</div>`}</div>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <div class="card">
          <h3>🎯 总体计划</h3>
          <p class="desc">长期目标 / 里程碑（你在此编辑，agent 一般不动）。</p>
          <textarea id="ws-overall" style="width:100%;min-height:90px;border:1px solid var(--border);border-radius:6px;padding:8px;font-size:13px;background:var(--bg);color:var(--text)" placeholder="例如：M2 完成后跑 H1–H10 评测，验证编译器修复拉升普通模式通过率">${esc(plan.overall || "")}</textarea>
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
      btn.disabled = true; btn.textContent = "发送中…";
      try {
        const r = await api("POST", "/commands", { text });
        if (r.ok) { ta.value = ""; toast("已发送，完成后 Bark 通知"); }
        else toast("失败：" + (r.data?.error || r.status));
      } catch (e) { toast(e.message || "发送失败"); }
      btn.disabled = false; btn.textContent = "发送";
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
    dispatching: ["发送中", "risk-medium"],
    dispatched: ["已送达", "risk-low"],
    failed: ["发送失败", "risk-high"],
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

// ── 部署授权码管理（P4）──
// ── 模板库（v1.5.2）──
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
      if (!(await confirmDialog("确定删除此模板？"))) return;
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
        safeCopy(r.data.dsl).then(ok => {
          if (ok) toast("已复制 DSL", "success"); else toast("复制失败，请手动选择", "error");
        });
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
      <textarea id="tpl-new-dsl" class="input" rows="6" placeholder="场景: ...
触发: {{sensor}} on
动作: light.turn_on({{light}})"></textarea>
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

// ── Token 统计（v1.5.7）──
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
            <div style='width:100%;background:var(--primary);opacity:0.7;border-radius:4px 4px 0 0;height:${Math.max(2, (d.estimated_tokens / maxTokens) * 100)}%' title='${esc(d.estimated_tokens)} tokens'></div>
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

// ── API Key 管理（v1.5.1）──
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
      safeCopy(data.key).then(ok => {
        if (ok) toast("已复制", "success"); else toast("复制失败，请手动选择", "error");
      });
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
  if (!(await confirmDialog("确定吊销此 API Key？吊销后立即失效，Agent 将无法调用网关。"))) return;
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
    <div class="card" style="background:var(--bg-soft);border-left:3px solid var(--primary);margin-bottom:16px">
      <div style="font-weight:600;margin-bottom:8px">📖 授权码使用方法</div>
      <ol style="margin:0;padding-left:20px;line-height:1.8;font-size:13px">
        <li><b>创建授权码</b>：点击右上角「创建授权码」，设置名称、目标 tab（可多选）、有效期和权限。</li>
        <li><b>复制授权码</b>：创建成功后立即复制授权码字符串（只显示一次，丢失后需重新创建）。</li>
        <li><b>告知 Agent</b>：在与 Agent 对话时说"使用授权码 xxxx 部署这个 Flow"，或在 Agent 的 MCP 配置中填入授权码。</li>
        <li><b>Agent 自动部署</b>：Agent 调用 MCP 接口时传入 <code>deploy_token</code> 参数，网关验证通过后自动部署到 Node-RED，无需用户在 WebUI 手动审批。</li>
        <li><b>安全边界</b>：Agent 只能在授权码绑定的 tab 内部署/修改，不会越界到其他 tab；超过节点数阈值或危险操作仍需人工审批。</li>
        <li><b>可回溯</b>：每次部署自动快照，可在授权码详情中查看历史并一键回滚到授权前状态。</li>
      </ol>
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
            目标 tab: <b>${esc((t.target_tabs && t.target_tabs.length) ? t.target_tabs.length + " 个 tab" : (t.target_tab || "不绑定"))}</b> ·
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
    <div class="card" style="background:var(--bg-soft);border-left:3px solid var(--primary);margin-bottom:16px;padding:10px 14px">
      <div style="font-size:12px;line-height:1.6">
        <b>授权码作用</b>：给受信任 Agent 发放后，Agent 可通过 MCP 自动部署 Flow，无需你在 WebUI 手动审批。
        创建后请立即复制授权码并告知 Agent（如"使用授权码 xxxx"），授权码只显示一次。
      </div>
    </div>
    <div class="field">
      <label>名称 *</label>
      <input type="text" id="dt-name" class="input" placeholder="如：客厅自动化 Agent">
    </div>
    <div class="field">
      <label>目标 tab（可多选，可选）</label>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
        <label style="font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap">
          <input type="checkbox" id="dt-tab-bind" style="margin:0"> 绑定到指定 tab
        </label>
        <span class="meta" style="font-size:11px;color:var(--text-muted)">不勾选则走 per_flow 模式，每个 flow 自动创建独立 tab</span>
      </div>
      <div id="dt-tab-list" style="max-height:220px;overflow-y:auto;overflow-x:hidden;border:1px solid var(--border);border-radius:8px;padding:6px 10px;display:none">
        <div style="color:var(--text-muted);font-size:12px;padding:4px 0">加载 tab 列表中…</div>
      </div>
      <div class="meta" style="font-size:11px;color:var(--text-muted);margin-top:4px">勾选后 Agent 只能在这些 tab 部署/修改，不会越界到其他 tab。</div>
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
  `, null, "关闭");

  // 绑定 tab 勾选框切换
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
          `<label style="display:flex;align-items:center;padding:6px 4px;cursor:pointer;border-bottom:1px solid var(--border);gap:8px;min-width:0">
             <input type="checkbox" class="dt-tab-item" value="${esc(t.id)}" style="flex-shrink:0;margin:0;width:16px;height:16px">
             <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px">${esc(t.label)}</span>
             <span style="color:var(--text-muted);font-size:11px;flex-shrink:0;white-space:nowrap">${t.node_count || 0} 节点</span>
           </label>`
        ).join("");
      } else if (list) {
        list.innerHTML = '<div style="color:var(--text-muted);font-size:12px">未找到 tab，请先在 Node-RED 中创建 tab</div>';
      }
    } catch (e) {
      if (tabList) tabList.innerHTML = '<div style="color:var(--danger);font-size:12px">加载 tab 列表失败</div>';
    }
  })();

  $("#dt-create-confirm").onclick = async () => {
    const name = $("#dt-name").value.trim();
    if (!name) { toast("名称不能为空"); return; }
    // 收集勾选的目标 tab
    const targetTabs = [];
    if ($("#dt-tab-bind")?.checked) {
      document.querySelectorAll(".dt-tab-item:checked").forEach(cb => {
        if (cb.value) targetTabs.push(cb.value);
      });
    }
    const targetTab = targetTabs.length ? targetTabs[0] : "";

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
        name, target_tab: targetTab, target_tabs: targetTabs,
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
            <p>目标 tab: <b>${esc((token.target_tabs && token.target_tabs.length) ? token.target_tabs.length + " 个 tab" : (token.target_tab || "不绑定（per_flow 模式）"))}</b></p>
            <p>有效期: ${esc((token.expires_at || "").slice(0, 19).replace("T", " "))}</p>
            <p>权限: ${(token.permissions || []).join(", ")}</p>
          </div>
          <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px;margin-top:12px">
            <div style="font-weight:600;color:#1e40af;margin-bottom:6px;font-size:13px">下一步：告知 Agent</div>
            <p style="font-size:12px;color:#1e3a8a;margin:0;line-height:1.6">
              复制上方授权码，在与 Agent 对话时说：<br>
              <code style="background:#fff;padding:2px 6px;border-radius:4px">"使用授权码 ${esc(token.token_plaintext.slice(0,8))}... 部署这个 Flow"</code><br>
              Agent 会在调用 MCP 时自动传入授权码，网关验证后直接部署，无需你手动审批。
            </p>
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
  if (!(await confirmDialog("确定吊销此授权码吗？吊销后立即失效，正在使用的 Agent 将无法自动部署。"))) return;
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
    if (!snaps.length) {
      modal("快照 - " + (token?.name || tokenId), '<div class="empty">暂无快照记录</div><div style="margin-top:16px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>');
      return;
    }

    // 构建快照选择器选项
    const snapOptions = snaps.map(s =>
      `<option value="${esc(s.snapshot_id)}">${esc(s.label || s.snapshot_id)} · ${esc((s.created_at || "").slice(0, 19).replace("T", " "))} · ${s.type === 'full' ? '全量' : '增量'} · ${s.node_count || 0}节点</option>`
    ).join("");

    modal("快照版本对比 - " + (token?.name || tokenId), `
      <div style="padding:4px 0">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
          <div>
            <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">基准版本 (旧)</label>
            <select id="snapSelect1" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--surface);font-size:13px">
              ${snapOptions}
            </select>
          </div>
          <div>
            <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">目标版本 (新)</label>
            <select id="snapSelect2" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--surface);font-size:13px">
              ${snapOptions}
            </select>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn primary sm" id="btnDiffCompare">🔍 对比差异</button>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="previewModeCheck">
            <span>预览模式（仅查看，不执行）</span>
          </label>
        </div>
        <div id="diffResult" style="margin-top:16px"></div>
      </div>
      <div style="margin-top:16px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>
    `);

    // 默认选择前两个快照
    if (snaps.length >= 2) {
      $("#snapSelect1").value = snaps[1].snapshot_id;
      $("#snapSelect2").value = snaps[0].snapshot_id;
    }

    // 绑定对比按钮
    $("#btnDiffCompare").onclick = async () => {
      const snap1 = $("#snapSelect1").value;
      const snap2 = $("#snapSelect2").value;
      if (!snap1 || !snap2) { toast("请选择两个快照"); return; }
      if (snap1 === snap2) { toast("请选择不同的快照"); return; }

      const previewMode = $("#previewModeCheck").checked;
      $("#diffResult").innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">🔄 正在对比...</div>';

      try {
        const r = await api("GET", `/deploy-tokens/${tokenId}/diff?snapshot_1=${encodeURIComponent(snap1)}&snapshot_2=${encodeURIComponent(snap2)}`);
        if (!r.ok || !r.data?.ok) {
          throw new Error(r.data?.error || "对比失败");
        }
        renderDiffResult(r.data, previewMode);
      } catch (e) {
        $("#diffResult").innerHTML = `<div class="empty err">${esc(e.message)}</div>`;
      }
    };

    // 绑定回滚按钮（在快照列表中）
    snaps.forEach(s => {
      const btn = document.createElement("button");
      btn.className = "btn sm danger";
      btn.style.cssText = "margin-top:8px;";
      btn.textContent = "回滚到此版本";
      btn.onclick = () => rollbackToSnapshot(tokenId, s.snapshot_id);
      // 添加到 modal 底部按钮区域
    });
  } catch (e) {
    toast("加载快照失败：" + e.message);
  }
}

// 渲染 diff 结果
function renderDiffResult(data, previewMode = false) {
  const resultEl = $("#diffResult");
  if (!resultEl) return;

  const fmtTime = (t) => t ? esc(t.slice(0, 19).replace("T", " ")) : "-";

  let html = `
    <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden">
      <div style="padding:12px 16px;background:var(--surface-2);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div>
          <span style="font-weight:600">版本对比结果</span>
          <span class="meta" style="margin-left:8px;font-size:12px">${fmtTime(data.snap1_time)} → ${fmtTime(data.snap2_time)}</span>
        </div>
        <div style="display:flex;gap:16px;font-size:13px">
          <span style="color:var(--ok)">+${data.added_count} 新增</span>
          <span style="color:var(--danger)">-${data.removed_count} 删除</span>
          <span style="color:var(--warn)">~${data.changed_count} 修改</span>
        </div>
      </div>
      <div style="max-height:500px;overflow:auto">
  `;

  // 新增节点
  if (data.added_nodes && data.added_nodes.length) {
    html += `
      <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
        <div style="font-weight:600;color:var(--ok);margin-bottom:8px;font-size:13px">▶ 新增节点 (${data.added_nodes.length})</div>
        <div class="code-box" style="font-size:12px;max-height:200px;overflow:auto">
          ${data.added_nodes.map(n => `
            <div style="padding:4px 0;border-bottom:1px dashed var(--border)">
              <span style="color:var(--text-muted)">[${esc(n.type || "")}]</span>
              <span style="font-weight:500">${esc(n.name || n.id || "")}</span>
              <span style="color:var(--text-muted);margin-left:8px">id: ${esc(n.id || "")}</span>
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  // 删除节点
  if (data.removed_nodes && data.removed_nodes.length) {
    html += `
      <div style="padding:12px 16px;border-bottom:1px solid var(--border);background:var(--danger-weak)">
        <div style="font-weight:600;color:var(--danger);margin-bottom:8px;font-size:13px">✗ 删除节点 (${data.removed_nodes.length})</div>
        <div class="code-box" style="font-size:12px;max-height:200px;overflow:auto;opacity:0.8">
          ${data.removed_nodes.map(n => `
            <div style="padding:4px 0;border-bottom:1px dashed var(--border)">
              <span style="color:var(--text-muted)">[${esc(n.type || "")}]</span>
              <span style="font-weight:500;text-decoration:line-through">${esc(n.name || n.id || "")}</span>
              <span style="color:var(--text-muted);margin-left:8px">id: ${esc(n.id || "")}</span>
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  // 修改的节点
  if (data.changed_details && data.changed_details.length) {
    html += `
      <div style="padding:12px 16px">
        <div style="font-weight:600;color:var(--warn);margin-bottom:8px;font-size:13px">~ 修改节点 (${data.changed_details.length})</div>
        <div style="font-size:12px">
    `;
    data.changed_details.forEach(d => {
      html += `
        <details style="margin-bottom:8px;border:1px solid var(--border);border-radius:8px">
          <summary style="padding:8px 12px;cursor:pointer;font-weight:500;background:var(--surface-2)">
            <span style="color:var(--text-muted)">[${esc(d.type)}]</span>
            ${esc(d.name || d.node_id)}
            <span class="meta" style="margin-left:8px">${d.field_diffs.length} 处变更</span>
          </summary>
          <div style="padding:8px 12px;background:var(--bg);font-size:12px">
      `;
      d.field_diffs.forEach(f => {
        const oldVal = typeof f.old === "object" ? JSON.stringify(f.old).slice(0, 100) : String(f.old ?? "");
        const newVal = typeof f.new === "object" ? JSON.stringify(f.new).slice(0, 100) : String(f.new ?? "");
        html += `
          <div style="padding:4px 0;border-bottom:1px dashed var(--border)">
            <span style="color:var(--text-muted);font-weight:500">${esc(f.field)}:</span>
            ${oldVal !== newVal ? `
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px">
                <div style="color:var(--danger);text-decoration:line-through;opacity:0.7">${esc(oldVal)}</div>
                <div style="color:var(--ok);font-weight:500">${esc(newVal)}</div>
              </div>
            ` : `<span style="color:var(--text-muted)">${esc(oldVal)}</span>`}
          </div>
        `;
      });
      html += `</div></details>`;
    });
    html += `</div></div>`;
  }

  if (!data.added_nodes?.length && !data.removed_nodes?.length && !data.changed_details?.length) {
    html += `<div style="padding:24px;text-align:center;color:var(--text-muted)">两个版本完全一致，无差异</div>`;
  }

  html += `</div></div>`;

  // 预览模式提示
  if (previewMode) {
    html += `<div style="margin-top:12px;padding:10px 14px;background:var(--warn-weak);border:1px solid var(--warn);border-radius:8px;font-size:13px;color:var(--warn)">
      🔒 预览模式：仅查看差异，未执行任何操作
    </div>`;
  }

  resultEl.innerHTML = html;
}

async function rollbackToSnapshot(tokenId, snapshotId) {
  if (!(await confirmDialog("确定回滚到此快照吗？回滚会恢复 tab 到快照时的状态，当前未保存的变更将丢失。回滚前会自动创建新快照。"))) return;
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

async function loadAgents() {
  const v = $("#view-agents");
  v.innerHTML = `<div class="view-head"><h2>Agent 管理</h2><span class="sub">创建和管理接入 AutoFlow 的 AI 客户端</span></div>
    <div class="agent-layout">
      <div class="agent-form">
        <div class="card form-card">
          <h3>新建 Agent</h3>
          <div class="field"><label>名称（如 deepseek++）</label><input id="a-name" placeholder="deepseek++"></div>
          <div class="field"><label>权限模式</label>
            <select id="a-mode">${MODES.map((m) => `<option value="${m}">${modeLabel(m)}</option>`).join("")}</select></div>
          <div class="field"><label>备注</label><textarea id="a-notes" placeholder="可选"></textarea></div>
          <button class="btn primary" id="a-create">生成接入令牌</button>
        </div>
      </div>
      ${renderAgentModeGuide()}
    </div>
    <div id="a-list" style="margin-top:14px"><div class="empty">加载中…</div></div>`;
  $("#a-create").onclick = createAgent;
  try {
    const r = await api("GET", "/agents");
    const list = $("#a-list");
    const agents = (r.data?.agents || []).slice()
      .sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh-Hans-CN"));
    if (!agents.length) { list.innerHTML = `<div class="empty">还没有 Agent，先在上方创建。</div>`; return; }
    list.innerHTML = agents.map((a) => `
      <div class="item">
        <div class="row">
          <div><span class="title">${esc(a.name)}</span> <span class="meta">(${esc(a.agent_id)})</span></div>
          <div>${badge("status-" + a.status, a.status)}</div>
        </div>
        <div class="desc">创建：${fmtTime(a.created_at)} ｜ 最近连接：${fmtTime(a.last_seen)}</div>
        ${badge("mode-" + (a.mode || "normal"), modeLabel(a.mode || "normal"))}
        ${a.notes ? `<div class="desc">备注：${esc(a.notes)}</div>` : ""}
        <div class="actions">
          <button class="btn sm" data-edit="${esc(a.agent_id)}">编辑</button>
          <button class="btn sm" data-regen="${esc(a.agent_id)}">重置接入令牌</button>
          <button class="btn sm danger" data-revoke="${esc(a.agent_id)}">停用</button>
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
  if (!name) return toast("请填写名称");
  const r = await api("POST", "/agents", { name, mode: $("#a-mode").value, notes: $("#a-notes").value });
  if (!r.ok) return toast("创建失败：" + (r.data?.error || r.status));
  const a = r.data.agent;
  const ep = endpointForMode(a.mode);
  const fullUrl = `${location.protocol}//${location.host}${ep}`;
  modal("接入令牌已生成（仅此一次）",
    `<p>请复制下方接入令牌，填入 <b>${esc(a.name)}</b> 的 MCP 配置：</p>
     <div class="code-box">${esc(a.identity_code)}</div>
     <p class="desc">MCP 服务器地址：<code>${esc(fullUrl)}</code><br>请求头：<code>Authorization: Bearer ${esc(a.identity_code)}</code></p>`);
  loadAgents();
}
async function regenAgent(id) {
  if (!(await confirmDialog("重置后旧接入令牌立即失效，确定？"))) return;
  const r = await api("POST", `/agents/${id}/regen`);
  if (!r.ok) return toast("失败：" + (r.data?.error || r.status));
  modal("新接入令牌（仅此一次）", `<div class="code-box">${esc(r.data.identity_code)}</div>`);
  loadAgents();
}
async function revokeAgent(id) {
  if (!(await confirmDialog("停用后该 Agent 无法连接网关（行仍保留，可恢复），确定？"))) return;
  const r = await api("POST", `/agents/${id}/revoke`);
  toast(r.ok ? "已停用" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadAgents();
}
async function deleteAgent(id) {
  if (!(await confirmDialog("⚠️ 彻底删除：该 Agent 将从身份库彻底移除（含接入令牌哈希），不可恢复。确定？"))) return;
  const r = await api("DELETE", `/agents/${id}`);
  toast(r.ok ? "已删除" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadAgents();
}
async function editAgent(id) {
  const r = await api("GET", "/agents");
  const a = (r.data?.agents || []).find((x) => x.agent_id === id);
  if (!a) return toast("找不到该 Agent");
  modal("编辑 Agent", `
    <p class="desc">接入令牌不可改（需重置请点卡片上「重置接入令牌」）。身份模式（普通/专家/开发者）用下方下拉框设置，无需再写 notes 魔法串。</p>
    <div class="field"><label>名称</label><input id="e-name" value="${esc(a.name)}"></div>
    <div class="field"><label>身份模式</label>
      <select id="e-mode">${MODES.map((m) => `<option value="${m}" ${m === (a.mode || "normal") ? "selected" : ""}>${modeLabel(m)}</option>`).join("")}</select></div>
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
    mode: $("#e-mode").value,
    status: $("#e-status").value,
    notes: $("#e-notes").value,
  });
  if (!r.ok) return toast("保存失败：" + (r.data?.error || r.status));
  toast("已保存");
  closeModal();
  loadAgents();
}



// ── 待审核流程（部署候选） ──
// 注：经验沉淀(P5)已推迟——DSL 是自顶向下编码经验，与自底向上提取 skill 方向相反。
//       本页面只处理 agent 提交的 DSL 部署候选：raw → 确认闸 → NR。

let _allProposals = [];  // 全量数据，供搜索过滤

function _renderProposals(items) {
  const list = $("#p-list");
  if (!items.length) { list.innerHTML = `<div class="empty">没有匹配的流程。</div>`; return; }
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
          <div class="desc">原生 flow：${nodeCount ?? "?"} 节点 ｜ 静态检查 ${le} 错误 / ${lw} 警告
            ${br ? ` ｜ <span style="color:#c0392b">硬伤(${esc(br)})</span>` : ""}
            ${logicBad ? ` ｜ <span style="color:#c0392b">逻辑不可达</span>` : ""}</div>
          <div class="desc">部署前需人工审核（不会自动部署到 NR）。</div>`;
      }
      let subflowMeta = "";
      if (isSubflow) {
        const sn = dslMeta?.dsl_name || dslMeta?.name || "(未命名)";
        const sfId = dslMeta?.definition_id || "";
        const sfNc = dslMeta?.node_count ?? "?";
        subflowMeta = `
          <div class="desc">子流程提案：DSL 调用名 <b>${esc(sn)}</b> ｜ NR 子流程 id <code>${esc(sfId || "?")}</code> ｜ ${esc(String(sfNc))} 节点</div>
          <div class="desc">点「注册子流程」后：在 NR 创建子流程实例并登记注册表，Agent 可经 MCP 调用。</div>`;
      }
      const _pStatusCls = gatePassed ? " status-pass" : (gate.passed === false ? " status-fail" : (p.requires_review ? " status-review" : ""));
      return `
      <div class="item proposal-item${_pStatusCls}">
        <div class="row">
          <div><span class="title">${esc(p.title)}</span> ${p.id ? `<span class="meta" title="${esc(p.id)}">(${esc(p.id)})</span>` : ""}</div>
          <div>${badge("kind-" + kindBadge, kindBadge)} ${badge("st-" + p.status, p.status)}
            ${gatePassed ? badge("ok", "安全闸 PASS") : (gate.passed === false ? badge("danger", "安全闸 FAIL") : "")}
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
        ${nodeCount && isDsl ? `<div class="desc">编译产物：${nodeCount} 节点 ｜ 无 Function 节点：✅</div>` : ""}
        <div class="actions">
          ${canDeploy ? `<button class="btn sm primary" data-dep="${esc(p.id)}">${isSubflow ? "注册子流程" : (p.deployed_flow_id ? "重新部署到 NR" : "部署到 NR")}</button>` : ""}
          ${p.status !== "rejected" ? `<button class="btn sm danger" data-prej="${esc(p.id)}">拒绝</button>` : ""}
          <button class="btn sm" data-del="${esc(p.id)}">删除</button>
          ${p.archived_at ? `<button class="btn sm" data-unarch="${esc(p.id)}">取消归档</button>` : `<button class="btn sm" data-arch="${esc(p.id)}">归档</button>`}
          ${p.deployed_flow_id && !isSubflow ? `<button class="btn sm danger" data-undep="${esc(p.deployed_flow_id)}">撤回</button>
            <span class="badge ok" style="margin-left:4px">已部署: ${esc(p.deployed_flow_id)}</span>` : ""}
          ${p.deployed_flow_id && isSubflow ? `<span class="badge ok" style="margin-left:4px">已注册: ${esc(p.deployed_flow_id)}</span>` : ""}
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
  v.innerHTML = `<div class="view-head"><h2>提案</h2><span class="sub">Agent 提交的 flow，经安全闸验证后可部署到 Node-RED</span></div>
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
  const reason = (await promptDialog("拒绝理由（可选）：", "")) ?? "";
  const r = await api("POST", `/proposals/${id}/reject`, { reason });
  toast(r.ok ? "已拒绝" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadProposals();
}
async function deleteProposal(id) {
  if (!(await confirmDialog("删除该流程？（不可恢复；若已部署，对应 flow 不会被自动移除，需到「已部署」手动移除）"))) return;
  const r = await api("DELETE", `/proposals/${id}/delete`);
  toast(r.ok ? "已删除" : "失败：" + (r.data?.error || r.status));
  if (r.ok) loadProposals();
}
async function archiveProposal(id) {
  if (!(await confirmDialog("归档该提案？（退休语义：默认从有效列表隐藏，仍可经「显示已归档」查看与恢复）"))) return;
  const r = await api("POST", `/proposals/${id}/archive`);
  toast(r.ok ? "已归档" : "失败：" + (r.data?.error || r.status));
  if (r.ok) _loadProposalPage();
}
async function unarchiveProposal(id) {
  const r = await api("POST", `/proposals/${id}/unarchive`);
  toast(r.ok ? "已取消归档" : "失败：" + (r.data?.error || r.status));
  if (r.ok) _loadProposalPage();
}
// 加载 Node-RED tab 列表（用于 P4 目标 tab 选择器）
async function _loadNRTabs() {
  try {
    const r = await api("GET", "/nr/tabs");
    if (r.ok && r.data && r.data.tabs) {
      return r.data.tabs;
    }
  } catch (e) {}
  return [];
}

async function deployProposal(id) {
  const p = _allProposals.find((x) => x.id === id);
  let isSub = false;
  let proposalTargetTab = "";
  try {
    const c = JSON.parse(p.content || "{}");
    isSub = !!(p && (p.kind === "subflow" || c.type === "subflow"));
    proposalTargetTab = c.target_tab || "";
  } catch (e) {}
  if (isSub) {
    if (!(await confirmDialog("确定注册该子流程到网关？\n（写 NR 子流程实例 + 登记子流程注册表，注册后 agent 可经 MCP 调用。冲突或失败不会动 NR。）"))) return;
    const r = await api("POST", `/proposals/${id}/deploy`, { target: "prod" });
    return _handleDeployResult(r, id);
  }
  // P4 混合模式：部署前显示目标 tab 选择器
  const tabOptions = await _loadNRTabs();
  const currentMode = (window._appConfig && window._appConfig.tab_org_mode) || "per_flow";
  let defaultTab = proposalTargetTab || "";
  if (!defaultTab && currentMode === "single_tab") defaultTab = "__auto_single__";

  modal("部署到 Node-RED", `
    <p style="line-height:1.7;margin-bottom:12px">确定部署 <b>${esc(p.title || p.id)}</b> 到 Node-RED？部署后可在「已部署」安全撤回。</p>
    <div class="field" style="margin-bottom:12px">
      <label style="display:block;margin-bottom:6px;font-weight:600">目标 tab（P4 混合模式，可选）</label>
      <select id="deploy-target-tab" class="input" style="width:100%">
        <option value="">按当前模式自动（${currentMode === "single_tab" ? "单 tab 集中" : "每个 flow 独立 tab"}）</option>
        <option value="__auto_single__">AutoFlow 集中 tab（单 tab 模式）</option>
        <optgroup label="已有 tab">
          ${tabOptions.map(t => `<option value="${esc(t.label)}" ${defaultTab === t.label ? "selected" : ""}>${esc(t.label)}（${t.node_count || 0} 节点）</option>`).join("")}
        </optgroup>
        <option value="__new__">➕ 新建 tab…</option>
      </select>
      <input type="text" id="deploy-new-tab-name" class="input" placeholder="输入新 tab 名称" style="width:100%;margin-top:8px;display:none">
      <p class="desc" style="font-size:12px;color:var(--text-muted);margin-top:6px">
        留空=按当前 Tab 组织模式部署；选择已有 tab=混合模式，flow 部署到该 tab 中；新建 tab=创建新 tab 并部署。
      </p>
    </div>
    <div style="margin-top:16px;text-align:right;display:flex;gap:8px;justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="deploy-confirm-btn">确认部署</button>
    </div>
  `, null, "关闭");
  // 新建 tab 输入框显隐
  const sel = $("#deploy-target-tab");
  const newInput = $("#deploy-new-tab-name");
  if (sel) sel.onchange = () => { if (newInput) newInput.style.display = sel.value === "__new__" ? "block" : "none"; };
  // 确认部署
  const confirmBtn = $("#deploy-confirm-btn");
  if (confirmBtn) confirmBtn.onclick = async () => {
    let targetTab = "";
    const sel2 = $("#deploy-target-tab");
    if (sel2) {
      if (sel2.value === "__new__") {
        targetTab = ($("#deploy-new-tab-name").value || "").trim();
        if (!targetTab) { toast("请输入新 tab 名称"); return; }
      } else if (sel2.value === "__auto_single__") {
        targetTab = ""; // 留空，后端会走 single_tab 模式
      } else {
        targetTab = sel2.value;
      }
    }
    closeModal();
    const body = { target: "prod" };
    if (targetTab) body.target_tab = targetTab;
    const r = await api("POST", `/proposals/${id}/deploy`, body);
    return _handleDeployResult(r, id, false);
  };
}

// 部署结果处理（从原 deployProposal 中抽离）
async function _handleDeployResult(r, id, isSub) {
  const p = _allProposals.find((x) => x.id === id);
  if (typeof isSub === "undefined") {
    try { isSub = !!(p && (p.kind === "subflow" || JSON.parse(p.content || "{}").type === "subflow")); } catch (e) { isSub = false; }
  }
  if (!r.ok) {
    if (r.data?.conflict) return toast("冲突：" + (r.data.error || "同名子流程已存在，可改名或 force 重建"));
    // 安全闸 / 测试环境拦截：常驻对话框，必须点「确定」才关闭（不自动消失）
    const errText = String(r.data?.error || r.status || "未知错误");
    const isGate = r.data?.stage === "gate" || /闸门|受保护对象|安全闸/.test(errText);
    if (isGate) {
      const ent = r.data?.protected_entity
        ? `<p style="color:#c0392b">受保护实体：${esc(r.data.protected_entity)}</p>` : "";
      const detail = r.data?.gate
        ? `<pre style="white-space:pre-wrap;max-height:240px;overflow:auto;background:#f6f6f6;padding:8px;border-radius:6px">${esc(JSON.stringify(r.data.gate, null, 2))}</pre>`
        : "";
      modal("安全闸 / 测试环境拦截",
        `<p style="line-height:1.7">${esc(errText)}</p>` +
        detail + ent +
        `<p style="color:#888">未部署。请检查 DSL 与预期状态是否一致，或调整安全闸规则后重试。</p>` +
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
  v.innerHTML = `<div class="view-head"><h2>已部署</h2><span class="sub">本网关部署到 Node-RED 的 flow，可安全撤回（仅移除网关节点）</span></div>
    <div id="d-list"><div class="empty">加载中…</div></div>`;
  try {
    const r = await api("GET", "/deployed");
    const list = $("#d-list");
    const items = r.data?.deployed || [];
    if (!items.length) { list.innerHTML = `<div class="empty">还没有通过本网关部署的 flow。</div>`; return; }
    list.innerHTML = items.map((d) => `
      <div class="item deployed-item${d.stale ? " status-stale stale" : " status-ok"}">
        <div class="row">
          <div><span class="title">${esc(d.label)}</span> <span class="meta" title="${esc(d.flow_id||'')}">(${esc(d.flow_id||'')})</span></div>
          <div>${d.stale ? badge("stale", "注册表漂移") : badge("ok", "已部署")}</div>
        </div>
        <div class="desc">来源 agent：${esc(d.owner_agent)} ｜ 节点数：${esc(d.node_count ?? "?")} ｜ ${fmtTime(d.deployed_at)}</div>
        ${d.server_resolved === false ? `<div class="desc" style="color:#c0392b">⚠ 触发器未绑定 HA server（部署时未能解析到），flow 不会自动触发，需在 NR 中手动绑定 server。</div>` : ""}
        ${d.stale ? `<div class="desc" style="color:#c0392b">⚠ 注册表↔NR 分叉：注册表记此 flow 已部署，但 Node-RED 实例里已无该 flow_id（可能已被手动删除、重命名或切换了 NR 实例）。撤回将仅清理注册表记录，不会触碰 NR。</div>` : ""}
        <div class="actions">
          <button class="btn sm" data-trg="${esc(d.flow_id)}">▶ 触发</button>
          <button class="btn sm" data-rb="${esc(d.flow_id)}">↩ 回滚</button>
          <button class="btn sm danger" data-und="${esc(d.flow_id)}">撤回</button>
        </div>
      </div>`).join("");
    $$("[data-und]").forEach((b) => (b.onclick = () => undeployProposal(b.dataset.und)));
    $$("[data-trg]").forEach((b) => (b.onclick = () => triggerFlow(b.dataset.trg)));
    $$("[data-rb]").forEach((b) => (b.onclick = () => openRollbackDialog(b.dataset.rb)));
  } catch (e) { $("#d-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
async function undeployProposal(id) {
  if (!(await confirmDialog("确定撤回？\n该 tab 中你自己的节点将被保留，仅移除本网关写入的节点。"))) return;
  let r = await api("POST", `/deployed/${id}/undeploy`, {});
  if (!r.ok && r.data?.code === "nr_unreachable") {
    if (await confirmDialog("Node-RED 当前不可达，无法确认 flow 状态。\n若你确认已在 NR 手动删除该 flow，是否仅清理本网关注册表？")) {
      r = await api("POST", `/deployed/${id}/undeploy`, { force: true });
    }
  }
  if (r.ok) {
    const d = r.data || {};
    if (d.action === "trimmed_tab") {
      toast(`已移除网关节点 ${d.gateway_nodes_removed} 个，保留你的节点 ${d.user_nodes_preserved} 个`);
    } else if (d.action === "deleted_tab") {
      toast(`已撤回并删除流程页「${d.label || id}」`);
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
  toast(`已触发 ${n} 个 inject 节点${errs ? `，失败 ${errs}` : ""}。可到「诊断」查看 debug 输出`);
}

async function openRollbackDialog(flowId) {
  // 先加载快照列表
  let r;
  try {
    r = await api("GET", `/flows/${flowId}/snapshots`);
  } catch (e) {
    toast("获取快照失败：" + e.message);
    return;
  }
  if (!r.ok) { toast("获取快照失败：" + (r.data?.error || r.status)); return; }
  const snaps = r.data?.snapshots || [];
  if (!snaps.length) {
    toast("该 flow 暂无可用快照，无法回滚");
    return;
  }

  // 构建快照选择列表（排除备份快照）
  const options = snaps
    .filter(s => !s.label || s.label.indexOf("备份") < 0)
    .map(s => `<option value="${esc(s.snapshot_id)}">${esc(s.label || s.snapshot_id)} · ${esc((s.ts || "").slice(0, 16))} · ${s.node_count || 0}节点</option>`)
    .join("");
  if (!options) { toast("无有效快照可回滚"); return; }

  // 二次确认对话框
  const sel = await promptDialog(
    `请选择要回滚到的快照版本：\n\n${snaps.map(s => `[${s.snapshot_id}] ${s.label || s.snapshot_id} (${s.ts}) · ${s.node_count}节点`).join("\n")}\n\n输入 snapshot_id 或编号(0-${snaps.length - 1})：`, ""
  );
  if (!sel) return;
  const idx = parseInt(sel, 10);
  const snapId = !isNaN(idx) && snaps[idx] ? snaps[idx].snapshot_id : (sel.trim() || "");
  if (!snapId || !snaps.some(s => s.snapshot_id === snapId)) {
    toast("无效的快照选择");
    return;
  }
  if (!(await confirmDialog(
    `确定回滚到快照「${snapId}」？\n` +
    `目标 flow: ${flowId}\n` +
    `快照内容: ${snaps.find(s => s.snapshot_id === snapId)?.label || snapId} (${snaps.find(s => s.snapshot_id === snapId)?.ts || "未知时间"})\n\n` +
    `回滚前会自动创建当前状态备份，恢复后可用「↩ 回滚」进一步操作。\n\n此操作不可撤销！`
  ))) return;

  try {
    const rb = await api("POST", `/flows/${flowId}/rollback`, { snapshot_id: snapId });
    if (rb.ok) {
      toast(`回滚成功：已恢复到 ${snaps.find(s => s.snapshot_id === snapId)?.label || snapId} 状态`);
      loadDeployed();
    } else {
      toast("回滚失败：" + (rb.data?.error || rb.status));
    }
  } catch (e) {
    toast("回滚出错：" + e.message);
  }
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
     <button class="btn primary" id="m-save">保存</button>`, null, "关闭");
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
  if (!(await confirmDialog("删除该笔记？"))) return;
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
  const ver = (await promptDialog(`设置 stage=${stage}。版本号（留空沿用现有，缺省 1.0.0）：`, "")) ?? "";
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
  if (!(await confirmDialog("确认将 NR 中所有 release 且版本号大于已推送版本的 flow 推送到 prod？\n（dev/agent 不会推送；推送后 flow 在 prod 自动启用）"))) return;
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
      <h2>🧪 直接部署模式</h2>
      <p class="desc">输入提示词给 Agent → 粘贴产出的 flow JSON → 校验/部署到 NR。</p>
    </div>
    <div class="lab-tabs">${tabs}</div>
    <div class="lab-workspace">
      <div class="lab-panel lab-prompt">
        <h3>📋 提示词</h3>
        <p class="hint">从 docs/test_prompts.md 复制提示词，粘贴到下方后发给 ${agent.id}。</p>
        <textarea id="labPrompt" placeholder="在此粘贴提示词（供参考，不影响部署）..."></textarea>
        <div class="lab-actions">
          <button class="btn primary" id="labValidate">仅校验（预览）</button>
          <button class="btn" id="labDeploy">部署到 NR</button>
        </div>
      </div>
      <div class="lab-panel lab-flow">
        <h3>📦 Flow JSON <span class="badge">${agent.id}</span></h3>
        <p class="hint">粘贴 Agent 产出的 nodes 数组或完整 flow JSON。</p>
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
  if (!raw) { toast("请先粘贴 flow JSON"); return; }

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

// ── 人工抽查（合并三家专家提交 → 单 tab，触发器禁用）──
async function loadSpotcheck() {
  const v = $("#view-spotcheck");
  v.innerHTML = `
    <div class="view-head"><h2>🔎 人工抽查</h2><span class="sub">挑选同一任务的 3 家专家提交，合并为一份专家提案（触发器默认禁用，不会自动点火；审核后部署到 NR）</span></div>
    <div class="card form-card">
      <div class="field">
        <label>任务</label>
        <select id="sc-task"><option>加载中…</option></select>
        <span class="meta" id="sc-task-meta"></span>
      </div>
      <div class="field">
        <label>tab 名称（部署到 NR 的名称）</label>
        <input id="sc-label" placeholder="抽查·wb_xxx">
      </div>
      <div class="actions">
        <button class="btn" id="sc-dry">预览合并</button>
        <button class="btn primary" id="sc-deploy">提交为待审核</button>
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
  if (!dry && !(await confirmDialog("确定把 3 家提交合并为一份「白盒提案」？\n（待你在「待审核流程」面板审核后一键部署到 NR；触发器已禁用，不会自动触发）"))) return;
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
    <strong>✅ ${esc(isProposal ? (dry ? "预览就绪" : "已提交为待审核") : (dry ? "预览就绪" : "已部署"))}</strong>
    ${dry ? `<span class="badge">预览（不部署到 NR）</span>` : (isProposal ? `<span class="badge">待人工审核后部署</span>` : ``)}
    <table class="info-table">
      <tr><td>tab 名称</td><td>${esc(d.label || "")}</td></tr>
      <tr><td>${isProposal ? "proposal_id" : "flow_id"}</td><td><code>${esc(d.proposal_id || d.flow_id || "")}</code></td></tr>
      <tr><td>合并节点数</td><td>${esc(d.node_count || 0)}</td></tr>
      <tr><td>静态检查</td><td>${lintE} error / ${lintW} warning${d.would_block_on_lint ? ` ｜ <span style="color:#c0392b">真部署将被硬伤规则拦截(${(d.would_block_rules || []).join(",")})</span>` : ""}</td></tr>
    </table>
    <details><summary class="meta">各 Agent 合并情况</summary>
      <table class="info-table"><tr><th>Agent</th><th>结果</th></tr>${rosterRows}</table>
    </details>
    ${dry ? `<div class="desc">确认无误后点「提交为待审核」。</div>` : (isProposal ? `<div class="desc">已生成提案，请在「待审核流程」面板审核后一键部署到 NR。</div>` : ``)}`;
  if (isProposal) toast("已提交提案：" + (d.proposal_id || ""));
  else if (!dry) toast("已部署到 NR：" + (d.flow_id || ""));
}

// ── 诊断查看器（P4-C，只读）──
async function loadDiagnostics() {
  const v = $("#view-diagnostics");
  v.innerHTML = `<div class="view-head"><h2>🩺 诊断</h2><span class="sub">网关健康状态、运行轨迹与评测任务（trace 重启后清空）</span>
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
    }).join("") : `<tr><td colspan="4" class="empty">暂无运行轨迹（网关重启后清空，有活动后自动累积）</td></tr>`;
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
    }).join("") : `<tr><td colspan="7" class="empty">暂无评测任务</td></tr>`;
    const body = $("#dx-body");
    body.innerHTML = `
      <div class="grid cols-4">
        <div class="card"><div class="meta">已接入 Agent</div><div class="stat">${c.agents ?? 0}</div></div>
        <div class="card"><div class="meta">待确认操作</div><div class="stat">${c.pending_ops ?? 0}</div></div>
        <div class="card"><div class="meta">已部署 flow</div><div class="stat">${c.deployed_flows ?? 0}</div></div>
        <div class="card"><div class="meta">提案总数</div><div class="stat">${c.proposals_total ?? 0}</div></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>环境 / 健康</h3>
        <div class="desc">环境 <b>${esc(d.env || "?")}</b> ｜ 部署策略 <b>${esc(d.deploy_policy || "review_all")}</b></div>
        <div class="desc">NR: <code>${esc(d.nr_url || "")}</code> ｜ HA: <code>${esc(d.hass_server || "")}</code></div>
        <div class="desc">普通 <code>${esc(d.mcp || "")}</code> ｜ 专家 <code>${esc(d.mcp_white || "")}</code> ｜ 开发者 <code>${esc(d.mcp_admin || "")}</code></div>
        <div class="desc" style="margin-top:6px">提案状态分布：${statusHtml} ｜ 已落地部署 ${c.proposals_deployed ?? 0}</div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>最近运行轨迹（${traces.length} 条）</h3>
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

// ── 账号区（登录/注册/改密/会话/用户管理）统一由文件末尾的 afAuth 模块渲染到 #authZone ──
// 旧「🔑 粘贴访问令牌」交互已废弃：账号密码登录见 webui_auth.py + afAuth。

// ── DSL 验证池开关 ──
$("#tpBtn").onclick = async () => {
  let enabled = true;
  try { const c = await api("GET", "/config"); enabled = !!(c.data && c.data.task_pool_enabled); } catch {}
  modal("DSL 验证池开关",
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
    if (r.data?.ok) { closeModal(); toast(on ? "验证池已开启" : "验证池已关闭（Agent 调用将被拒绝）"); }
    else toast("保存失败: " + (r.data?.error || r.status));
  };
};

// ── 原生节点开关（Phase 4，中风险逃生舱）──
$("#rnBtn").onclick = async () => {
  let enabled = false;
  try { const c = await api("GET", "/config"); enabled = !!(c.data && c.data.raw_node_escape_enabled); } catch {}
  modal("原生节点开关",
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
  modal("部署策略",
    `<p class="desc">决定自动生成的 flow 是否需要人工审核后再部署：<br>
     • <b>review_all</b>：所有提案（含编译器产物）都需用户在 WebUI 点 Deploy 后部署（默认，最稳）。<br>
     • <b>compiler_auto</b>：编译器产物标「可信」徽章、可自动部署；原生手写(raw)永远需人审。<br>
     无论哪种策略，实际部署都仍过 staging 闸门(validate/lint/E2E)，且始终由用户在 WebUI 触发——绝不无人值守部署。</p>
     <div class="field"><label>部署策略</label>
       <div class="seg" id="dpSeg">
         <button class="seg-btn" data-v="review_all">全部审核 (review_all)</button>
         <button class="seg-btn" data-v="compiler_auto">自动生成可直装 (compiler_auto)</button>
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
            <button class="seg-btn active" data-v="black">普通（DSL 提案）</button>
            <button class="seg-btn" data-v="white">专家（直部署）</button>
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

// ── 自动修复：重试次数（WebUI 可配，0=禁用自主重试，1~20=单 (agent, flow) 滑动窗口内最多自主重试）──
$("#shBtn").onclick = async () => {
  let cur = 3;
  try { const c = await api("GET", "/config"); cur = (c.data && c.data.selfheal_budget) || 3; } catch {}
  modal("自动修复重试次数",
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
      return toast("自动修复重试次数必须是 0~20 之间的整数");
    }
    const r = await api("PUT", "/settings", { selfheal_budget: n });
    if (r.data?.ok) { closeModal(); toast(`自动修复重试次数已设为 ${n}（即时生效，无需重启）`); }
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
    <button class="btn primary" id="sf-import-btn">＋ 从 NR 导入</button></div>
    <div class="row" style="gap:8px;margin:8px 0">
      <select id="sf-filter"><option value="">全部</option><option value="managed">内置</option><option value="imported">已导入</option></select>
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
  if (!rows.length) { el.innerHTML = `<div class="empty">暂无子流程（点右上「＋ 导入」从 NR 自动检测导入）。</div>`; return; }
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
        <div>${badge("tier-" + (isManaged ? "managed" : "imported"), isManaged ? "内置" : "已导入")} ${kindBadge} ${badge("status-" + s.status, s.status)}</div>
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
    ? "安装 Bark 子流程到 Node-RED（已存在则跳过，安全重复安装）"
    : "请先在「设置 → 连接配置 → Bark」填写服务器地址与密钥";
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
      : d.created ? "已安装 Bark 子流程到 Node-RED：" + key : "安装完成：" + key);
    await refreshSfList();
  } catch (e) { toast("安装失败：" + e.message); }
}
// ── A1：Link API Tab（网关 HTTP 桥接：link_out / http_api）─────────────
// A2：配置表单 / A3：一键安装「AutoFlow API」tab 到 NR（按节点 id 增量合并，不删旧节点）。
async function loadLinkApis() {
  _sfView = "link_api";
  const v = $("#view-link_apis");
  v.innerHTML = `<div class="view-head"><h2>Link API（网关 HTTP 桥接）</h2>
    <button class="btn" id="la-import-tab" title="从 Node-RED tab 链接只读自省，注册成可调用 Link API">🔗 从 tab 链接导入</button></div>
    <div class="row" style="gap:8px;margin:8px 0">
      <span class="meta" id="la-count"></span>
    </div>
    <div id="la-list"><div class="empty">加载中…</div></div>`;
  $("#la-import-tab").onclick = showImportLinkApiFromTab;
  await refreshLinkApis();
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
  if (!rows.length) { el.innerHTML = `<div class="empty">暂无 Link API。</div>`; return; }
  el.innerHTML = rows.map((s) => {
    const ins = Array.isArray(s.input_schema) ? s.input_schema.length : 0;
    const kindBadge = badge("kind-link_out", "link API");
    const idLine = s.kind === "link_out"
      ? `link out 入口：${esc(s.entry_link_id || "—")}`
      : "网关内联（不生成 NR 节点）";
    // #C：仅「真的会在 NR 派生节点」的 Link API 才给安装按钮；其余（http_api 内联 /
    // 已导入的 tab-link link_out 零写入）不给，避免点了却无事发生。
    const installBtn = s.needs_nr_flow
      ? `<button class="btn sm" data-la-install="${esc(s.key)}" title="把该 Link API 合并到 NR 的 AutoFlow API tab（增量更新）">📦 安装到 Node-RED</button>`
      : "";
    return `<div class="item" data-key="${esc(s.key)}">
      <div class="row">
        <div><span class="title">${esc(s.title || s.key)}</span> <span class="meta">${esc(s.key)}</span></div>
        <div>${badge("tier-" + (s.source_type === "managed" ? "managed" : "imported"), s.source_type === "managed" ? "内置" : "已导入")} ${kindBadge} ${badge("status-" + s.status, s.status)}</div>
      </div>
      <div class="desc">DSL 调用：<code>调用子流程: ${esc(s.key)}(...)</code></div>
      <div class="desc">前置参数 ${ins} 项 ｜ ${idLine}</div>
      <div class="actions">
        <button class="btn sm" data-la-cfg="${esc(s.key)}" title="填写 token / 坐标等运行时参数">⚙️ 配置</button>
        ${installBtn}
        <button class="btn sm danger" data-la-del="${esc(s.key)}" title="清空本机配置并移除 AutoFlow API tab 里由它派生的节点">🗑️ 卸载</button>
      </div>
    </div>`;
  }).join("");
  $$("[data-la-cfg]").forEach((b) => (b.onclick = () => showLinkApiConfig(b.dataset.laCfg)));
  $$("[data-la-install]").forEach((b) => (b.onclick = () => installSingleLinkApi(b.dataset.laInstall)));
  $$("[data-la-del]").forEach((b) => (b.onclick = () => deleteLinkApi(b.dataset.laDel)));
}

// #C：单个 Link API 的「安装到 Node-RED」按钮。
async function installSingleLinkApi(key) {
  try {
    const r = await api("POST", "/link-apis/" + encodeURIComponent(key) + "/install", {});
    if (!r.ok) {
      const d = r.data || {};
      if (Array.isArray(d.missing) && d.missing.length) {
        const info = d.missing.map((m) => `${esc(m.title || m.name)}：缺少 ${m.missing.join("、")}`).join("；");
        return toast("安装失败：" + (d.error || "配置不完整") + " — " + info);
      }
      return toast("安装失败：" + (d.error || r.status));
    }
    const d = r.data || {};
    let msg = d.skipped
      ? `「${key}」已是最新，未改动 NR（tab ${d.tab_id || "?"}）。`
      : `已${d.tab_created ? "创建" : "更新"}「${key}」对应的 AutoFlow API tab 节点`
        + `（tab ${d.tab_id || "?"}）：新增 ${d.nodes_added || 0}、刷新 ${d.nodes_updated || 0}，共 ${d.nodes_total || 0} 个。`;
    if (Array.isArray(d.duplicate_tabs) && d.duplicate_tabs.length > 1) {
      msg += ` ⚠️ NR 上有 ${d.duplicate_tabs.length} 个同名 tab，请手动清理多余的：${d.duplicate_tabs.join(", ")}`;
    }
    toast(msg);
  } catch (e) { toast("安装失败：" + e.message); }
}

// ── #182：删除（卸载）Link API ──
// 语义要跟用户讲清楚：删的是「你的配置 + NR 里那条链」，能力声明本身留在网关，
// 日后想用还能重新配置+安装。不说清楚，用户会以为能力被永久拆了而不敢点。
async function deleteLinkApi(key) {
  const s = _sfList.find((x) => x.key === key);
  const title = s ? (s.title || key) : key;
  if (!(await confirmDialog(
    `确定删除 Link API「${title}」？\n`
    + "\n将清空它的本机配置（token 等），并移除 Node-RED「AutoFlow API」tab 中"
    + "由它派生的节点。\n其它 Link API 的链路与你自己的流程不受影响；"
    + "能力声明保留在网关，之后可重新配置并安装回去。"))) return;
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
    <button class="btn primary" id="la-cfg-save">保存</button>`, null, "关闭");
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
      ? "\n⚠️ 这是本网关内置的子流程，Node-RED 上的子流程实例将被一并删除。"
      : "\n（仅从本网关注册表移除，Node-RED 上的子流程保持原样。）";
  if (!(await confirmDialog(`确定删除子流程「${key}」？${tail}\n此操作不可撤销，已引用它的 flow 将失效。`))) return;
  try {
    const r = await api("DELETE", "/subflows/" + encodeURIComponent(key));
    if (!r.ok) return toast("删除失败：" + (r.data?.error || r.status));
    toast("已删除：" + key + (r.data?.nr_removed ? "（Node-RED 实例已删除）"
      : r.data?.nr_kept ? "（Node-RED 子流程已保留）" : ""));
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
    <div class="desc">类型：${esc(s.source_type === "managed" ? "内置" : "已导入")} ｜ 形态：${esc(kindLabel)} ｜ 状态：${esc(s.status)} ｜ ${idLabel}</div>
    <h3>前置参数（调用方需传入）</h3>
    <table class="tbl"><thead><tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr></thead><tbody>${inRows}</tbody></table>
    <h3>需配置的 env 变量（owner 侧）</h3>
    <ul class="kv">${envRows}</ul>
    <details><summary class="meta">完整 JSON</summary><div class="code-box">${esc(JSON.stringify(s, null, 2))}</div></details>`);
}
function showImportSubflow() {
  modal("从 NR 导入子流程（自动检测输入参数）", `
    <div class="field"><label>NR 子流程 id（在 NR 子流程属性里复制）</label><input id="sf-nr" placeholder="如 b0bbc86abb2172a5"></div>
    <div class="field"><label>DSL 调用名 key（唯一，勿与内置撞名）</label><input id="sf-key" placeholder="如 my_custom_push"></div>
    <div class="field"><label>标题（可选）</label><input id="sf-title" placeholder="我的子流程"></div>
    <div class="field"><label>owner（可选）</label><input id="sf-owner" placeholder="webui"></div>
    <div class="field"><label>状态</label><select id="sf-status"><option value="active">active（立即可用）</option><option value="pending_review">pending_review（待审核）</option><option value="disabled">disabled（禁用）</option></select></div>
    <p class="desc">提交后本网关会读取该 NR 子流程的 in 端口和环境变量，自动提取输入参数，无需手动填写。</p>
    <button class="btn primary" id="sf-do-import">检测并导入</button>`, null, "关闭");
  $("#sf-do-import").onclick = doImportSubflow;
}
async function doImportSubflow() {
  const nr_subflow_id = $("#sf-nr").value.trim();
  const key = $("#sf-key").value.trim();
  if (!nr_subflow_id) return toast("请填写 NR 子流程 id");
  if (!key) return toast("请填写 DSL 调用名");
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
  } finally { btn.disabled = false; btn.textContent = "检测并导入"; }
}

// ── #C-tab：从 NR tab 链接逆生成 Link API ──
// 只读自省用户 tab，注册薄桥接（link_out），不改动用户 NR 流。多参数 TTS 队列支持逐行增删编辑。
function showImportLinkApiFromTab() {
  modal("从 tab 链接导入 Link API", `
    <p class="desc">把 Node-RED 编辑器里的 tab 链接（如 <code>http://<NAS_IP>:1990/#flow/e70a201b5f004927</code>）粘贴进来。
    网关会<b>只读</b>自省该 tab，判断能否注册成可调用 API——之后 agent 写 flow 时说「使用 TTS」或「智能语音播报队列」即可调用。不会改动你的 NR 流。</p>
    <div class="field"><label>tab 链接</label><input id="lt-url" placeholder="http://host:1990/#flow/<tabid>"></div>
    <button class="btn primary" id="lt-detect">检测能否注册</button>
    <div id="lt-result" style="margin-top:10px"></div>`, null, "关闭");
  $("#lt-detect").onclick = detectLinkApiFromTab;
}
async function detectLinkApiFromTab() {
  const url = $("#lt-url").value.trim();
  if (!url) return toast("请粘贴 tab 链接");
  const box = $("#lt-result");
  box.innerHTML = `<div class="empty">检测中…</div>`;
  let d;
  try {
    const r = await api("POST", "/link-apis/import-from-url", { url });
    if (!r.ok) { box.innerHTML = errBox(r.data?.error || r.status); return; }
    d = r.data;
  } catch (e) { box.innerHTML = errBox(e.message); return; }
  if (!d.registerable) {
    box.innerHTML = `<div class="card warn">⚠️ 该 tab 暂不能注册为 Link API<br><span class="desc">${esc(d.reason || "")}</span></div>`;
    return;
  }
  const rows = (d.params || []).map(paramRowHtml).join("");
  box.innerHTML = `
    <div class="card ok">✅ 可注册：tab「${esc(d.title)}」入口为 link in（entry <code>${esc(d.entry_id)}</code>）</div>
    <div class="field"><label>DSL 调用名 key（唯一，agent 写 flow 时说「使用 ${esc(d.suggested_key || "此 API")}」即可调用）</label>
      <input id="lt-key" value="${esc(d.suggested_key || "")}" placeholder="如 TTS"></div>
    <div class="field"><label>标题（可选）</label><input id="lt-title" value="${esc(d.title)}"></div>
    <h3>调用参数</h3>
    <p class="desc">自省推断自 tab 内部 <code>msg.&lt;x&gt;</code> 读取；复杂队列可逐行增删 / 改类型 / 设必填。</p>
    <table class="tbl" id="lt-params"><thead><tr><th>参数名</th><th>类型</th><th>必填</th><th>说明</th><th></th></tr></thead>
      <tbody>${rows || `<tr id="lt-empty"><td colspan="5" class="meta">无推断参数（也可手动添加）</td></tr>`}</tbody></table>
    <button class="btn sm" id="lt-add">＋ 添加参数</button>
    <div class="row" style="margin-top:12px"><button class="btn primary" id="lt-register">注册为 Link API</button></div>`;
  const tb = $("#lt-params").querySelector("tbody");
  tb.addEventListener("click", (e) => {
    if (e.target.classList && e.target.classList.contains("lt-del"))
      e.target.closest("tr").remove();
  });
  $("#lt-add").onclick = () => {
    const empty = document.getElementById("lt-empty"); if (empty) empty.remove();
    tb.insertAdjacentHTML("beforeend", paramRowHtml({ name: "", required: false, type: "str", desc: "" }));
  };
  $("#lt-register").onclick = () => registerLinkApiFromTab(url);
}
function paramRowHtml(p) {
  const types = ["str", "int", "float", "bool"];
  const opts = types.map((t) =>
    `<option value="${t}"${t === (p.type || "str") ? " selected" : ""}>${t}</option>`).join("");
  return `<tr>
    <td><input class="lt-pname" value="${esc(p.name || "")}" placeholder="param_name"></td>
    <td><select class="lt-ptype">${opts}</select></td>
    <td style="text-align:center"><input class="lt-preq" type="checkbox"${p.required ? " checked" : ""}></td>
    <td><input class="lt-pdesc" value="${esc(p.desc || "")}" placeholder="说明"></td>
    <td><button class="btn sm danger lt-del" title="删除此参数">✕</button></td>
  </tr>`;
}
async function registerLinkApiFromTab(url) {
  const key = $("#lt-key").value.trim();
  if (!key) return toast("请填写 DSL 调用名");
  const title = $("#lt-title").value.trim();
  const params = [];
  $("#lt-params").querySelectorAll("tbody tr").forEach((tr) => {
    const name = tr.querySelector(".lt-pname").value.trim();
    if (!name) return;          // 跳过空行
    params.push({
      name,
      type: tr.querySelector(".lt-ptype").value,
      required: tr.querySelector(".lt-preq").checked,
      desc: tr.querySelector(".lt-pdesc").value.trim(),
    });
  });
  const btn = $("#lt-register"); btn.disabled = true; btn.textContent = "注册中…";
  try {
    const r = await api("POST", "/link-apis/register-from-tab", { url, key, title, params });
    if (!r.ok) return toast("注册失败：" + (r.data?.error || r.status));
    toast("注册成功：" + key + "（已加入 Link API 列表）");
    closeModal();
    await refreshLinkApis();
  } catch (e) { toast("注册失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "注册为 Link API"; }
}

// ── 设置管理界面（C3/C21/C25）──
async function loadSettings() {
  const v = $("#view-settings");
  v.innerHTML = `
    <div class="view-head"><h2>设置</h2><span class="sub">连接配置 · 操作日志 · 高级设置</span></div>
    <div class="tabs sub" id="settings-tabs">
      <button class="stab active" data-s="conn">连接配置</button>
      <button class="stab" data-s="audit">操作日志</button>
      <button class="stab" data-s="advanced">高级设置</button>
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
  if (s === "advanced") return loadAdvancedSettings();
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
      保存后立即生效，无需重启。界面设置优先级高于环境变量。</p>
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
  if (!(await confirmDialog(`确定清除「${f.label}」？清除后将回退到环境变量或默认值。`))) return;
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
  v.innerHTML = `<div class="view-head"><h2>安全闸</h2><span class="sub">设备保护：先导入全屋设备目录，再勾选需保护的实体</span></div>
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
      <p class="desc">用中文/英文搜索设备（按 friendly_name / area / entity_id），点选即加入保护。Tier-0 触及需人工确认；Tier-1 放行但记录操作日志。</p>
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
// ── 高级设置 ──
async function loadAdvancedSettings() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    // 同时加载 config 和 tab-org 状态
    const [cfgR, statusR] = await Promise.all([
      api("GET", "/config"),
      api("GET", "/tab-org/status").catch((e) => ({ ok: false, data: { error: e.message }, statusError: true }))
    ]);
    if (!cfgR.ok) throw new Error(cfgR.data?.error || "加载失败");
    const cfg = cfgR.data || {};
    const status = statusR.data || {};
    const statusError = statusR.statusError || !statusR.ok;
    const currentMode = cfg.tab_org_mode || "per_flow";
    const perFlowCount = status.per_flow_count || 0;
    const singleTabCount = status.single_tab_count || 0;
    const warning = status.warning;

    body.innerHTML = `
      ${statusError ? `
      <div class="card" style="border-left:4px solid #ef4444;background:#fef2f2">
        <h3 style="color:#991b1b">❌ 状态服务不可用</h3>
        <p class="desc" style="color:#7f1d1d">Tab 组织模式状态获取失败：${status.error || "未知错误"}。迁移功能暂不可用，请检查网关日志或稍后重试。</p>
      </div>` : ""}
      ${warning ? `
      <div class="card" style="border-left:4px solid #f59e0b;background:#fffbeb">
        <h3 style="color:#92400e">⚠️ 分流预警</h3>
        <p class="desc" style="color:#78350f">${warning.message || ""}</p>
        <p class="meta" style="font-size:12px;color:#92400e">当前节点数：${warning.node_count || "?"} / 阈值：${warning.threshold || "?"}</p>
      </div>` : ""}
      <div class="card">
        <h3>Tab 组织模式</h3>
        <p class="desc">控制 AutoFlow 部署的 flow 在 Node-RED 中的组织方式。修改后<strong>新部署的 flow</strong>按新模式组织，已部署的 flow 保持原模式。</p>
        <div class="field">
          <label>选择模式</label>
          <select id="adv-tab-mode" class="input">
            <option value="per_flow" ${currentMode === "per_flow" ? "selected" : ""}>每个 flow 独立 tab（默认）</option>
            <option value="single_tab" ${currentMode === "single_tab" ? "selected" : ""}>单 tab 集中模式</option>
          </select>
        </div>
        <div id="adv-mode-desc" class="desc" style="margin-top:8px;padding:10px;background:var(--bg-soft);border-radius:8px">
          ${currentMode === "single_tab" ? `
            <b>单 tab 集中模式：</b>所有 AutoFlow 部署的 flow 合并到固定的「AutoFlow」tab 中，每个 flow 用 comment 节点（AF_START/AF_END）标记边界，方便搜索定位。每个 flow 分配独立的坐标区域，避免视觉重叠。撤回时按节点 ID 精确删除，不会误伤其他 flow。
          ` : `
            <b>每个 flow 独立 tab：</b>每个 AutoFlow 部署的 flow 创建独立的 Node-RED tab，互不干扰。适合 flow 数量较少、每个 flow 较复杂需要独立查看的场景。
          `}
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn primary" id="adv-save-mode">保存设置</button>
          <span id="adv-save-hint" class="meta" style="font-size:12px"></span>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>一键迁移（P2）</h3>
        <p class="desc">将已部署的 flow 在两种模式之间迁移。迁移过程中会重新分配坐标、更新账本，<strong>建议先备份 Node-RED flows</strong>。</p>
        <div style="display:flex;gap:12px;margin:12px 0;flex-wrap:wrap">
          <div style="flex:1;min-width:150px;padding:10px;background:var(--bg-soft);border-radius:8px;text-align:center">
            <div style="font-size:24px;font-weight:bold">${perFlowCount}</div>
            <div class="meta" style="font-size:12px">独立 tab 模式</div>
          </div>
          <div style="flex:1;min-width:150px;padding:10px;background:var(--bg-soft);border-radius:8px;text-align:center">
            <div style="font-size:24px;font-weight:bold">${singleTabCount}</div>
            <div class="meta" style="font-size:12px">单 tab 模式</div>
          </div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" id="adv-migrate-to-single" ${perFlowCount === 0 ? "disabled" : ""}>
            迁移到单 tab 模式（${perFlowCount} 个 flow）
          </button>
          <button class="btn" id="adv-migrate-to-perflow" ${singleTabCount === 0 ? "disabled" : ""}>
            迁移到独立 tab 模式（${singleTabCount} 个 flow）
          </button>
        </div>
        <div id="adv-migrate-result" style="margin-top:12px"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>混合模式（P4）</h3>
        <p class="desc">部署单个 flow 时可手动指定目标 tab，实现按房间/场景分组。在部署 flow 时填写「目标 tab」即可。</p>
        <div class="desc" style="font-size:12px;color:var(--text-muted)">
          示例：指定 target_tab="客厅"，该 flow 将部署到「客厅」tab 中，与其他模式的 flow 共存。
        </div>
      </div>`;
    const sel = $("#adv-tab-mode");
    if (sel) {
      sel.onchange = () => {
        const desc = $("#adv-mode-desc");
        if (desc) {
          desc.innerHTML = sel.value === "single_tab" ? `
            <b>单 tab 集中模式：</b>所有 AutoFlow 部署的 flow 合并到固定的「AutoFlow」tab 中，每个 flow 用 comment 节点（AF_START/AF_END）标记边界，方便搜索定位。每个 flow 分配独立的坐标区域，避免视觉重叠。撤回时按节点 ID 精确删除，不会误伤其他 flow。
          ` : `
            <b>每个 flow 独立 tab：</b>每个 AutoFlow 部署的 flow 创建独立的 Node-RED tab，互不干扰。适合 flow 数量较少、每个 flow 较复杂需要独立查看的场景。
          `;
        }
      };
    }
    const saveBtn = $("#adv-save-mode");
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const mode = $("#adv-tab-mode").value;
        saveBtn.disabled = true;
        saveBtn.textContent = "保存中…";
        try {
          const r = await api("PUT", "/settings", { tab_org_mode: mode });
          if (r.ok) {
            $("#adv-save-hint").textContent = "✅ 已保存，新部署的 flow 将按新模式组织";
            setTimeout(() => { $("#adv-save-hint").textContent = ""; }, 5000);
          } else {
            $("#adv-save-hint").textContent = "❌ 保存失败：" + (r.data?.error || r.status);
          }
        } catch (e) {
          $("#adv-save-hint").textContent = "❌ 保存出错：" + e.message;
        }
        saveBtn.disabled = false;
        saveBtn.textContent = "保存设置";
      };
    }

    // P2: 迁移按钮事件
    const migrateSingleBtn = $("#adv-migrate-to-single");
    const migratePerflowBtn = $("#adv-migrate-to-perflow");
    const migrateResult = $("#adv-migrate-result");

    async function doMigrate(targetMode, btn) {
      if (!(await confirmDialog(`确定要将所有 flow 迁移到${targetMode === "single_tab" ? "单 tab" : "独立 tab"}模式吗？\n\n建议先备份 Node-RED flows。迁移过程中 NR 可能短暂不可用。`))) return;
      btn.disabled = true;
      btn.textContent = "迁移中…";
      migrateResult.innerHTML = `<div class="desc">正在迁移，请稍候…</div>`;
      try {
        const r = await api("POST", "/tab-org/migrate", { target_mode: targetMode });
        if (r.ok && r.data?.ok) {
          const migrated = r.data.migrated || [];
          const failed = r.data.failed || [];
          migrateResult.innerHTML = `
            <div style="padding:10px;background:#ecfdf5;border-radius:8px;border-left:4px solid #10b981">
              <b>迁移完成</b>：成功 ${migrated.length} 个，失败 ${failed.length} 个
              ${failed.length > 0 ? `<br><span style="color:#dc2626;font-size:12px">${failed.map(f => f.flow_id + ": " + f.error).join("<br>")}</span>` : ""}
            </div>`;
          setTimeout(() => loadAdvancedSettings(), 2000);
        } else {
          migrateResult.innerHTML = `<div style="padding:10px;background:#fef2f2;border-radius:8px;border-left:4px solid #ef4444"><b>迁移失败</b>：${r.data?.error || r.status}</div>`;
        }
      } catch (e) {
        migrateResult.innerHTML = `<div style="padding:10px;background:#fef2f2;border-radius:8px;border-left:4px solid #ef4444"><b>迁移出错</b>：${e.message}</div>`;
      }
      btn.disabled = false;
      btn.textContent = targetMode === "single_tab" ? `迁移到单 tab 模式（${perFlowCount} 个 flow）` : `迁移到独立 tab 模式（${singleTabCount} 个 flow）`;
    }

    if (migrateSingleBtn) migrateSingleBtn.onclick = () => doMigrate("single_tab", migrateSingleBtn);
    if (migratePerflowBtn) migratePerflowBtn.onclick = () => doMigrate("per_flow", migratePerflowBtn);
  } catch (e) {
    body.innerHTML = errBox(e.message || "加载失败", loadAdvancedSettings);
  }
}

async function loadAudit() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="card"><h3>操作日志</h3><p class="desc">去人审后唯一可追溯性来源（部署 / 应用修正均留痕）。</p><div id="audit-list"><div class="empty">加载中…</div></div></div>`;
  try {
    const r = await api("GET", "/audit?limit=100");
    const items = r.data?.audit || [];
    if (!items.length) { $("#audit-list").innerHTML = `<div class="empty">暂无操作记录。</div>`; return; }
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
    <span class="sub">AutoFlow 与对端服务的对等连接令牌（明文仅显示一次）</span>
    <button class="btn primary" id="acpCreateBtn">＋ 新建令牌</button></div>
    <div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:12px">
      <div style="flex:1;min-width:0">
        <div class="meta">ACP 功能开关</div>
        <div class="sub">关闭后 /acp 停止服务，delegate/ask_llm 工具返回禁用。</div>
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
        toast(sw.checked ? "跨服务连接已启用" : "跨服务连接已关闭");
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
        <div class="meta">出站连接配置（autoflow → 对端）</div>
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
    const st = t.status === "active" ? badge("ok", "有效") : badge("warn", "已停用");
    const last = t.last_seen ? fmtTime(t.last_seen) : "从未使用";
    const revBtn = t.status === "active"
      ? `<button class="btn sm" data-rev="${esc(t.token_id)}">停用</button>` : `<span class="sub">已停用</span>`;
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
    if (!name) { toast("请填写名称"); return; }
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
  modal("ACP 令牌已生成（仅此一次，请立即保存）", `
    <div class="desc">请立即复制并写入对端（memory-worker）的 <code>AUTOFLOW_ACP_TOKEN</code> 环境变量。关闭后不可再查看。</div>
    <div class="card" style="word-break:break-all;font-family:monospace;padding:10px;margin-top:8px">${esc(plain)}</div>
    <div class="modal-foot">
      <button class="btn ghost" id="acpClose">我已保存，关闭</button>
      <button class="btn primary" id="acpCopy">复制</button>
    </div>`);
  $("#acpCopy").onclick = () => {
    safeCopy(plain).then(ok => {
      if (ok) toast("已复制"); else toast("复制失败，请手动选择", "error");
    });
  };
  $("#acpClose").onclick = () => { closeModal(); loadAcpTokens(); };
}

async function revokeAcpToken(id) {
  if (!(await confirmDialog("停用该 ACP 令牌？停用后该对端无法再调 /acp（哈希保留用于审计）。"))) return;
  try {
    const r = await api("POST", `/acp/tokens/${id}/revoke`);
    if (!r.ok) throw new Error(r.data?.error || "停用失败");
    toast("已停用");
    loadAcpTokens();
  } catch (e) { toast(e.message || "停用失败"); }
}

async function deleteAcpToken(id) {
  if (!(await confirmDialog("⚠️ 彻底删除该令牌？含记录一并删除，不可恢复。确定？"))) return;
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
    <span class="sub">OpenAI 兼容接口 · 配置存本地，密钥不回显</span></div>
    <div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:12px">
      <div style="flex:1;min-width:0">
        <div class="meta">启用 LLM 助手</div>
        <div class="sub">关闭后「AI 对话」页不可用（免重启生效）。</div>
      </div>
      <label style="display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap">
        <input type="checkbox" id="llmEnabled" style="width:18px;height:18px" />
        <span class="sub" id="llmEnabledState">读取中…</span>
      </label>
    </div>
    <div class="card account-pool" style="margin-bottom:12px">
      <div class="meta" style="font-size:15px;font-weight:600">模型服务列表</div>
      <div class="sub" style="margin:2px 0 10px">按列表顺序依次尝试，遇限流 / 超时 / 鉴权失败自动切换下一个。优先级 = 列表顺序（可上移 / 下移）。</div>
      <div class="conn-actions" style="margin-bottom:10px">
        <button class="btn sm primary" id="llmAddBackend">+ 新增模型服务</button>
        <button class="btn sm" id="llmTestAll">测试全部</button>
        <span class="sub" id="llmTestAllState"></span>
      </div>
      <div id="llmPool"></div>
      <details style="margin-top:8px">
        <summary class="sub" style="cursor:pointer">兼容模式：单服务（可选）</summary>
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
      pool.innerHTML = `<div class="sub" style="padding:6px 0">尚未添加后端。点「+ 新增模型服务」开始；或填写下方单后端。</div>`;
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
            <div><label class="lbl">最大输出长度（tokens）</label><input class="inp bc-maxtok" type="number" value="${b.max_tokens ?? 4096}" /></div>
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
    if (!payload.url || !payload.model) { resEl.textContent = "⚠ 需填地址与模型"; return; }
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
      ? `<div class="desc">✅ 模型服务已配置（${esc(c.model || (backends[0] && backends[0].model) || "")}）。保存后免重启生效。</div>`
      : `<div class="desc">⚠️ 尚未配置模型服务。填好地址 / API Key / Model 后点保存。</div>`;
  } catch (e) {
    $("#llmCfgState").innerHTML = errBox(e.message || "加载失败", loadLlmSettings);
    sw.checked = false; st.textContent = "读取失败";
  }

  if (sw) sw.onchange = async () => {
    try {
      const r = await api("PUT", "/llm/config", { enabled: sw.checked });
      if (!r.ok) throw new Error(r.data?.error || "切换失败");
      st.textContent = sw.checked ? "已启用" : "已关闭";
      toast(sw.checked ? "大模型已启用" : "大模型已关闭");
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
      toast("大模型配置已保存");
      keyI.value = ""; singleKeyTouched = false;
      backends.forEach((b) => { b._keyTouched = false; });
      const cs = $("#llmCfgState");
      if (cs) cs.innerHTML = `<div class="desc">✅ 模型服务已配置。保存后免重启生效。</div>`;
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

// ── 在线更新（方案 C · 受控自更新）──
async function loadUpdate() {
  const v = $("#view-update");
  v.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await api("GET", "/admin/update-check");
    const d = (r.data || {});
    if (!r.ok) throw new Error(d.error || "加载失败");
    if (!d.git_present) {
      v.innerHTML = `<div class="view-head"><h2>在线更新</h2></div>
        <div class="card"><div class="desc">当前环境未启用 git 自更新（容器内未安装 git 或仓库未初始化）。请改用镜像升级，或重建含 git 的镜像后在「设置 → 连接」中配置 <code>AF_REPO_DIR</code>。</div></div>`;
      return;
    }
    const cur = d.current ? d.current.slice(0, 12) : "—";
    const tgt = d.target_commit ? d.target_commit.slice(0, 12) : "—";
    const curVer = d.current_version ? esc(d.current_version) : `<span style="color:var(--muted,#888)">未知</span>`;
    const latestTag = d.latest_tag ? esc(d.latest_tag) : "—";
    const tags = (d.tags || []).map((t) => `<li><code>${esc(t.tag)}</code> · ${esc((t.commit || "").slice(0, 12))}</li>`).join("");
    v.innerHTML = `
      <div class="view-head"><h2>在线更新</h2></div>
      <div class="card">
        <h3>当前状态</h3>
        <div class="desc">
          当前版本：<code>${curVer}</code> · 提交 <code>${esc(cur)}</code><br>
          最新版本 tag：<code>${latestTag}</code> · 提交 <code>${esc(tgt)}</code><br>
          ${d.available ? `<b>可更新到 <code>${esc(d.target_ref || "")}</code></b>` : "<b>已是最新版本</b>"}
          ${!d.available && d.current_version ? "" : ""}
        </div>
        <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          ${d.available ? `<button class="btn primary" id="doUpdate">更新到 ${esc(d.target_ref || "最新版")}</button>` : ""}
          <select id="updateMirror" class="input" style="max-width:280px">
            <option value="">GitHub 直连（默认）</option>
            <option value="https://ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 镜像</option>
            <option value="https://mirror.ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 备用</option>
            <option value="https://gitclone.com/github.com/lidicn/AutoFlow.git">gitclone 镜像</option>
            <option value="https://kkgithub.com/lidicn/AutoFlow.git">kkgithub 镜像</option>
            <option value="https://hub.gitmirror.com/https://github.com/lidicn/AutoFlow.git">gitmirror 镜像</option>
            <option value="__custom__">自定义镜像…</option>
          </select>
          <input type="text" id="updateMirrorCustom" class="input" style="max-width:280px;display:none;margin-top:6px" placeholder="输入镜像 URL，如 https://xxx/https://github.com/...">
          <span class="meta" id="mirrorHint" style="font-size:12px;color:var(--text-muted)">国内网络建议选镜像；若全部失败可手动 SCP 离线更新</span>
        </div>
        <div id="updateProgress" style="margin-top:12px;display:none">
          <div class="tutorial-progress-bar" style="height:8px;margin-bottom:6px"><div class="tutorial-progress-fill" id="updateProgressFill" style="width:0%"></div></div>
          <div class="desc" id="updateProgressText" style="font-size:13px">正在备份…</div>
        </div>
        <div id="updateMsg" class="desc" style="margin-top:10px"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>版本更新日志</h3>
        <div id="changelog-list">${renderChangelog()}</div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>历史版本</h3>
        <ul class="desc">${tags || "<li>无</li>"}</ul>
      </div>`;
    const btn = $("#doUpdate");
    if (btn) btn.onclick = doUpdate;
    // 自定义镜像切换
    const mirrorSel = $("#updateMirror");
    if (mirrorSel) {
      mirrorSel.onchange = () => {
        const customInput = $("#updateMirrorCustom");
        if (customInput) customInput.style.display = mirrorSel.value === "__custom__" ? "block" : "none";
      };
    }
  } catch (e) {
    v.innerHTML = errBox(e.message || "加载失败", loadUpdate);
  }
}
async function doUpdate() {
  const msg = $("#updateMsg");
  const prog = $("#updateProgress");
  const progFill = $("#updateProgressFill");
  const progText = $("#updateProgressText");
  let mirror = $("#updateMirror")?.value || "";
  if (mirror === "__custom__") {
    mirror = ($("#updateMirrorCustom")?.value || "").trim();
    if (!mirror) { toast("请输入自定义镜像 URL"); return; }
  }
  const btn = $("#doUpdate");
  if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
  if (prog) { prog.style.display = "block"; }
  if (msg) msg.textContent = "";
  // 模拟进度（后端是同步阻塞，前端用阶段提示）
  const stages = [
    { pct: 15, text: "正在备份当前版本…" },
    { pct: 35, text: mirror ? `正在通过镜像拉取更新…` : "正在从 GitHub 拉取更新…" },
    { pct: 60, text: "正在校验代码…" },
    { pct: 80, text: "正在应用更新…" },
    { pct: 95, text: "即将重启网关…" },
  ];
  let stageIdx = 0;
  const progTimer = setInterval(() => {
    if (stageIdx < stages.length) {
      if (progFill) progFill.style.width = stages[stageIdx].pct + "%";
      if (progText) progText.textContent = stages[stageIdx].text;
      stageIdx++;
    }
  }, 1500);
  try {
    const r = await api("POST", "/admin/self-update", mirror ? { mirror } : {});
    const d = (r.data || {});
    clearInterval(progTimer);
    if (!r.ok || !d.ok) {
      if (progFill) progFill.style.width = "100%";
      if (progFill) progFill.style.background = "var(--danger)";
      if (progText) progText.innerHTML = `<span style="color:var(--danger)">更新失败</span>`;
      if (msg) msg.innerHTML = `<div style="color:var(--danger);padding:10px;background:var(--danger-weak);border-radius:8px;margin-top:8px"><b>更新失败：</b>${esc(d.error || "未知错误")}<br><span style="font-size:12px;opacity:0.8">已自动回滚，未重启。建议切换国内镜像后重试。</span></div>`;
      if (btn) { btn.disabled = false; btn.textContent = "重试更新"; }
      return;
    }
    if (progFill) progFill.style.width = "100%";
    if (progText) progText.innerHTML = `<span style="color:var(--ok)">更新成功</span>`;
    if (msg) msg.innerHTML = `<div style="color:var(--ok);padding:10px;background:var(--ok-weak);border-radius:8px;margin-top:8px"><b>更新成功！</b>版本 <code>${esc((d.target_commit || "").slice(0, 12))}</code>，网关即将重启，请稍候刷新页面。<br>备份：${esc(d.backup || "")}</div>`;
  } catch (e) {
    clearInterval(progTimer);
    if (progFill) progFill.style.width = "100%";
    if (progFill) progFill.style.background = "var(--danger)";
    if (progText) progText.innerHTML = `<span style="color:var(--danger)">请求失败</span>`;
    if (msg) msg.innerHTML = `<div style="color:var(--danger);padding:10px;background:var(--danger-weak);border-radius:8px;margin-top:8px"><b>更新请求出错：</b>${esc(e.message || "")}<br><span style="font-size:12px;opacity:0.8">请检查网络连接，或切换国内镜像后重试。</span></div>`;
    if (btn) { btn.disabled = false; btn.textContent = "重试更新"; }
  }
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
      <span class="badge env">模型服务列表</span>
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
      <div class="input-row">
        <textarea id="llmInput" rows="2" placeholder="输入消息，/ 查看命令，@ 添加上下文" inputmode="text" autocapitalize="none" autocomplete="off" autocorrect="off"></textarea>
        <button class="btn primary send-btn" id="llmSend" type="button" aria-label="发送">
          <img src="/static/icons/ic-send.svg" alt="" loading="lazy" />
        </button>
      </div>
    </div>`;
  const chat = $("#llmChat"), input = $("#llmInput"), send = $("#llmSend");
  const clear = $("#llmClear");
  const buildSel = $("#llmBuild"), modelSel = $("#llmModel");
  // 模式切换时同步提示与可用状态
  function refreshLlmInputState() {
    const isAcp = buildSel && buildSel.value === "acp";
    if (modelSel) modelSel.disabled = isAcp;
    input.placeholder = isAcp
      ? "已选择 ACP 模式，消息将直接委派给对端服务"
      : "输入消息，/ 查看命令，@ 添加上下文";
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

  const SEND_ICON_HTML = `<img src="/static/icons/ic-send.svg" alt="" loading="lazy" />`;
  const SPINNER_HTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="animation:af-spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>`;

  async function sendMsg() {
    const text = input.value.trim();
    if (!text) return;
    const mode = buildSel ? buildSel.value : "autoflow";
    const backendIndex = modelSel ? parseInt(modelSel.value, 10) : -1;
    input.value = "";
    renderBubble({ role: "user", content: text });
    _llmHistory.push({ role: "user", content: text }); _saveLlmChat();
    send.disabled = true; send.innerHTML = SPINNER_HTML;
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
      send.disabled = false; send.innerHTML = SEND_ICON_HTML;
    }
  }
  if (send) send.onclick = sendMsg;
  if (input) input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  // iOS：点击输入条空白处时，把焦点显式交给 textarea（规避绝对/静态定位切换时的焦点丢失）
  const inputBar = $("#llmInputBar");
  if (inputBar && input) {
    inputBar.addEventListener("click", (e) => {
      if (e.target === inputBar || e.target === input || e.target.closest(".input-tools") === null) {
        input.focus();
      }
    });
  }
}

// ════════════════════════════════════════════════════════════════
// 账号登录（WebUI 改造）：登录 / 注册 / 改密 / 会话 / 用户管理
// 对应后端 webui_auth.py。会话 Cookie 由浏览器自动携带（HttpOnly），
// 前端不读不存令牌，只在 401 时弹登录框。CSRF 头由 api() 统一加。
// ════════════════════════════════════════════════════════════════
const afAuth = (() => {
  let state = {
    auth_mode: "password_only", initialized: false, logged_in: false, user: null,
    csrf_header: "x-requested-with", csrf_value: "autoflow",
    min_password_len: 8, roles: ["viewer", "admin", "owner"], registration_open: false,
  };
  let busy = false;

  async function refresh() {
    try {
      const r = await api("GET", "/auth/state");
      if (r.ok) state = Object.assign(state, r.data || {});
    } catch (e) {}
    renderZone();
    return state;
  }

  function renderZone() {
    const z = $("#authZone"); if (!z) return;
    if (state.logged_in && state.user) {
      z.innerHTML = `<button class="btn ghost" id="afUserBtn">${esc(state.user.username)} ▾</button>`;
      $("#afUserBtn").onclick = openMenu;
    } else {
      z.innerHTML = `<button class="btn primary sm" id="afLoginBtn">登录</button>`;
      $("#afLoginBtn").onclick = () => onUnauthorized();
    }
  }

  async function boot() {
    await refresh();
    if (state.logged_in && state.user && state.user.must_change) showChangePassword(true);
  }

  function openMenu() {
    const u = state.user || {};
    const isOwner = u.role === "owner";
    const items = [
      `<button class="btn sm" data-act="pw">修改密码</button>`,
      `<button class="btn sm" data-act="sess">我的会话</button>`,
    ];
    if (isOwner) items.push(`<button class="btn sm" data-act="users">用户管理</button>`);
    items.push(`<button class="btn sm danger" data-act="logout">退出登录</button>`);
    modal("账号：" + esc(u.username || ""),
      `<div class="col gap8">${items.join("")}</div>
       <p class="desc">角色：${esc(u.role || "")}${u.must_change ? "（需修改密码）" : ""}</p>`, null, "关闭");
    $$("#modalBody [data-act]").forEach((b) => {
      b.onclick = () => {
        const a = b.dataset.act;
        closeModal();
        if (a === "pw") showChangePassword(false);
        else if (a === "sess") showSessions();
        else if (a === "users") showUsers();
        else if (a === "logout") logout();
      };
    });
  }

  function onUnauthorized() {
    if (busy) return; busy = true;
    refresh().then(() => {
      if (!state.initialized) showRegister();
      else showLogin();
    }).finally(() => { busy = false; });
  }

  function showLogin() {
    modal("登录 AutoFlow", `
      <div class="field"><label>用户名</label><input id="afUser" autocomplete="username" autofocus></div>
      <div class="field"><label>密码</label><input id="afPass" type="password" autocomplete="current-password"></div>
      <label class="chk"><input type="checkbox" id="afRemember"> 记住我（7 天）</label>
      <div id="afErr" class="errbox" hidden></div>
      <button class="btn primary" id="afLogin">登录</button>`, null, "关闭");
    const go = async () => {
      $("#afErr").hidden = true;
      const r = await api("POST", "/auth/login", {
        username: $("#afUser").value.trim(), password: $("#afPass").value,
        remember: $("#afRemember").checked,
      });
      if (r.ok) location.reload();
      else { $("#afErr").hidden = false; $("#afErr").textContent = (r.data && r.data.error) || "登录失败"; }
    };
    $("#afLogin").onclick = go;
    $("#afPass").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }

  function showRegister() {
    modal("初始化管理员账号", `
      <p class="desc">首次使用，请创建管理员账号（创建后注册入口永久关闭）。</p>
      <div class="field"><label>用户名</label><input id="afUser" autocomplete="username" autofocus></div>
      <div class="field"><label>密码</label><input id="afPass" type="password" autocomplete="new-password"></div>
      <div class="field"><label>确认密码</label><input id="afConf" type="password" autocomplete="new-password"></div>
      <p class="desc">密码至少 ${state.min_password_len} 位，且不能过于简单（不能与用户名相同）。</p>
      <div id="afErr" class="errbox" hidden></div>
      <button class="btn primary" id="afReg">创建并登录</button>`, null, "关闭");
    const go = async () => {
      $("#afErr").hidden = true;
      const pw = $("#afPass").value, conf = $("#afConf").value;
      if (pw !== conf) { $("#afErr").hidden = false; $("#afErr").textContent = "两次输入的密码不一致"; return; }
      const r = await api("POST", "/auth/register", { username: $("#afUser").value.trim(), password: pw, confirm: conf });
      if (r.ok) location.reload();
      else { $("#afErr").hidden = false; $("#afErr").textContent = (r.data && r.data.error) || "创建失败"; }
    };
    $("#afReg").onclick = go;
    $("#afConf").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }

  function showChangePassword(forced) {
    modal(forced ? "请先修改密码" : "修改密码", `
      <div class="field"><label>原密码</label><input id="afOld" type="password" autocomplete="current-password" autofocus></div>
      <div class="field"><label>新密码</label><input id="afNew" type="password" autocomplete="new-password"></div>
      <div class="field"><label>确认新密码</label><input id="afNew2" type="password" autocomplete="new-password"></div>
      <div id="afErr" class="errbox" hidden></div>
      <button class="btn primary" id="afChg">保存</button>
      ${forced ? "" : `<button class="btn ghost" id="afChgCancel">取消</button>`}`, null, forced ? undefined : "取消");
    const go = async () => {
      $("#afErr").hidden = true;
      const nw = $("#afNew").value, n2 = $("#afNew2").value;
      if (nw !== n2) { $("#afErr").hidden = false; $("#afErr").textContent = "两次新密码不一致"; return; }
      const r = await api("POST", "/auth/change-password", { old_password: $("#afOld").value, new_password: nw, confirm: n2 });
      if (r.ok) { toast("密码已更新"); if (forced) location.reload(); else closeModal(); }
      else { $("#afErr").hidden = false; $("#afErr").textContent = (r.data && r.data.error) || "修改失败"; }
    };
    $("#afChg").onclick = go;
    if (!forced && $("#afChgCancel")) $("#afChgCancel").onclick = closeModal;
  }

  async function showSessions() {
    const r = await api("GET", "/auth/sessions");
    if (!r.ok) { toast("加载会话失败"); return; }
    const rows = (r.data && r.data.sessions || []).map((s) => `
      <div class="row between"><span>${esc(s.ip || "")} · ${esc((s.user_agent || "").slice(0, 40))}<br>
      <small>创建 ${esc(s.created_at || "")} · 过期 ${esc(s.expires_at || "")}</small></span>
      <button class="btn sm danger" data-sid="${esc(s.session_id)}">踢出</button></div>`).join("") ||
      `<p class="desc">暂无其它会话</p>`;
    modal("我的会话", `<div class="col gap6">${rows}</div>`);
    $$("#modalBody [data-sid]").forEach((b) => {
      b.onclick = async () => { await api("DELETE", "/auth/sessions/" + b.dataset.sid); showSessions(); };
    });
  }

  async function showUsers() {
    const r = await api("GET", "/auth/users");
    if (!r.ok) { toast("加载用户失败"); return; }
    const me = (state.user && state.user.user_id) || "";
    const rows = (r.data && r.data.users || []).map((u) => `
      <div class="row between"><span>${esc(u.username)} · <b>${esc(u.role)}</b>${u.status !== "active" ? " · <i>已禁用</i>" : ""}</span>
      <span>
        <button class="btn sm" data-reset="${esc(u.user_id)}">重置密码</button>
        ${u.user_id === me ? "" : `<button class="btn sm danger" data-del="${esc(u.user_id)}">删除</button>`}
      </span></div>`).join("") || `<p class="desc">暂无用户</p>`;
    modal("用户管理（仅 owner）", `
      <div class="col gap6">${rows}</div>
      <hr>
      <div class="field"><label>新用户名</label><input id="nuName"></div>
      <div class="field"><label>密码</label><input id="nuPass" type="password" autocomplete="new-password"></div>
      <div class="field"><label>角色</label><select id="nuRole">${state.roles.map((x) => `<option>${x}</option>`).join("")}</select></div>
      <button class="btn primary" id="nuAdd">新增用户</button>`, null, "关闭");
    $("#nuAdd").onclick = async () => {
      const rr = await api("POST", "/auth/users", {
        username: $("#nuName").value.trim(), password: $("#nuPass").value, role: $("#nuRole").value,
      });
      if (rr.ok) showUsers(); else toast((rr.data && rr.data.error) || "创建失败");
    };
    $$("#modalBody [data-reset]").forEach((b) => {
      b.onclick = async () => {
        const pw = (await promptDialog("设置新密码（该用户下次登录需改密）：", "")) ?? "";
        if (!pw) return;
        const rr = await api("POST", "/auth/users/" + b.dataset.reset + "/reset-password", { new_password: pw });
        toast(rr.ok ? "已重置" : ((rr.data && rr.data.error) || "失败"));
      };
    });
    $$("#modalBody [data-del]").forEach((b) => {
      b.onclick = async () => {
        if (!(await confirmDialog("确认删除该用户？"))) return;
        const rr = await api("DELETE", "/auth/users/" + b.dataset.del);
        if (rr.ok) showUsers(); else toast((rr.data && rr.data.error) || "删除失败");
      };
    });
  }

  async function logout() {
    await api("POST", "/auth/logout");
    location.reload();
  }

  return { boot, refresh, renderZone, onUnauthorized, state: () => state };
})();
window.__afAuth = afAuth;

// 启动
(async () => {
  if (window.__afAuth) { try { await window.__afAuth.boot(); } catch (e) {} }
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

// ═══════════════════════════════════════════════════════════
// 版本更新日志
// ═══════════════════════════════════════════════════════════
const CHANGELOG = [
  {
    version: "v1.2.4",
    date: "2026-09-02",
    items: [
      "在线更新页面新增版本更新日志，每个版本更新内容一目了然",
      "全面统一页面标题和术语（Agent 管理/提案/原生 flow/权限模式）",
      "概览页快速上手增加教程入口和快捷按钮",
      "安全闸页面文案优化",
    ],
  },
  {
    version: "v1.2.3",
    date: "2026-09-02",
    items: [
      "在线更新新增国内镜像选择（ghproxy/gitclone），更新失败率大幅降低",
      "更新过程增加进度条和阶段提示，失败时红色提示框建议切换镜像",
      "ACP 令牌页面标题明示与 memory-agent 对接，移除内部名称",
      "教程系统重构：新增「两种使用途径」排第一，8教程37步精简为6教程25步",
      "教程新增黑白箱模式概念解释，Link API 教程增加示范链接",
    ],
  },
  {
    version: "v1.2.2",
    date: "2026-09-02",
    items: [
      "WebUI 文案 v2：面向 hassbian 极客用户，保留技术术语不做过度白话化",
      "新增交互式教程系统：8个教程37步，含进度持久化和步骤跳转",
      "核心页面首次访问自动弹出功能引导",
      "侧边导航16项分4组，提案/已部署列表增加状态色条",
      "Toast 通知支持 success/error/warn/info 四种类型",
      "帮助系统新增核心概念详解（DSL/安全闸/vhass/自动修复/Link API等7个概念）",
      "顶栏4个开关按钮增加文字标签",
    ],
  },
  {
    version: "v1.2.1",
    date: "2026-08-28",
    items: [
      "受控自更新机制稳定化：备份→fetch→checkout→py_compile→重启全链路",
      "更新失败自动回滚，不重启不破坏现有环境",
    ],
  },
  {
    version: "v1.2.0",
    date: "2026-08-20",
    items: [
      "ACP 对等连接协议支持，可与 memory-agent 等外部服务对接",
      "子流程注册表：内置 managed + 已导入 imported 两类管理",
      "Link API：HTTP 桥接，支持 link out 推送和 http_api 内联两种模式",
    ],
  },
  {
    version: "v1.1.0",
    date: "2026-08-10",
    items: [
      "自动修复（自愈闭环）：inject点火→debug回读→分析→apply修正循环",
      "安全闸 Tier-0/Tier-1 两级设备保护",
      "vhass 虚拟孪生重放：staging 环境虚拟验证",
    ],
  },
  {
    version: "v1.0.0",
    date: "2026-07-28",
    items: [
      "AutoFlow 网关首个正式版本",
      "DSL 编译器：结构化描述转标准 NR 节点，不含 Function 节点故可信",
      "MCP 服务器：标准/专家/管理员三种权限模式",
      "提案→审核→部署→已部署全流程管理",
      "WebUI 管理界面",
    ],
  },
];

function renderChangelog() {
  return CHANGELOG.map((rel) => `
    <div class="changelog-item" style="margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
        <span style="font-weight:700;font-size:15px">${esc(rel.version)}</span>
        <span style="font-size:12px;color:var(--text-muted)">${esc(rel.date)}</span>
      </div>
      <ul style="margin:0;padding-left:20px;font-size:13px;line-height:1.8;color:var(--text)">
        ${rel.items.map((item) => `<li>${esc(item)}</li>`).join("")}
      </ul>
    </div>
  `).join("");
}
