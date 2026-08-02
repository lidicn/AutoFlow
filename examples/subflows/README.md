# 子流程教学导入示例

本目录提供三个「真实能力」的教学导入模板：**bark_push**（iPhone 通知）、**anysearch_batch**（资讯搜索）、**llm_caiyun_weather**（彩云天气）。

它们演示如何把你**自己的** Node-RED 子流程接入 AutoFlow，供 DSL 编译器以 `调用子流程:` 一行触发。仓库本身已把这三者在 `api_specs.py` / `subflows.py` 中以**占位符密钥**的形式作为内置能力发布（多数用户无需自建即可用，只要替换各自的密钥/坐标占位符），这里的 JSON 是给「想完全自建 / 自托管 / 学习原理」的人看的搭建参考。

> 仓库的**默认示例子流程**是 `demo_notify`（一个不连真实下游的 link_out 占位，仅用于演示编译路径与回归测试），不包含任何外部密钥。

---

## 导入契约（与 WebUI 一致）

后端路由：`POST /api/subflows/import`

| 字段 | 必填 | 说明 |
|------|------|------|
| `nr_subflow_id` | ✅ | 你的 Node-RED 子流程 id。先建好子流程，再把它填进模板里的 `<NR_XXX_SUBFLOW_ID>` 占位符。 |
| `key` | ✅ | DSL 调用名（如 `bark_push`）。不要与内置名撞车。 |
| `title` | ⬜ | 展示标题，缺省取 key。 |
| `owner` | ⬜ | 注册来源，缺省 `webui`。 |
| `status` | ⬜ | `active`（默认，立即可用）或 `pending_review`（走人工审核）。 |

`source_type` 由网关强制为 `imported`（你传了也会被忽略）。`input_schema` 与 `env_requirements` **由网关自省 NR 子流程后自动抽取**，因此本目录 JSON 里的这两个字段仅作搭建参考，**POST 时网关以自省结果为准**。

### 最简导入（只发网关真正读取的字段）

```bash
curl -X POST http://127.0.0.1:8000/api/subflows/import \
  -H 'Content-Type: application/json' \
  -d '{"nr_subflow_id":"<你的真实子流程id>","key":"bark_push","title":"Bark 推送(iPhone 通知)"}'
```

或直接 POST 整份 JSON 文件（多余字段被忽略）：

```bash
curl -X POST http://127.0.0.1:8000/api/subflows/import \
  -H 'Content-Type: application/json' \
  --data-binary @bark_push.json
```

导入成功后 DSL 即可：

```
调用子流程: bark_push(title=`告警`, body=`空调异常`)
```

---

## 密钥放哪里（永远不进这些 JSON）

所有密钥/坐标都是**占位符**，真实值请写在：

- **网关 `.env`**（见仓库根 `.env.example`）：`BARK_SERVER` / `BARK_KEY` / `ANYSEARCH_API_KEY` / `CAIYUN_TOKEN` / `CAIYUN_LON` / `CAIYUN_LAT`；或
- **Node-RED 子流程的 env 节点**（推荐，密钥留在 NR 侧，网关只引用子流程 id）。

> ⚠️ 切勿把真实密钥填进本目录 JSON 后再提交——它们会被 git 记录。本目录所有 `<...>` 均为占位符。

---

## 各能力说明

### bark_push.json
- 形态：**subflow**（有返回值，可被链式调用 / 分支）。
- 自建：NR 子流程读 `msg.title` / `msg.body` / 可选 `msg.bark_*`，内部读 `BARK_SERVER`、`BARK_KEY` 调 Bark HTTP API。
- 参数：`title`(必填)、`body`(必填)，其余 `bark_level/sound/url/group/...` 可选（见 JSON `input_schema`）。

### anysearch.json
- 形态：内置为 **link_out**（fire-and-forget）。JSON 演示以 subflow 形态自建导入。
- 自建：NR 节点向 `https://api.anysearch.com/mcp` 发 JSON-RPC（`tools/call → batch_search`），Header 带 `Authorization: Bearer <ANYSEARCH_API_KEY>`。
- 参数：`keywords`(必填，逗号分隔)、`max_results`(可选，默认 5)。

### caiyun.json
- 形态：内置为 **link_out**（GET、无请求体）。JSON 演示以 subflow 形态自建导入。
- 自建：NR 节点 GET `https://api.caiyunapp.com/v2.7/<CAIYUN_TOKEN>/<CAIYUN_LON>,<CAIYUN_LAT>/weather?alert=true`，把 URL 里的 token / 坐标占位符替换为你的真实值。
- 参数：无入参（坐标/密钥编码在 URL 模板里）。
