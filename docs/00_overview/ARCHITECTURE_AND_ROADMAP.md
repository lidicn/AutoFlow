# AutoFlow 架构与路线图汇总（v1.7.1）

> **更新时间**：2026-09-05
> **当前版本**：v1.7.1（经验复用稳定版 + 修复）
> **负责人**：dw

---

## 一、产品定位与分级

| 版本 | 名称 | 定位 | 连接方式 | 适用场景 |
|------|------|------|---------|---------|
| Core | 基础版 | 极客直连 | nr_client.py 直连 NR Admin API | 完全自主、不需要安全闸门 |
| Standard | 标准版 | MCP 接入 | Agent 通过 MCP 调用网关 | 通用 Agent 集成、需要安全闸门 |
| Pro | 专业版 | 轻量客户端+网关 | nr_client.py --gateway 调用 REST API | 省 token、需要经验复用、API Key 管理 |

**核心设计理念**：
- 网关是统一数据入口，所有操作经过安全闸门
- DSL 优先（省 token、可审计、可复用），raw 作为逃生舱
- 双层授权：Agent 身份（API Key）+ 授权范围（Tab 授权）
- 经验数据自动收集，越用越聪明

---

## 二、当前架构

### 2.1 系统组件

```
┌─────────────────────────────────────────────────────────┐
│                    Agent / 用户                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Core 版  │  │Standard版│  │      Pro 版           │  │
│  │nr_client │  │  MCP     │  │ nr_client --gateway  │  │
│  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘  │
│       │              │                   │              │
└───────┼──────────────┼───────────────────┼──────────────┘
        │              │                   │
        ▼              ▼                   ▼
┌─────────────────────────────────────────────────────────┐
│              AutoFlow Gateway (autoflow_gateway)         │
│                                                          │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────┐   │
│  │ REST API   │  │ MCP Server │  │   WebUI          │   │
│  │ /api/core/*│  │ /mcp-white │  │   (静态页面)     │   │
│  │ /api/*     │  │            │  │                  │   │
│  └─────┬──────┘  └─────┬──────┘  └────────┬─────────┘   │
│        │               │                   │             │
│        ▼               ▼                   ▼             │
│  ┌──────────────────────────────────────────────────┐   │
│  │              核心服务层                            │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │   │
│  │  │ DSL编译  │ │安全闸门  │ │ 快照/回滚       │  │   │
│  │  └──────────┘ └──────────┘ └──────────────────┘  │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │   │
│  │  │API Key   │ │模板库    │ │ Token统计        │  │   │
│  │  └──────────┘ └──────────┘ └──────────────────┘  │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │   │
│  │  │错误知识库│ │经验收集  │ │ 智能推荐         │  │   │
│  │  └──────────┘ └──────────┘ └──────────────────┘  │   │
│  └──────────────────────────────────────────────────┘   │
│                           │                              │
│                           ▼                              │
│  ┌──────────────────────────────────────────────────┐   │
│  │           数据存储 (JSON 文件)                     │   │
│  │  data/<env>/                                      │   │
│  │  ├── api_keys.json       (API Key + 审计日志)     │   │
│  │  ├── templates.json      (模板库)                 │   │
│  │  ├── token_stats/        (Token 统计按天)         │   │
│  │  ├── error_knowledge.json (错误知识库)            │   │
│  │  ├── experience/         (经验数据)               │   │
│  │  │   ├── logs/YYYY-MM-DD.jsonl (操作日志)         │   │
│  │  │   ├── entity_cooccur.json (实体共现)           │   │
│  │  │   └── dsl_patterns.json   (DSL模式)            │   │
│  │  └── deploy_tokens.json    (部署授权码)           │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
              ┌──────────────────────┐
              │   Node-RED (NR)      │
              │   Home Assistant     │
              └──────────────────────┘
```

