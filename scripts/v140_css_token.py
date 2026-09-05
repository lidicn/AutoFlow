#!/usr/bin/env python3
"""修改 style.css: 增加授权码相关样式"""

CSS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\style.css"
with open(CSS, "r", encoding="utf-8") as f:
    content = f.read()

# 在文件末尾增加授权码样式
token_css = '''

/* ── 部署授权码（P4）── */
.deploy-token-card {
  margin-bottom: 12px;
  border-left: 4px solid var(--accent, #4f46e5);
}
.deploy-token-card:hover {
  border-left-color: var(--accent-hover, #4338ca);
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 9999px;
  font-size: 11px;
  font-weight: 600;
}
'''

if "deploy-token-card" not in content:
    content += token_css
    print("1. 增加授权码 CSS 样式")
else:
    print("CSS 样式已存在，跳过")

with open(CSS, "w", encoding="utf-8") as f:
    f.write(content)

print("\nstyle.css 修改完成")
