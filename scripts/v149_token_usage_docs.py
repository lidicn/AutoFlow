#!/usr/bin/env python3
"""增加授权码使用方法文案"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在授权码管理页面顶部增加使用说明卡片
old_page = '''  v.innerHTML = `
    <div class="view-head">
      <h2>部署授权码</h2>
      <span class="sub">给受信任 Agent 发放授权码，可在限定范围内自动部署 Flow</span>
      <button class="btn primary" id="dt-create-btn" style="margin-left:auto">➕ 创建授权码</button>
    </div>
    <div id="dt-list"><div class="empty">加载中…</div></div>
    <div id="dt-detail" hidden></div>`;'''

new_page = '''  v.innerHTML = `
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
    <div id="dt-detail" hidden></div>`;'''

if old_page in content:
    content = content.replace(old_page, new_page, 1)
    print("1. 管理页面增加使用说明: OK")
else:
    print("1. 管理页面增加使用说明: NOT FOUND")

# 2. 在创建授权码弹窗顶部增加简要说明
old_modal = '''  modal("创建部署授权码", `
    <div class="field">
      <label>名称 *</label>'''

new_modal = '''  modal("创建部署授权码", `
    <div class="card" style="background:var(--bg-soft);border-left:3px solid var(--primary);margin-bottom:16px;padding:10px 14px">
      <div style="font-size:12px;line-height:1.6">
        <b>授权码作用</b>：给受信任 Agent 发放后，Agent 可通过 MCP 自动部署 Flow，无需你在 WebUI 手动审批。
        创建后请立即复制授权码并告知 Agent（如"使用授权码 xxxx"），授权码只显示一次。
      </div>
    </div>
    <div class="field">
      <label>名称 *</label>'''

if old_modal in content:
    content = content.replace(old_modal, new_modal, 1)
    print("2. 创建弹窗增加说明: OK")
else:
    print("2. 创建弹窗增加说明: NOT FOUND")

# 3. 在创建成功弹窗中增加使用步骤
old_success = '''          <div class="meta" style="font-size:12px;color:var(--text-muted)">
            <p>目标 tab: <b>${esc((token.target_tabs && token.target_tabs.length) ? token.target_tabs.join(", ") : (token.target_tab || "不绑定（per_flow 模式）"))}</b></p>
            <p>有效期: ${esc((token.expires_at || "").slice(0, 19).replace("T", " "))}</p>
            <p>权限: ${(token.permissions || []).join(", ")}</p>
          </div>'''

new_success = '''          <div class="meta" style="font-size:12px;color:var(--text-muted)">
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
          </div>'''

if old_success in content:
    content = content.replace(old_success, new_success, 1)
    print("3. 成功弹窗增加使用步骤: OK")
else:
    print("3. 成功弹窗增加使用步骤: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
