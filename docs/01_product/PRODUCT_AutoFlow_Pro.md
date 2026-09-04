# AutoFlow Pro — 产品定位与设计文档

> **创建时间**：2026-09-05
> **版本**：v1.1（规划稿，更新授权模型）
> **状态**：待开发，计划 v1.5.0 首发
> **曾用名**：AutoFlow Lite（2026-09-05 更名为 Pro）

---

## 一、一句话定位

**AutoFlow Pro 是专业版 Agent 客户端，让 Agent 用最少的 token 享受网关的安全编译、对话即部署、快照回滚和经验复用能力。**

---

## 二、产品分级

| 版本 | 名称 | 定位 | 交互方式 | 目标用户 |
|------|------|------|---------|---------|
| 基础版 | **AutoFlow Core** | 直连 NR，无网关 | skill + nr_client.py 直连 | 极客/离线 |
| 标准版 | **AutoFlow Standard** | MCP 完整版，功能全 | MCP 协议，40+ 工具 | 普通用户/复杂场景 |
| 专业版 | **AutoFlow Pro** | 轻量客户端 + 网关增强 | skill + 网关 REST API | **专业用户/日常首选** |

**Pro 的核心优势**：比 Standard 省 10K+ token/次，比 Core 多安全闸门和经验复用，是性价比最高的版本。

---

## 三、为什么需要 Pro 版

### 问题

| 方式 | 固定开销 | 输出开销 | 安全性 | 经验复用 | 部署体验 |
|------|---------|---------|--------|---------|---------|
| MCP 标准版 | ~15K token（40工具） | DSL 省 / raw 费 | 高 | ✅ | 需点部署按钮 |
| Core 直连 NR | ~2K token | raw 费（完整JSON） | 低 | ❌ | 直接部署 |
| 纯裸调 API | ~0 | raw 费 | 无 | ❌ | 直接部署 |

**痛点**：
- 标准版功能全，但固定开销太大，简单场景也得加载 15K token 工具定义
- Core 直连轻量，但没有安全闸门，数据不经过网关，经验复用无从谈起
- 标准版的"点部署按钮"是仪式感——跟 agent 对话那一刻起就是在授权了

### 解决方案

AutoFlow Pro = Core 的轻量 skill + 网关的 REST API + Agent 级授权管理

- 固定开销：~3K token（SKILL.md，只比 Core 多 1K）
- 输出：DSL 文本（比 raw JSON 省 5-10 倍）
- 安全性：编译闸门 + 快照回滚 + Agent 授权范围
- 部署体验：**对话即部署**，授权范围内不需要点按钮
- 经验复用：所有操作经过网关，数据可收集可复用

---

## 四、核心设计原则

### 1. DSL 优先，raw 补充

**为什么**：DSL 比 raw JSON 省 5-10 倍 token，且有编译闸门兜底，成功率更高。

**如何设计**：

**命令层面（明确排序）**：
```
1. propose-dsl      ★首选  提交 DSL，网关编译+闸门校验，自动部署
2. deploy-proposal         部署已通过的提案（预览模式用）
3. list-templates          列出可用模板
4. render-template         渲染模板生成 DSL
5. deploy-raw        ⚠️高级  直接提交 raw JSON（逃生舱）
6. rollback                回滚到快照
```

**SKILL.md 引导**：
- 开头第一句："**99% 的场景用 propose-dsl，只有 DSL 确实表达不了时才用 deploy-raw**"
- deploy-raw 文档加警告："此命令输出冗长、无编译校验，仅作为逃生舱"
- 提供 DSL 语法速查表，降低学习成本

**网关侧引导**：
- Agent 调用 deploy-raw 时，返回结果附带 DSL 转换建议
- 统计 DSL/raw 使用比例，WebUI 展示"DSL 使用率"

### 2. Agent 身份 + 授权范围 双层模型

**为什么**：只用 API Key 风险大（无法细粒度控制、无法追溯、无法快速吊销）。需要"身份"和"权限"分离。

