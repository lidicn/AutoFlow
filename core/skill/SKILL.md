---
name: autoflow-core
description: AutoFlow Core / Pro — 让 agent 安全编写/修改/验证 Node-RED flow。支持两种模式：直连 NR（Core）和连网关（Pro，推荐，省 token + 安全闸门）。nr_client.py 唯一写入通道。
---

# AutoFlow Core / Pro（核心版 / 专业版）

你（agent）通过本 skill 获得对 Node-RED 实例的安全编程能力：读取、编写、修改、验证 flow。
所有写入经 `scripts/nr_client.py`（纯标准库，无 pip 依赖），自带护栏：写前快照、
结构 lint、节点数熔断、prod 闸、操作日志、回读校验。

## ⭐ 两种模式（优先用 Pro 网关模式）

| 模式 | 命令前缀 | 特点 | 推荐场景 |
|------|---------|------|---------|
| **Pro（网关模式）** ★推荐 | `nr_client.py --gateway ...` | DSL 优先，省 5-10 倍 token，有编译闸门和快照，对话即部署 | 99% 日常场景 |
| Core（直连模式） | `nr_client.py ...` | 直连 NR 写 JSON，无网关，无编译校验 | 离线/网关不可用/极端定制 |

**默认用 Pro 网关模式。** 只有网关不可用或 DSL 确实表达不了时才用 Core 直连模式。

## 🚀 Pro 网关模式（首选）

### 配置

配置文件 `~/.autoflow-core/config.json` 增加：
```json
{
  "gateway_url": "http://<gateway-host>:8000",
  "api_key": "af_pro_xxxxxx",
  "agent_id": "my-agent"
}
```

或环境变量：`AF_GATEWAY_URL`、`AF_API_KEY`、`AF_AGENT_ID`。

### 标准流程（Pro 模式）

```
1. --gateway doctor                    # 自检：网关连通性
2. --gateway resolve-entity "书房灯"   # 获取真实 entity_id（写 DSL 前必调）
3. --gateway propose-dsl "触发: 书房人体传感器 on\n动作: light.turn_on(书房灯)"  # ★首选
4. 成功 → 自动部署（授权范围内）
5. 失败 → 看编译错误，改 DSL，重跑步骤 3（上限 3 轮）
6. 不满意 → --gateway snapshots 查看快照，--gateway rollback <id> 回滚
```

### Pro 模式命令

```bash
# ★首选：提交 DSL，网关编译+闸门校验，自动部署
nr_client.py --gateway propose-dsl "触发: 传感器 on\n动作: light.turn_on(灯)"
nr_client.py --gateway propose-dsl --dsl-file flow.dsl   # 从文件读 DSL
nr_client.py --gateway propose-dsl "..." --preview        # 仅生成提案，不部署

# ⚠️逃生舱：直接提交 raw JSON（仅在 DSL 表达不了时使用）
nr_client.py --gateway deploy-raw --flow-file flow.json

# 实体查询
nr_client.py --gateway entities --domain light --area 书房
nr_client.py --gateway resolve-entity "书房灯"

# 快照与回滚
nr_client.py --gateway snapshots
nr_client.py --gateway rollback <snapshot_id>

# 自检
nr_client.py --gateway doctor
nr_client.py --gateway version
```

### DSL 语法速查

```
场景: <名称>
触发: <entity_id> <状态>    # 如 binary_sensor.motion on
条件: <entity_id> <状态>    # 可选，多条件用 AND
动作: <service>(<entity_id>)  # 如 light.turn_on(light.lamp)
动作: <service>(<entity_id>, brightness=80)
延时: <秒数>秒
分支: <条件> → <动作>
否则: <动作>
并行: <动作1> | <动作2>
调用子流程: <子流程名>(参数=值)
```

**写 DSL 前必须先调 `resolve-entity` 获取真实 entity_id，禁止凭记忆编造。**

## 🚨 黄金法则（违反任何一条立即停止）

1. **`af_*` 前缀 = 你的所有权**（代码层硬拦截）。新建 tab/flow 一律 `af_<场景名>` 命名。
2. **用户手工流只读——硬拦截，不靠自觉**。`write-flow` / `create_tab` 对不带 `af_` 前缀的
   目标**默认抛 `NRGuardError` 拒绝**；确需改动用户流必须显式加 `--allow-user-flow`
   （仍会照常快照留底）。归属以**线上 label 为准**，伪造前缀绕不过。
3. **写前必快照，写后必回读**。用 `write-flow`（内置快照+回读校验），不要手工拼 PUT。
4. **prod 默认禁写**。URL 含 `:1880` 或用户明确说是生产实例 → 需用户显式同意才可 `--allow-prod`。
5. **禁止整体替换**。任何"把整个 flows 数组 PUT 回去"的操作都是禁区（会删掉未列出的节点）。
   单 flow 更新只走 `write-flow`。
6. **不确定就问**。实体 ID、设备语义拿不准时问用户，不要猜。

