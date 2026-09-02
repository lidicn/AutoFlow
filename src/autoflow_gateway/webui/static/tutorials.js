/**
 * AutoFlow 交互式教程系统
 * 包含：教程数据、教程列表渲染、分步引导渲染、首次访问引导
 * 依赖：app.js 的 showTab / modal / toast / api 函数
 */

// ═══════════════════════════════════════════════════════════
// 教程数据
// ═══════════════════════════════════════════════════════════

const TUTORIALS = [
  // ── 教程 1：接入 Agent ──
  {
    id: 't1-agent',
    title: '接入你的第一个 Agent',
    icon: '🔗',
    category: '入门',
    description: '在 Agent 管理中创建 Agent，获取接入令牌，配置 MCP 连接',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是 Agent？',
        content: `
          <p>Agent 是连接 AutoFlow 的 AI 客户端（如 DeepSeek、Claude、Cursor 等）。它通过 MCP 协议与网关通信，帮你编写和部署自动化 flow。</p>
          <p>每个 Agent 需要一个<strong>接入令牌</strong>（identity token）来验证身份。令牌仅在创建时显示一次，请妥善保存。</p>
        `,
      },
      {
        title: '创建 Agent',
        content: `
          <p>打开左侧导航的 <strong>Agent 管理</strong>，填写：</p>
          <ul>
            <li><strong>名称</strong>：如 <code>deepseek++</code></li>
            <li><strong>权限模式</strong>：
              <ul>
                <li><strong>标准模式</strong>（normal）：仅提交 DSL，最安全，推荐新手</li>
                <li><strong>高级模式</strong>（expert）：可提交 raw flow 并直接部署</li>
                <li><strong>管理员模式</strong>（admin）：运维/调试专用</li>
              </ul>
            </li>
          </ul>
          <p>点击 <strong>生成接入令牌</strong>。</p>
        `,
        action: { tab: 'agents', label: '前往 Agent 管理' },
      },
      {
        title: '保存接入令牌',
        content: `
          <p>弹窗会显示<strong>一次性接入令牌</strong>和连接信息。<strong style="color:var(--danger)">请立即复制保存</strong>，关闭后无法再次查看。</p>
          <p>连接信息包含：</p>
          <ul>
            <li><strong>MCP 服务器地址</strong>：根据权限模式不同（/mcp、/mcp-white、/mcp-admin）</li>
            <li><strong>请求头</strong>：<code>Authorization: Bearer &lt;接入令牌&gt;</code></li>
          </ul>
        `,
      },
      {
        title: '配置 MCP 客户端',
        content: `
          <p>在你的 AI 客户端（如 DeepSeek、Claude Desktop、Cursor）的 MCP 配置中填写：</p>
          <div class="code-box">{
  "mcpServers": {
    "autoflow": {
      "url": "http://&lt;网关地址&gt;:8000/mcp",
      "headers": {
        "Authorization": "Bearer &lt;你的接入令牌&gt;"
      }
    }
  }
}</div>
          <p>配置完成后，重启 AI 客户端，它应该能看到 AutoFlow 提供的 MCP 工具（如 <code>submit_dsl</code>、<code>deploy_flow</code> 等）。</p>
        `,
        tip: '如果连接失败，检查：① 地址端口是否正确（默认 8000）② 令牌是否完整无多余空格 ③ Agent 是否被停用',
      },
      {
        title: '验证连接',
        content: `
          <p>回到 <strong>Agent 管理</strong> 页面，查看 Agent 卡片的「最近连接」时间。如果显示了最近的连接时间，说明 MCP 配置成功。</p>
          <p>🎉 恭喜！你已经成功接入第一个 Agent。接下来可以学习如何让 Agent 编写 flow。</p>
        `,
        action: { tab: 'agents', label: '查看 Agent 状态' },
      },
    ],
  },

  // ── 教程 2：让 Agent 写 flow ──
  {
    id: 't2-write-flow',
    title: '让 Agent 写第一个 flow',
    icon: '✍️',
    category: '入门',
    description: '用自然语言描述需求，让 Agent 通过 MCP 提交 DSL 或 raw flow',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '两种提交方式',
        content: `
          <p>Agent 可以通过两种方式向 AutoFlow 提交自动化流程：</p>
          <table class="tbl">
            <thead><tr><th>类型</th><th>说明</th><th>安全等级</th></tr></thead>
            <tbody>
              <tr><td><strong>DSL 场景</strong></td><td>Agent 提交结构化 DSL，由网关编译器生成 flow，无 Function 节点</td><td><span class="badge ok">可信·可自动部署</span></td></tr>
              <tr><td><strong>原生 raw flow</strong></td><td>Agent 直接提交 Node-RED flow JSON，可含原生节点</td><td><span class="badge st-candidate">手写·需人工审核</span></td></tr>
            </tbody>
          </table>
          <p>新手推荐使用 DSL 方式，更安全。</p>
        `,
      },
      {
        title: '如何向 Agent 描述需求',
        content: `
          <p>用自然语言描述你想要的自动化场景，越具体越好。示例：</p>
          <div class="code-box">📝 好的描述：
"书房 PC 开机时（检测到 office-pc 的 power 状态变为 on），
把书房显示器挂灯调到 60% 亮度、暖白光（色温 3000K），
同时把书房空调设为 26 度制冷模式。"

❌ 模糊的描述：
"帮我搞个书房的自动化"</div>
          <p>好的描述包含：<strong>触发条件</strong>（什么事件触发）+ <strong>执行动作</strong>（控制哪些设备、设为什么状态）。</p>
        `,
      },
      {
        title: 'Agent 提交后会发生什么',
        content: `
          <p>Agent 通过 MCP 工具提交后，流程会出现在 <strong>提案</strong> 页面，你可以看到：</p>
          <ul>
            <li><strong>安全闸结果</strong>：PASS（通过）或 FAIL（未通过）</li>
            <li><strong>安全闸断言</strong>：逐条列出预期状态是否满足</li>
            <li><strong>静态检查</strong>：原生 flow 的 lint 错误/警告、硬伤、逻辑不可达</li>
            <li><strong>来源标签</strong>：编译产物·可信 或 手写·需审</li>
          </ul>
          <p>你不必看懂 DSL 细节——看安全闸 PASS/FAIL 和断言就够了。</p>
        `,
        action: { tab: 'proposals', label: '查看提案' },
      },
      {
        title: '如果安全闸 FAIL 怎么办',
        content: `
          <p>安全闸 FAIL 意味着流程可能会操作受保护设备，或预期状态无法满足。处理方法：</p>
          <ol>
            <li>查看<strong>安全闸断言</strong>，找到失败的具体项</li>
            <li>查看<strong>受保护实体</strong>列表，确认是否误操作了重要设备</li>
            <li>把报错原文<strong>复制给 Agent</strong>，让它修改后重新提交</li>
            <li>如果确认操作是安全的，可以调整安全闸规则后重试</li>
          </ol>
          <p>💡 提示：把页面上的报错原文直接发给 Agent，它通常能自行修正。</p>
        `,
      },
    ],
  },

  // ── 教程 3：审核并部署 ──
  {
    id: 't3-deploy',
    title: '审核并部署到 Node-RED',
    icon: '🚀',
    category: '入门',
    description: '在提案页面审核流程，通过安全闸后部署到 Node-RED',
    estimatedTime: '3 分钟',
    steps: [
      {
        title: '提案页面操作',
        content: `
          <p>在 <strong>提案</strong> 页面，对每个流程可以做以下操作：</p>
          <ul>
            <li><strong>部署到 NR</strong>：安全闸通过且你认可，点击部署到 Node-RED</li>
            <li><strong>拒绝</strong>：明显不对，打回给 Agent 重做</li>
            <li><strong>归档</strong>：暂时搁置，从活跃列表隐藏（可恢复）</li>
            <li><strong>删除</strong>：彻底移除（不可恢复）</li>
            <li><strong>注册子流程</strong>：如果是子流程类提案，注册后 Agent 可复用</li>
          </ul>
        `,
        action: { tab: 'proposals', label: '前往提案页面' },
      },
      {
        title: '部署前检查清单',
        content: `
          <p>点击「部署到 NR」之前，建议检查：</p>
          <ol>
            <li>✅ <strong>安全闸 PASS</strong>：没有操作受保护设备</li>
            <li>✅ <strong>静态检查无 error</strong>：原生 flow 没有硬伤</li>
            <li>✅ <strong>设备名称正确</strong>：确认操作的是你想操作的设备</li>
            <li>✅ <strong>触发条件合理</strong>：确认触发逻辑符合预期</li>
          </ol>
          <p>可信 DSL（编译产物·可信）可以一键部署；原生 raw flow 需要你确认无误后部署。</p>
        `,
      },
      {
        title: '部署到 Node-RED',
        content: `
          <p>点击 <strong>部署到 NR</strong>，确认弹窗后，网关会：</p>
          <ol>
            <li>在 Node-RED 中创建一个新的 tab（流程页）</li>
            <li>写入 flow 节点并连接</li>
            <li>触发 Node-RED 部署（使流程生效）</li>
            <li>在网关注册表中记录部署信息</li>
          </ol>
          <p>部署成功后，流程会出现在 <strong>已部署</strong> 页面。</p>
          <p style="color:var(--warn)">⚠️ 部署会操作真实设备，请确认无误后再点击。</p>
        `,
      },
      {
        title: '验证部署结果',
        content: `
          <p>部署完成后，建议手动验证：</p>
          <ol>
            <li>到 <strong>已部署</strong> 页面，找到刚部署的 flow</li>
            <li>点击 <strong>▶ 触发</strong> 手动点火，验证 flow 是否真跑通</li>
            <li>到 <strong>诊断</strong> 页面查看 debug 输出，确认没有报错</li>
            <li>观察真实设备状态是否符合预期</li>
          </ol>
          <p>如果触发后设备没有反应，到「诊断」查看 debug 输出，把报错发给 Agent 修正。</p>
        `,
        action: { tab: 'deployed', label: '查看已部署' },
      },
    ],
  },

  // ── 教程 4：修改已部署的 flow ──
  {
    id: 't4-modify-flow',
    title: '修改已部署的 flow',
    icon: '🔄',
    category: '进阶',
    description: '让 Agent 修改已部署的 flow，或手动撤回后重新部署',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '两种修改方式',
        content: `
          <p>已部署的 flow 需要修改时，有两种方式：</p>
          <table class="tbl">
            <thead><tr><th>方式</th><th>适用场景</th><th>操作</th></tr></thead>
            <tbody>
              <tr><td><strong>让 Agent 修改</strong></td><td>需求变化、逻辑调整</td><td>告诉 Agent 修改需求，它会提交新提案，审核后重新部署</td></tr>
              <tr><td><strong>手动撤回</strong></td><td>flow 有问题、不再需要</td><td>在已部署页面点击「撤回」，安全移除</td></tr>
            </tbody>
          </table>
        `,
      },
      {
        title: '让 Agent 修改 flow',
        content: `
          <p>告诉 Agent 你想怎么改，例如：</p>
          <div class="code-box">"把书房灯的自动化改一下：
          1. 触发条件改成 'PC 开机且时间在 18:00 之后'
          2. 挂灯亮度改成 80%
          3. 去掉空调控制"</div>
          <p>Agent 会：</p>
          <ol>
            <li>读取已部署的 flow（通过 MCP 工具）</li>
            <li>根据你的需求修改</li>
            <li>提交新的提案（旧的 flow 不会被自动覆盖）</li>
            <li>你审核新提案后部署</li>
          </ol>
          <p style="color:var(--warn)">⚠️ 新提案部署后，旧的 flow 仍然存在。你需要手动撤回旧的，避免两个 flow 同时运行冲突。</p>
        `,
      },
      {
        title: '撤回已部署的 flow',
        content: `
          <p>在 <strong>已部署</strong> 页面，点击 flow 卡片的 <strong>撤回</strong> 按钮。</p>
          <p>撤回机制非常安全：</p>
          <ul>
            <li><strong>只移除网关写入的节点</strong>，你在该 tab 中自己创建的节点会被保留</li>
            <li>如果整个 tab 都是网关部署的，会整 tab 删除</li>
            <li>撤回后 toast 会提示移除了多少个网关节点、保留了多少个你的节点</li>
          </ul>
          <p>如果 Node-RED 不可达，网关会询问是否只清理注册表（不碰 NR）。</p>
        `,
        action: { tab: 'deployed', label: '前往已部署' },
      },
      {
        title: '注册表漂移是什么',
        content: `
          <p>如果已部署的 flow 显示 <span class="badge stale">注册表漂移</span>，意味着：</p>
          <ul>
            <li>网关的注册表记录着这个 flow 已部署</li>
            <li>但 Node-RED 里已经找不到这个 flow_id 了</li>
          </ul>
          <p>常见原因：你手动在 Node-RED 里删除了该 flow，或者更换了 Node-RED 实例。</p>
          <p>处理方法：点击「撤回」，网关会只清理注册表、不碰 Node-RED（因为 flow 已经不在了）。这是正常现象，不是错误。</p>
        `,
      },
    ],
  },

  // ── 教程 5：自动修复 ──
  {
    id: 't5-selfheal',
    title: 'flow 不工作？用自动修复',
    icon: '🔧',
    category: '进阶',
    description: 'flow 部署后不工作，让 Agent 通过自动修复循环自动调试',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是自动修复',
        content: `
          <p><strong>自动修复</strong>（原称自愈闭环）是 AutoFlow 的核心能力：当已部署的 flow 不工作时，Agent 可以自动调试并修复，无需你手动排查。</p>
          <p>自动修复的循环：</p>
          <div class="desc" style="line-height:2">
            <strong>① inject 点火</strong> →
            <strong>② debug 回读</strong>（网关旁路订阅 NR 输出）→
            <strong>③ 分析报错</strong> →
            <strong>④ apply 修正</strong> flow →
            再点火验证 → 直到跑通
          </div>
        `,
      },
      {
        title: '先自己验证',
        content: `
          <p>在让 Agent 自动修复之前，建议先自己确认问题：</p>
          <ol>
            <li>到 <strong>已部署</strong> 页面，点击 <strong>▶ 触发</strong> 手动点火</li>
            <li>到 <strong>诊断</strong> 页面，查看 <strong>debug 输出</strong></li>
            <li>注意红色报错、空输出、节点数异常等现象</li>
            <li>把看到的报错原文复制下来</li>
          </ol>
          <p>💡 即使你看不懂报错，把原文发给 Agent 也能帮它快速定位问题。</p>
        `,
        action: { tab: 'diagnostics', label: '前往诊断' },
      },
      {
        title: '让 Agent 走自动修复',
        content: `
          <p>对 Agent 说：</p>
          <div class="code-box">"这个 flow 跑不通，走自动修复修一下。
          报错信息：[粘贴 debug 输出]"</div>
          <p>Agent 会自动执行修复循环。你可以在 <strong>诊断</strong> 页面实时看到：</p>
          <ul>
            <li>运行轨迹（trace）：每次点火、修正、验证的记录</li>
            <li>debug 输出：每次点火后的 NR 输出</li>
          </ul>
        `,
      },
      {
        title: '自动修复预算（防死循环）',
        content: `
          <p>为防止 Agent 陷入「修了又错、错了又修」的死循环，网关有<strong>自动修复预算</strong>：</p>
          <ul>
            <li>默认<strong>最多 3 次</strong>自动修正尝试（按 Agent + flow 计失败次数）</li>
            <li>顶部 <strong>♻️ 自动修复</strong> 按钮可修改这个上限</li>
            <li>设为 <strong>0 即关闭自动修复</strong>（任何修正都回到人工审核）</li>
            <li>预算用尽仍失败，Agent 会如实告诉你「已超重试上限，需人工介入」，不会假装成功</li>
          </ul>
        `,
        tip: '如果自动修复一直修不好，可能是 flow 的逻辑本身有根本性问题。建议撤回后让 Agent 重新写一个，而不是无限修复。',
      },
      {
        title: 'inject 节点是必须的吗？',
        content: `
          <p><strong>不是必须。</strong>inject 节点只是「自动点火的便捷入口」。</p>
          <p>如果 flow 没有 inject 节点：</p>
          <ul>
            <li>自动修复预算照常生效（不依赖 inject）</li>
            <li>debug 回读照常工作（任何触发产生的 debug 帧都能读到）</li>
            <li>只是「自动点火验证」这一步没有目标</li>
          </ul>
          <p>Agent 会自动选择：① 补一个 inject 节点 ② 改用 flow 已有的其他触发器（HA 状态变化、HTTP in 等）③ 请你手动触发。</p>
        `,
      },
    ],
  },

  // ── 教程 6：安全闸 ──
  {
    id: 't6-safety-gate',
    title: '安全闸：保护重要设备',
    icon: '🛡️',
    category: '安全',
    description: '配置安全闸规则，防止 Agent 误操作高价值/危险设备',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是安全闸',
        content: `
          <p><strong>安全闸</strong>是 AutoFlow 的真实设备保护机制。因为 AutoFlow 能控制真实电器，误操作可能造成现实后果（设备损坏、能耗、安全隐患）。</p>
          <p>安全闸让你预先声明「哪些设备 Agent 不能乱动」，在 Agent 的部署/操作触及这些设备时强制拦截或留痕。</p>
          <p>核心原则：<strong>宁可先把所有重要设备圈起来，再按需放宽。</strong></p>
        `,
      },
      {
        title: '导入设备目录',
        content: `
          <p>打开 <strong>安全闸</strong> 页面（在「设置」中），点击 <strong>导入全部设备</strong>：</p>
          <ul>
            <li>从 Home Assistant / Node-RED 拉取全屋实体目录</li>
            <li>仅手动触发，不会随测试连接自动运行</li>
            <li>导入后可以在「设备目录」中搜索和浏览</li>
          </ul>
          <p>搜索支持设备名、区域、英文 entity_id（如「书房灯」「客厅」「office」）。</p>
        `,
        action: { tab: 'safe', label: '前往安全闸' },
      },
      {
        title: '设置保护等级',
        content: `
          <p>给每个受保护设备选择保护等级：</p>
          <table class="tbl">
            <thead><tr><th>等级</th><th>行为</th><th>适用设备</th></tr></thead>
            <tbody>
              <tr><td><span class="badge tier-0">Tier-0 需确认</span></td><td>Agent 触及即<strong>暂停等人工确认</strong></td><td>高价值/危险设备：空调、总闸、门锁、热水器</td></tr>
              <tr><td><span class="badge tier-1">Tier-1 放行</span></td><td>放行但<strong>记操作日志</strong></td><td>低风险常调设备：夜灯、风扇、窗帘</td></tr>
            </tbody>
          </table>
          <p>建议：空调、门锁、总闸、热水器等重要设备先设为 Tier-0，确认安全后再放宽。</p>
        `,
      },
      {
        title: '安全闸如何工作',
        content: `
          <p>当 Agent 提交的 flow 触及受保护设备时：</p>
          <ol>
            <li>网关在部署前检查 flow 中操作的设备</li>
            <li>如果触及 Tier-0 设备，<strong>安全闸 FAIL</strong>，阻止部署</li>
            <li>弹窗显示「受保护实体」列表和具体拦截原因</li>
            <li>你可以：调整 flow 后重新提交，或调整安全闸规则</li>
          </ol>
          <p>Tier-1 设备不会阻止部署，但所有操作会记录在<strong>操作日志</strong>中（设置 → 操作日志）。</p>
        `,
      },
      {
        title: '操作日志的重要性',
        content: `
          <p>开启自动修复后（去人审），<strong>操作日志是你唯一的可追溯来源</strong>。</p>
          <p>建议定期查看「设置 → 操作日志」，关注：</p>
          <ul>
            <li>哪些设备被操作了</li>
            <li>操作的时间和频率</li>
            <li>是否有异常操作（如深夜操作空调、频繁开关门锁）</li>
          </ul>
          <p>如果发现异常，立即撤回相关 flow，并调整安全闸规则。</p>
        `,
        action: { tab: 'settings', label: '查看操作日志' },
      },
    ],
  },

  // ── 教程 7：子流程 ──
  {
    id: 't7-subflow',
    title: '子流程：复用通用能力',
    icon: '📦',
    category: '进阶',
    description: '注册和使用子流程，让 Agent 复用通用能力（如 Bark 推送、历史查询）',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是子流程',
        content: `
          <p><strong>子流程</strong>（subflow）是 Node-RED 中的可复用流程模块。AutoFlow 把子流程注册到「子流程注册表」后，Agent 可以通过 MCP 调用这些通用能力。</p>
          <p>典型的子流程：</p>
          <ul>
            <li><strong>Bark 推送</strong>：发送 iOS 推送通知</li>
            <li><strong>历史查询</strong>：查询 HA 实体的历史状态</li>
            <li><strong>自定义工具</strong>：你自己编写的通用能力</li>
          </ul>
          <p>子流程让 Agent 不必每次重复编写通用逻辑，直接调用即可。</p>
        `,
      },
      {
        title: '内置子流程：Bark 推送',
        content: `
          <p>AutoFlow 内置了 Bark 推送子流程（iOS 推送通知）。安装方法：</p>
          <ol>
            <li>先在「设置 → 连接配置 → Bark」填写 BARK_SERVER 和 BARK_KEY</li>
            <li>到 <strong>子流程</strong> 页面，找到 Bark 子流程</li>
            <li>点击 <strong>安装到 NR</strong>（安全重复安装，已存在则跳过）</li>
          </ol>
          <p>安装后，Agent 可以在 flow 中调用 Bark 推送，例如「flow 执行完成后发推送通知」。</p>
        `,
        action: { tab: 'subflows', label: '前往子流程' },
      },
      {
        title: '从 Node-RED 导入子流程',
        content: `
          <p>如果你在 Node-RED 中已经有自定义的子流程，可以导入到 AutoFlow：</p>
          <ol>
            <li>在 Node-RED 中打开子流程，复制其 ID（在子流程属性中）</li>
            <li>到 <strong>子流程</strong> 页面，点击 <strong>＋ 从 NR 导入</strong></li>
            <li>填写：NR 子流程 ID、DSL 调用名（唯一，如 <code>my_custom_push</code>）、标题</li>
            <li>点击 <strong>检测并导入</strong></li>
          </ol>
          <p>网关会自动读取子流程的输入端口和环境变量，提取参数，无需手动填写。</p>
          <p>导入后，Agent 可以通过「使用 &lt;调用名&gt;」来调用这个子流程。</p>
        `,
      },
      {
        title: 'Agent 如何调用子流程',
        content: `
          <p>子流程注册后，Agent 在编写 flow 时可以直接调用。在 DSL 中写：</p>
          <div class="code-box">调用子流程: bark_push(title="自动化完成", body="书房灯已开启")</div>
          <p>或者在 raw flow 中使用对应的 subflow 节点。</p>
          <p>你可以在子流程页面查看每个子流程的：</p>
          <ul>
            <li><strong>输入参数</strong>：调用时需要传入什么</li>
            <li><strong>需配置的环境变量</strong>：使用前需要配置什么</li>
            <li><strong>DSL 调用示例</strong>：如何在 DSL 中调用</li>
          </ul>
        `,
      },
    ],
  },

  // ── 教程 8：Link API ──
  {
    id: 't8-link-api',
    title: 'Link API：连接外部服务',
    icon: '🌐',
    category: '进阶',
    description: '通过 Link API 桥接外部 HTTP 服务，让 flow 可以调用外部 API',
    estimatedTime: '5 分钟',
    steps: [
      {
        title: '什么是 Link API',
        content: `
          <p><strong>Link API</strong>是 AutoFlow 的 HTTP 桥接能力，让 flow 可以调用外部 HTTP 服务。有两种模式：</p>
          <table class="tbl">
            <thead><tr><th>模式</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td><strong>link out 推送</strong></td><td>把 flow 的输出推送到外部 HTTP 端点（如 webhook、第三方 API）</td></tr>
              <tr><td><strong>http_api 内联</strong></td><td>网关内置 HTTP 接口，不生成 NR 节点，直接由网关处理</td></tr>
            </tbody>
          </table>
          <p>Link API 安装后会合并到 Node-RED 的「AutoFlow API」tab 中。</p>
        `,
      },
      {
        title: '从 tab 链接导入 Link API',
        content: `
          <p>最简单的方式是从 Node-RED 的 tab 链接导入：</p>
          <ol>
            <li>在 Node-RED 编辑器中，打开包含 link in 节点的 tab</li>
            <li>复制浏览器地址栏中的 tab 链接（格式：<code>http://host:1990/#flow/&lt;tabid&gt;</code>）</li>
            <li>到 <strong>Link API</strong> 页面，点击 <strong>🔗 从 tab 链接导入</strong></li>
            <li>粘贴 tab 链接，点击「检测能否注册」</li>
            <li>网关会自动检测 tab 的 link in 入口和调用参数</li>
            <li>填写 DSL 调用名（唯一），点击「注册为 Link API」</li>
          </ol>
        `,
        action: { tab: 'link_apis', label: '前往 Link API' },
      },
      {
        title: '配置和安装',
        content: `
          <p>注册 Link API 后，需要配置运行参数：</p>
          <ol>
            <li>在 Link API 页面，点击 <strong>⚙️ 配置</strong></li>
            <li>填写所需的密钥/token（如外部 API 的认证信息）</li>
            <li>密钥仅存储在本机，不会上传，保存后立即生效</li>
            <li>点击 <strong>📦 安装到 Node-RED</strong></li>
          </ol>
          <p>安装时网关会增量合并到 Node-RED 的「AutoFlow API」tab：</p>
          <ul>
            <li>已存在的节点会被更新（不是重复创建）</li>
            <li>新增的节点会被添加</li>
            <li>如果 NR 上有多个同名 tab，会警告你手动清理</li>
          </ul>
        `,
      },
      {
        title: 'Agent 如何调用 Link API',
        content: `
          <p>Link API 注册后，Agent 可以在 flow 中调用。在 DSL 中写：</p>
          <div class="code-box">调用子流程: my_api(param1="value1", param2="value2")</div>
          <p>你可以在 Link API 页面查看每个连接的：</p>
          <ul>
            <li><strong>入口类型</strong>：link out 推送 或 http_api 内联</li>
            <li><strong>输入参数</strong>：调用时需要传入什么</li>
            <li><strong>DSL 调用示例</strong>：如何在 DSL 中调用</li>
          </ul>
          <p>删除 Link API 时，只会移除网关生成的节点，你自己在「AutoFlow API」tab 中创建的节点不会被影响。</p>
        `,
      },
    ],
  },
];

