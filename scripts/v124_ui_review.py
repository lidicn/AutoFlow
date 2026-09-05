#!/usr/bin/env python3
"""v1.2.4 全面 UI/文案审查修改 + 版本简介"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    js = f.read()

# ═══════════════════════════════════════════════════════════
# 1. 统一页面标题（去掉 emoji，保持专业一致）
# ═══════════════════════════════════════════════════════════
js = js.replace('<h2>🛡️ 安全闸</h2>', '<h2>安全闸</h2>', 1)
js = js.replace('<h2>⚙️ 设置</h2>', '<h2>设置</h2>', 1)

# ═══════════════════════════════════════════════════════════
# 2. Agent 管理页面：标题统一 + 术语统一
# ═══════════════════════════════════════════════════════════
js = js.replace('<div class="view-head"><h2>Agents</h2></div>',
                '<div class="view-head"><h2>Agent 管理</h2><span class="sub">创建和管理接入 AutoFlow 的 AI 客户端</span></div>', 1)
js = js.replace('<label>身份模式</label>', '<label>权限模式</label>', 1)

# ═══════════════════════════════════════════════════════════
# 3. 提案页面：标题统一为"提案"
# ═══════════════════════════════════════════════════════════
js = js.replace('<h2>待审核流程</h2><span class="sub">Agent 提交的 DSL/flow，经安全闸验证后可部署到 Node-RED</span>',
                '<h2>提案</h2><span class="sub">Agent 提交的 flow，经安全闸验证后可部署到 Node-RED</span>', 1)

# ═══════════════════════════════════════════════════════════
# 4. 概览页面：术语统一 + 快速上手链接到教程
# ═══════════════════════════════════════════════════════════
js = js.replace('<div class="meta">待审 raw flow</div>', '<div class="meta">待审原生 flow</div>', 1)

# 快速上手部分替换为新版本（链接到教程）
old_quickstart = '''      <div class="card" style="margin-top:14px">
        <h3>快速上手</h3>
        <div class="desc">
          1. 到 <b>Agent 管理</b> 创建 Agent（如 deepseek++），复制生成的接入令牌。<br>
          2. 在 agent 的 MCP 配置里填 <code>Authorization: Bearer &lt;接入令牌&gt;</code>，指向 <code>${esc(c.mcp || "")}</code>。<br>
          3. agent 提交场景 DSL 后，到 <b>提案</b> 面板查看闸门结果，点「部署到 NR」部署。<br>
          4. 部署后在 <b>已部署</b> 面板可随时安全撤回（仅移除本网关部署的 flow）。<br>
          5. <b>笔记</b> 记录你那些暂时落不了地的智能家居想法。
        </div>
      </div>`;'''
new_quickstart = '''      <div class="card" style="margin-top:14px">
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
    if (gl) gl.onclick = () => setTab("llm_agent");'''
js = js.replace(old_quickstart, new_quickstart, 1)

# ═══════════════════════════════════════════════════════════
# 5. 安全闸页面：文案优化
# ═══════════════════════════════════════════════════════════
js = js.replace('Tier-0 触及需人工确认；Tier-1 放行但记审计。',
                'Tier-0 触及需人工确认；Tier-1 放行但记录操作日志。', 1)

# ═══════════════════════════════════════════════════════════
# 6. 在线更新页面：增加版本简介（CHANGELOG）
# ═══════════════════════════════════════════════════════════
# 在 "可用版本 tag" 卡片之前插入版本简介
old_tags_card = '''      <div class="card" style="margin-top:14px">
        <h3>可用版本 tag</h3>
        <ul class="desc">${tags || "<li>无</li>"}</ul>
      </div>`;'''
new_tags_card = '''      <div class="card" style="margin-top:14px">
        <h3>版本更新日志</h3>
        <div id="changelog-list">${renderChangelog()}</div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>历史版本</h3>
        <ul class="desc">${tags || "<li>无</li>"}</ul>
      </div>`;'''
js = js.replace(old_tags_card, new_tags_card, 1)

# ═══════════════════════════════════════════════════════════
# 7. 在文件末尾添加 CHANGELOG 数据和渲染函数
# ═══════════════════════════════════════════════════════════
changelog_code = '''

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
'''

# 在文件末尾添加
js = js.rstrip() + changelog_code

with open(APP, "w", encoding="utf-8") as f:
    f.write(js)

print("app.js: UI/copy review changes applied")
print("- Page titles unified (emoji removed)")
print("- Agent management title + terminology unified")
print("- Proposals page title unified")
print("- Dashboard quickstart updated with tutorial links")
print("- Safety gate copy optimized")
print("- CHANGELOG added to update page (7 versions)")
