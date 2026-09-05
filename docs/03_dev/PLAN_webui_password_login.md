# WebUI 登录改造计划：从「访问令牌」到「账号密码 + 会话」

> 状态：**已拍板 · 已实现 · 待 NAS 干净重部署验收**（W1–W4 完成；W5 重部署需用户签收）
> 提出：2026-08-30（用户：「webui访问令牌始终不太友好。改成账号密码登陆。制定完善计划」）
> 落地：2026-08-30（代码 `webui_auth.py` + `webui.py` 闸门 + 前端 `app.js/index.html/style.css` + `tests/test_webui_password_login.py` 12 项全绿）
> 影响面：`src/autoflow_gateway/webui.py` + 新增 `webui_auth.py` + `webui/static/{app.js,index.html,style.css}` + `tests/test_webui_password_login.py`
> 不触碰：MCP `af_` 身份码体系、ACP `acp_` 对等令牌体系（三套隔离铁律）

---

## 0. 一句话结论

**令牌不是删掉，而是降为「兼容通道」；主通道改成「账号密码登录 → 服务端会话 Cookie」。**
先做**并存期**（旧令牌继续能用，NAS 现有部署零风险），观察一轮后再决定是否默认关令牌。

---

## 0.1 决策落地（D1–D5，2026-08-30 用户拍板）

| # | 原提案 | **用户决策（已落地）** | 落地要点 |
|---|---|---|---|
| **D1** | 先单用户 `owner` | **一步到位多用户**：`viewer` / `admin` / `owner` 三档 RBAC | `webui_auth.PERM_RULES` 声明式路径前缀 + 方法授权；未登记写路径 fail-closed 要求 `admin`；`/api/auth/users` 仅 `owner` |
| **D2** | 12h 滑动 + 7d 记住我 | **采纳** | `SESSION_TTL_HOURS=12`（滑动）、`REMEMBER_TTL_DAYS=7`（硬上限）、`SLIDING_REFRESH_AFTER=30min` |
| **D3** | 要 8 位引导码 | **改为「初次打开网关引导注册」**：去掉引导码这一步 | 零账号时 `GET /api/auth/state` 返回 `initialized:false` → 前端渲染注册向导；`AF_WEBUI_OPEN_REGISTER`（默认 1）+ 零用户 → 注册窗口开放，建首账号后永久关闭。GitHub 已公开但无人访问，故不靠引导码也能安全初始化 |
| **D4** | 先并存（M1） | **直接关旧令牌**：无脚本/CI 在用 `?token=` | 默认 `AF_WEBUI_TOKEN_MODE=password_only`，旧令牌通道默认关闭（`resolve_legacy_token` 在 `password_only` 直接返回 `False`） |
| **D5** | 要记住我 | **采纳** | 登录 `remember:true` → 会话 7 天绝对上限；前端登录框含「记住我」勾选 |

> 实现状态：后端 `webui_auth.py`（PBKDF2-HMAC-SHA256 600k / 服务端有状态会话 / RBAC / 失败锁定 / 审计）、`webui.py` 三态闸门 + 13 个 `/api/auth/*` 路由、前端 `app.js`（`api()` 加 `X-Requested-With` + 401 弹登录框 + 注册/改密/会话/用户管理）、测试 `tests/test_webui_password_login.py`（12 项全绿）均已落地。W5 NAS 干净重部署待用户签收。

---

## 1. 现状盘点（代码锚点，已实测核对）

### 1.1 现在的认证是怎么跑的