// ═══════════════════════════════════════════════════════════
// 首次访问引导数据
// ═══════════════════════════════════════════════════════════

const FIRST_VISIT_GUIDES = {
  proposals: {
    title: '📋 提案页面',
    content: `
      <p><strong>提案</strong>是 Agent 提交的自动化流程，需要你审核后才能部署。</p>
      <ul>
        <li><span class="badge ok">安全闸 PASS</span>：流程安全，可以部署</li>
        <li><span class="badge danger">安全闸 FAIL</span>：触及受保护设备，需调整</li>
        <li><span class="badge st-candidate">手写·需审</span>：原生 raw flow，必须人工审核</li>
      </ul>
      <p>点击「部署到 NR」安装到 Node-RED，点击「拒绝」打回给 Agent。</p>
    `,
  },
  deployed: {
    title: '🚀 已部署页面',
    content: `
      <p><strong>已部署</strong>显示所有通过 AutoFlow 安装到 Node-RED 的 flow。</p>
      <ul>
        <li><strong>▶ 触发</strong>：手动点火验证 flow 是否跑通</li>
        <li><strong>撤回</strong>：安全移除（只删网关节点，保留你的节点）</li>
        <li><span class="badge stale">注册表漂移</span>：NR 里已无此 flow，撤回只清记录</li>
      </ul>
    `,
  },
  agents: {
    title: '🔗 Agent 管理',
    content: `
      <p><strong>Agent 管理</strong>用于创建和管理连接 AutoFlow 的 AI 客户端。</p>
      <ul>
        <li>点击「生成接入令牌」创建新 Agent</li>
        <li>令牌仅显示一次，请立即保存</li>
        <li>三种权限模式：标准（仅 DSL）、高级（可部署）、管理员（运维）</li>
      </ul>
    `,
  },
  safe: {
    title: '🛡️ 安全闸',
    content: `
      <p><strong>安全闸</strong>保护重要设备不被 Agent 误操作。</p>
      <ul>
        <li><strong>Tier-0</strong>：触及即暂停，等人工确认（空调、门锁、总闸）</li>
        <li><strong>Tier-1</strong>：放行但记日志（夜灯、风扇）</li>
        <li>先「导入全部设备」，再给重要设备设保护等级</li>
      </ul>
    `,
  },
  subflows: {
    title: '📦 子流程',
    content: `
      <p><strong>子流程</strong>是可复用的 NR 流程模块，注册后 Agent 可以调用。</p>
      <ul>
        <li>内置 Bark 推送：安装后 Agent 可发 iOS 通知</li>
        <li>「从 NR 导入」：把你自己的子流程注册进来</li>
        <li>注册后 Agent 用「调用子流程: 名称(参数)」调用</li>
      </ul>
    `,
  },
  link_apis: {
    title: '🌐 Link API',
    content: `
      <p><strong>Link API</strong>桥接外部 HTTP 服务，让 flow 可以调用外部 API。</p>
      <ul>
        <li>「从 tab 链接导入」：自动检测 NR 中的 link in 入口</li>
        <li>配置密钥后「安装到 Node-RED」</li>
        <li>安装到「AutoFlow API」tab，增量更新不重复</li>
      </ul>
    `,
  },
  diagnostics: {
    title: '🩺 诊断',
    content: `
      <p><strong>诊断</strong>页面查看网关健康状态和运行轨迹。</p>
      <ul>
        <li><strong>运行轨迹</strong>：每次部署、触发、修正的记录（重启后清空）</li>
        <li><strong>debug 输出</strong>：NR 的 debug 事件流，排查 flow 问题</li>
        <li><strong>评测任务</strong>：golden/验收测试的运行记录</li>
      </ul>
    `,
  },
};