## 配置（~/.autoflow-core/config.json）

```json
{"url": "http://<host>:<port>", "username": "...", "password": "..."}
```

优先级：环境变量 NR_URL/NR_USER/NR_PASS > 配置文件 > 函数参数。
**推荐用环境变量**（配置文件是明文密码文件，仅限本机、勿入版本库/勿外发）。
可选（verify 的 HA 断言用）：`HASS_SERVER` + `HASS_TOKEN` 环境变量。

连接排障：若报 `Client sent an HTTP request to an HTTPS server` → 端点其实是 HTTPS，
把 url 改 `https://`；若报证书错误 → 用带有效证书的域名（如 Tailscale 的 `*.ts.net`），
不要用裸 IP。

## 标准写入流程（每次写 flow 都走这七步）

```
1. inventory                     # 看现状：哪些 tab、归属谁、可写性
2. get <tab-id> --compact        # 读参考 flow（省 token；学习用户节点的写法）
3. 写 JSON（见节点构建约定）        # z 必须指向真实 tab id；新 tab 用 af_ 前缀
4. write-flow <id> --file f.json --dry-run   # 预览
5. write-flow <id> --file f.json             # 快照→PUT→回读校验一体
6. inject-read <inject-node-id>  # 自愈闭环：触发+回读 context 捕获
7. 不符 → 改 → 重跑 4-6（上限 3 轮，仍失败则停下报告用户，附快照路径）
```

## 自愈闭环（inject → 回读 → apply）

被测 flow 在**验证点**接一个 function 节点，把结果写到 context：

```js
global.set("af_dbg", {ok: true, state: msg.payload});  // 结构自定，但要能断言
return msg;
```

然后：

```bash
python scripts/nr_client.py inject-read <inject节点id> --key af_dbg --timeout 10
```

- 捕获到值 → 与期望比对 → 一致即通过；不一致 → 分析原因 → 修改 → 重验。
- 超时 None → flow 没跑到验证点（检查触发条件/连线/节点使能）。
- 命令会先清旧值再触发，不怕读到上一轮残留。

## 命令速查

```bash
python scripts/nr_client.py doctor                     # 安装后自检（验收标准：全绿）
python scripts/nr_client.py inventory                  # 全 tab 只读概览
python scripts/nr_client.py get <id> --compact         # 读 flow（去坐标省 token）
python scripts/nr_client.py search <keyword>           # 全局搜节点
python scripts/nr_client.py write-flow <id> -f f.json [--dry-run]
python scripts/nr_client.py inject-read <inject-id> [--key af_dbg]
python scripts/nr_client.py lint f.json                # 离线结构校验
python scripts/nr_client.py verify <flow-id> --yes     # 端到端（含可选 HA 断言）
```

## 节点构建约定

- 新节点 id 用 `uuid4()`；`z` = 所属 tab id（**新建 tab 时 NR 的 `POST /flow` 会忽略
  你给的 id，必须用响应返回的真实 id**，后续节点 z 都用它）。
- HA 节点（server-state-changed / api-call-service / api-current-state 等）需要实例上
  已有的 server config 节点：先 `search server` 找到现成的 config 节点 id 复用。
- 完整构建器 API（v6 schema 强制）：`from nr_client import NodeRedClient` 后调
  `build_inject / build_function / build_debug / build_api_current_state /
  build_server_state_changed / build_subflow_entries` 等。

## 已知坑（实测，别再踩）

- **1880 与 1990 是两个独立实例**，不共享 flows；改动不会互相同步。
- **`POST /flow` 忽略 body 里的 id**，自分配真实 id 返回在响应里；"先 get 探测再建"
  的写法恒 404，会导致重复建 tab。正解：POST 后立刻用响应 id 建本地台账。
- 1990 实例对 `POST /flows` 全量部署返回 200 但**不生效**；单 flow 更新必须
  `PUT /flow/:id`（write-flow 已封装）。
- 测试替身的 `get_flow` 未命中必须抛错（模拟 404），返回空壳会掩盖 bug。

## 红线声明（给用户看的安全承诺）

- **用户手工 flow 只读是硬拦截**：不带 `af_` 前缀的目标，`write-flow` / `create_tab` 默认直接拒绝，
  想误伤都做不到（外加 inventory 归属标注 + 节点数熔断三层保险）。
- 每次写入前自动全量快照，出事可 `restore_snapshot` 回滚。
- 所有写入操作记录在 `~/.autoflow-core/logs/nr_operations.log`。

## ⚠️ 回滚须知（v1.0.1 起）

- `restore_snapshot` = **整实例还原**（内部走 `POST /flows` 全量重部署），不是单 flow 回滚。
- 它不会只回滚某一条 flow：快照之后新建的 tab 会**被删除**，除非你传 `allow_partial=False`
  让护栏先把这次还原拦下（默认即如此，遇子集快照会拒绝）。
- 只想撤销单条 flow 的改动 → 从快照里取出那一条，用 `write-flow` 写回即可，不要用整实例还原。
