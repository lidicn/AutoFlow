#!/usr/bin/env python3
"""添加镜像切换事件"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    const btn = $("#doUpdate");
    if (btn) btn.onclick = doUpdate;'''

new = '''    const btn = $("#doUpdate");
    if (btn) btn.onclick = doUpdate;
    // 自定义镜像切换
    const mirrorSel = $("#updateMirror");
    if (mirrorSel) {
      mirrorSel.onchange = () => {
        const customInput = $("#updateMirrorCustom");
        if (customInput) customInput.style.display = mirrorSel.value === "__custom__" ? "block" : "none";
      };
    }'''

if old in content:
    content = content.replace(old, new, 1)
    print("镜像切换事件: OK")
else:
    print("镜像切换事件: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