// ═══════════════════════════════════════════════════════════
// 教程系统渲染逻辑
// ═══════════════════════════════════════════════════════════

const TUTORIAL_STATE = {
  currentTutorial: null,
  currentStep: 0,
};

function renderTutorialList() {
  const container = document.getElementById('view-tutorials');
  if (!container) return;

  const categories = {};
  TUTORIALS.forEach(t => {
    if (!categories[t.category]) categories[t.category] = [];
    categories[t.category].push(t);
  });

  let html = `
    <div class="view-head">
      <h2>📚 交互式教程</h2>
      <span class="sub">从入门到进阶，手把手掌握 AutoFlow</span>
    </div>
    <div class="tutorial-stats">
      <div class="stat-card"><span class="stat-num">${TUTORIALS.length}</span><span class="stat-label">个教程</span></div>
      <div class="stat-card"><span class="stat-num">${TUTORIALS.reduce((s, t) => s + t.steps.length, 0)}</span><span class="stat-label">个步骤</span></div>
      <div class="stat-card"><span class="stat-num">${Object.keys(categories).length}</span><span class="stat-label">个分类</span></div>
    </div>
  `;

  for (const [cat, tutorials] of Object.entries(categories)) {
    html += `<h3 class="tutorial-cat-title">${cat}</h3><div class="tutorial-grid">`;
    tutorials.forEach(t => {
      const progress = getTutorialProgress(t.id);
      const pct = Math.round((progress / t.steps.length) * 100);
      html += `
        <div class="tutorial-card" data-tutorial="${t.id}">
          <div class="tutorial-card-head">
            <span class="tutorial-icon">${t.icon}</span>
            <div class="tutorial-card-title">${t.title}</div>
          </div>
          <p class="tutorial-card-desc">${t.description}</p>
          <div class="tutorial-card-meta">
            <span>⏱ ${t.estimatedTime}</span>
            <span>${t.steps.length} 步</span>
          </div>
          <div class="tutorial-progress">
            <div class="tutorial-progress-bar"><div class="tutorial-progress-fill" style="width:${pct}%"></div></div>
            <span class="tutorial-progress-text">${progress}/${t.steps.length}</span>
          </div>
          <button class="btn primary tutorial-start-btn" data-tutorial="${t.id}">
            ${progress > 0 ? '继续学习' : '开始学习'}
          </button>
        </div>
      `;
    });
    html += '</div>';
  }

  container.innerHTML = html;

  // 绑定点击事件
  container.querySelectorAll('.tutorial-card, .tutorial-start-btn').forEach(el => {
    el.addEventListener('click', (e) => {
      const tid = e.currentTarget.dataset.tutorial || e.target.closest('[data-tutorial]')?.dataset.tutorial;
      if (tid) startTutorial(tid);
    });
  });
}

