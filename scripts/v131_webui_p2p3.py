#!/usr/bin/env python3
"""修改 app.js 高级设置页面：增加 P2 迁移功能 + P3 预警显示"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# 替换 loadAdvancedSettings 函数，增加迁移功能和预警显示
old_func_start = '''// ── 高级设置 ──
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
      </div>`;'''

new_func_start = '''// ── 高级设置 ──
async function loadAdvancedSettings() {
  const body = $("#settings-body");
  body.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    // 同时加载 config 和 tab-org 状态
    const [cfgR, statusR] = await Promise.all([
      api("GET", "/config"),
      api("GET", "/tab-org/status").catch(() => ({ ok: false, data: {} }))
    ]);
    if (!cfgR.ok) throw new Error(cfgR.data?.error || "加载失败");
    const cfg = cfgR.data || {};
    const status = statusR.data || {};
    const currentMode = cfg.tab_org_mode || "per_flow";
    const perFlowCount = status.per_flow_count || 0;
    const singleTabCount = status.single_tab_count || 0;
    const warning = status.warning;

    body.innerHTML = `
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
      </div>`;'''

if old_func_start in content:
    content = content.replace(old_func_start, new_func_start, 1)
    print("1. 替换 loadAdvancedSettings 函数开头")
else:
    print("WARNING: 未找到 loadAdvancedSettings 函数开头")

# 增加迁移按钮的事件处理（在 saveBtn 事件处理之后）
old_save_end = '''        saveBtn.disabled = false;
        saveBtn.textContent = "保存设置";
      };
    }
  } catch (e) {
    body.innerHTML = errBox(e.message || "加载失败", loadAdvancedSettings);
  }
}'''

new_save_end = '''        saveBtn.disabled = false;
        saveBtn.textContent = "保存设置";
      };
    }

    // P2: 迁移按钮事件
    const migrateSingleBtn = $("#adv-migrate-to-single");
    const migratePerflowBtn = $("#adv-migrate-to-perflow");
    const migrateResult = $("#adv-migrate-result");

    async function doMigrate(targetMode, btn) {
      if (!confirm(`确定要将所有 flow 迁移到${targetMode === "single_tab" ? "单 tab" : "独立 tab"}模式吗？\\n\\n建议先备份 Node-RED flows。迁移过程中 NR 可能短暂不可用。`)) return;
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
}'''

if old_save_end in content:
    content = content.replace(old_save_end, new_save_end, 1)
    print("2. 增加迁移按钮事件处理")
else:
    print("WARNING: 未找到 saveBtn 事件处理结尾")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)

print("\napp.js 修改完成")
