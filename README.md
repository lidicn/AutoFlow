# AutoFlow Gateway（网关核心 MVP）

AutoFlow 的**中央网关**：agent 唯一接触面。独占 HA/NR 凭证，是所有动作的唯一执行点，
集中落地安全策略。agent 永不直连 HA/NR，可随时更换（WorkBuddy / DeepSeek++ / Trac Solo）。

## 设计要点
- **L0 驱动复用（已 vendoring）**：`ha_client.py` / `nr_client.py` 已复制到 `src/autoflow_gateway/lib/`，与包一同分发，**不依赖 skills 目录存在**（本地与容器自洽）。加载顺序：vendored lib → `HA_CLIENT_PATH`/`NR_CLIENT_PATH` 环境变量 → skills 目录。
- **共享态（L1）**：`device_catalog` / `flow_catalog` / `entity_mapping` / `intent_log`，存于 `data/<env>/state/`。
- **环境分级**：`staging` / `prod`（仅用于 data/ 子目录隔离；NR 默认 1880，连真实设备即 prod）。
- **防御层**：结构上不提供 replace-all / delete-all；爆炸半径上限；受保护流；所有权隔离；高危域升级确认。
- **确认闸**：所有写操作进待确认队列，人工批准后才落地（零信任）。
- **双接口**：MCP（Streamable HTTP `/mcp` 主，SSE 备用）+ 脚本/JSON 兜底。
- **MCP 身份层（已实现）**：连 MCP 必须 `Authorization: Bearer <身份码>`，匿名直接拒。
  每个 agent 在 WebUI 生成身份码 + 独立存档；写操作强制用已认证身份，不可伪造。
- **WebUI 控制面（已实现，响应式）**：手机/平板/电脑自适应，承载 ①确认闸(approve/reject)
  ②agent 身份管理(生成码/重置/吊销) ③提案治理(agent 提 skill→人审→升格公用) ④用户笔记(智能家居想法)。
  关键原则：**批准/升格只在 WebUI，MCP 不暴露**，杜绝 agent 自己批准自己。

## 目录
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
  tests/                  单元测试（含 test_identity / test_webui）
  examples/               示例意图
  pyproject.toml / Dockerfile / docker-compose.yml / .env.example
```

## 一键安装（Docker，推荐）

一句话拉起完整网关（MCP + WebUI 同端口 `:8000`）：

```bash
curl -fsSL https://raw.githubusercontent.com/lidicn/autoflow-gateway/main/install.sh | sh
```

脚本会自动：检查/安装 Docker → 拉取仓库构建上下文 → `docker compose up -d --build` → 等待 WebUI 就绪。
安装目录默认 `/opt/autoflow`（macOS `~/autoflow`），数据持久化在 `<DIR>/data`，连接配置写 `<DIR>/.env`。

启动后浏览器打开 `http://<本机IP>:8000`：

1. **设置 → 连接配置**：填写 HA / Node-RED 地址与令牌（也可直接在 `.env` 里写）。
2. **Agents 面板**：给 agent 生成身份码（MCP 强制鉴权，匿名被拒）。

更新：`sh install.sh --update`（保留 `data/` 与 `.env`）。本机无 Docker 时加 `--install-docker` 可由脚本代为安装（仅 Linux）。

## 快速开始（开发机，不触真实设备）

**方式 A — 可编辑安装（推荐，一次）：**
```bash
PY=受管python路径   # 如 /path/to/python（任意 Python 3.12+）
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
> 凭证：复制 `.env.example` 为 `.env` 填写，网关启动自动 `load_dotenv()`；或设环境变量 `HASS_TOKEN`/`NR_PASS`/`HASS_SERVER`/`NR_URL` 等。

## 测试
```bash
$PY tests/test_gateway.py    # 网关核心单测（mock 后端，零设备接触）
$PY tests/test_vhass.py      # 虚拟 HA 端点/服务变更/合成触发/种子生成
$PY tests/test_staging.py    # staging 集成：gateway 指向 vhass 全链路验证
```

## P2 虚拟孪生 staging（已完成）

让 agent 在**不碰真实设备**的前提下迭代 flow：用 `vhass` 镜像真实 HA 的 entity_id/区域/状态，
NR 指向它即可安全练手；Docker/业务 API 用 `mock_docker_api` 模拟。

**① 从真实 catalog 生成种子 + 镜像进 staging（一次性）**
```bash
# 把 prod catalog（含区域）镜像进 staging catalog，并生成 vhass 种子
autoflow seed-vhass --mirror --src data/prod/state/device_catalog.json --seed-out data/vhass_seed.json
# 仅生成种子（不镜像）：
autoflow seed-vhass --src data/prod/state/device_catalog.json --seed-out data/vhass_seed.json
```

**② 运行虚拟 HA（staging 数据源）**
```bash
autoflow vhass --port 8124 --seed data/vhass_seed.json
# 或：python -m autoflow_gateway.vhass --port 8124 --seed data/vhass_seed.json
# 合成触发（模拟现实事件，供 P3 验证）：
#   POST /api/trigger {"entity_id":"device_tracker.me","state":"home"}
```

**③ 运行 mock 非实体 API**
```bash
autoflow mock-api --port 9100 --registry data/mock_api_registry.json
# 发现可用 API：GET /api/registry
```

**④ staging 网关指向 vhass**：把 `HASS_SERVER` 设为 `http://vhass:8124`（容器内）或
`http://127.0.0.1:8124`（本机）。此后 agent 经网关 discovery/commit 都落在虚拟环境，
`refresh_catalog` 走 `GET /api/areas` 兜底（无需 hass-cli 即可带区域）。