function startTutorial(tutorialId) {
  const t = TUTORIALS.find(x => x.id === tutorialId);
  if (!t) return;
  TUTORIAL_STATE.currentTutorial = t;
  const progress = getTutorialProgress(t.id);
  TUTORIAL_STATE.currentStep = progress > 0 ? progress : 0;
  renderTutorialDetail();
}

function renderTutorialDetail() {
  const t = TUTORIAL_STATE.currentTutorial;
  if (!t) return;
  const container = document.getElementById('view-tutorials');
  if (!container) return;

  const step = t.steps[TUTORIAL_STATE.currentStep];
  const total = t.steps.length;
  const idx = TUTORIAL_STATE.currentStep;
  const pct = Math.round(((idx + 1) / total) * 100);

  let html = `
    <div class="tutorial-detail">
      <div class="tutorial-detail-head">
        <button class="btn ghost tutorial-back-btn">← 返回教程列表</button>
        <div class="tutorial-detail-title">
          <span class="tutorial-icon">${t.icon}</span>
          <span>${t.title}</span>
        </div>
        <div class="tutorial-detail-progress">
          <span>第 ${idx + 1} / ${total} 步</span>
          <div class="tutorial-progress-bar small"><div class="tutorial-progress-fill" style="width:${pct}%"></div></div>
        </div>
      </div>

      <div class="tutorial-step-nav">
        ${t.steps.map((s, i) => `
          <button class="tutorial-step-dot ${i === idx ? 'active' : ''} ${i < idx ? 'done' : ''}"
                  data-step="${i}" title="${s.title}">${i < idx ? '✓' : i + 1}</button>
        `).join('')}
      </div>

      <div class="tutorial-step-content card">
        <h3 class="tutorial-step-title">${step.title}</h3>
        <div class="tutorial-step-body">${step.content}</div>
        ${step.tip ? `<div class="tutorial-tip">💡 ${step.tip}</div>` : ''}
        ${step.action ? `<button class="btn primary tutorial-action-btn" data-tab="${step.action.tab}">${step.action.label} →</button>` : ''}
      </div>

      <div class="tutorial-detail-foot">
        <button class="btn ghost tutorial-prev-btn" ${idx === 0 ? 'disabled' : ''}>← 上一步</button>
        ${idx === total - 1
          ? '<button class="btn primary tutorial-finish-btn">🎉 完成教程</button>'
          : '<button class="btn primary tutorial-next-btn">下一步 →</button>'
        }
      </div>
    </div>
  `;

  container.innerHTML = html;

  // 绑定事件
  container.querySelector('.tutorial-back-btn')?.addEventListener('click', () => {
    TUTORIAL_STATE.currentTutorial = null;
    renderTutorialList();
  });

  container.querySelectorAll('.tutorial-step-dot').forEach(dot => {
    dot.addEventListener('click', () => {
      TUTORIAL_STATE.currentStep = parseInt(dot.dataset.step);
      saveTutorialProgress(t.id, TUTORIAL_STATE.currentStep);
      renderTutorialDetail();
    });
  });

  container.querySelector('.tutorial-prev-btn')?.addEventListener('click', () => {
    if (TUTORIAL_STATE.currentStep > 0) {
      TUTORIAL_STATE.currentStep--;
      saveTutorialProgress(t.id, TUTORIAL_STATE.currentStep);
      renderTutorialDetail();
    }
  });

  container.querySelector('.tutorial-next-btn')?.addEventListener('click', () => {
    if (TUTORIAL_STATE.currentStep < total - 1) {
      TUTORIAL_STATE.currentStep++;
      saveTutorialProgress(t.id, TUTORIAL_STATE.currentStep);
      renderTutorialDetail();
    }
  });

  container.querySelector('.tutorial-finish-btn')?.addEventListener('click', () => {
    saveTutorialProgress(t.id, total);
    TUTORIAL_STATE.currentTutorial = null;
    renderTutorialList();
    if (typeof toast === 'function') toast(`🎉 完成教程「${t.title}」`);
  });

  container.querySelector('.tutorial-action-btn')?.addEventListener('click', (e) => {
    const tab = e.currentTarget.dataset.tab;
    if (typeof showTab === 'function') showTab(tab);
  });
}

