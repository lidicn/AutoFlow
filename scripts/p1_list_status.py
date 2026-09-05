#!/usr/bin/env python3
"""P1: 提案/已部署列表添加状态色条 class"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(FILE, "r", encoding="utf-8") as f:
    js = f.read()

# 1. 提案列表：在 return 模板中给 item 添加 proposal-item + 状态 class
# 在第 521 行 return ` 之前，计算状态 class
old_proposal_return = '''      return `
      <div class="item">
        <div class="row">
          <div><span class="title">${esc(p.title)}</span>'''

new_proposal_return = '''      const _pStatusCls = gatePassed ? " status-pass" : (gate.passed === false ? " status-fail" : (p.requires_review ? " status-review" : ""));
      return `
      <div class="item proposal-item${_pStatusCls}">
        <div class="row">
          <div><span class="title">${esc(p.title)}</span>'''

js = js.replace(old_proposal_return, new_proposal_return, 1)

# 2. 已部署列表：给 item 添加 deployed-item + 状态 class
old_deployed = '''    list.innerHTML = items.map((d) => `
      <div class="item${d.stale ? " stale" : ""}">'''

new_deployed = '''    list.innerHTML = items.map((d) => `
      <div class="item deployed-item${d.stale ? " status-stale stale" : " status-ok"}">'''

js = js.replace(old_deployed, new_deployed, 1)

with open(FILE, "w", encoding="utf-8") as f:
    f.write(js)

print("proposal/deployed list status bars added")
