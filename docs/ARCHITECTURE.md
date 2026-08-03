# AutoFlow Gateway — 架构与设计说明（开发者向）

> 面向用户的上手文档见根目录 [README.md](../README.md)。
> 本文是原 README 的技术部分归档，读者假定为要改网关代码或做二次开发的人。

AutoFlow 的**中央网关**：agent 唯一接触面。独占 HA/NR 凭证，是所有动作的唯一执行点，
集中落地安全策略。agent 永不直连 HA/NR，可随时更换（WorkBuddy / DeepSeek++ / Trac Solo）。

## 设计要点

- **L0 驱动复用（已 vendoring）**：`ha_client.py` / `nr_client.py` 已复制到
  `src/autoflow_gateway/lib/`，与包一同分发，**不依赖 skills 目录存在**（本地与容器自洽）。
  加载顺序：vendored lib → `HA_CLIENT_PATH`/`NR_CLIENT_PATH` 环境变量 → skills 目录。
- **共享态（L1）**：`device_catalog` / `flow_catalog` / `entity_mapping` / `intent_log`，
  存于 `data/<env>/state/`。
- **环境分级**：`staging` / `prod`（仅用于 data/ 子目录隔离；NR 默认 1880，连真实设备即 prod）。
- **防御层**：结构上不提供 replace-all / delete-all；爆炸半径上限；受保护流；所有权隔离；
  高危域升级确认。
- **确认闸**：所有写操作进待确认队列，人工批准后才落地（零信任）。
- **双接口**：MCP（Streamable HTTP `/mcp` 主，SSE 备用）+ 脚本/JSON 兜底。
- **MCP 身份层**：连 MCP 必须 `Authorization: Bearer <身份码>`，匿名直接拒。
  每个 agent 在 WebUI 生成身份码 + 独立存档；写操作强制用已认证身份，不可伪造。
- **WebUI 控制面（响应式）**：手机/平板/电脑自适应，承载 ①确认闸(approve/reject)
  ②agent 身份管理(生成码/重置/吊销) ③提案治理(agent 提 skill→人审→升格公用) ④用户笔记。
  关键原则：**批准/升格只在 WebUI，MCP 不暴露**，杜绝 agent 自己批准自己。

## 三端点能力分层

| 端点 | 能力 | 面向 |
|------|------|------|
| `/mcp` | 编译器路径。只能写 DSL，网关编译 + 虚拟 HA 重放自证。禁 function 节点、禁部署刀 | 默认用户面 agent |
| `/mcp-white` | 编译器 + 原生手写路径。可直写 flow JSON（允许白名单内原生节点），落提案待人审 | 进阶/开发 agent |
| `/mcp-admin` | 上述全集 + 运维/测试杠杆（重启网关、任务池管理等） | 管理员 |

历史术语对照：「黑箱」= 编译器路径，「白箱」= 原生手写路径。

## 目录结构

```
autoflow_gateway/
  src/autoflow_gateway/   网关包
    config.py             配置（env 优先）
    state.py              共享态（原子 JSON 持久化）
    schemas.py            SceneIntent 契约 + 校验
    defense.py            防御层
    confirm.py            人工确认闸
    ha_layer.py           HA 读 + 受控写
    nr_layer.py           NR 安全写（无 replace-all）
    build_scene.py        意图→NR 流（幂等）
    dsl_engine.py         DSL 编译器（意图 → NR 节点 + 静态校验）
    api_specs.py          API 能力 spec 加载器（数据源 data/api_specs.json）
    subflows.py           预置子流程 spec 加载器（数据源 data/subflows/subflows.json）
    gateway.py            编排核心
    cli.py                脚本/JSON 兜底接口
    mcp_server.py         MCP 服务（Streamable HTTP / SSE）+ 身份鉴权中间件
    identity.py           MCP 身份层：agent 身份码/存档/拒绝匿名
    proposals.py          提案/经验沉淀（raw→candidate→public，升格落盘公用 skill）
    notes.py              用户笔记（智能家居想法，长期存档）
    webui.py              WebUI 后端（/api + 静态首页，可选 token 闸门）
    webui/static/         响应式前端（手机/平板/电脑自适应）
    vhass.py              虚拟 HA（数字孪生 / staging 数据源，纯标准库）
    mock_docker_api.py    staging 非实体能力模拟（Docker/业务 API）
    lib/                  vendored HA/NR 客户端（ha_client.py / nr_client.py）
    data/                 运行时数据（api_specs.json / subflows/ 子流程定义 + nr_defs）
  tests/                  单元测试（含 test_identity / test_webui）
  examples/               示例意图
  pyproject.toml / Dockerfile / docker-compose.yml / .env.example
```

## 开发机运行（不触真实设备）

**方式 A — 可编辑安装（推荐，一次）：**

