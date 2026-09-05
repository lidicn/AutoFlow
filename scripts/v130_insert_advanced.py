#!/usr/bin/env python3
"""在 loadAudit 之前插入 loadAdvancedSettings 函数"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

old_audit = '''async function loadAudit() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="card"><h3>操作日志</h3><p class="desc">去人审后唯一可追溯性来源（部署 / 应用修正均留痕）。</p><div id="audit-list"><div class="empty">加载中…</div></div></div>`;'''

new_advanced = '''// ── 高级设置 ──
async function loadAdvancedSettings() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await api("GET", "/config");
    if (!r.ok) throw new Error(r.data?.error || "加载失败");
    const cfg = r.data || {};
    const currentMode = cfg.tab_org_mode || "per_flow";
    body.innerHTML = `
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
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button class="btn primary" id="adv-save-mode">保存设置</button>
          <span id="adv-save-hint" class="meta" style="font-size:12px"></span>
        </div>
        <div class="desc" style="margin-top:12px;font-size:12px;color:var(--text-muted)">
          ⚠️ 切换模式不会自动迁移已部署的 flow。如需迁移，请先撤回旧 flow，再在新模式下重新部署。
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>其他高级选项</h3>
        <div class="desc">更多高级设置将在后续版本中开放。</div>
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
          const r = await api("POST", "/settings", { tab_org_mode: mode });
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
  } catch (e) {
    body.innerHTML = errBox(e.message || "加载失败", loadAdvancedSettings);
  }
}

async function loadAudit() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="card"><h3>操作日志</h3><p class="desc">去人审后唯一可追溯性来源（部署 / 应用修正均留痕）。</p><div id="audit-list"><div class="empty">加载中…</div></div></div>`;'''

if old_audit in content:
    content = content.replace(old_audit, new_advanced, 1)
    with open(APP, "w", encoding="utf-8") as f:
        f.write(content)
    print("loadAdvancedSettings 函数已插入")
else:
    print("ERROR: 未找到 loadAudit 位置")
