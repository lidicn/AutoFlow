#!/usr/bin/env python3
"""将教程系统集成到 app.js：TABS 数组、setTab 分支、首次访问引导"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(FILE, "r", encoding="utf-8") as f:
    js = f.read()

# 1. TABS 数组加入 tutorials
old_tabs = 'const TABS = ["dashboard", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
new_tabs = 'const TABS = ["dashboard", "tutorials", "safe", "proposals", "deployed", "subflows", "link_apis", "agents", "diagnostics", "notes", "settings", "help", "acp_tokens", "llm_settings", "llm_agent", "update"];'
js = js.replace(old_tabs, new_tabs, 1)

# 2. setTab 中加入 tutorials 分支和首次访问引导
# 在 dashboard 分支后加入 tutorials
old_dash = '  if (tab === "dashboard") loadDashboard();'
new_dash = '''  if (tab === "dashboard") loadDashboard();
  else if (tab === "tutorials") { if (typeof renderTutorialList === "function") renderTutorialList(); }'''
js = js.replace(old_dash, new_dash, 1)

# 3. 在 setTab 末尾（所有 if/else 之后）加入首次访问引导
# 找到 setTab 函数的结束位置（在 llm_agent/update 分支之后）
old_end = '''  else if (tab === "llm_agent") loadLlmAgent();
  else if (tab === "update") loadUpdate();
  location.hash = tab;'''
new_end = '''  else if (tab === "llm_agent") loadLlmAgent();
  else if (tab === "update") loadUpdate();
  // 首次访问引导
  if (typeof checkFirstVisit === "function") checkFirstVisit(tab);
  location.hash = tab;'''
js = js.replace(old_end, new_end, 1)

with open(FILE, "w", encoding="utf-8") as f:
    f.write(js)

print("app.js: tutorials tab integration complete")
