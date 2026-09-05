#!/usr/bin/env python3
"""修改 index.html: 增加授权码菜单项和页面容器"""

HTML = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"
with open(HTML, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在"管理"分组中增加"授权码"菜单项（在 Agent 管理之后）
old_nav = '''        <button class="navitem" data-tab="agents"><img class="ic" src="/static/icons/ic-agents.svg" alt="" width="20" height="20" />Agent 管理</button>
        <button class="navitem" data-tab="safe"><img class="ic" src="/static/icons/ic-safe.svg" alt="" width="20" height="20" />安全闸</button>'''

new_nav = '''        <button class="navitem" data-tab="agents"><img class="ic" src="/static/icons/ic-agents.svg" alt="" width="20" height="20" />Agent 管理</button>
        <button class="navitem" data-tab="deploy_tokens"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:15px">🔑</span>授权码</button>
        <button class="navitem" data-tab="safe"><img class="ic" src="/static/icons/ic-safe.svg" alt="" width="20" height="20" />安全闸</button>'''

if old_nav in content:
    content = content.replace(old_nav, new_nav, 1)
    print("1. 增加授权码菜单项")
else:
    print("WARNING: 未找到导航位置")

# 2. 增加页面容器（在 view-agents 之后）
old_view = '''      <section class="view" id="view-agents" hidden></section>'''
new_view = '''      <section class="view" id="view-agents" hidden></section>
      <section class="view" id="view-deploy_tokens" hidden></section>'''

if old_view in content:
    content = content.replace(old_view, new_view, 1)
    print("2. 增加授权码页面容器")
else:
    print("WARNING: 未找到 view-agents 位置")

with open(HTML, "w", encoding="utf-8") as f:
    f.write(content)

print("\nindex.html 修改完成")