### 2.2 关键模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 网关主程序 | `src/autoflow_gateway/webui.py` | REST API + MCP + WebUI 路由 |
| 认证 | `webui_auth.py` | Session 认证 + /api/core/* 白名单 |
| API Key | `api_keys.py` | SHA-256 hash 存储、权限分级、tab 授权、吊销、审计 |
| 模板库 | `templates.py` | CRUD、变量渲染、内置模板 |
| Token 统计 | `token_stats.py` | 按天/Agent/模式聚合 |
| 错误知识库 | `error_knowledge.py` | 自动记录、按类型分类、搜索 |
| 经验收集 | `experience.py` | 操作日志、实体关联、DSL模式、最佳实践、智能推荐 |
| 核心客户端 | `core/skill/scripts/nr_client.py` | 直连模式 + --gateway 模式（v3.2.0） |
| Pro Skill | `core/skill/SKILL.md` | Agent 使用指南，DSL 优先引导 |

### 2.3 API 体系

**Pro 客户端 API（/api/core/*，需 API Key）**：
- `GET /version`、`GET /health`
- `POST /propose-dsl`（★首选，返回 _suggestions 智能推荐）
- `POST /deploy-proposal`、`POST /deploy-raw`（逃生舱）
- `POST /raw-to-dsl`（raw 转 DSL 草稿）
- `GET /entities`、`GET /resolve-entity`
- `GET /snapshots`、`POST /rollback`
- `GET /token-stats`

**WebUI 内部 API（/api/*，需 Session）**：
- API Key 管理：`/api/keys/*`
- 模板库：`/api/templates/*`
- Token 统计：`/api/token-stats`
- 错误知识库：`/api/errors`、`/api/errors/stats`
- 经验数据：`/api/experience/*`（summary/logs/entities/patterns/best-practices/agent-comparison/recommend/similar/suggest-fix/recommend-entities）
- 部署授权码：`/api/deploy-tokens/*`
- Tab 组织：`/api/tab-org/*`

---

## 三、已实现功能（v1.0 - v1.7.1）

### v1.x 基础能力
- 网关 + MCP 接口
- DSL 编译器 + 安全闸门
- 快照 + 回滚
- WebUI 基础界面
- 教程系统（8教程35步骤）
- 在线更新（版本简介、国内镜像、进度条）

### v1.3.x Tab 组织模式
- 三种模式可选：每flow一个tab / 单tab+comment / 分级方案
- WebUI 目标 tab 选择器
- 分级方案：agent 修改 flow 不串台

### v1.4.x 部署授权
- 部署授权码（deploy token）
- 多 tab 勾选授权
- 授权码使用说明文案
- 授权码可回溯（快照回滚）

### v1.5.x AutoFlow Pro
- v1.5.0: /api/core/* REST API + nr_client.py --gateway 模式
- v1.5.1: API Key 管理（SHA-256、权限分级、tab 授权、吊销、审计）
- v1.5.2: 模板库（CRUD、变量渲染、3内置模板）+ 认证白名单修复
- v1.5.3: Token 统计
- v1.5.4: propose-dsl json bug + 吊销兼容性
- v1.5.5: target_tab 死参数修复（P1 安全）
- v1.5.6: version 路径修复（多路径尝试）
- v1.5.7: Token 统计可视化 + 错误知识库
- v1.5.8: 离线降级 + raw-to-dsl 转换
- v1.5.9: raw-to-dsl BFS 修复 + version BOM 修复
- v1.6.0: Pro 稳定版

### v1.6.x 经验复用
- v1.6.1: 数据收集管道（操作日志、实体关联、DSL模式）
- v1.6.2: 经验库（最佳实践、Agent对比、模板推荐）
- v1.6.3: 智能推荐（相似案例、修复建议、实体推荐）
- v1.6.4: DSL 模式提取修复（支持5种格式）
- v1.7.0: 经验复用稳定版
- v1.7.1: Token 统计页面认证修复

---

## 四、未来计划

### Phase 4：竞技场模式（v2.0）— 规划中

**核心理念**：授权一批真实/虚拟设备，让 Agent 们通过 Pro 版往竞技场编写 flow，统一 inject 节点开始，通过自动验收收获经验数据、DSL bug、优质 flow。

**三层架构**：
- **L1 沙盒层**：独立 NR 实例 + vhass 虚拟设备，完全隔离，可随意折腾
- **L2 仿真层**：真实设备只读/限频验证，不影响生产
- **L3 实战层**：用户授权真实设备，严格配额 + 安全闸门

**核心功能**：
- 挑战任务系统（明确任务 + 验收标准）
- 多 Agent 支持（每个 Agent 独立 tab/命名空间）
- 自动验收（批量触发 + 状态对比 + 评分）
- 排行榜（成功率、token 效率、flow 质量）
- 经验自动提取（失败→错误库，成功→模板库）
- 优质 flow 一键入库

### 更远期
- v2.1: 经验市场（用户间分享优质 flow 和模板）
- v2.2: 联邦学习（多网关经验聚合，不泄露隐私）
- v3.0: 完全自治（Agent 自主发现设备、自主编写 flow、自主验证）

---

## 五、开发节奏

```
开发迭代（2-3个小版本）→ 集中测试（wb2）→ 修复发布（稳定版）→ 下一阶段
```

- 每个小版本：功能完成即发布，dw 自验（语法+冒烟）
- 每 2-3 个版本：集中开工单给 wb2 全面测试
- 每个 Phase 结束：发布稳定版（x.y.0）

---

## 六、关键技术决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 存储 | JSON 文件 | 轻量、无依赖、易备份、NAS 友好 |
| 认证双轨 | /api/core/* 用 API Key，/api/* 用 Session | Pro 客户端无浏览器，WebUI 有浏览器 |
| DSL 优先 | propose-dsl 是首选入口 | 省 token、可审计、可经验复用 |
| raw 逃生舱 | deploy-raw 保留 | 极端情况下 Agent 可绕过 DSL 限制 |
| 离线降级 | 网关不可用时 nr_client 直连 NR | 提高可用性，Pro 不绑死网关 |
| 经验自动收集 | propose-dsl 成功/失败自动记录 | 无需人工干预，数据自然积累 |
