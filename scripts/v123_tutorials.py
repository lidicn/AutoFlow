#!/usr/bin/env python3
"""v1.2.3 教程系统重构：新增两种使用途径、合并精简、黑白箱概念、Link API示范链接"""

FILE = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\tutorials.js"
with open(FILE, "r", encoding="utf-8") as f:
    content = f.read()

# 找到 TUTORIALS 数据的起止
start_marker = "const TUTORIALS = ["
end_marker = "\n];"

start_idx = content.index(start_marker)
end_idx = content.index(end_marker, start_idx) + len(end_marker)

new_tutorials = '''const TUTORIALS = [
  // ── 教程 0：两种使用途径（最重要，排第一）──
  {
    id: 't0-usage',
    title: '两种使用途径',
    icon: '🧭',
    category: '入门',
    description: 'AutoFlow 有两种使用方式：Agent 调用 MCP，或 WebUI 内置 AI 对话',
    estimatedTime: '3 分钟',
    steps: [
      {
        title: '途径一：Agent 调用 MCP（推荐）',
        content: `
          <p>在你常用的 AI 客户端（DeepSeek、Claude、Cursor 等）中配置 AutoFlow 的 MCP 服务器，然后<strong>直接跟 AI 对话</strong>，让它帮你编写和部署 flow。</p>
          <div class="tutorial-tip">
            <b>适合：</b>已经有 AI 客户端、希望在熟悉的环境中工作的用户。<br>
            <b>优势：</b>AI 可以调用 MCP 工具直接操作 AutoFlow，全流程自动化。
          </div>
          <p>配置需要：MCP 服务器地址 + 接入令牌（在「Agent 管理」中创建）。</p>
        `,
        action: { tab: 'agents', label: '去创建 Agent' },
      },
      {
        title: '途径二：WebUI 内置 AI 对话',
        content: `
          <p>直接在 AutoFlow WebUI 的「AI 对话」页面中跟内置 AI 聊天，无需配置外部 MCP 客户端。</p>
          <div class="tutorial-tip">
            <b>适合：</b>新手、快速体验、或没有外部 AI 客户端的用户。<br>
            <b>前提：</b>需要先在「大模型设置」中配置 LLM 后端（API Key 等）。
          </div>
          <p>在对话中直接说需求，如"帮我写一个晚上 10 点关灯的 flow"，AI 会生成提案，你审核后一键部署。</p>
        `,
        action: { tab: 'llm_agent', label: '打开 AI 对话' },
      },
      {
        title: '两种途径可以混用',
        content: `
          <p>两种途径共享同一套后端：你在 WebUI 中写的 flow，Agent 也能看到和修改；反之亦然。</p>
          <p><strong>建议工作流：</strong></p>
          <ol>
            <li>用 WebUI AI 对话快速创建第一个 flow（学习成本低）</li>
            <li>熟悉后切换到 Agent + MCP（更强大、可自动化）</li>
            <li>日常维护用 WebUI 查看状态、手动调整</li>
          </ol>
          <p>接下来的教程以 Agent + MCP 为主线，WebUI 操作同理。</p>
        `,
      },
    ],
  },

  // ── 教程 1：接入 Agent ──
  {
    id: 't1-agent',
    title: '接入你的第一个 Agent',
    icon: '🔗',
    category: '入门',
    description: '创建 Agent、获取接入令牌、配置 MCP 连接',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是 Agent？',
        content: `
          <p>Agent 是连接 AutoFlow 的 AI 客户端。它通过 MCP 协议与网关通信，帮你编写和部署自动化 flow。</p>
          <p>每个 Agent 需要一个<strong>接入令牌</strong>来验证身份。令牌仅在创建时显示一次，请妥善保存。</p>
        `,
      },
      {
        title: '创建 Agent 并获取令牌',
        content: `
          <p>打开「Agent 管理」，填写名称（如 <code>deepseek</code>），选择权限模式：</p>
          <ul>
            <li><strong>标准模式</strong>：仅提交 DSL，最安全，推荐新手</li>
            <li><strong>高级模式</strong>：可提交 raw flow 并直接部署</li>
          </ul>
          <p>点击「生成接入令牌」，<strong style="color:var(--danger)">立即复制保存</strong>，关闭后无法再次查看。</p>
        `,
        action: { tab: 'agents', label: '前往 Agent 管理' },
      },
      {
        title: '配置 MCP 客户端',
        content: `
          <p>在你的 AI 客户端的 MCP 配置中填写：</p>
          <div class="tutorial-code">
mcp_servers:
  autoflow:
    url: "http://&lt;网关地址&gt;:&lt;端口&gt;/mcp"
    headers:
      Authorization: "Bearer &lt;你的接入令牌&gt;"
          </div>
          <p>标准模式用 <code>/mcp</code>，高级模式用 <code>/mcp-white</code>。</p>
        `,
      },
      {
        title: '验证连接',
        content: `
          <p>在 AI 客户端中问："你能调用 AutoFlow 的哪些工具？"如果能列出工具列表（如 <code>submit_dsl</code>、<code>deploy_flow</code> 等），说明连接成功。</p>
          <p>连接失败常见原因：</p>
          <ul>
            <li>接入令牌拼写错误或已过期</li>
            <li>网关地址/端口不正确</li>
            <li>防火墙阻止了 AI 客户端访问网关</li>
          </ul>
        `,
      },
    ],
  },

  // ── 教程 2：编写并部署 flow（合并写flow+部署）──
  {
    id: 't2-write-deploy',
    title: '编写并部署你的第一个 flow',
    icon: '✍️',
    category: '核心',
    description: '跟 Agent 对话编写 flow，审核后部署到 Node-RED',
    estimatedTime: '8 分钟',
    steps: [
      {
        title: '用自然语言描述需求',
        content: `
          <p>跟 Agent 说你的需求，例如：</p>
          <div class="tutorial-code">"帮我写一个 flow：每天晚上 10 点关闭客厅灯，并发送通知到手机"</div>
          <p>Agent 会分析需求，调用 Home Assistant 实体查询工具，确认设备名称（如 <code>light.living_room</code>），然后生成 DSL 提案。</p>
          <div class="tutorial-tip">
            <b>提示：</b>描述越具体越好。包含触发条件、执行动作、涉及设备。如果 Agent 不确定设备名称，它会主动问你。
          </div>
        `,
      },
      {
        title: '审核提案',
        content: `
          <p>Agent 提交后，在「提案」页面可以看到生成的 flow。重点看：</p>
          <ul>
            <li><strong>安全闸断言</strong>：✅/❌ 逐条列出 flow 会操作哪些设备，是否在安全范围内</li>
            <li><strong>DSL 内容</strong>：结构化描述，比原生 flow JSON 更易读</li>
            <li><strong>预期状态</strong>：flow 执行后期望达到的状态</li>
          </ul>
          <p>看不懂 DSL 没关系，看安全闸断言就够了——它告诉你 flow 会碰哪些设备。</p>
        `,
        action: { tab: 'proposals', label: '查看提案' },
      },
      {
        title: '部署到 Node-RED',
        content: `
          <p>审核通过后，点击「部署到 NR」。网关会：</p>
          <ol>
            <li>将 DSL 编译为标准 Node-RED flow 节点（<strong>不含 Function 节点</strong>，因此可信）</li>
            <li>检查安全闸：受保护设备需人工确认</li>
            <li>写入 Node-RED 的「AutoFlow API」tab</li>
          </ol>
          <p>部署成功后，在「已部署」页面可以看到该 flow，状态为「运行中」。</p>
        `,
        action: { tab: 'deployed', label: '查看已部署' },
      },
      {
        title: '在 Node-RED 中查看',
        content: `
          <p>打开 Node-RED，切换到「AutoFlow API」tab，你会看到刚部署的 flow。节点都是标准类型（inject、api-current-state、call-service、debug 等），没有自定义节点。</p>
          <p>你可以手动触发 inject 节点测试 flow 是否正常工作。</p>
          <div class="tutorial-tip">
            <b>注意：</b>不要在 Node-RED 中手动修改 AutoFlow 管理的 flow，否则会导致注册表漂移。如需修改，通过 Agent 对话修改。
          </div>
        `,
      },
      {
        title: '验证 flow 运行',
        content: `
          <p>等待触发条件满足（如到了晚上 10 点），或手动在 Node-RED 中点击 inject 节点触发。</p>
          <p>如果 flow 不工作，不要急着删——使用「自动修复」功能让 Agent 自动排查。下一个教程会讲。</p>
        `,
      },
    ],
  },

  // ── 教程 3：修改与自动修复（合并修改+自动修复）──
  {
    id: 't3-modify-heal',
    title: '修改 flow 与自动修复',
    icon: '🔧',
    category: '核心',
    description: '让 Agent 修改已有 flow，flow 不工作时用自动修复排查',
    estimatedTime: '8 分钟',
    steps: [
      {
        title: '让 Agent 修改已有 flow',
        content: `
          <p>跟 Agent 说修改需求，例如：</p>
          <div class="tutorial-code">"把晚上 10 点关灯改成 10 点半，再加一个睡前提醒"</div>
          <p>Agent 会：</p>
          <ol>
            <li>查询当前已部署的 flow 列表</li>
            <li>找到目标 flow，读取当前 DSL</li>
            <li>生成修改后的新提案</li>
            <li>你审核后重新部署（覆盖旧版本）</li>
          </ol>
          <p>所有修改都有版本记录，可在「操作日志」中查看。</p>
        `,
        action: { tab: 'deployed', label: '查看已部署 flow' },
      },
      {
        title: 'flow 不工作？用自动修复',
        content: `
          <p>如果已部署的 flow 不工作（如灯没关、通知没发），不要手动排查。跟 Agent 说：</p>
          <div class="tutorial-code">"这个 flow 不工作，帮我自动修复"</div>
          <p>自动修复的工作原理：</p>
          <ol>
            <li><strong>inject 点火</strong>：自动触发 flow 的 inject 节点</li>
            <li><strong>debug 回读</strong>：网关旁路订阅 Node-RED 的 debug 事件流，获取报错信息</li>
            <li><strong>分析报错</strong>：Agent 根据 debug 输出定位问题</li>
            <li><strong>apply 修正</strong>：修改 flow 并重新部署</li>
            <li><strong>再点火验证</strong>：确认修复成功</li>
          </ol>
        `,
      },
      {
        title: '自动修复预算',
        content: `
          <p>为防止死循环，自动修复有<strong>预算限制</strong>（默认最多 3 次重试）。预算用尽后 Agent 会如实告知「需人工介入」，不会假装成功。</p>
          <p>你可以在「设置」中调整自动修复预算。</p>
          <div class="tutorial-tip">
            <b>常见自动修复场景：</b>
            <ul>
              <li>实体名称错误（如 light.living_room 写成了 light.livingroom）</li>
              <li>服务参数格式不对</li>
              <li>节点连接缺失</li>
              <li>条件判断逻辑错误</li>
            </ul>
          </div>
        `,
      },
      {
        title: '撤回与回滚',
        content: `
          <p>如果修改后 flow 反而不工作了，可以：</p>
          <ul>
            <li><strong>撤回</strong>：在「已部署」页面点击「撤回」，从 Node-RED 删除该 flow（同时清理注册表）</li>
            <li><strong>重新部署旧版本</strong>：在「提案」页面找到旧版本提案，重新部署</li>
          </ul>
          <p>如果 Node-RED 里手动删了 flow，会出现「注册表漂移」——点击撤回只清理记录即可。</p>
        `,
      },
      {
        title: '手动调试辅助',
        content: `
          <p>自动修复搞不定时，可以用 WebUI 的调试工具：</p>
          <ul>
            <li><strong>诊断</strong>：检查网关与 Node-RED、Home Assistant 的连接状态</li>
            <li><strong>验证池</strong>：在虚拟环境中重放 flow，不碰真实设备</li>
            <li><strong>操作日志</strong>：查看所有部署、修改、修复记录</li>
          </ul>
          <p>顶栏的「🧪验证池」开关可以开启虚拟环境测试。</p>
        `,
        action: { tab: 'diagnostics', label: '打开诊断' },
      },
    ],
  },

  // ── 教程 4：安全闸与黑白箱（合并安全闸+黑白箱概念）──
  {
    id: 't4-safety',
    title: '安全闸与黑白箱模式',
    icon: '🛡️',
    category: '安全',
    description: '理解安全闸如何保护真实设备，黑箱/白箱两种提交模式的区别',
    estimatedTime: '6 分钟',
    steps: [
      {
        title: '为什么需要安全闸？',
        content: `
          <p>AI 生成的 flow 可能误操作真实设备（如误开暖气、误关路由器）。安全闸在 flow 部署前检查它会操作哪些设备，是否在安全范围内。</p>
          <p>安全闸分两级：</p>
          <ul>
            <li><strong>Tier-0（需确认）</strong>：触及即暂停，等人工确认后才能继续。适用于高风险设备（门锁、暖气、总开关）</li>
            <li><strong>Tier-1（放行+审计）</strong>：允许操作，但记录到操作日志。适用于普通设备（灯、开关）</li>
          </ul>
        `,
        action: { tab: 'safe', label: '配置安全闸' },
      },
      {
        title: '黑箱模式（DSL，推荐）',
        content: `
          <p><strong>黑箱模式</strong>：Agent 提交结构化 DSL，网关的编译器将其转换为标准 Node-RED 节点。</p>
          <div class="tutorial-tip">
            <b>为什么叫黑箱？</b>你不需要看懂 DSL 的内部结构，编译器会保证输出的 flow 是安全的——就像你不需要知道编译器怎么把 C 代码翻译成汇编，只需要知道它不会生成恶意指令。
          </div>
          <p><strong>关键安全特性：</strong>编译器生成的 flow <strong>不含 Function 节点</strong>（无法执行任意 JS 代码），因此被标记为「编译产物·可信」，可免人工审核直接部署。</p>
          <p>标准模式（normal）的 Agent 只能使用黑箱模式。</p>
        `,
      },
      {
        title: '白箱模式（raw flow，高级）',
        content: `
          <p><strong>白箱模式</strong>：Agent 直接提交原生 Node-RED flow JSON，可能包含 Function 节点（可执行任意 JS）。</p>
          <div class="tutorial-tip">
            <b>为什么叫白箱？</b>flow 的内部结构完全透明，你需要逐节点审核——就像审查源代码一样，每个节点的逻辑都要看清楚。
          </div>
          <p><strong>安全要求：</strong>白箱模式的 flow <strong>必须人工审核</strong>后才能部署，因为 Function 节点可能执行任意代码。</p>
          <p>只有高级模式（expert）或管理员模式（admin）的 Agent 才能使用白箱模式。顶栏的「🧬原生节点」开关控制是否允许白箱提交。</p>
        `,
      },
      {
        title: '部署策略与受保护设备',
        content: `
          <p>在「设置」中可以配置：</p>
          <ul>
            <li><strong>部署策略</strong>：自动部署（免审核）/ 需审核（所有 flow 都要人审）</li>
            <li><strong>受保护设备</strong>：将高风险设备加入 Tier-0，触及即暂停</li>
            <li><strong>自动修复预算</strong>：默认 3 次，防止死循环</li>
          </ul>
          <p>顶栏的「⚖部署策略」开关可以快速切换自动/审核模式。</p>
          <div class="tutorial-tip">
            <b>建议：</b>新手保持默认（黑箱+自动部署+Tier-0 保护关键设备）。熟悉后再逐步开放白箱和高级功能。
          </div>
        `,
        action: { tab: 'settings', label: '打开设置' },
      },
    ],
  },

  // ── 教程 5：子流程与 Link API（合并子流程+Link API）──
  {
    id: 't5-advanced',
    title: '子流程与 Link API',
    icon: '📦',
    category: '进阶',
    description: '复用子流程、通过 Link API 让 flow 调用外部 HTTP 服务',
    estimatedTime: '6 分钟',
    steps: [
      {
        title: '什么是子流程？',
        content: `
          <p>子流程（Subflow）是 Node-RED 中可复用的流程模块。AutoFlow 维护一个<strong>子流程注册表</strong>，注册后的子流程，Agent 可以在 DSL 中直接调用。</p>
          <p>例如，注册了「Bark 推送」子流程后，Agent 可以写：</p>
          <div class="tutorial-code">调用子流程: Bark推送(标题="提醒", 内容="该睡觉了")</div>
          <p>子流程分两类：</p>
          <ul>
            <li><strong>内置（managed）</strong>：AutoFlow 自带，网关负责安装和更新</li>
            <li><strong>已导入（imported）</strong>：你从 Node-RED 导入的自定义子流程</li>
          </ul>
        `,
        action: { tab: 'subflows', label: '管理子流程' },
      },
      {
        title: '导入自定义子流程',
        content: `
          <p>如果你在 Node-RED 中已经写好了一个可复用的子流程，可以导入到 AutoFlow：</p>
          <ol>
            <li>在 Node-RED 中选中子流程，导出 JSON</li>
            <li>在 AutoFlow「子流程」页面点击「导入」</li>
            <li>填写名称和描述，Agent 就能在 DSL 中调用了</li>
          </ol>
          <p>导入的子流程，网关只登记不修改 Node-RED 实例。删除时只从注册表移除，Node-RED 上的子流程保持原样。</p>
        `,
      },
      {
        title: '什么是 Link API？',
        content: `
          <p>Link API 让 flow 可以调用外部 HTTP 服务，或接收外部 webhook。两种模式：</p>
          <ul>
            <li><strong>link out 推送</strong>：把 flow 的输出推送到外部 HTTP 端点（如第三方 API、webhook）</li>
            <li><strong>http_api 内联</strong>：网关内置 HTTP 接口，不生成 NR 节点，直接由网关处理请求</li>
          </ul>
          <p>Link API 安装后会合并到 Node-RED 的「AutoFlow API」tab 中。</p>
        `,
        action: { tab: 'link_apis', label: '管理 Link API' },
      },
      {
        title: '从 Node-RED 导入 Link API',
        content: `
          <p>如果你在 Node-RED 中已经配置了 link in 节点（HTTP 入口），可以一键导入：</p>
          <ol>
            <li>在「Link API」页面点击「从 tab 链接导入」</li>
            <li>网关自动检测 Node-RED 中的 link in 入口</li>
            <li>选择要导入的入口，配置密钥后点击「安装到 Node-RED」</li>
          </ol>
          <p>示例：如果你的 Node-RED 中有一个 link in 节点，它的 flow 地址可能是：</p>
          <div class="tutorial-code">http://<NAS_IP>:1880/#flow/1abccdeb942bc34</div>
          <p>导入后，外部服务就可以通过这个 HTTP 端点触发 AutoFlow 管理的 flow 了。</p>
        `,
      },
    ],
  },
];'''

new_content = content[:start_idx] + new_tutorials + content[end_idx:]

with open(FILE, "w", encoding="utf-8") as f:
    f.write(new_content)

# 统计教程数和步骤数
import re
tutorial_count = new_content.count("id: 't")
step_count = len(re.findall(r"title: '", new_content))
print(f"tutorials.js: restructured — {tutorial_count} tutorials, ~{step_count} steps")
print("New tutorial t0-usage added (two usage paths)")
print("Black-box/white-box concept added to t4-safety")
print("Link API example URL added to t5-advanced")
