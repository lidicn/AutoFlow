---
name: autoflow-core
description: AutoFlow Core — 让 agent 安全编写/修改/验证 Node-RED flow 的最小核心。nr_client.py 唯一写入通道，含 doctor 自检、只读 inventory、自愈闭环验证。
---

# AutoFlow Core（核心版）

你（agent）通过本 skill 获得对 Node-RED 实例的安全编程能力：读取、编写、修改、验证 flow。
所有写入经 `scripts/nr_client.py`（纯标准库，无 pip 依赖），自带护栏：写前快照、
结构 lint、节点数熔断、prod 闸、操作日志、回读校验。

## 🚨 黄金法则（违反任何一条立即停止）

1. **`af_*` 前缀 = 你的所有权**。你新建的 tab/flow 一律 `af_<场景名>` 命名，只有这些可写可删。
2. **用户手工流只读**。label 不带 `af_` 前缀的 flow 是用户亲手搭的：可以读、可以引用，
   **绝不修改、绝不删节点、绝不断线**。inventory 命令会标注归属，以它为准。
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
可选（verify 的 HA 断言用）：`HASS_SERVER` + `HASS_TOKEN` 环境变量。

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

- 用户手工 flow 只读，想误伤都做不到（write-flow 的节点数熔断 + inventory 归属标注双保险）。
- 每次写入前自动全量快照，出事可用 `restore_snapshot` 一键回滚。
- 所有写入操作记录在 `~/.autoflow-core/logs/nr_operations.log`。