| 环节 | 位置 | 现状 |
|---|---|---|
| 闸门中间件 | `webui.py:1828-1877` `guarded()` | 只拦 **`/api` 前缀**；`/` 与 `/static/*` 完全匿名 |
| 令牌来源 | `webui.py:53-70` `_resolve_webui_token()` | ① env `AF_WEBUI_TOKEN` ② 文件 `<data_dir>/.webui_token` |
| 首跑引导 | `webui.py:73-108` `_bootstrap_webui_token()` | opt-in：`AF_WEBUI_TOKEN_AUTO=1` 时生成 `token_urlsafe(24)` 落盘 + 打 stdout |
| 携带方式 | `webui.py:1858-1866` | `Authorization: Bearer` / `?token=` / Cookie `af_ui_token` |
| 比对 | `webui.py:1867-1873` | `hmac.compare_digest`（S-1 常量时间，防时序） |
| 无令牌兜底 | `webui.py:1847-1855` | S-4：仅放行回环，远程一律 403（防 Docker 0.0.0.0 裸奔） |
| 前端取令牌 | `app.js:11-12` `uiToken()` | localStorage 优先，回退 cookie |
| 前端报错 | `app.js:28` | 403 → 「访问被拒绝：需要 WebUI 令牌（点右上角 🔑 设置）」 |
| 前端粘贴入口 | `app.js:1277-1287` `#tokenBtn` 模态 | 存 localStorage + 写 cookie（`max-age=31536000; SameSite=Lax`） |
| 部署开关 | `docker-compose.yml:23` | `AF_WEBUI_TOKEN_AUTO: "1"` |

### 1.2 现有回归守卫（改动必须不破）

- `tests/regression/test_p0_auth.py`
  - S-1：非 ASCII Bearer（`café`）必须 403 不能 500
  - S-4：公网 Peer `/api` 403、本机放行；**伪造 `X-Forwarded-For: 127.0.0.1` 仍 403**
- `tests/test_no_secrets.py`（P-2 门禁）：**任何凭证不得落进被跟踪文件** → 计划里不允许出现默认密码/真实 IP
- 三套令牌隔离：`af_`（MCP，`mcp_server.py:1788+`）/ `acp_`（`/acp`，`mcp_server.py:1804+`）/ WebUI —— **互不相认**

### 1.3 依赖约束

`pyproject.toml` 只有 `starlette>=0.37` 等，**没有 bcrypt / passlib / argon2 / PyJWT**。
→ **不新增任何依赖**，密码哈希用标准库 `hashlib.pbkdf2_hmac`，会话 ID 用 `secrets.token_urlsafe`。

---

## 2. 痛点：为什么「始终不太友好」

| # | 痛点 | 具体场景 |
|---|---|---|
| P1 | **要抄 33 位随机串** | 首次装完必须 `docker compose logs` 里翻 `?token=Vv7...`，再手工粘进浏览器 |
| P2 | **手机上基本无解** | 手机开 NAS 的 WebUI，没法看服务器日志；得先回电脑抄串再手输到手机 |
| P3 | **不可记忆、不可自助** | 换浏览器/换设备/清缓存 = 重抄一遍；串丢了只能 SSH 上去 `cat` 文件 |
| P4 | **无身份、无审计** | 一个串全员共享，审计里看不出「这次批准是谁点的」 |
| P5 | **泄露后无法自助吊销** | 只能上服务器改文件 + 重启；前端「退出」只清本地，串本身不变 |
| P6 | **交互反直觉** | 正常人是找「登录」，不是找右上角 🔑 |
| P7 | **无过期、无锁定** | 静态串永久有效，失败了也不限流 |

---

## 3. 目标 / 非目标

### 目标（G）
- G1 主通道改为 **用户名 + 密码登录**，登录一次即可，不用抄串
- G2 会话可**服务端吊销**（登出即失效）、可过期（12h 滑动 / 7d 记住我）
- G3 **首跑引导极简**：8 位分组引导码（如 `K7M2-9QX4`）→ 设密码 → 进系统，串消失
- G4 支持**自助改密码**、审计里能看到「谁登录/谁批准」
- G5 旧令牌**并存不破**，已部署的 NAS 升级零风险
- G6 全程 **0 新增依赖**

