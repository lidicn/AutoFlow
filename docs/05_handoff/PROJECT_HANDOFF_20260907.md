# AutoFlow 项目交接文档

> **交接日期**：2026-09-07
> **当前版本**：v2.0.11-beta（竞技场 beta）
> **原负责人**：dw
> **项目地址**：https://github.com/lidicn/AutoFlow
> **本地路径**：E:\NAS\autoflow

---

## 一、项目概述

AutoFlow 是一个面向 Home Assistant + Node-RED 极客用户的 AI 自动化编排系统。核心价值：让 Agent 通过自然语言/DSL 编写 Node-RED flow，经网关安全闸门验证后部署，实现"对话即自动化"。

### 1.1 产品分级

| 版本 | 名称 | 定位 | 连接方式 | 适用场景 |
|------|------|------|---------|---------|
| Core | 基础版 | 极客直连 | nr_client.py 直连 NR Admin API | 完全自主、不需要安全闸门 |
| Standard | 标准版 | MCP 接入 | Agent 通过 MCP 调用网关 | 通用 Agent 集成、需要安全闸门 |
| Pro | 专业版 | 轻量客户端+网关 | nr_client.py --gateway 调用 REST API | 省 token、经验复用、API Key 管理 |

### 1.2 核心设计理念

- **网关是统一数据入口**，所有操作经过安全闸门（vhass 虚拟环境重放验证）
- **DSL 优先**（省 token、可审计、可复用），raw flow 作为逃生舱
- **双层授权**：Agent 身份（API Key）+ 授权范围（Tab 授权码）
- **经验数据自动收集**，越用越聪明（实体共现、DSL 模式、错误知识库）
- **竞技场模式**（v2.0）：多 Agent 自由命题编写 flow，竞争锁定题目，积累经验数据

---

## 二、当前版本状态

### 2.1 版本历史

| 阶段 | 版本范围 | 状态 | 核心内容 |
|------|---------|------|---------|
| v1.5.x Pro 基础 | v1.5.0 ~ v1.5.9 | ✅ 已完成 | Pro 版 REST API、API Key、模板库、Token 统计 |
| v1.6.x 经验复用 | v1.6.0 ~ v1.6.4 | ✅ 已完成 | 经验收集、错误知识库、智能推荐 |
| v1.7.x 稳定版 | v1.7.0 ~ v1.7.1 | ✅ 已完成 | 经验复用稳定版、Token 统计页面修复 |
| v2.0.x 竞技场 | v2.0.0-alpha ~ v2.0.11-beta | 🔄 进行中 | 竞技场模式（自由作文优先） |

### 2.2 当前版本：v2.0.11-beta

