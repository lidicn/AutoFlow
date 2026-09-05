#!/usr/bin/env python3
"""P1: toast 类型支持 + 列表状态色条"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(FILE, "r", encoding="utf-8") as f:
    js = f.read()

# 1. toast 函数支持类型参数
old_toast = '''function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}'''
new_toast = '''function toast(msg, type) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (type ? " " + type : "");
  t.hidden = false;
  clearTimeout(toast._t);
  const dur = type === "error" ? 4000 : type === "warn" ? 3200 : 2600;
  toast._t = setTimeout(() => (t.hidden = true), dur);
}'''
js = js.replace(old_toast, new_toast, 1)

# 2. 提案列表：给卡片添加 proposal-item class 和状态 class
# 找到提案卡片的渲染，添加 class
# 提案卡片通常是 <div class="card"> 或类似结构
# 我们在提案的 card 上添加 proposal-item + 状态 class
old_proposal_card = 'v.innerHTML = items.map((p) => {'
# 这个太泛了，让我找更具体的

# 实际上，让我找提案列表中包含 "闸门 PASS" 或 "安全闸" 的卡片渲染
# 先看看提案渲染的结构
import re

# 找到 loadProposals 函数中的卡片渲染
# 给每个提案 div 添加 proposal-item class
# 我们找包含 proposal 渲染的典型模式

# 已部署列表类似处理
# 先做 toast，列表色条通过给现有 card 添加 class 来实现

# 找提案卡片渲染中的 class="card" 模式（在 loadProposals 中）
# 这个比较复杂，让我用更简单的方式：在提案和已部署的空态和列表容器上做文章

# 实际上，最稳妥的方式是找到具体的卡片渲染字符串
# 让我搜索提案卡片的典型结构

# 先保存 toast 修改
with open(FILE, "w", encoding="utf-8") as f:
    f.write(js)

print("toast function updated with type support")
print("Note: list status bars need targeted card class additions - checking render code")