### 非目标（NG，本期不做）
- NG1 ~~不做多用户体系~~ → **D1 已改为一步到位多用户**：`viewer` / `admin` / `owner` 三档 RBAC（见 §0.1、§5.4）。本期只做权限矩阵与用户管理 UI，不做组/租户。
- NG2 不做 OAuth / SSO / TOTP / 短信验证码
- NG3 **不改 MCP `af_` 身份码、不改 ACP `acp_` 令牌**
- NG4 不改 `/mcp*` 的任何语义
- NG5 不做密码找回邮件（改密只能：已登录自助改 / 服务器 CLI 逃生口）

---

## 4. 总体设计

### 4.1 认证形态：密码换会话（不是每请求带密码）

```
浏览器 ──POST /api/auth/login {username,password}──▶ 网关
       ◀── 200 + Set-Cookie: af_session=<32B随机>; HttpOnly; SameSite=Lax; Path=/ ──
浏览器 ──GET /api/pending (自动带 Cookie)──▶ 网关 → 查 webui_sessions 表 → 200
浏览器 ──POST /api/auth/logout──▶ 服务端删会话 + 清 Cookie → 旧会话立即失效
```

关键点：会话是**服务端有状态**的（SQLite 表），不是无状态 JWT。原因：JWT 无法即时吊销，而「登出立刻失效」正是 G2 的核心诉求。

### 4.2 三态判定（中间件新逻辑）

`guarded()` 的判定从「比一个串」扩成「三选一通过」：

```
请求 /api/*（除白名单）
  ├─ 1) 会话 Cookie af_session 命中 webui_sessions 且未过期  →  放行（主通道）
  ├─ 2) 旧令牌（并存期仅当 AF_WEBUI_TOKEN_MODE != off）：
  │      Authorization: Bearer / ?token= / Cookie af_ui_token  →  放行（兼容通道）
  └─ 3) 都不通过 → 401 {"ok":false,"error":"unauthorized","auth_required":true}
```

**401 而不是 403**：401 让前端能明确区分「要登录」与「无权限」，触发登录弹窗。
**S-4 语义保留**：若「零账号 + 零旧令牌 + 远程」→ 仍 403（`webui token required for non-local access`），防止公网裸奔，现有 S-4 测试不退化。

### 4.3 白名单（无需认证即可访问）

| 路径 | 理由 |
|---|---|
| `/`、`/static/*` | 登录页所在 SPA 必须能匿名打开 |
| `GET /api/auth/state` | 前端探测「是否已登录 / 是否需要初始化 / 是否禁用令牌」 |
| `POST /api/auth/login` | 登录本身 |
| `POST /api/auth/register` | 首开注册向导（仅零账号 + `AF_WEBUI_OPEN_REGISTER≠0` 时开放，建首账号后永久关闭；**D3 取代原引导码方案**） |
| `GET /health`（若存在） | 容器健康检查 |

### 4.4 首跑初始化（D3：用「初次打开引导注册」取代原 8 位引导码方案）

**决策变更（D3）**：用户拍板「什么是引导码？」后，改为**初次打开网关引导用户注册登录**——直接去掉引导码这一步。

**方案：首账号开放注册窗口**

```
网关启动 / 每次判定
  registration_open() = bool(AF_WEBUI_OPEN_REGISTER != "0") and not has_users()
  情况A：零账号 + 开关未关  → /api/auth/register 开放，任何人可建首个 owner
  情况B：已有账号          → 注册窗口永久关闭（后续加用户只能由 owner 在 /api/auth/users 创建）
  情况C：AF_WEBUI_OPEN_REGISTER=0 且零账号 → 无注册入口，只能靠 CLI 逃生口建号
```

> 安全依据：GitHub 仓库虽公开但无人访问，NAS 的 WebUI 经 Docker 0.0.0.0 暴露时需靠 S-4（零账号 + 远程 → 403）兜底；首账号注册仅本地回环可完成初始化（同 §4.2 的 S-4 语义），远程无法抢注。若担忧公网窗口，可设 `AF_WEBUI_OPEN_REGISTER=0` 完全关闭、仅用 CLI 逃生口建号。

