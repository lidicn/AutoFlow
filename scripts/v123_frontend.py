#!/usr/bin/env python3
"""v1.2.3 前端修改：更新页面 + ACP文案"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    js = f.read()

# ═══════════════════════════════════════════════════════════
# 1. 更新页面：去掉副标题，增加镜像选择、进度条
# ═══════════════════════════════════════════════════════════

# 去掉副标题
js = js.replace(
    '<div class="view-head"><h2>在线更新</h2><span class="sub">从 GitHub 拉取更新并自动部署（受控自更新）</span></div>',
    '<div class="view-head"><h2>在线更新</h2></div>',
    1
)

# 更新按钮区域：增加镜像选择下拉框
old_update_btn = '''        ${d.available ? `<button class="btn primary" id="doUpdate" style="margin-top:10px">更新到 ${esc(d.target_ref || "最新版")}</button>` : ""}
        <div id="updateMsg" class="desc" style="margin-top:10px"></div>'''
new_update_btn = '''        <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          ${d.available ? `<button class="btn primary" id="doUpdate">更新到 ${esc(d.target_ref || "最新版")}</button>` : ""}
          <select id="updateMirror" class="input" style="max-width:280px">
            <option value="">GitHub 直连（默认）</option>
            <option value="https://ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 镜像</option>
            <option value="https://gitclone.com/github.com/lidicn/AutoFlow.git">gitclone 镜像</option>
            <option value="https://mirror.ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 备用镜像</option>
          </select>
          <span class="meta" id="mirrorHint" style="font-size:12px;color:var(--text-muted)">国内网络建议选镜像</span>
        </div>
        <div id="updateProgress" style="margin-top:12px;display:none">
          <div class="tutorial-progress-bar" style="height:8px;margin-bottom:6px"><div class="tutorial-progress-fill" id="updateProgressFill" style="width:0%"></div></div>
          <div class="desc" id="updateProgressText" style="font-size:13px">正在备份…</div>
        </div>
        <div id="updateMsg" class="desc" style="margin-top:10px"></div>'''
js = js.replace(old_update_btn, new_update_btn, 1)

# doUpdate 函数：增加进度条和镜像参数
old_do_update = '''async function doUpdate() {
  const msg = $("#updateMsg");
  if (msg) msg.textContent = "正在备份并拉取更新，完成后网关会自动重启（约数秒）…";
  try {
    const r = await api("POST", "/admin/self-update", {});
    const d = (r.data || {});
    if (!r.ok || !d.ok) {
      if (msg) msg.innerHTML = `<span style="color:var(--danger,#c0392b)">更新失败：${esc(d.error || "未知错误")}</span>（已自动回滚，未重启）`;
      return;
    }
    if (msg) msg.innerHTML = `已应用更新（<code>${esc((d.target_commit || "").slice(0, 12))}</code>），网关即将重启，请稍候刷新页面。<br>备份：${esc(d.backup || "")}`;
  } catch (e) {
    if (msg) msg.textContent = "更新请求出错：" + (e.message || "");
  }
}'''
new_do_update = '''async function doUpdate() {
  const msg = $("#updateMsg");
  const prog = $("#updateProgress");
  const progFill = $("#updateProgressFill");
  const progText = $("#updateProgressText");
  const mirror = $("#updateMirror")?.value || "";
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
}'''
js = js.replace(old_do_update, new_do_update, 1)

# ═══════════════════════════════════════════════════════════
# 2. ACP 令牌页面：副标题明示跟 memory-agent 对接，去掉 memory-worker
# ═══════════════════════════════════════════════════════════

# 找到 ACP 页面的渲染
js = js.replace(
    'v.innerHTML = `<div class="view-head"><h2>ACP 对等令牌</h2><span class="sub">AutoFlow 与对端服务的对等连接令牌（明文仅显示一次）</span></div>',
    'v.innerHTML = `<div class="view-head"><h2>ACP 对等令牌</h2><span class="sub">AutoFlow 与 memory-agent 的对等连接密钥（明文仅显示一次）</span></div>',
    1
)

# 去掉"改名前叫 memory-worker"
js = js.replace(
    '改名前叫 memory-worker',
    '',
    1
)

# 出向连接配置标题
js = js.replace(
    '出向委派配置（autoflow → memory-agent）',
    '出站连接配置（AutoFlow → memory-agent）',
    1
)

with open(APP, "w", encoding="utf-8") as f:
    f.write(js)

print("app.js: update page redesigned + ACP copy updated")