**架构**：

```
┌─────────────────────────────────────────────────────┐
│  WebUI Agent 管理页面                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │ 豆包管家     │  │ Claude      │  │ WorkBuddy   │ │
│  │ API Key: ***│  │ API Key: ***│  │ API Key: ***│ │
│  │ 授权tab:     │  │ 授权tab:     │  │ 授权tab:     │ │
│  │ ☑ 客厅       │  │ ☑ 全屋       │  │ ☑ 书房       │ │
│  │ ☑ 书房       │  │              │  │              │ │
│  │ 权限: 部署   │  │ 权限: 部署   │  │ 权限: 只读   │ │
│  └─────────────┘  └─────────────┘  └─────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Agent 身份（API Key）**：
- 每个 agent 有独立的 API Key，绑定 agent_id
- 长期有效，可吊销
- 复用现有 MCP Bearer token 设计（hash 存储、不存明文）
- 用于身份认证和审计

**授权范围（Tab 授权）**：
- 在 WebUI Agent 页面管理
- 每个 agent 授权可操作的 tab 列表
- 权限级别：只读 / 可部署 / 可修改 / 可删除
- 可随时修改，实时生效

**临时授权（可选）**：
- 想临时给 agent 授权某个 tab，设置过期时间
- 管理入口统一到 agent 页面
- 过期自动失效

### 3. 对话即部署（默认）

**为什么**：跟 agent 对话那一刻起就是在授权了，点部署按钮是仪式感。

**三种操作模式**：

| 模式 | 触发方式 | 适用场景 | 安全级别 |
|------|---------|---------|---------|
| **自动部署** | 对话即部署，不需要点按钮 | 授权范围内的常规操作 | 中（有授权范围+快照） |
| **提案预览** | agent 加 preview=true，只生成不部署 | 复杂 flow、首次使用、不确定 | 高 |
| **危险操作确认** | 网关返回"需要确认"，agent 提示用户 | 删除节点、覆盖其他 tab、超阈值 | 最高 |

**设计原则**：
- 默认自动部署：授权范围内常规操作直接部署
- 保留提案预览：agent 可选择只生成不部署
- 危险操作必须确认：删除、覆盖、超阈值，agent 提示用户确认
- 可回滚：所有自动部署都有快照，不满意一键回滚

### 4. 统一经过网关，数据可收集

所有操作（除离线降级外）经过网关，记录：Agent ID、时间、操作类型、输入输出、成功/失败。
这些数据是 Token 统计、错误知识库、实体关联分析的基础。

### 5. 离线降级（可选，v1.5.3）

网关不可用时自动 fallback 到直连 NR，警告 agent"失去安全闸门"，网关恢复后自动切回。

---

## 五、API 设计

### 基础信息

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/version` | GET | 网关版本 + 兼容性检查 |
| `/api/core/health` | GET | 网关健康检查 |

### Flow 编写

| API | 方法 | 功能 | 说明 |
|-----|------|------|------|
| `/api/core/propose-dsl` | POST | 提交 DSL，编译+闸门校验，自动部署 | ★首选入口 |
| `/api/core/deploy-proposal` | POST | 部署已通过的提案 | 预览模式用 |
| `/api/core/deploy-raw` | POST | 直接提交 raw JSON | ⚠️逃生舱，附 DSL 转换警告 |

### 实体查询

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/entities` | GET | 实体目录（按域/区域/关键词过滤） |
| `/api/core/resolve-entity` | GET | 自然语言设备名→entity_id |

### 模板库（v1.5.1）

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/templates` | GET | 列出可用模板 |
| `/api/core/templates/render` | POST | 渲染模板生成 DSL |
| `/api/core/templates/save` | POST | 从提案保存为模板 |

### 快照与回滚

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/snapshots` | GET | 快照列表 |
| `/api/core/rollback` | POST | 回滚到指定快照 |

### 统计（v1.5.2）

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/token-stats` | GET | Token 消耗统计 |
| `/api/core/error-knowledge` | GET | 错误知识库 |

