# AutoFlow v1.5.x 开发计划 — Pro 版 + 经验复用

> **创建时间**：2026-09-05
> **负责人**：dw
> **核心目标**：推出 AutoFlow Pro（核心版连网关），统一数据入口，启动经验复用体系
> **版本范围**：v1.5.0 ~ v1.6.0

---

## 一、版本路线图

| 版本 | 主题 | 核心内容 | 预计工作量 |
|------|------|---------|-----------|
| v1.5.0 | AutoFlow Pro 首发 | 网关 REST API + 核心版网关模式 + SKILL.md | 小 |
| v1.5.1 | Flow 模板库 | 内置模板 + 渲染 API + 一键保存模板 | 中 |
| v1.5.2 | Token 统计 + 错误知识库 | Token 统计面板 + 错误自动记录 + 相似案例推荐 | 中 |
| v1.5.3 | 离线降级 + raw→DSL 转换 | 网关不可用时直连降级 + raw JSON 自动转 DSL | 中 |
| v1.6.0 | 经验库完善 | 实体关联统计 + 用户偏好学习 + DSL 语法增强 | 中 |

---

## 二、v1.5.0 — AutoFlow Pro 首发

### 目标

让核心版用户通过网关 API 享受安全闸门、DSL 编译、快照回滚，同时保持轻量省 token。

### 后端（网关侧）

**新增 `/api/core/*` REST API 组**：

| API | 方法 | 功能 | 对应 MCP 工具 |
|-----|------|------|-------------|
| `/api/core/version` | GET | 网关版本 + 兼容性检查 | 无 |
| `/api/core/health` | GET | 网关健康检查 | 无 |
| `/api/core/propose-dsl` | POST | 提交 DSL，编译+闸门校验，返回提案 | autoflow_propose_dsl |
| `/api/core/deploy-proposal` | POST | 部署已通过的提案到 NR | deploy_proposal |
| `/api/core/deploy-raw` | POST | 直接提交 raw JSON（逃生舱，附警告） | autoflow_deploy_raw |
| `/api/core/entities` | GET | 实体目录查询（过滤/分页） | autoflow_list_entities |
| `/api/core/resolve-entity` | GET | 自然语言设备名→entity_id | autoflow_resolve_entity |
| `/api/core/snapshots` | GET | 快照列表 | 无 |
| `/api/core/rollback` | POST | 回滚到指定快照 | 无 |

**认证机制**：
- 日常操作：API Key（`Authorization: Bearer <api_key>`）
- 自动部署：复用 deploy_token 体系（传入 `deploy_token` 参数）
- API Key 在 WebUI「设置」页面生成和管理

**Token 统计基础**：
- 每次 API 调用返回 `_telemetry` 字段（输入/输出字符数、耗时、模式）
- 网关侧按 Agent ID 聚合存储

### 前端（核心版侧）

**nr_client.py 增加 `--gateway` 模式**：
```bash
# 网关模式
python nr_client.py --gateway http://192.168.2.200:8000 propose-dsl "人体传感器触发开灯"
python nr_client.py --gateway http://192.168.2.200:8000 deploy-proposal <proposal_id>
python nr_client.py --gateway http://192.168.2.200:8000 rollback <snapshot_id>

# 直连模式（现有，保持不变）
python nr_client.py deploy --flow '{...}'
```

**SKILL.md 更新**：
- 新增「网关模式」章节（~50 行）
- 明确引导：**99% 场景用 propose-dsl，deploy-raw 仅作逃生舱**
- DSL 语法速查表
- 配置说明：网关地址、API Key、环境变量

**配置管理**：
- 配置文件：`~/.autoflow-lite/config.json`
- 环境变量：`AF_GATEWAY_URL`、`AF_API_KEY`
- `doctor` 自检增加网关连通性检查

### WebUI 侧

- 「设置」页面增加 API Key 管理
- 「关于」页面显示 Pro 版入口和安装命令

### 验收标准

- [ ] 核心版通过网关 API 成功提交 DSL 并部署
- [ ] 核心版通过网关 API 查询实体
- [ ] deploy-raw 返回 DSL 转换建议警告
- [ ] API Key 认证生效，无 key 调用返回 401
- [ ] doctor 自检包含网关连通性
- [ ] SKILL.md 明确引导 DSL 优先

---

## 三、v1.5.1 — Flow 模板库

### 目标

让 Agent 可以直接调用已验证的模板，减少重复编写，提高成功率。

### 功能

**内置模板（5-10 个常用场景）**：
- 人体传感器触发灯
- 温度自动调节空调
- 门窗开启通知
- 定时开关设备
- 多条件联动（如"晚上+有人+亮度低→开灯"）

