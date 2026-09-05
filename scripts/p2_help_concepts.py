#!/usr/bin/env python3
"""P2: 帮助系统添加核心概念详解"""
import sys

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\index.html"
with open(FILE, "r", encoding="utf-8") as f:
    html = f.read()

# 1. TOC 添加核心概念详解
old_toc = '<li><a href="#h-extra">补充：接入令牌安全 / 顶部开关 / 子流程 / 操作日志</a></li>\n            <li><a href="#h-faq">常见问题 FAQ</a></li>'
new_toc = '<li><a href="#h-extra">补充：接入令牌安全 / 顶部开关 / 子流程 / 操作日志</a></li>\n            <li><a href="#h-concepts">核心概念详解</a></li>\n            <li><a href="#h-faq">常见问题 FAQ</a></li>'

if old_toc in html:
    html = html.replace(old_toc, new_toc, 1)
    print("TOC updated")
else:
    print("ERROR: TOC not found")
    sys.exit(1)

# 2. 在 FAQ 之前插入核心概念详解
concepts_html = '''        <!-- 七-B 核心概念详解 -->
        <div class="card" id="h-concepts">
          <h3>核心概念详解</h3>
          <p class="desc">AutoFlow 涉及一些特有概念，这里逐一解释原理和作用。</p>

          <h4>DSL（领域特定语言）</h4>
          <p class="desc">DSL 是 AutoFlow 定义的一种结构化场景描述语言，比原生 Node-RED flow JSON 更简洁、更安全。Agent 提交 DSL 后，网关的<strong>编译器</strong>会将其转换为标准 NR flow 节点。</p>
          <p class="desc">编译器生成的 flow <strong>不含 Function 节点</strong>（无法执行任意 JS 代码），因此被标记为「编译产物·可信」，可免人工审核直接部署。这是 DSL 模式比 raw flow 模式更安全的根本原因。</p>

          <h4>安全闸（Safety Gate）</h4>
          <p class="desc">安全闸是 AutoFlow 的<strong>真实设备保护机制</strong>。在 flow 部署到 Node-RED 之前，网关会检查 flow 中操作的设备是否在受保护列表中：</p>
          <ul class="desc">
            <li><strong>Tier-0（需确认）</strong>：触及即暂停，等人工确认后才能继续</li>
            <li><strong>Tier-1（放行+审计）</strong>：允许操作，但记录到操作日志</li>
          </ul>
          <p class="desc">安全闸的检查结果以「断言」形式展示（✅/❌ 逐条列出预期状态），你不必看懂 DSL，看断言就知道 flow 会操作哪些设备。</p>

          <h4>vhass（虚拟孪生重放）</h4>
          <p class="desc">vhass 是 AutoFlow 的<strong>虚拟环境测试</strong>能力。它可以在不触碰真实设备的情况下，模拟 Home Assistant 环境来验证 flow 的行为。</p>
          <p class="desc">当 flow 部署到 staging（测试环境）时，网关会先在虚拟环境中重放 flow 的执行过程，检查预期状态是否满足。只有虚拟验证通过后，才允许部署到 prod（真实环境）。</p>
          <p class="desc">这相当于给自动化流程加了一道「彩排」——先在虚拟舞台上演一遍，确认没问题再正式演出。</p>

          <h4>自动修复（原称自愈闭环）</h4>
          <p class="desc">自动修复是 AutoFlow 的核心能力：当已部署的 flow 不工作时，Agent 可以自动调试并修复，无需你手动排查。</p>
          <p class="desc">修复循环：<strong>inject 点火</strong> → <strong>debug 回读</strong>（网关旁路订阅 NR 的 debug 事件流）→ <strong>分析报错</strong> → <strong>apply 修正</strong> flow → 再点火验证 → 直到跑通。</p>
          <p class="desc">为防止死循环，网关有<strong>自动修复预算</strong>（默认最多 3 次重试，按 Agent+flow 计失败次数）。预算用尽后 Agent 会如实告知「需人工介入」，不会假装成功。</p>

          <h4>Link API（HTTP 桥接）</h4>
          <p class="desc">Link API 是 AutoFlow 的<strong>外部 HTTP 服务桥接</strong>能力，让 flow 可以调用外部 API 或接收外部 webhook。</p>
          <p class="desc">两种模式：</p>
          <ul class="desc">
            <li><strong>link out 推送</strong>：把 flow 的输出推送到外部 HTTP 端点（如第三方 API、webhook）</li>
            <li><strong>http_api 内联</strong>：网关内置 HTTP 接口，不生成 NR 节点，直接由网关处理请求</li>
          </ul>
          <p class="desc">Link API 安装后会合并到 Node-RED 的「AutoFlow API」tab 中，增量更新不重复创建节点。</p>

          <h4>子流程注册表（Subflow Registry）</h4>
          <p class="desc">子流程注册表是 AutoFlow 管理可复用 NR 子流程的目录。注册后的子流程，Agent 可以通过 MCP 直接调用（在 DSL 中写「调用子流程: 名称(参数)」）。</p>
          <p class="desc">子流程分两类：</p>
          <ul class="desc">
            <li><strong>内置（managed）</strong>：AutoFlow 自带的子流程（如 Bark 推送），网关负责安装和更新</li>
            <li><strong>已导入（imported）</strong>：你从 Node-RED 导入的自定义子流程，网关只登记不修改 NR 实例</li>
          </ul>
          <p class="desc">删除内置子流程会同时删除 NR 实例；删除已导入子流程只从注册表移除，NR 上的子流程保持原样。</p>

          <h4>注册表漂移（Registry Drift）</h4>
          <p class="desc">注册表漂移指网关的注册表记录着某个 flow「已部署」，但 Node-RED 里已经找不到这个 flow_id 了。</p>
          <p class="desc">常见原因：你手动在 NR 里删除了该 flow，或者更换了 Node-RED 实例。这不是错误，而是记录不一致。</p>
          <p class="desc">处理方法：点击「撤回」，网关会只清理注册表、不碰 Node-RED（因为 flow 已经不在了）。</p>
        </div>

        <!-- 八 FAQ -->'''

old_faq = '        <!-- 八 FAQ -->'
if old_faq in html:
    html = html.replace(old_faq, concepts_html, 1)
    print("Concepts section inserted")
else:
    print("ERROR: FAQ marker not found")
    sys.exit(1)

with open(FILE, "w", encoding="utf-8") as f:
    f.write(html)

print("DONE: help page concepts added")
