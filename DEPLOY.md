# AutoFlow Gateway — 本地运行与容器部署

## 一、本地运行（开发 / 调试）

网关是纯 Python，需 Python 3.12+。

### 方式 A：可编辑安装（推荐）
```bash
cd <REPO_DIR>
python -m pip install -e .
```
装好后随处可用：
```bash
autoflow config                       # 查看当前配置（不含凭证明文）
autoflow discover --keyword 客厅 --limit 20
autoflow propose --file examples/scene_home_arrive.json
autoflow commit  --file examples/scene_home_arrive.json
autoflow pending
autoflow approve --id op_xxx

python -m autoflow_gateway.mcp_server        # 启动 MCP（Streamable HTTP :8000/mcp）
python -m autoflow_gateway.mcp_server --transport sse   # SSE :8000/sse
```

### 方式 B：零摩擦启动（无需安装，直接跑）
```bash
cd <REPO_DIR>
python -m pip install mcp pydantic uvicorn starlette python-dotenv   # 仅首次装依赖
python run.py mcp                                   # 启 MCP 服务
python run.py cli discover --keyword 客厅           # 走脚本接口
```

### 配置（凭证只在网关内，agent 不可见）
复制 `.env.example` 为 `.env` 并填值，网关启动时自动 `load_dotenv()`：
```bash
cp .env.example .env
# 编辑 .env：HASS_TOKEN / NR_PASS / HASS_SERVER / NR_URL 等
```
或用环境变量：`AUTOFLLOW_ENV=staging|prod`、`HASS_SERVER`、`HASS_TOKEN`、`NR_URL`、`NR_USER`、`NR_PASS`、`AF_MCP_PORT` 等。

- `staging`（默认）：数据落在 `data/staging/`，适合先在虚拟/测试 HA 上练手。
- `prod`：数据落在 `data/prod/`，连真实设备；写操作默认需人工确认（WebUI 连接设置 / `AF_AUTO_APPROVE`）。
- NR 默认 `http://localhost:1880`（可用 `NR_URL` 覆盖指向你的 Node-RED 实例）。

### 本地验证（不触真实设备）
```bash
cd <REPO_DIR>
python -m pytest tests/test_gateway.py     # 单测（mock 后端，零设备接触）
```

---

## 二、容器化部署（Docker / NAS）

镜像基于 `ghcr.io/astral-sh/uv:latest`，在容器内用 `uv` 装包，**宿主机无需预装 Python**。

### 步骤
1. 把整个仓库目录拷到部署机（保持 Dockerfile / docker-compose.yml / src / pyproject.toml 在一起）：
   ```
   <DEPLOY_DIR>/autoflow-gateway/
   ```
2. 在部署机建 `.env`（含 HASS_TOKEN / NR_PASS 等），并建持久卷目录：
   ```
   <DEPLOY_DIR>/autoflow-gateway/data/
   ```
3. 启动：
   ```bash
   cd <DEPLOY_DIR>/autoflow-gateway
   docker compose up -d --build
   ```
4. 验证 MCP 端点：
   ```bash
   curl -i http://<HOST>:8000/mcp -X POST \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
   ```

### 给 agent 的配置（DeepSeek++ 等）
- 传输：**Streamable HTTP**
- URL：`http://<HOST>:8000/mcp`
- （仅支持 SSE 的客户端用 `http://<HOST>:8000/sse`）

### 持久化
- `./data:/data`：device_catalog / flow_catalog / entity_mapping / intent_log / 待确认 / 备份，容器重启不丢。
- `AUTOFLLOW_ENV` 控制数据落在 staging 还是 prod 子目录。

### 升级
改代码后：`docker compose up -d --build` 重新构建即可。客户端 `ha_client.py` / `nr_client.py` 已 vendoring 进 `src/autoflow_gateway/lib/`，与镜像一同更新。

---

## 三、已知约束
- 网关是 agent 唯一通路；网关挂了所有 agent 停。`restart: unless-stopped` 会自拉起。
- 写操作一律进人工确认闸；`staging` 可放宽（`AF_AUTO_APPROVE=true`）便于快速迭代，`prod` 必须人工。
- 容器内访问宿主 HA 用 `http://host.docker.internal:8123`，不要写 `localhost`。
