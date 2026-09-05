# AutoFlow 核心版使用反馈

**日期**：2026-09-02
**测试环境**：Node-RED 192.168.2.200:1880（生产实例）
**测试任务**：TV Cam 智能保活 flow 编写与部署

---

## 一、登录与认证

### 1.1 认证方式

Node-RED 1880 实例启用了用户名密码认证，采用 **OAuth2 Password Grant** 方式获取 token：

```
POST /auth/token
Content-Type: application/json

{
  "client_id": "node-red-admin",
  "grant_type": "password",
  "scope": "*",
  "username": "lidicn",
  "password": "longyin1003"
}
```

返回：
```json
{
  "access_token": "<JWT Token>",
  "token_type": "Bearer",
  "expires_in": 28800
}
```

后续请求在 Header 中携带：`Authorization: Bearer <token>`

### 1.2 遇到的问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 首次认证失败 `invalid_grant` | 密码拼写错误（longying → longyin） | 确认正确密码后重试成功 |
| PowerShell `Invoke-WebRequest` 报错 "NonInteractive mode" | 非交互模式下 IWR 有已知问题 | 改用 `Invoke-RestMethod` |
| `/flows` 返回类型为 String 而非 JSON 对象 | Node-RED 返回 `Content-Type: text/plain` | 手动 `ConvertFrom-Json` 或用 Python 解析 |
| PowerShell `ConvertFrom-Json` 报错重复键 `mergeContext`/`mergecontext` | Node-RED 节点同时包含新旧版本字段名 | 改用 Python `json.load()`（允许重复键，后者覆盖前者） |

### 1.3 结论

- **用户名密码认证方式有效**，token 有效期 8 小时
- **不需要额外的静态 token**，用账号密码动态获取即可
- autoflow core 的 `nr_client.py` 封装了完整的认证流程，直接使用环境变量 `NR_URL/NR_USER/NR_PASS` 即可

---

## 二、Node-RED 实例现状

### 2.1 规模

- **总节点数**：1002
- **Tab 数**：28
- 主要 tab：全屋情境模式、客厅、书房、小米电视、智能调度核心、豆包中枢、Lab 等

### 2.2 已安装的关键节点

- `node-red-contrib-home-assistant-websocket` v0.80.3（HA 集成）
- `node-red-node-ui-table`（仪表盘表格）
- `axios-request`（HTTP 请求）
- MQTT 节点

### 2.3 Home Assistant 集成

- HA server config 节点 ID：`e93e1ad9c034e866`
- Android TV 实体：`media_player.android_tv_192_168_2_238`
- 支持 `androidtv.adb_command` 服务调用

---

## 三、保活 Flow 实现

### 3.1 旧 Flow 的问题

用户原有的保活 flow（`6239f4e783b15389` 小米电视 tab 中）存在以下问题：

| 问题 | 说明 |
|------|------|
| ❌ 命令无效 | `echo keepalive` 仅在电视上打印一行字，**App 被杀掉后不会被拉起** |
| ❌ 无健康检查 | 不知道 App 是否真的在运行，盲目执行命令 |
| ❌ 间隔太长 | 每 5 分钟才检查一次 |
| ⚠️ 仅证明 ADB 连接 | 唯一作用是证明 ADB 连接还在，与 App 保活无关 |

### 3.2 新 Flow 设计

新建 tab `af_tv_cam_keepalive`（ID: `1e2e8cfcb942bc34`），实现智能保活：

```
每2分钟触发 (inject, repeat=120s)
    ↓
HTTP 健康检查 (GET http://192.168.2.238:8080/api/health)
    ↓
判断是否需要拉起 (function: 检查 statusCode + payload)
    ↓
switch 分流
    ├─ 正常 → debug "✅ TV Cam 正常运行"
    └─ 无响应 → HA api-call-service
                  (androidtv.adb_command: am start -n com.example.arcfaceandroid/.TvMainActivity)
                  → debug "🔄 已通过ADB拉起"
```

### 3.3 关键改进

| 改进项 | 说明 |
|--------|------|
| ✅ 先健康检查 | 只有 App 真的死掉才拉起，不盲目执行 |
| ✅ 真正的拉起命令 | `am start -n com.example.arcfaceandroid/.TvMainActivity` |
| ✅ 更短间隔 | 每 2 分钟检查一次（原来 5 分钟） |
| ✅ 详细日志 | 正常/拉起/失败都有 debug 输出 |
| ✅ 复用现有配置 | HA server 节点和 androidtv 实体复用现有配置 |

### 3.4 当前状态

新 flow 已部署并运行，但 flow 中同时存在新旧两组节点（用户导入时合并到了同一 tab）：

- **新 flow（9节点）**：健康检查 + 智能拉起 ✅ 有效
- **旧 flow（5节点）**：`echo keepalive` 无效保活 ❌ 建议删除或禁用

---

## 四、ADB 连接保活问题（核心痛点）

### 4.1 问题描述