前端 `/` 打开 → `GET /api/auth/state` 返回 `{"initialized":false}` → 渲染「创建管理员账号」向导：
用户名（默认 `owner`）+ 新密码 + 确认密码 → `POST /api/auth/register` → 成功即自动登录（发会话 Cookie），注册窗口随之关闭。

**逃生口（服务器侧，不走网络）**：新增 CLI 子命令
`python run.py cli webui-passwd --user owner` → 交互式设密/改密，直接写库。
用途：窗口期关闭、忘记密码、不想让注册端点对公网开放。**这条必须做**，否则「窗口关闭且人在外地」会把自己锁死。

---

## 5. 详细设计

### 5.1 新模块：`src/autoflow_gateway/webui_auth.py`

职责：密码哈希、用户存储、会话存储、登录限流。**与 `identity.py` 平级，独立表，不污染 agent 身份模型**（沿用 `AcpTokenStore` 的既有做法）。

```python
# ── 密码哈希（标准库，0 新增依赖）──
# 格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
PBKDF2_ALGO   = "sha256"
PBKDF2_ITERS  = 600_000      # OWASP 2023 建议值；随硬件上调，旧哈希登录成功时自动 rehash
SALT_BYTES    = 16

def hash_password(pw: str, *, iterations=PBKDF2_ITERS, salt=None) -> str: ...
def verify_password(pw: str, stored: str) -> tuple[bool, bool]:
    """返回 (是否通过, 是否需要 rehash) —— compare_digest 常量时间比对"""
```

> 迭代次数存进哈希串 → 未来提标不需要一次性迁移；登录成功时若低于当前标准，静默 rehash 重写。

### 5.2 表结构（复用现有 `data_dir/autoflow.db`，与 `agents` / `acp_tokens` 同库不同表）

```sql
CREATE TABLE IF NOT EXISTS webui_users (
    user_id       TEXT PRIMARY KEY,          -- usr_<12hex>
    username      TEXT NOT NULL UNIQUE,
    pw_hash       TEXT NOT NULL,             -- pbkdf2_sha256$iters$salt$hash
    role          TEXT NOT NULL DEFAULT 'owner',   -- owner|admin|viewer（P1 只用 owner，预留）
    status        TEXT NOT NULL DEFAULT 'active',  -- active|disabled
    must_change   INTEGER NOT NULL DEFAULT 0,      -- 引导码设密后为 0
    failed_count  INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,                      -- ISO8601，失败锁定到期
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    notes         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webui_sessions (
    session_id    TEXT PRIMARY KEY,          -- ses_<16hex>，仅内部主键
    sess_hash     TEXT NOT NULL UNIQUE,      -- sha256(明文 cookie 值)，库中永不存明文
    user_id       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,             -- 绝对上限：12h（默认）/ 7d（记住我）
    last_seen_at  TEXT,
    user_agent    TEXT DEFAULT '',
    ip            TEXT DEFAULT ''
);
```

**纯增量 `CREATE TABLE IF NOT EXISTS`**，旧库零迁移、回滚零成本。

