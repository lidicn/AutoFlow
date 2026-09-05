#!/usr/bin/env python3
"""P4 WebUI 目标 tab 选择器：后端 + 前端"""

# 1. 修改 webui.py: deploy_proposal 增加 target_tab 参数
WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

old_deploy = '''        require_e2e = b.get("require_e2e", None)
        # allow_prod：人手动部署默认 True（显式授权写 prod）；如需强制守卫可传 false。
        allow_prod = b.get("allow_prod", True)
        try:
            res = await asyncio.to_thread(gw.deploy_proposal, pid, agent_id="human",
                                          target=target, force=force, validate=validate,
                                          require_e2e=require_e2e, allow_prod=allow_prod)'''

new_deploy = '''        require_e2e = b.get("require_e2e", None)
        # allow_prod：人手动部署默认 True（显式授权写 prod）；如需强制守卫可传 false。
        allow_prod = b.get("allow_prod", True)
        # P4 混合模式：用户可在 WebUI 部署时指定目标 tab
        target_tab = b.get("target_tab") or None
        if target_tab:
            target_tab = str(target_tab).strip() or None
        try:
            res = await asyncio.to_thread(gw.deploy_proposal, pid, agent_id="human",
                                          target=target, force=force, validate=validate,
                                          require_e2e=require_e2e, allow_prod=allow_prod,
                                          target_tab=target_tab)'''

if old_deploy in content:
    content = content.replace(old_deploy, new_deploy, 1)
    print("1. 后端 deploy_proposal 增加 target_tab 参数")
else:
    print("WARNING: 未找到 deploy_proposal 调用")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

# 2. 修改前端 app.js: deployProposal 增加目标 tab 选择器
APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# 替换 deployProposal 函数的确认对话框部分
old_confirm = '''async function deployProposal(id) {
  const p = _allProposals.find((x) => x.id === id);
  let isSub = false;
  try { isSub = !!(p && (p.kind === "subflow" || JSON.parse(p.content || "{}").type === "subflow")); } catch (e) {}
  const msg = isSub
    ? "确定注册该子流程到网关？\\n（写 NR 子流程实例 + 登记子流程注册表，注册后 agent 可经 MCP 调用。冲突或失败不会动 NR。）"
    : "确定部署到 Node-RED？部署后可在「已部署」安全撤回。";
  if (!confirm(msg)) return;
  const r = await api("POST", `/proposals/${id}/deploy`, { target: "prod" });'''

new_confirm = '''async function deployProposal(id) {
  const p = _allProposals.find((x) => x.id === id);
  let isSub = false;
  let proposalTargetTab = "";
  try {
    const c = JSON.parse(p.content || "{}");
    isSub = !!(p && (p.kind === "subflow" || c.type === "subflow"));
    proposalTargetTab = c.target_tab || "";
  } catch (e) {}
  if (isSub) {
    if (!confirm("确定注册该子流程到网关？\\n（写 NR 子流程实例 + 登记子流程注册表，注册后 agent 可经 MCP 调用。冲突或失败不会动 NR。）")) return;
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
  `);
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
    return _handleDeployResult(r, id);
  };
}

// 部署结果处理（从原 deployProposal 中抽离）
async function _handleDeployResult(r, id) {
  const p = _allProposals.find((x) => x.id === id);'''

if old_confirm in content:
    content = content.replace(old_confirm, new_confirm, 1)
    print("2. 前端 deployProposal 增加目标 tab 选择器")
else:
    print("WARNING: 未找到 deployProposal 确认对话框")

# 增加 _loadNRTabs 辅助函数（在 deployProposal 之前）
old_helper = '''async function deployProposal(id) {'''
new_helper = '''// 加载 Node-RED tab 列表（用于 P4 目标 tab 选择器）
async function _loadNRTabs() {
  try {
    const r = await api("GET", "/catalog");
    if (r.ok && r.data) {
      const flows = r.data.flows || r.data.nr_flows || [];
      return flows.filter(f => f.type !== "subflow").map(f => ({
        id: f.id,
        label: f.label || f.id,
        node_count: (f.nodes || []).length
      }));
    }
  } catch (e) {}
  return [];
}

async function deployProposal(id) {'''

if old_helper in content:
    content = content.replace(old_helper, new_helper, 1)
    print("3. 增加 _loadNRTabs 辅助函数")
else:
    print("WARNING: 未找到 deployProposal 函数定义")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)

print("\nP4 WebUI 目标 tab 选择器完成")