```bash
PY=<任意 Python 3.12+>
cd autoflow_gateway
$PY -m pip install -e .
autoflow config                                  # 控制台命令，随处可用
autoflow propose --file examples/scene_home_arrive.json
python -m autoflow_gateway.mcp_server            # 启 MCP（Streamable HTTP :8000/mcp）
python -m autoflow_gateway.mcp_server --transport sse
```

**方式 B — 零摩擦启动器（无需安装）：**

```bash
cd autoflow_gateway
$PY -m pip install mcp pydantic uvicorn starlette python-dotenv   # 仅首次
python run.py cli discover --keyword 客厅        # 脚本接口
python run.py mcp                                # 启 MCP 服务（强制身份，但无 WebUI）
python run.py serve                              # 启 MCP(/mcp) + WebUI(/) 同端口 :8000
```

> 凭证：复制 `.env.example` 为 `.env` 填写，网关启动自动 `load_dotenv()`；
> 或设环境变量 `HASS_TOKEN`/`NR_PASS`/`HASS_SERVER`/`NR_URL` 等。
> 注意：WebUI「连接设置」里填过的值优先级高于 `.env`。

## 测试

```bash
$PY -m pytest tests/ -q               # 全量
$PY tests/test_gateway.py             # 网关核心单测（mock 后端，零设备接触）
$PY tests/test_vhass.py               # 虚拟 HA 端点/服务变更/合成触发/种子生成
$PY tests/test_staging.py             # staging 集成：gateway 指向 vhass 全链路验证
```

## 虚拟孪生 staging

让 agent 在**不碰真实设备**的前提下迭代 flow：用 `vhass` 镜像真实 HA 的 entity_id/区域/状态，
NR 指向它即可安全练手；Docker/业务 API 用 `mock_docker_api` 模拟。

**① 从真实 catalog 生成种子 + 镜像进 staging（一次性）**

```bash
autoflow seed-vhass --mirror --src data/prod/state/device_catalog.json --seed-out data/vhass_seed.json
# 仅生成种子（不镜像）：
autoflow seed-vhass --src data/prod/state/device_catalog.json --seed-out data/vhass_seed.json
```

**② 运行虚拟 HA（staging 数据源）**

```bash
autoflow vhass --port 8124 --seed data/vhass_seed.json
# 合成触发（模拟现实事件）：POST /api/trigger {"entity_id":"device_tracker.me","state":"home"}
```

**③ 运行 mock 非实体 API**

```bash
autoflow mock-api --port 9100 --registry data/mock_api_registry.json
# 发现可用 API：GET /api/registry
```

**④ staging 网关指向 vhass**：把 `HASS_SERVER` 设为 `http://vhass:8124`（容器内）或
`http://127.0.0.1:8124`（本机）。此后 agent 经网关 discovery/commit 都落在虚拟环境，
`refresh_catalog` 走 `GET /api/areas` 兜底（无需 hass-cli 即可带区域）。

> **已知缺口**：NR 若经 HA websocket 订阅状态变化，需 vhass 支持 HA websocket 协议
> （当前 vhass 仅实现 REST）。验证闭环推荐做法：staging flow 触发器用 NR `inject` 节点
> （手动/网关注入），不依赖实时事件流；或用网关 `POST /api/trigger` 注入后断言 vhass 状态。

## 容器部署细节

见 [DEPLOY.md](../DEPLOY.md)。要点：

- 镜像基于 `ghcr.io/astral-sh/uv:latest`，容器内用 `uv` 装包，宿主机无需预装 Python。
- `docker-compose.yml` 把 `./src`、`./skills`、`./data` 挂进容器 —— 改源码后
  `docker compose restart` 即生效（配合 Dockerfile 的 `-e` 可编辑安装）。
- 容器内访问宿主 HA 用 `http://host.docker.internal:8123`，**不要写 `localhost`**。
- `docker-compose.yml` 默认 `AUTOFLLOW_ENV=prod` → 所有 NR 写操作需 `allow_prod=True`。

## 已知约束

- 网关是 agent 唯一通路；网关挂了所有 agent 停。`restart: unless-stopped` 会自拉起。
- 写操作一律进人工确认闸；`staging` 可放宽（`AF_AUTO_APPROVE=true`）便于快速迭代，
  `prod` 必须人工。
- `AF_MCP_HOST` 默认 `127.0.0.1`（安全默认）。容器 compose 已设 `0.0.0.0`；
  裸机部署要局域网访问需显式改。暴露前请评估：这是能控制物理设备的网关。

## 后续 phase

- P3 验证闭环（合成触发 + 断言；staging inject 节点 + vhass 状态断言）
- P4 准入基准（参考真值归因）
- P6 加固（失败标签 telemetry / 熔断 / 行为级回滚）