### 5.3 新增 / 改造的 API

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| GET | `/api/auth/state` | 免 | `{initialized, auth_mode, open_register, logged_in, user, csrf_header}`（`initialized=false` 时前端渲染注册向导，D3） |
| POST | `/api/auth/login` | 免 | body `{username,password,remember}` → 200 发 Cookie；失败 401（I-5 恒定） |
| POST | `/api/auth/register` | 免（仅零账号 + `open_register`） | `{username,password,confirm}` → 建首个 `owner` 并自动登录；建号后窗口永久关闭（D3） |
| POST | `/api/auth/logout` | 会话 | 服务端删会话 + 清 Cookie（I-4） |
| GET | `/api/auth/me` | 会话 | `{user_id,username,role,last_login_at,must_change}` |
| POST | `/api/auth/change-password` | 会话 | `{old_password,new_password,confirm}` → 成功后**吊销该用户其它会话**（保留当前） |
| GET | `/api/auth/sessions` | 会话 | 列出本人会话（IP/UA/创建时间/过期） |
| DELETE | `/api/auth/sessions/{id}` | 会话 | 踢掉指定会话（**自助吊销** = 补上 P5） |
| GET | `/api/auth/users` | **owner** | 列出全部用户（D1 多用户） |
| POST | `/api/auth/users` | **owner** | 创建用户 `{username,password,role}`（role ∈ viewer/admin/owner） |
| PATCH | `/api/auth/users/{id}` | **owner** | 改角色 / 禁用账号 `{role,status}` |
| DELETE | `/api/auth/users/{id}` | **owner** | 删除用户（不能删自己） |
| POST | `/api/auth/users/{id}/reset-password` | **owner** | 管理员代重置密码 |

**RBAC（D1，已落地 `webui_auth.PERM_RULES`）**：`viewer` 只读 + 自助改密；`admin` 可批准/部署/代理等高危写；`owner` 额外独享用户管理。未登记路径的写操作 fail-closed 要求 `admin`。

`auth_mode` 取值（env `AF_WEBUI_TOKEN_MODE`）：
- `password_only`（**D4 默认**）：仅账号密码会话；旧令牌通道关闭
- `both`：会话 + 旧令牌并存（兼容过渡）
- `token_only`：回滚开关，仅旧令牌，密码子系统关闭（登录/注册端点返回 401）

### 5.4 安全不变量（**红线，逐条写进测试**）

| ID | 不变量 | 说明 |
|---|---|---|
| **I-1** | 密码绝不明文落盘/落日志 | 只存 pbkdf2 哈希；登录请求体禁止记进 trace/日志 |
| **I-2** | 三套令牌隔离不变 | 本次只改 WebUI；`/mcp*` 仍 401/200 原语义，`acp_` 不受影响 |
| **I-3** | 会话 Cookie：`HttpOnly; SameSite=Lax; Path=/` | 生产若走 https 加 `Secure`；**禁止** `SameSite=None` |
| **I-4** | 登出 = 服务端删除 | 不能只清前端 localStorage |
| **I-5** | 登录失败**恒定响应** | 不区分「用户不存在」/「密码错」，防用户枚举 |
| **I-6** | 失败计数 + 锁定 | 同用户名 5 次 / 同 IP 20 次（15 分钟窗口）→ 锁 15~30 分钟；成功清零 |
| **I-7** | S-4 不退化 | 零账号 + 零令牌 + 远程 → 403；伪造 XFF 仍 403 |
| **I-8** | 常量时间比对 | 密码与会话均用 `hmac.compare_digest`（沿用 S-1） |
| **I-9** | **CSRF 防护（本次新增风险面）** | 见下 |
| **I-10** | 审计留痕 | 登录成功/失败、登出、改密、踢会话全部落审计（现有 `AuditStore`） |
| **I-11** | P-2 不破 | 新增文件不得含默认密码/真实 IP（`<NAS_IP>` 占位） |

#### I-9 CSRF：这是从「令牌」转「Cookie」的**新增攻击面**，必须单独处理

令牌模式下，跨站请求拿不到令牌（不在 Cookie 里），天然免疫 CSRF。
改成 Cookie 会话后，**浏览器会自动带上 Cookie**，攻击者页面可以静默调用 `/api/pending/{id}/approve`——批准闸是 WebUI 最高危动作。

三层防御：
1. **Cookie `SameSite=Lax`**（默认）：跨站 POST 不携带 Cookie，拦住绝大多数场景。
2. **自定义头校验（纵深）**：非 GET/HEAD 的 `/api` 请求，若**走 Cookie 认证**，则必须带 `X-Requested-With: autoflow`。跨站脚本无法自定义头（会触发 CORS 预检被拒）。前端 `api()` 统一加。
3. **豁免 Bearer / `?token=` 认证的写请求**：这些不是「环境权限」（ambient authority），脚本/CI 用旧令牌继续可用，**不破兼容**。