用户反馈："试了一段时间还是经常需要手动打开 ADB"

这说明 **App 保活 flow 本身是有效的，但 ADB 连接本身会周期性断开**，导致 `am start` 命令执行失败。

### 4.2 可能原因

| 原因 | 可能性 | 说明 |
|------|--------|------|
| 电视 ADB 调试超时 | 高 | 红米电视（MiTV-MFTP0）可能在无操作一段时间后自动关闭 USB 调试 |
| 电视休眠后 ADB 停止 | 高 | 电视进入深度休眠后，ADB 服务可能停止 |
| 网络波动 | 中 | Wi-Fi/以太网不稳定导致 TCP 连接断开 |
| HA androidtv 集成无自动重连 | 中 | 集成可能在 ADB 断开后不会自动重连 |

### 4.3 解决方案

#### 方案 A：电视端设置（最根本，推荐优先尝试）

1. **开发者选项**：
   - 设置 → 关于 → 连续点击"版本号"开启开发者选项
   - 开发者选项中开启"USB 调试"
   - 查找"USB 调试超时"或"ADB 超时"，设为"从不"（如有）

2. **休眠设置**：
   - 设置 → 通用 → 休眠，设为"永不"或较长时间
   - 确保"休眠时保持网络连接"开启（Wi-Fi 始终开启）

3. **应用后台管理**：
   - 设置 → 应用 → Arcface → 允许后台运行
   - 关闭"电池优化"对 Arcface 的限制

#### 方案 B：Node-RED 用 exec 节点直接调用 adb（不依赖 HA 集成）

如果 HA androidtv 集成在 ADB 断开后不会自动重连，可以改用 exec 节点直接调用 adb 命令：

```
健康检查失败
    ↓
exec: adb connect 192.168.2.238:5555 && adb shell am start -n com.example.arcfaceandroid/.TvMainActivity
```

**优点**：每次执行前先 `adb connect`，即使 ADB 断开也能自动重连
**前提**：Node-RED Docker 容器内需要安装 adb 工具

#### 方案 C：在 Arcface App 内增加 ADB 保活（需系统权限）

App 内定期发送广播保持 ADB 唤醒，但普通第三方 App 无法控制系统 ADB 服务，**不推荐**。

### 4.4 建议执行顺序

1. **先做方案 A**（电视端设置），这是最根本的解决方案
2. 如果 ADB 还是经常断，再考虑**方案 B**（exec 节点直接调用 adb）
3. 方案 C 不推荐

---

## 五、autoflow 核心版评价

### 5.1 优点

| 优点 | 说明 |
|------|------|
| ✅ 认证封装完善 | `nr_client.py` 自动处理 OAuth2 token 获取和刷新 |
| ✅ 安全护栏 | 写前快照、节点数熔断、用户流只读硬拦截 |
| ✅ 无外部依赖 | 纯标准库实现，不需要 pip install |
| ✅ 环境变量配置 | `NR_URL/NR_USER/NR_PASS` 三选一配置方式灵活 |

### 5.2 遇到的坑

| 坑 | 说明 | 建议 |
|----|------|------|
| ⚠️ PowerShell 兼容性 | `Invoke-WebRequest` 非交互模式报错、`ConvertFrom-Json` 不允许重复键 | 推荐用 Python 调用 `nr_client.py`，或直接用 Python urllib |
| ⚠️ `/flows` 返回 text/plain | PowerShell `Invoke-RestMethod` 返回 String 而非对象 | 需手动 `ConvertFrom-Json`，或用 Python |
| ⚠️ 1880 是生产实例 | autoflow core 默认禁止写 prod，需 `--allow-prod` | 生产环境操作需谨慎，先 dry-run |

### 5.3 改进建议

1. **增加 PowerShell 兼容示例**：SKILL.md 中补充 PowerShell 环境下的调用示例和常见坑
2. **增加 flow 清理功能**：支持删除指定 tab 中的旧节点（当前只能整体更新 flow）
3. **增加节点级 diff 预览**：`write-flow --dry-run` 时显示节点级别的增删改 diff

---

## 六、后续待办

- [ ] **清理 flow 中的旧节点**：删除 `af_tv_cam_keepalive` tab 中旧的 `echo keepalive` 5个节点
- [ ] **电视端 ADB 设置优化**：按方案 A 检查并设置电视端 ADB 不超时
- [ ] **验证 ADB 保活效果**：观察 24-48 小时，看 ADB 是否还会断开
- [ ] **如仍断开，实施方案 B**：Node-RED 容器内安装 adb，改用 exec 节点直接调用
- [ ] **TV 端 8 层保活文档整理**：内部 7 层 + Node-RED 外部 1 层

---

## 七、相关链接

- Node-RED：http://192.168.2.200:1880
- 保活 flow：http://192.168.2.200:1880/#flow/1e2e8cfcb942bc34
- TV Cam 健康检查：http://192.168.2.238:8080/api/health
- autoflow core：https://github.com/lidicn/AutoFlow/tree/master/core
