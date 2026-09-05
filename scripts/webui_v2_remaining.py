#!/usr/bin/env python3
"""AutoFlow WebUI v2 剩余改动：顶栏文字标签 + app.js 残留 + CSS 配色"""

# === 1. index.html 顶栏按钮加文字标签 ===
INDEX = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"
with open(INDEX, "r", encoding="utf-8") as f:
    html = f.read()

html = html.replace(
    '<button id="tpBtn" class="btn ghost" title="DSL 验证池开关（关闭后 Agent 的 DSL 编译请求将被拒绝）">🧪</button>',
    '<button id="tpBtn" class="btn ghost" title="DSL 验证池开关（关闭后 Agent 的 DSL 编译请求将被拒绝）"><span class="btn-emoji">🧪</span><span class="btn-text">验证池</span></button>'
)
html = html.replace(
    '<button id="dpBtn" class="btn ghost" title="部署策略（自动生成的 flow 可免审直装，手写 raw flow 仍须人工审核）">⚖</button>',
    '<button id="dpBtn" class="btn ghost" title="部署策略（自动生成的 flow 可免审直装，手写 raw flow 仍须人工审核）"><span class="btn-emoji">⚖</span><span class="btn-text">部署策略</span></button>'
)
html = html.replace(
    '<button id="shBtn" class="btn ghost" title="自动修复：Agent 自主调试已部署 flow 的最大重试次数（默认 3，防死循环）">♻️</button>',
    '<button id="shBtn" class="btn ghost" title="自动修复：Agent 自主调试已部署 flow 的最大重试次数（默认 3，防死循环）"><span class="btn-emoji">♻️</span><span class="btn-text">自动修复</span></button>'
)

with open(INDEX, "w", encoding="utf-8") as f:
    f.write(html)
print("index.html: topbar text labels added")

# === 2. app.js 残留术语修复 ===
APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    js = f.read()

js = js.replace('⚠️ 真删除：该 agent', '⚠️ 彻底删除：该 Agent')
js = js.replace('自愈重试次数（自动修复）', '自动修复重试次数')
js = js.replace('自愈预算', '自动修复预算')

with open(APP, "w", encoding="utf-8") as f:
    f.write(js)
print("app.js: residual terms fixed")

# === 3. style.css 配色微调 ===
CSS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\style.css"
with open(CSS, "r", encoding="utf-8") as f:
    css = f.read()

css = css.replace('--primary: #2F6BFF;', '--primary: #3B6FE8;')
css = css.replace('--primary-700: #1F4FD6;', '--primary-700: #2B54C7;')
css = css.replace('--primary-400: #5B8CFF;', '--primary-400: #6B95F0;')
css = css.replace('--primary-weak: #E8EFFF;', '--primary-weak: #EBF0FE;')
css = css.replace('--bg: #f4f6fb;', '--bg: #F5F7FA;')
css = css.replace('--border: #e2e8f0;', '--border: #E4E9F0;')
css = css.replace('--danger: #e0464b;', '--danger: #DC4A4F;')
css = css.replace('--warn: #c9821a;', '--warn: #D4881F;')
css = css.replace('--ok: #1f9d6b;', '--ok: #22A570;')
css = css.replace(
    '0 1px 3px rgba(16,24,40,.08), 0 1px 2px rgba(16,24,40,.04)',
    '0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04)'
)
css = css.replace(
    '0 10px 40px rgba(16,24,40,.18)',
    '0 8px 32px rgba(15,23,42,.14)'
)
css = css.replace(
    '0 4px 14px rgba(47,107,255,.35)',
    '0 4px 14px rgba(59,111,232,.30)'
)
# brand gradient
css = css.replace(
    'linear-gradient(135deg, #5B8CFF, #2F6BFF)',
    'linear-gradient(135deg, #6B95F0, #3B6FE8)'
)

with open(CSS, "w", encoding="utf-8") as f:
    f.write(css)
print("style.css: color palette updated")

print("\nAll P0 changes complete!")
