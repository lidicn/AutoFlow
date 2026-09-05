#!/usr/bin/env python3
"""将教程系统接入 index.html"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"
with open(FILE, "r", encoding="utf-8") as f:
    html = f.read()

# 1. 侧边栏：在"概览"后添加"教程"
old_nav = '<button class="navitem" data-tab="dashboard"><img class="ic" src="/static/icons/ic-dashboard.svg" alt="" width="20" height="20" />概览</button>'
new_nav = old_nav + '\n      <button class="navitem" data-tab="tutorials"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:16px">📚</span>教程</button>'
html = html.replace(old_nav, new_nav, 1)

# 2. 主内容区：在 view-dashboard 后添加 view-tutorials
old_view = '<section class="view" id="view-dashboard" hidden></section>'
new_view = old_view + '\n      <section class="view" id="view-tutorials" hidden></section>'
html = html.replace(old_view, new_view, 1)

# 3. 移动端"更多"抽屉：在"概览"后添加"教程"
old_mobile = '<button class="navitem" data-tab="dashboard"><img class="ic" src="/static/icons/ic-dashboard.svg" alt="" width="22" height="22" />概览</button>'
new_mobile = old_mobile + '\n          <button class="navitem" data-tab="tutorials"><span class="ic" style="display:inline-flex;align-items:center;justify-content:center;font-size:16px">📚</span>教程</button>'
html = html.replace(old_mobile, new_mobile, 1)

# 4. 在 app.js 前引入 tutorials.js
old_script = '<script src="/static/app.js"></script>'
new_script = '<script src="/static/tutorials.js"></script>\n  <script src="/static/app.js"></script>'
html = html.replace(old_script, new_script, 1)

with open(FILE, "w", encoding="utf-8") as f:
    f.write(html)

print("index.html: tutorials nav, view, and script reference added")
