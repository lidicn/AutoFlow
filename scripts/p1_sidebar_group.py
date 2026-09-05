#!/usr/bin/env python3
"""P1-1: 侧边导航分组"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"
with open(FILE, "r", encoding="utf-8") as f:
    html = f.read()

# 替换整个侧边栏
old_sidebar = '''    <nav class="sidebar" id="sidebar">
      <button class="navitem" data-tab="dashboard"><img class="ic" src="/static/icons/ic-dashboard.svg" alt="" width="20" height="20" />概览</button>
      <button class="navitem" data-tab="tutorials"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:16px">📚</span>教程</button>
      <button class="navitem" data-tab="safe"><img class="ic" src="/static/icons/ic-safe.svg" alt="" width="20" height="20" />安全闸</button>
      <button class="navitem" data-tab="proposals"><img class="ic" src="/static/icons/ic-proposals.svg" alt="" width="20" height="20" />提案</button>
      <button class="navitem" data-tab="deployed"><img class="ic" src="/static/icons/ic-deployed.svg" alt="" width="20" height="20" />已部署</button>
      <button class="navitem" data-tab="subflows"><img class="ic" src="/static/icons/ic-subflows.svg" alt="" width="20" height="20" />子流程</button>
      <button class="navitem" data-tab="link_apis"><img class="ic" src="/static/icons/ic-link.svg" alt="" width="20" height="20" />Link API</button>
      <button class="navitem" data-tab="agents"><img class="ic" src="/static/icons/ic-agents.svg" alt="" width="20" height="20" />Agent 管理</button>
      <button class="navitem" data-tab="diagnostics"><img class="ic" src="/static/icons/ic-diagnostics.svg" alt="" width="20" height="20" />诊断</button>
      <button class="navitem" data-tab="notes"><img class="ic" src="/static/icons/ic-notes.svg" alt="" width="20" height="20" />笔记</button>
      <button class="navitem" data-tab="settings"><img class="ic" src="/static/icons/ic-settings.svg" alt="" width="20" height="20" />设置</button>
      <button class="navitem" data-tab="help"><img class="ic" src="/static/icons/ic-help.svg" alt="" width="20" height="20" />帮助</button>
      <button class="navitem" data-tab="acp_tokens"><img class="ic" src="/static/icons/ic-acp.svg" alt="" width="20" height="20" />ACP 令牌</button>
      <button class="navitem" data-tab="llm_settings"><img class="ic" src="/static/icons/ic-llm-settings.svg" alt="" width="20" height="20" />大模型设置</button>
      <button class="navitem" data-tab="llm_agent"><img class="ic" src="/static/icons/ic-llm.svg" alt="" width="20" height="20" />AI 对话</button>
      <button class="navitem" data-tab="update"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:15px">⬆️</span>系统更新</button>
    </nav>'''

new_sidebar = '''    <nav class="sidebar" id="sidebar">
      <div class="nav-group">
        <div class="nav-group-title">核心</div>
        <button class="navitem" data-tab="dashboard"><img class="ic" src="/static/icons/ic-dashboard.svg" alt="" width="20" height="20" />概览</button>
        <button class="navitem" data-tab="tutorials"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:16px">📚</span>教程</button>
        <button class="navitem" data-tab="proposals"><img class="ic" src="/static/icons/ic-proposals.svg" alt="" width="20" height="20" />提案</button>
        <button class="navitem" data-tab="deployed"><img class="ic" src="/static/icons/ic-deployed.svg" alt="" width="20" height="20" />已部署</button>
        <button class="navitem" data-tab="llm_agent"><img class="ic" src="/static/icons/ic-llm.svg" alt="" width="20" height="20" />AI 对话</button>
      </div>
      <div class="nav-group">
        <div class="nav-group-title">管理</div>
        <button class="navitem" data-tab="subflows"><img class="ic" src="/static/icons/ic-subflows.svg" alt="" width="20" height="20" />子流程</button>
        <button class="navitem" data-tab="link_apis"><img class="ic" src="/static/icons/ic-link.svg" alt="" width="20" height="20" />Link API</button>
        <button class="navitem" data-tab="agents"><img class="ic" src="/static/icons/ic-agents.svg" alt="" width="20" height="20" />Agent 管理</button>
        <button class="navitem" data-tab="safe"><img class="ic" src="/static/icons/ic-safe.svg" alt="" width="20" height="20" />安全闸</button>
      </div>
      <div class="nav-group">
        <div class="nav-group-title">工具</div>
        <button class="navitem" data-tab="diagnostics"><img class="ic" src="/static/icons/ic-diagnostics.svg" alt="" width="20" height="20" />诊断</button>
        <button class="navitem" data-tab="notes"><img class="ic" src="/static/icons/ic-notes.svg" alt="" width="20" height="20" />笔记</button>
      </div>
      <div class="nav-group">
        <div class="nav-group-title">系统</div>
        <button class="navitem" data-tab="settings"><img class="ic" src="/static/icons/ic-settings.svg" alt="" width="20" height="20" />设置</button>
        <button class="navitem" data-tab="help"><img class="ic" src="/static/icons/ic-help.svg" alt="" width="20" height="20" />帮助</button>
        <button class="navitem" data-tab="acp_tokens"><img class="ic" src="/static/icons/ic-acp.svg" alt="" width="20" height="20" />ACP 令牌</button>
        <button class="navitem" data-tab="llm_settings"><img class="ic" src="/static/icons/ic-llm-settings.svg" alt="" width="20" height="20" />大模型设置</button>
        <button class="navitem" data-tab="update"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:15px">⬆️</span>系统更新</button>
      </div>
    </nav>'''

if old_sidebar in html:
    html = html.replace(old_sidebar, new_sidebar, 1)
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print("sidebar grouped: 4 groups (核心/管理/工具/系统)")
else:
    print("ERROR: old sidebar not found")