**API**：
- `GET /api/core/templates`：列出可用模板
- `POST /api/core/templates/render`：传入参数，渲染成 DSL
- `POST /api/core/templates/save`：从现有提案保存为模板（用户贡献）

**模板格式**：
```json
{
  "name": "人体传感器触发灯",
  "description": "检测到人时开灯，无人后延时关灯",
  "params": [
    {"name": "sensor", "type": "entity", "domain": "binary_sensor"},
    {"name": "light", "type": "entity", "domain": "light"},
    {"name": "delay", "type": "number", "default": 300}
  ],
  "dsl_template": "触发: {{sensor}} on\n动作: light.turn_on({{light}})\n延时: {{delay}}秒\n动作: light.turn_off({{light}})"
}
```

### 验收标准

- [ ] 内置 5 个以上模板可正常渲染
- [ ] Agent 可通过 API 列出和渲染模板
- [ ] 用户可从提案一键保存为模板
- [ ] 模板参数支持 entity 类型校验

---

## 四、v1.5.2 — Token 统计 + 错误知识库

### 目标

让用户看到 token 消耗，让 Agent 从历史错误中学习。

### Token 统计面板（WebUI）

- 今日/本周/本月 token 消耗趋势
- DSL vs raw 使用比例
- 按 Agent 维度排行
- 平均每次消耗、节省 token 估算（对比 raw 模式）
- Token 估算方式：字符数 / 4（粗略但足够趋势分析）

### 错误知识库

- propose_dsl / deploy_raw 失败时自动记录：错误类型、DSL/JSON 原文、修复建议
- 下次遇到相似错误，返回结果中附带「历史相似案例」
- WebUI 可浏览错误知识库

### 验收标准

- [ ] WebUI Token 统计面板可正常显示
- [ ] 错误自动记录，可按类型筛选
- [ ] 相似错误返回历史案例参考

---

## 五、v1.5.3 — 离线降级 + raw→DSL 转换

### 离线降级

- 核心版配置两个 endpoint：网关地址 + NR 直连地址
- 网关超时/不可达时自动 fallback 到直连 NR
- fallback 时警告 Agent"失去安全闸门"
- 网关恢复后自动切回

### raw→DSL 自动转换

- `POST /api/core/raw-to-dsl`：输入 raw JSON，输出 DSL 参考
- Agent 调用 deploy-raw 时，返回结果附带 DSL 建议
- 长期目标：raw 只作为输入，最终都转成 DSL 存储

### 验收标准

- [ ] 网关不可用时核心版自动降级直连
- [ ] 网关恢复后自动切回
- [ ] raw→DSL 转换对常见 flow 有效

---

## 六、v1.6.0 — 经验库完善

### 实体关联统计

- 分析历史部署，统计 entity 共现频率
- Agent 调用 resolve_entity 时，附带「经常一起使用的实体」
- 示例：写"书房自动化"时，推荐"书房人体传感器 + 书房灯"

### 用户偏好学习

- 记录用户喜欢的节点类型、命名风格、tab 组织模式
- 生成 DSL 时自动应用用户偏好
- 用户可在 WebUI 查看和编辑偏好

### DSL 语法增强

- 根据模板库和用户反馈，增强 DSL 表达能力
- 减少需要用 deploy-raw 逃生舱的场景

---

## 七、不做的事（v1.5.x 范围外）

- ❌ 智能推荐 / AI 自动补全（需要大量数据，v2.0 再考虑）
- ❌ 多用户协作 / 团队功能
- ❌ 移动端 App
- ❌ 完整的可视化 flow 编辑器（WebUI 已有基础画布，不做重度编辑器）

---

## 八、风险与依赖

| 风险 | 影响 | 应对 |
|------|------|------|
| 网关 API 与 MCP 工具逻辑重复 | 维护成本增加 | 复用 gateway.py 现有逻辑，API 层只做参数转换 |
| API Key 管理复杂 | 用户体验差 | WebUI 一键生成，支持环境变量配置 |
| 模板质量参差不齐 | 影响成功率 | 内置模板经过验证，用户模板标注来源 |
| Token 统计不精确 | 用户质疑数据 | 明确标注"估算值"，用于趋势分析 |

---

## 九、立即行动（v1.5.0 开工清单）

1. [ ] 网关新增 `/api/core/*` REST API（复用现有逻辑）
2. [ ] WebUI 新增 API Key 管理
3. [ ] nr_client.py 增加 `--gateway` 模式
4. [ ] core/skill/SKILL.md 增加网关模式章节
5. [ ] 核心版安装脚本更新（支持 Lite 模式）
6. [ ] README 增加 Pro 版介绍和安装命令
7. [ ] 测试：核心版通过网关完成完整 flow 编写部署