### 5.5 前端改造（`webui/static/`）

| 位置 | 改动 |
|---|---|
| `app.js:11-28` `api()` | 401 → 不再报「点右上角🔑」，改为**弹登录模态**（密码错可重试）；非 GET 统一加 `X-Requested-With: autoflow` |
| 新增 `showLoginModal()` | 用户名 / 密码 / 记住我 / 错误提示 |
| 新增 `showBootstrapModal()` | 初始化管理员（引导码 + 新密码 + 确认） |
| `app.js:1277` `#tokenBtn` | 🔑 改为「已登录 owner ▾」下拉：**改密码 / 会话管理 / 退出登录**；旧令牌设置折进「高级（兼容通道）」折叠区 |
| `index.html:44` | 顶栏按钮语义改掉 |
| 启动流程 | 打开先 `GET /api/auth/state` → 决定渲染 登录 / 初始化 / 主界面 |
| PWA / SW | `sw.js` 已声明不缓存 `/api`，无需改；但登录态过期后离线 shell 会白屏 → 401 时清壳重加载 |

---

## 6. 兼容与迁移路径（**三段式，不做一刀切**）

| 阶段 | 开关 | 行为 | 风险 |
|---|---|---|---|
| **M1 并存（可选）** | `AF_WEBUI_TOKEN_MODE=both`（非默认） | 会话 + 旧令牌**都**能用；WebUI 提示「建议改用登录」 | 低：旧部署升级无感，出问题可用旧令牌进；**D4 已决定默认不开，仅在确认有脚本依赖时临时开启** |
| **M2 默认切换** | 新装默认 `password_only`；升级上来的保持 `both` | 新用户只见登录页 | 低 |
| **M3 可选关闭** | `AF_WEBUI_TOKEN_MODE=off` | 令牌通道彻底关闭 | 需先确认无脚本依赖 |
| **回滚开关** | `AF_WEBUI_TOKEN_MODE=token_only` | 完全退回今天的行为 | 随时可用 |

**已部署的 NAS 怎么迁（D4 默认 `password_only`）**：全新部署默认关闭旧令牌，首次打开 WebUI 即走「注册管理员」向导（零账号本地回环完成）。若现有 NAS 已配 `AF_WEBUI_TOKEN` 且不想立刻切，可临时设 `AF_WEBUI_TOKEN_MODE=both` 并存过渡，确认无脚本依赖后再改回 `password_only`。回滚：设 `token_only` 立即退回今天的行为，无需还原代码。

---

## 7. 测试守卫清单（新增 `tests/test_webui_password_login.py`）

沿用现有套路（进程内 fake / `Starlette TestClient` / `TmpCfgMixin`）：

1. 未登录远程访问 `/api/*` → **401**（非 403），且带 `auth_required`
2. 正确密码登录 → `Set-Cookie` 含 `HttpOnly`、`SameSite=Lax`；后续 `/api` 200
3. 错误密码 → 401，且响应体与「用户不存在」**完全一致**（I-5）
4. 伪造 / 随机 `af_session` → 401
5. 登出 → 旧会话**立即**失效（I-4，服务端删而非仅清前端）
6. 并存期旧 `AF_WEBUI_TOKEN` 仍可通过（M1 兼容，不破 NAS）
7. `token_only` 模式 → 会话 Cookie 不被接受（回滚开关有效）
8. 引导码：**单次有效** / 15 分钟过期 / 5 次尝试后锁定 / 用后文件删除
9. 配了旧令牌时，引导端点**必须同时**校验引导码 + 旧令牌（防窗口期抢注）
10. 密码哈希：库里**无明文**、两用户同密码哈希不同（盐随机）、`verify` 用 `compare_digest`
11. CSRF：跨 `Origin` 的 Cookie 认证 POST `/api/pending/{id}/approve` 被拒；带 `X-Requested-With` 放行
12. CSRF 豁免：Bearer 旧令牌的写请求**不被** CSRF 拦（兼容不破）
13. 失败锁定：连错 5 次 → 第 6 次即使密码对也拒（I-6）
14. S-4 不退化：零账号零令牌远程 → 403；伪造 XFF 仍 403（I-7）
15. 三套隔离：`/mcp` 无 `af_` 身份码仍 401，不因 WebUI 登录而放行（I-2）
16. P-2：新增文件不含默认密码 / 内网 IP（`test_no_secrets` 自动覆盖）