### Agent 管理（v1.5.1）

| API | 方法 | 功能 |
|-----|------|------|
| `/api/agents` | GET | 列出已授权的 agent |
| `/api/agents` | POST | 创建 agent + API Key |
| `/api/agents/{id}` | PUT | 更新 agent 授权范围 |
| `/api/agents/{id}` | DELETE | 吊销 agent API Key |

### 工具（v1.5.3）

| API | 方法 | 功能 |
|-----|------|------|
| `/api/core/raw-to-dsl` | POST | raw JSON 转 DSL 参考 |

---

## 六、认证与授权流程

### 日常调用流程

```
Agent 调用 propose-dsl
    │
    ▼
网关验证 API Key（身份）
    │
    ▼
检查 agent_id 与 API Key 绑定关系
    │
    ▼
检查操作的 tab 是否在 agent 授权范围内
    │
    ├── 不在授权范围 → 返回 403 "tab 不在授权范围内"
    │
    ▼
检查是否危险操作（删除/覆盖/超阈值）
    │
    ├── 是危险操作 → 返回 "需要用户确认"，agent 提示用户
    │
    ▼
编译 DSL → 闸门校验 → 自动部署 → 自动快照
    │
    ▼
返回结果 + 审计日志
```

### API Key 格式

复用现有 MCP Bearer token 设计：
- 格式：`af_pro_<random>`
- 存储：sha256 hash，不存明文
- 验证：`Authorization: Bearer <api_key>`
- 关联：agent_id、授权 tab 列表、权限级别

---

## 七、Token 统计设计

### 估算方式

```
输入 token ≈ 输入字符数 / 4
输出 token ≈ 输出字符数 / 4
固定开销 ≈ SKILL.md 大小（已知，~3K token）
总消耗 ≈ 固定开销 + 输入 + 输出
```

### 统计维度

- 时间：今日 / 本周 / 本月
- 模式：DSL vs raw 的比例和消耗对比
- Agent：按 Agent ID 排行
- 节省估算：对比"如果都用 raw"的理论消耗

---

## 八、安装与配置

### 一句话安装

```bash
curl -fsSL https://raw.githubusercontent.com/lidicn/AutoFlow/main/core/install-pro.sh | bash
```

### 配置

配置文件：`~/.autoflow-pro/config.json`
```json
{
  "gateway_url": "http://192.168.2.200:8000",
  "api_key": "af_pro_xxxxxx",
  "agent_id": "my-agent",
  "default_mode": "dsl",
  "auto_deploy": true,
  "offline_fallback": true,
  "nr_direct_url": "http://192.168.2.200:1880"
}
```

### 给 Agent 的安装提示词

```
请帮我安装 AutoFlow Pro（专业版 Agent 客户端），全程自主完成：
1. 执行 curl -fsSL https://raw.githubusercontent.com/lidicn/AutoFlow/main/core/install-pro.sh | bash
2. 编辑 ~/.autoflow-pro/config.json，填入网关地址、API Key 和 agent_id
3. 运行 python ~/.autoflow-pro/scripts/nr_client.py doctor 自检
4. 读取 ~/.autoflow-pro/skill/SKILL.md，掌握 propose-dsl 用法
5. 用 propose-dsl 写一个测试 flow 验证连通性
```

---

## 九、成功指标

| 指标 | 目标 |
|------|------|
| Pro 版用户占比 | >50% 的 Core 用户迁移 |
| DSL 使用率 | >90% 的操作走 DSL |
| 平均单次 token 消耗 | <8K（对比标准版 ~17K） |
| 一次成功率 | >85%（对比 Core 直连 ~70%） |
| 自动部署比例 | >80% 的操作无需点按钮 |

---

## 十、后续演进（v2.0+）

- 基于经验库的智能推荐（Flow 自动补全、错误自动修复）
- 多 Agent 协作（不同 Agent 共享模板和经验）
- 云端同步（多设备共享配置和模板）
- 社区模板市场
