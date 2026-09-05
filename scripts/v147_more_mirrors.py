#!/usr/bin/env python3
"""增加更多镜像选项和自定义镜像输入框"""

APP_JS = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP_JS, "r", encoding="utf-8") as f:
    content = f.read()

old = '''          <select id="updateMirror" class="input" style="max-width:280px">
            <option value="">GitHub 直连（默认）</option>
            <option value="https://ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 镜像</option>
            <option value="https://gitclone.com/github.com/lidicn/AutoFlow.git">gitclone 镜像</option>
            <option value="https://mirror.ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 备用镜像</option>
          </select>
          <span class="meta" id="mirrorHint" style="font-size:12px;color:var(--text-muted)">国内网络建议选镜像</span>'''

new = '''          <select id="updateMirror" class="input" style="max-width:280px">
            <option value="">GitHub 直连（默认）</option>
            <option value="https://ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 镜像</option>
            <option value="https://mirror.ghproxy.com/https://github.com/lidicn/AutoFlow.git">ghproxy 备用</option>
            <option value="https://gitclone.com/github.com/lidicn/AutoFlow.git">gitclone 镜像</option>
            <option value="https://kkgithub.com/lidicn/AutoFlow.git">kkgithub 镜像</option>
            <option value="https://hub.gitmirror.com/https://github.com/lidicn/AutoFlow.git">gitmirror 镜像</option>
            <option value="__custom__">自定义镜像…</option>
          </select>
          <input type="text" id="updateMirrorCustom" class="input" style="max-width:280px;display:none;margin-top:6px" placeholder="输入镜像 URL，如 https://xxx/https://github.com/...">
          <span class="meta" id="mirrorHint" style="font-size:12px;color:var(--text-muted)">国内网络建议选镜像；若全部失败可手动 SCP 离线更新</span>'''

if old in content:
    content = content.replace(old, new, 1)
    print("1. 镜像选项扩展: OK")
else:
    print("1. 镜像选项扩展: NOT FOUND")

# 2. 在 doUpdate 函数中处理自定义镜像
old_do = '''  const mirror = $("#updateMirror")?.value || "";'''
new_do = '''  let mirror = $("#updateMirror")?.value || "";
  if (mirror === "__custom__") {
    mirror = ($("#updateMirrorCustom")?.value || "").trim();
    if (!mirror) { toast("请输入自定义镜像 URL"); return; }
  }'''

if old_do in content:
    content = content.replace(old_do, new_do, 1)
    print("2. 自定义镜像处理: OK")
else:
    print("2. 自定义镜像处理: NOT FOUND")

# 3. 增加下拉菜单切换事件，显示/隐藏自定义镜像输入框
old_btn = '''  if (btn) { btn.onclick = doUpdate; }'''
new_btn = '''  if (btn) { btn.onclick = doUpdate; }
  // 自定义镜像切换
  const mirrorSel = $("#updateMirror");
  if (mirrorSel) {
    mirrorSel.onchange = () => {
      const customInput = $("#updateMirrorCustom");
      if (customInput) customInput.style.display = mirrorSel.value === "__custom__" ? "block" : "none";
    };
  }'''

if old_btn in content:
    content = content.replace(old_btn, new_btn, 1)
    print("3. 镜像切换事件: OK")
else:
    print("3. 镜像切换事件: NOT FOUND")

with open(APP_JS, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