---

## 8. 分阶段实施计划

| 阶段 | 内容 | 交付物 | 依赖 |
|---|---|---|---|
| **W1** | 本计划冻结 | `docs/PLAN_webui_password_login.md` | **用户拍板** |
| **W2** | 后端：`webui_auth.py`（哈希/用户/会话/限流/引导码）+ `guarded()` 三态改造 + 7 个 API + CLI 逃生口 | 模块 + 路由 | W1 |
| **W3** | 前端：登录模态 / 初始化页 / 401 拦截 / 顶栏账号菜单 / 令牌降级到高级区 | `app.js` + `index.html` | W2 |
| **W4** | 测试守卫（第 7 节 16 项）+ 本地全量回归 | `tests/test_webui_password_login.py` | W2/W3 |
| **W5** | 部署 NAS：md5 预检 → 备份 → 仅 scp `src/autoflow_gateway/` → py_compile + 重启 → 运行时内省 | 部署记录 | W4 |
| **W6** | 兼容期观察 1~2 周（旧令牌并存），确认无脚本依赖后再议 M3 | 观察结论 | W5 |

**回滚预案**：代码默认 `both`，出问题改 env `AF_WEBUI_TOKEN_MODE=token_only` 立即退回今天的行为，无需还原代码；数据库只加表不加列，旧代码读到新表直接忽略。

---

## 9. 决策点（已全部拍板，见 §0.1 落地表）

| # | 决策 | **用户拍板结果（2026-08-30）** |
|---|---|---|
| **D1** | 单用户 vs 多用户 | **一步到位多用户**（viewer/admin/owner RBAC，§5.3/§5.4） |
| **D2** | 会话时长 | **12h 滑动 + 7d 记住我硬上限**（已落地常量） |
| **D3** | 引导码 | **不要引导码**，改为「初次打开网关引导注册」（§4.4） |
| **D4** | 旧令牌关闭时机 | **直接关**：默认 `password_only`，旧令牌通道默认关闭（§5.3 / §6） |
| **D5** | 记住我 | **要**（已落地） |

> 另：GitHub 仓库已公开（仅无人访问）；账号登陆改造完成后，可能需要在 NAS 从头干净部署一遍验证，确认无问题再做推广。

---

## 10. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| Cookie 化引入 CSRF | **高** | I-9 三层防御（SameSite=Lax + 自定义头 + Bearer 豁免），守卫 #11/#12 |
| 引导码窗口期被抢注 | 中 | 15 分钟过期 + 5 次锁定 + 有旧令牌时双因子 + CLI 逃生口 |
| 把自己锁在门外 | 中 | CLI `webui-passwd` 逃生口（服务器侧，不经网络）+ 旧令牌并存兜底 |
| 600k 次 PBKDF2 拖慢登录 | 低 | 单次约 0.2~0.4s，登录是低频动作；迭代数可调 |
| 前端改动面大（app.js 15 万字符） | 中 | 只动 `api()`、顶栏、新增两个模态；先备份 `app.js`，W3 前打 `.bak-<date>` |
| PWA 离线壳 + 会话过期白屏 | 低 | 401 时清 shell 缓存并重载，SW 策略不变 |