> **已知缺口（P3 解决）**：NR 若经 HA websocket 订阅状态变化，需 vhass 支持 HA websocket
> 协议（当前 vhass 仅实现 REST）。P3 验证闭环的推荐做法：staging flow 触发器用 NR `inject` 节点
> （手动/网关注入），不依赖实时事件流；或用网关 `POST /api/trigger` 注入后断言 vhass 状态。

## 部署到 NAS（飞牛 OS 无 Python → 容器化）
详见 **[DEPLOY.md](DEPLOY.md)**。要点：把 `autoflow_gateway/` 整目录拷到 `//<NAS_IP>/docker/autoflow_gateway/`，NAS 上 `docker compose up -d --build`。镜像基于 `uv` 基础镜像在容器内装包，无需宿主机有 Python。客户端已 vendoring 进镜像，`/mcp` 端点对齐 DeepSeek++ 已验证的 Streamable HTTP。compose 已包含 `vhass` 与 `mock_api` 服务，staging 网关默认指向 `http://vhass:8124`。

## 下一步（后续 phase）
- P3 验证闭环（合成触发 + 断言；staging inject 节点 + vhass 状态断言）
- P4 准入基准（参考真值归因）
- P6 加固（失败标签 telemetry / 熔断 / 行为级回滚）

---

## 身份层 + WebUI 控制面（已实现）

### 1. 给 agent 发身份码（MCP 连接前提）
任何 agent 连 `http://<host>:8000/mcp` 都必须在 `Authorization: Bearer <身份码>` 携带身份码，
否则被 401 拒绝。匿名 agent 无法连入——这是「可归因 / 每 agent 独立存档」的强制底座。

在 WebUI（浏览器开 `http://<host>:8000/`）的 **Agents** 面板：
- 输入用户名（如 `deepseek++`），点「创建」→ 生成明文身份码（**仅显示一次**，立即复制）。
- 把该码填入 agent 的 MCP 配置里 `Authorization: Bearer <码>`。
- 支持「重置码」（旧码失效）与「吊销」（agent 立即失去连接资格）。
- 或用无头命令：`autoflow agents-create --name deepseek++ --tier staging`。

agent 经 MCP 提交场景/提案时，`agent_id` 由网关从身份码自动注入，**agent 无法伪造他人身份**。

### 2. 确认闸（WebUI 审核 agent 的写操作）
agent 的 `commit` / HA 服务调用只会进「待确认队列」。在 **Pending** 面板人工 approve/reject；
MCP 不暴露 approve/reject，agent 不能自己批准自己。

### 3. 提案治理（经验复利）
agent 通过 MCP `autoflow_submit_proposal` 提交经验 skill / 约定修正 / 缺陷修复建议（落为 `raw`）。
在 **Proposals** 面板审核：升格 `raw → candidate → public`。到 `public` 时内容自动落盘为
`data/<env>/experience/public/<slug>.md` 公用 skill，反哺网关、可被多 agent 复用。

### 4. 笔记（你的智能家居想法）
**Notes** 面板是你的私人思考区：记录那些「想到了但暂不落地」的智能家居系统想法，可贴标签、
可搜索。与提案不同，笔记不进入升格流程，仅作长期存档。

> 可选加固：设环境变量 `AF_WEBUI_TOKEN=xxx` 后，WebUI 的 `/api` 也需 `?token=xxx` 或
> `Authorization: Bearer xxx` 才能访问（默认仅本机开放并打告警）。

### 关键安全边界
| 动作 | 入口 | 谁能动 |
|------|------|--------|
| 连 MCP / 提交场景 / 提交提案 | MCP（带身份码） | agent |
| 批准部署 / 升格提案 / 管理身份 | WebUI（人） | 人类 |
| 自己批准自己 | — | **永不允许**（MCP 不暴露 approve） |