// ═══════════════════════════════════════════════════════════
// 进度存储（localStorage）
// ═══════════════════════════════════════════════════════════

function getTutorialProgress(tutorialId) {
  try {
    const data = JSON.parse(localStorage.getItem('autoflow_tutorial_progress') || '{}');
    return data[tutorialId] || 0;
  } catch { return 0; }
}

function saveTutorialProgress(tutorialId, step) {
  try {
    const data = JSON.parse(localStorage.getItem('autoflow_tutorial_progress') || '{}');
    data[tutorialId] = step;
    localStorage.setItem('autoflow_tutorial_progress', JSON.stringify(data));
  } catch {}
}

// ═══════════════════════════════════════════════════════════
// 首次访问引导
// ═══════════════════════════════════════════════════════════

function checkFirstVisit(tabId) {
  const guide = FIRST_VISIT_GUIDES[tabId];
  if (!guide) return;
  try {
    const visited = JSON.parse(localStorage.getItem('autoflow_visited_tabs') || '{}');
    if (visited[tabId]) return;
    visited[tabId] = true;
    localStorage.setItem('autoflow_visited_tabs', JSON.stringify(visited));
    if (typeof modal === 'function') {
      modal(guide.title, guide.content + '<div style="text-align:right;margin-top:12px"><button class="btn primary" onclick="closeModal()">我知道了</button></div>');
    }
  } catch {}
}

// 重置首次访问引导（调试用）
function resetFirstVisitGuides() {
  localStorage.removeItem('autoflow_visited_tabs');
  if (typeof toast === 'function') toast('已重置首次访问引导');
}

// ═══════════════════════════════════════════════════════════
// 导出到全局
// ═══════════════════════════════════════════════════════════

window.TUTORIALS = TUTORIALS;
window.renderTutorialList = renderTutorialList;
window.startTutorial = startTutorial;
window.renderTutorialDetail = renderTutorialDetail;
window.checkFirstVisit = checkFirstVisit;
window.resetFirstVisitGuides = resetFirstVisitGuides;