**已完成功能：**
- 竞技场核心模块（arena.py，751 行）
- 3 个初始分区（书房/客厅/主卧室），各 8 个虚拟设备
- 三层题目审核（实体重叠度>60% / 文本相似度>85% / LLM 考官模糊区间 0.6-0.85）
- 创造力评分（新颖性40%+复杂度20%+实用性20%+描述质量20%）
- vhass 虚拟环境验收（不接触真实设备）
- 题目锁定机制（第一个验收通过的 Agent 锁定）
- 排行榜 + 全局统计
- WebUI 竞技场前端页面（分区卡片/题目列表/排行榜/设备池/提交弹窗）
- Agent Bearer API Key 接入（/api/arena/* 支持 Bearer 认证）
- skills/arena.md Agent 接入文档
- 首次访问引导弹窗
- **从真实 HA 同步设备**（v2.0.8+）：按区域/domain/关键词筛选，用户勾选同步，可移除
- 同步设备弹窗加宽（860px）+ 左 friendly_name 右 entity_id 布局

**待完成功能：**
- memory-agent 创造力对接（交接单已写，等待 ma 团队）
- NR1990 真实部署验证（NR1990 刚恢复备份，暂用 vhass）
- Phase 2 命题作文模式（每区积累 20 题后自动切换）
- 竞技场教程/引导完善
- CHANGELOG.md 更新（v1.6.x-v2.0.x 缺失）

---

## 三、代码结构

### 3.1 目录结构

```
E:\NAS\autoflow\
├── src\autoflow_gateway\          # 网关核心代码
│   ├── gateway.py                 # 主网关（8589 行，核心业务逻辑）
│   ├── webui.py                   # WebUI 路由（3413 行）
│   ├── arena.py                   # 竞技场模块（751 行，v2.0 新增）
│   ├── api_keys.py                # API Key 管理
│   ├── deploy_tokens.py           # 部署授权码
│   ├── dsl_engine.py              # DSL 编译器
│   ├── vhass.py                   # 虚拟 Home Assistant（579 行）
│   ├── proposals.py               # 提案/经验沉淀（671 行）
│   ├── experience.py              # 经验收集
│   ├── error_knowledge.py         # 错误知识库
│   ├── templates.py               # 模板库
│   ├── token_stats.py             # Token 统计
│   ├── task_store.py              # 任务池（849 行 SQLite，当前停用）
│   ├── tab_organizer.py           # Tab 组织（分级方案）
│   ├── ha_layer.py                # HA 连接层（HALayer 封装 HAClient）
│   ├── nr_layer.py                # NR 连接层
│   ├── llm_client.py              # LLM 客户端
│   ├── mcp_server.py              # MCP Server
│   ├── self_update.py             # 在线更新
│   ├── config.py                  # 配置
│   ├── state.py                   # 状态管理（device_catalog 等）
│   ├── lib\
│   │   ├── ha_client.py           # HA REST + WebSocket 客户端
│   │   └── nr_client.py           # NR Admin API 客户端（1700+ 行）
│   ├── webui\static\
│   │   ├── index.html             # WebUI 主页面
│   │   ├── app.js                 # WebUI 前端逻辑（5100+ 行）
│   │   └── style.css              # 样式
│   └── templates\                 # 模板文件
├── skills\
│   ├── autoflow.md                # 主 SKILL 文档（Agent 接入指南）
│   ├── arena.md                   # 竞技场 SKILL 文档（v2.0 新增）
│   └── prompt_library.md          # Prompt 库
├── docs\                          # 项目文档
│   ├── 00_overview\               # 架构与路线图
│   ├── 01_product\                # 产品文档
│   ├── 02_architecture\           # 架构设计
│   ├── 03_dev\                    # 开发计划
│   ├── 04_ideas\                  # 创意/设计（竞技场相关）
│   ├── 05_handoff\                # 交接文档
│   ├── 协作\                      # 双团队协作规范
│   └── 工单\                      # FFL 工单
├── data\                          # 运行时数据（不入库）
├── Dockerfile                     # Docker 构建
├── docker-compose.yml             # Docker 编排
├── pyproject.toml                 # Python 依赖（含 websockets>=12.0）
├── VERSION                        # 当前版本号（无 BOM UTF-8）
└── README.md
```

### 3.2 关键模块说明

| 模块 | 行数 | 职责 | 关键注意事项 |
|------|------|------|-------------|
| gateway.py | 8589 | 核心业务逻辑 | 最大文件，propose_dsl/部署/快照等核心方法 |
| webui.py | 3413 | WebUI 路由 + API | 路由注册在文件末尾 Route 列表 |
| arena.py | 751 | 竞技场 | v2.0 新增，独立模块，通过 gateway 引用 |
| vhass.py | 579 | 虚拟 HA | 支持从 device_catalog 克隆真实设备 |
| ha_client.py | ~300 | HA 客户端 | REST + WebSocket，entity_areas() 走 websocket |
| nr_client.py | 1700+ | NR 客户端 | Admin API 封装，Pro 版用 --gateway 模式 |

---

## 四、开发环境与部署

### 4.1 本地开发

```powershell
# 项目路径
cd E:\NAS\autoflow

# 语法检查
python -c "import ast; ast.parse(open('src/autoflow_gateway/xxx.py', encoding='utf-8').read())"
node --check src/autoflow_gateway/webui/static/app.js

# 本地运行（需要 HA/NR 连接）
python -m autoflow_gateway.webui
```

### 4.2 Git 提交规范

- **分支**：只有 `main`（master 已删除）
- **必须打 tag**：每个版本必须打 tag，否则在线更新无法获取新版本
- **VERSION 文件**：必须用**无 BOM UTF-8** 写入
  ```powershell
  # 正确方式（Write 工具或 Python）
  # 错误方式：[System.IO.File]::WriteAllText 可能不生效
  ```
- **提交信息**：约定式提交 `feat/fix/docs/release(scope): 描述`
- **SSH key**：~/.ssh/id_ed25519（workbuddy key），git 身份 user.name=lidicn
- **禁止入库**：outputs/、gen_r21_flows.py、scripts/ 下临时脚本、*.bak 文件

### 4.3 NAS 部署（关键！）

**NAS 完全无法访问 GitHub**，必须用 SCP 离线部署：

```powershell
# 1. 本地打包
cd E:\NAS\autoflow
git archive --format=tar.gz --output=C:\Users\lidicn\AppData\Local\Temp\autoflow_vXXX.tar.gz HEAD

# 2. SCP 到 NAS
scp C:\Users\lidicn\AppData\Local\Temp\autoflow_vXXX.tar.gz lidicn@192.168.2.200:/tmp/

# 3. SSH 解压并重启
ssh lidicn@192.168.2.200 "cd /vol1/1000/docker/autoflow && tar -xzf /tmp/autoflow_vXXX.tar.gz --overwrite && docker restart autoflow_gateway"

# 4. 验证
curl http://192.168.2.200:8000/api/core/version
```

**NAS 关键路径：**
- 网关路径：`/vol1/1000/docker/autoflow`（不是 /docker/autoflow）
- 网关容器：`autoflow_gateway`，端口 8000，host 网络
- Python 环境：`/app/.venv/bin/python`（容器内），websockets 已安装
- 数据目录：`/data/`（容器内），device_catalog 在 `/data/prod/state/device_catalog.json`
- NR1880 生产：容器 `node-red`，端口 1880
- NR1990 开发：容器 `node-red-dev`，端口 1990，数据路径 `/vol1/1000/docker/Node-RED-dev/flows.json`

**Node-RED flows 替换注意：** 必须先 `docker stop` 再替换文件再 `docker start`，否则重启时内存旧 flows 会覆盖文件。

### 4.4 在线更新机制

- 用户在 WebUI 点击"在线更新"
- 网关检查 GitHub latest tag，对比 VERSION
- 下载 tarball 解压重启
- **NAS 无法用此功能**（不通 GitHub），只能 SCP 离线部署

---

## 五、竞技场模式详解（v2.0 核心）

### 5.1 设计理念

**自由作文优先**：Agent 自己命题（如"电脑开机同步打开显示器挂灯"），编写 DSL Flow，经 vhass 虚拟环境验收通过后，第一个完成的 Agent 锁定该题目。积累到 20 题后进入 Phase 2 命题作文。

### 5.2 三层题目审核

1. **实体重叠度 >60%** → 直接判重（与已锁定题目对比）
2. **文本相似度 >0.85** → 直接判重（difflib SequenceMatcher）
3. **模糊区间 0.6-0.85** → LLM 考官仲裁（LLM 不可用时 fail-open）

### 5.3 创造力评分

| 维度 | 权重 | 说明 |
|------|------|------|
| 新颖性 | 40% | 与已有题目的差异度 |
| 复杂度 | 20% | 2-4个设备最优 |
| 实用性 | 20% | 设备是否在分区设备池中 |
| 描述质量 | 20% | 20-100字最优 |

低于 0.3 不通过。

### 5.4 数据存储

```
data/arena/
├── arenas.json          # 分区配置（含设备列表）
├── tasks.json           # 题目库
├── submissions.json     # 提交记录
├── {arena_id}_seed.json # vhass 设备种子
└── {arena_id}_state.json # vhass 运行状态
```

### 5.5 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/arena/arenas | 列出分区 |
| GET | /api/arena/arenas/{id} | 分区详情（含设备/题目） |
| GET | /api/arena/arenas/{id}/tasks | 题目列表 |
| POST | /api/arena/arenas/{id}/propose | 提交题目 |
| POST | /api/arena/arenas/{id}/submit | 提交 DSL 验收 |
| GET | /api/arena/arenas/{id}/leaderboard | 排行榜 |
| GET | /api/arena/stats | 全局统计 |
| GET | /api/arena/ha_areas | HA 区域列表（用于设备同步筛选） |
| GET | /api/arena/ha_devices | HA 设备列表（支持 area/domain/keyword 筛选） |
| POST | /api/arena/arenas/{id}/sync_devices | 同步选定设备到分区 |
| POST | /api/arena/arenas/{id}/remove_device | 从分区移除设备 |

### 5.6 设备同步机制（v2.0.8+）

- 从 HA websocket 注册表获取 entity_id → area_name 映射（`ha.client.entity_areas()`）
- **注意**：`gateway.ha` 是 HALayer 对象，没有 entity_areas 方法，必须用 `gateway.ha.client.entity_areas()`
- device_catalog 的 area 字段可能全空（refresh 失败时），不能依赖
- 用户可按区域/domain/关键词筛选，勾选后同步，可随时移除
- 同步后自动更新 vhass 种子并重置

---

## 六、双团队协作模式

### 6.1 团队分工

| 角色 | 负责人 | 职责 |
|------|--------|------|
| **项目负责人** | dw（交接后为新负责人） | 架构设计、新功能开发、版本发布、最终代码审查 |
| **FlowForge Labs (FFL)** | Hermes 团队 (PM+前端+后端+测试) | bug 修复、自动化测试、性能优化、安全加固、前端改进 |
| **测试验证** | wb2 | 回归测试、NAS 部署验证 |

### 6.2 仓库结构

- **主仓库**：https://github.com/lidicn/AutoFlow（稳定版，main 分支）
- **开发仓库**：https://github.com/lidicn/AutoFlow-dev（FFL 工作区）

### 6.3 协作流程

1. 主负责人在主仓库开发新功能，直接提交 main
2. 需要 FFL 帮忙时，在 `docs/工单/` 创建工单（用 TEMPLATE.md 模板）
3. FFL PM 认领，在 AutoFlow-dev 开发
4. FFL 完成后提交 PR 到主仓库
5. 主负责人审查 PR，通过后合并
6. wb2 回归测试，主负责人发布版本

### 6.4 工单规范

- 位置：`docs/工单/`
- 命名：`TICKET-YYYYMMDD-序号-简短标题.md`
- 模板：`docs/工单/TEMPLATE.md`
- FFL 边界：不擅自改架构、不直接合并 main、不发布版本

### 6.5 当前工单状态

- TICKET-001（分支迁移）：✅ 已完成
- TICKET-002（性能与前端安全修复回传主仓库）：🔄 FFL 已认领，进行中

---

## 七、关键技术决策与注意事项

### 7.1 认证体系

- **WebUI 管理面**：Session Cookie（用户名密码登录）
- **Agent API 面**：Bearer API Key（格式 `af_pro_<32位hex>`，存储 SHA-256 hash）
- **竞技场 API**：同时支持 WebUI Session 和 Bearer API Key
- **部署授权码**：Tab 级授权，Agent 持码可在指定 tab 部署/修改 flow
- **P0 历史 bug**：api_keys.py 过期校验曾 fail-open（except Exception: pass 静默吞掉 naive datetime 错误导致 API Key 永不过期），已在 v2.0.1-alpha 修复为 fail-closed

### 7.2 前端注意事项

- `modal(title, html)` 函数**只接受两个参数**，不支持 `{title, body, actions}` 对象格式
- `TABS` 数组必须包含所有 tab 名称，否则 `setTab()` 会重置为 dashboard
- app.js 5100+ 行，竞技场代码在末尾（loadArena 系列函数）
- 前端修改后用户需要**强刷浏览器**（Ctrl+F5）清除缓存

### 7.3 HA 连接注意事项

- `gateway.ha` 是 HALayer 对象，`gateway.ha.client` 才是 HAClient
- HAClient 的 `entity_areas()` / `get_areas()` 走 **WebSocket**，需要 websockets 库
- 容器内 Python 在 `/app/.venv/bin/python`，websockets 17.1 已安装
- device_catalog 的 area 字段不可靠，用 `ha.client.entity_areas()` 获取区域映射

### 7.4 VERSION 文件

- 必须用**无 BOM UTF-8** 写入
- PowerShell 的 `[System.IO.File]::WriteAllText` 可能不生效，用 Write 工具或 Python
- 版本号变更必须同时：改 VERSION → git commit → git tag → git push → SCP 部署

### 7.5 已知技术债

- `gateway.py` 8589 行过大，建议拆分
- `webui.py` 3413 行，路由和业务逻辑混在一起
- `app.js` 5100+ 行，无模块化
- CHANGELOG.md 自 v1.5.9 后未更新
- task_store.py（849 行 SQLite 任务池）已停用但代码保留
- 多个 .bak 文件未清理（gateway.py.bak.*, webui.py.bak.*）

---

## 八、测试与验证

### 8.1 开发节奏

```
开发迭代（2-3个小版本）→ 集中测试（wb2）→ 修复发布（稳定版）→ 下一阶段
```

- 每个版本提交前做语法检查 + 冒烟测试（curl 验证核心接口）
- 每 2-3 个开发版本后集中开工单给 wb2
- P0/P1 bug 立即修复
- 测试工单路径：`\\100.112.138.64\share\AutoFlowTestv2\tests`

### 8.2 wb2 测试环境

- Tailscale 网关：https://fn7t314d3.ts.net:8000
- 测试工单放共享目录，wb2 完成后返回 TEST_RESULT_*.md

### 8.3 冒烟测试清单

```bash
# 版本验证
curl http://192.168.2.200:8000/api/core/version

# 竞技场分区列表
curl -b "session=xxx" http://192.168.2.200:8000/api/arena/arenas

# 竞技场区域列表（设备同步用）
curl -b "session=xxx" http://192.168.2.200:8000/api/arena/ha_areas

# WebUI 页面加载
curl -I http://192.168.2.200:8000/
```

---

## 九、外部依赖

### 9.1 memory-agent

- 运行在 192.168.2.200:8086
- 项目目录：E:\NAS\memory-agent
- 竞技场对接交接单已写入：`E:\NAS\memory-agent\docs\交接单_AutoFlow竞技场对接.md`
- 4 个任务：get_arena_inspiration (P0)、evaluate_creativity (P0)、record_arena_result (P1)、数据快照 (P1)
- 状态：等待 memory-agent 团队评估

### 9.2 Home Assistant

- 地址：http://192.168.2.200:8123
- Long-Lived Token 配置在网关环境变量
- 2883 个设备，15 个区域

### 9.3 Node-RED

- NR1880 生产：http://192.168.2.200:1880，33 tab/1061 节点
- NR1990 开发：http://192.168.2.200:1990，35 tab/1282 节点（已从备份恢复）
- NR1990 空竞技场备份：`/vol1/1000/docker/Node-RED-dev/flows.json.arena-empty-backup`

---

## 十、下一步建议

### 10.1 短期（v2.0.x beta 收尾）

1. 竞技场设备同步功能 wb2 测试验证
2. 修复测试发现的问题
3. 更新 CHANGELOG.md
4. 清理 .bak 文件和临时脚本

### 10.2 中期（v2.0 正式版）

1. memory-agent 创造力对接（等 ma 团队）
2. NR1990 真实部署验证（需先迁移生产 tab 到 NR1880）
3. Phase 2 命题作文模式
4. 竞技场教程系统

### 10.3 长期（v2.1+）

1. 竞技场经验数据回流到经验复用系统
2. DSL bug 库迭代（从竞技场失败案例中学习）
3. 优质 flow 模板自动生成
4. 代码重构（gateway.py / webui.py / app.js 拆分）

---

## 十一、联系人

| 角色 | 联系方式 | 说明 |
|------|---------|------|
| 项目所有者 | lidicn（用户） | 最终决策、产品方向 |
| wb1 | 原开发者 | 了解项目历史，可咨询 |
| wb2 | 远端测试者 | 测试工单，共享目录 |
| FFL PM | Hermes 团队 | 工单协作，AutoFlow-dev 仓库 |

---

> **交接确认**：本文档覆盖项目架构、代码结构、部署流程、竞技场设计、协作模式、注意事项。接手人如有疑问，优先查阅 docs/ 下相关设计文档，或咨询 wb1 了解历史背景。
