#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — WebUI 后端（人类控制面）

承载：确认闸(approve/reject) + 场景提案(部署到NR/拒绝) + agent 身份管理(生成码/吊销) + 用户笔记。
响应式前端在 webui/static/（手机/平板/电脑自适应）。

安全：WebUI 是网关 owner 的控制面。若设了 AF_WEBUI_TOKEN，则 /api 必须带
`?token=` 或 `Authorization: Bearer <token>`；否则默认开放（仅本机）并打告警。
MCP 身份闸另由 mcp_server 的 ASGI 中间件独立强制（两者互不替代）。
"""
import os
import asyncio
import secrets
import hmac
import re
import tempfile
from typing import Optional, Dict

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, FileResponse
from starlette.staticfiles import StaticFiles
from starlette.requests import Request

from .config import get_config, is_task_pool_enabled, is_raw_node_escape_enabled, is_submit_gate_enabled, set_feature_flag, get_deploy_policy, set_deploy_policy, load_feature_flags, is_acp_enabled, load_llm_config, save_llm_config
from .gateway import Gateway
from .identity import AgentStore, AcpTokenStore
from . import webui_auth as _wa
from .webui_auth import WebUIAuth
# llm_client 含 `import httpx` —— 惰性导入（见 build_webui_asgi 启动处与 autoflow_ask_llm），
# 避免 httpx 未安装时网关启动期 ImportError 全功能宕机（ACP 属小众，不应绑架 boot）。
from .proposals import ProposalStore
from .notes import NoteStore
from .device_guard import DeviceGuardStore
from .audit import AuditStore
from .subflows import (
    introspect_nr_subflow, validate_subflow_registration,
    introspect_nr_tab, _parse_tab_url, _KEY_RE,
)
from .api_config_store import ApiConfigStore
from .api_specs import (
    SYSTEM_PLACEHOLDERS,
    ApiSpec,
    get_api_spec,
    resolve_system_placeholders,
)
from . import connections

# 人工抽查（spotcheck）与评测工作台（eval）已从网关剥离，迁移至 archive/agent-loop-migration/（C4）。


def _js(data, status=200):
    return JSONResponse(data, status_code=status)


def _resolve_webui_token(cfg) -> Optional[str]:
    """WebUI 访问令牌：优先环境变量 AF_WEBUI_TOKEN；否则回退到 data_dir/.webui_token 文件。

    回退原因：网关以 LocalSystem 服务运行，用户会话的环境变量（Machine/User）无法注入，
    nssm 配置又需管理员权限。把令牌落盘到 data_dir 是最稳的免管理员方式（LocalSystem 可读本地文件）。
    环境变量一旦设置则覆盖文件。"""
    env_tok = os.environ.get("AF_WEBUI_TOKEN")
    if env_tok:
        return env_tok
    try:
        p = os.path.join(cfg.data_dir, ".webui_token")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
                return t or None
    except Exception:
        pass
    return None


def _bootstrap_webui_token(cfg) -> Optional[str]:
    """首跑令牌引导（opt-in：仅当 AF_WEBUI_TOKEN_AUTO=1）。

    bare 部署未设 AF_WEBUI_TOKEN 且无 data_dir/.webui_token 文件时，自动生成随机令牌落盘，
    避免外部 IP 被 guarded() 拦 403 而进不去 WebUI 填连接设置。

    - 已设 AF_WEBUI_TOKEN 或文件已存在 → 不生成，返回 None（幂等）。
    - 生成的令牌写入 data_dir/.webui_token（docker 下即 ./data/.webui_token，持久化、重启不失效），
      并打印到 stdout 供 `docker compose logs` 查看。guarded() 每次请求实时解析，写文件即时生效。
    - 本地开发不设 AF_WEBUI_TOKEN_AUTO 时，维持原行为（无令牌则仅本机开放、外部 403）。
    """
    if os.environ.get("AF_WEBUI_TOKEN_AUTO", "").lower() not in ("1", "true", "yes"):
        return None
    if _resolve_webui_token(cfg) is not None:
        return None
    tok = secrets.token_urlsafe(24)
    try:
        d = cfg.data_dir
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".webui_token")
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".webui_token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(tok)
            os.replace(tmp, p)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception as e:
        print(f"[WebUI] 警告：生成 .webui_token 失败（{e}），WebUI 仅本机开放。", flush=True)
        return None
    print(f"[WebUI] 首次启动已生成访问令牌 -> {p}", flush=True)
    print(f"[WebUI] 浏览器访问 WebUI 请携带 ?token={tok}（或登录页粘贴）。令牌已写入文件，重启不失效。", flush=True)
    return tok


def _cookie_token(headers: dict) -> Optional[str]:
    """从 Cookie 头取 WebUI 令牌（前端持久化用，Edge 对第一方 cookie 保留比 localStorage 鲁棒）。"""
    raw = headers.get(b"cookie")
    if not raw:
        return None
    from urllib.parse import unquote
    for part in raw.decode().split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        if k == "af_ui_token":
            return unquote(v)
    return None


def _client_host(scope: dict) -> str:
    """取请求端 IP（用于 S-4 止血：未配置 WebUI token 时仅放行本机/回环，远程一律拒）。

    策略：直连接 Peer 优先；仅当 Peer 为回环（本机/可信代理）时才采信 X-Forwarded-For
    还原真实客户端。这样：① 远端攻击者直连（Peer 为公网 IP）无法用伪造 XFF 伪装回环；
    ② 本机反向代理（Peer=127.0.0.1）转发的真实远程客户端会被 XFF 识别并拦截。
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        host = str(client[0])
        # 仅 Peer 为回环才信任 XFF（代理在本地）；直连公网 IP 直接判定远程，防 XFF 伪造绕过
        if host in _LOOPBACK_HOSTS:
            headers = dict(scope.get("headers", []))
            xff = headers.get(b"x-forwarded-for")
            if xff:
                return xff.decode().split(",")[0].strip()
        return host
    # ✦S-4 加固：Peer 缺失（client=None 等）时未知来源，绝不采信 X-Forwarded-For，
    # 防止在 client 未填充的 ASGI 配置下伪造 XFF: 127.0.0.1 绕过 403。未知 Peer 一律判远端（拒）。
    return ""


# 含 Starlette TestClient 哨兵 "testclient"：进程内测试客户端以 ("testclient", port) 标识自身，
# 代表本机本地请求，等同回环；该值仅测试出现，生产中 client[0] 恒为真实 IP，故不削弱安全。
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient")


def _is_loopback(scope: dict) -> bool:
    """请求是否来自本机/回环（S-4 止血判定用）。"""
    return _client_host(scope) in _LOOPBACK_HOSTS


def build_webui_asgi(cfg=None, gateway: Optional[Gateway] = None):
    cfg = cfg or get_config()
    _bootstrap_webui_token(cfg)   # 首跑无令牌时自动生成，确保能从外部 IP 进 WebUI 填连接设置
    gw = gateway or Gateway(cfg)
    agents = AgentStore(cfg)
    acp_tokens = AcpTokenStore(cfg)  # ACP：kind=acp 令牌（acp_ 前缀），与 af_/WebUI JWT 三套隔离
    auth = WebUIAuth(cfg)            # WebUI 账号密码登录与会话（与 af_/acp_ 三套隔离）
    # LLM 钩子：惰性建路由单例（按 env 配置多后端），未配置也不崩。
    # 惰性导入 llm_client：httpx 缺失时仅警告，不致命（ACP 关闭/未装 httpx 网关照常启动）。
    try:
        from .llm_client import get_llm_router
        get_llm_router(cfg)
    except Exception as e:  # ImportError(httpx 缺失) / 其它 —— 仅 LLM 不可用，不影响网关其余
        import logging as _log
        _log.getLogger("webui").warning("LLM 钩子未启用（llm_client 导入失败，可能缺 httpx）：%s", e)
    # LLM 配置：若 data/<env>/llm_config.json 存在且有后端，则覆盖路由（WebUI 设置免重启生效）。
    try:
        from .llm_client import reconfigure_router_from_llm_config
        reconfigure_router_from_llm_config(cfg)
    except Exception as e:
        import logging as _log
        _log.getLogger("webui").warning("LLM 路由未从 llm_config.json 覆盖（沿用 env 配置）：%s", e)
    proposals = ProposalStore(cfg)
    notes = NoteStore(cfg)
    device_guard = DeviceGuardStore(cfg)
    audit_store = AuditStore(gw)
    api_configs = ApiConfigStore(cfg)  # A0/A2：Link API 运行时配置（api_configs 表）
    static_dir = os.path.join(os.path.dirname(__file__), "webui", "static")
    index_path = os.path.join(static_dir, "index.html")

    # ── 助手：解析 JSON body ──
    async def _body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    # ─────────────────────────────────────────────────────────────
    # 账号登录 / 会话 / 用户管理（WebUI 专用，与 af_ / acp_ 三套隔离）
    # ─────────────────────────────────────────────────────────────
    def _ua(request: Request) -> str:
        for k, v in request.scope.get("headers", []):
            if k.decode("latin-1").lower() == "user-agent":
                return v.decode("latin-1", "ignore")[:300]
        return ""

    def _public_user(u: dict) -> dict:
        """对外脱敏：绝不回传 pw_hash（I-1）。"""
        return {k: u.get(k) for k in (
            "user_id", "username", "role", "status", "must_change",
            "created_at", "last_login_at", "notes", "locked")}

    async def auth_state(request: Request):
        """免鉴权探测：前端据此决定渲染 登录 / 注册向导 / 主界面。"""
        sess = auth.resolve_session(_wa.session_id_from_scope(request.scope))
        return _js({
            "ok": True,
            "auth_mode": auth.auth_mode,
            "initialized": auth.has_users(),
            "registration_open": auth.registration_open(),
            "logged_in": bool(sess),
            "user": (_public_user(sess) if sess else None),
            "csrf_header": _wa.CSRF_HEADER,
            "csrf_value": _wa.CSRF_VALUE,
            "min_password_len": _wa.MIN_PASSWORD_LEN,
            "roles": list(_wa.ROLES),
        })

    async def auth_register(request: Request):
        """首开注册向导：仅在「零账号 + AF_WEBUI_OPEN_REGISTER≠0」时开放，建号后永久关闭。"""
        if auth.auth_mode == "token_only":
            # 回滚模式：密码子系统整体关闭，只认旧令牌。
            # 返回 401（而非 403）：这是鉴权探测端点，401 = "请改用令牌认证"。
            return _js({"ok": False, "error": "当前为 token_only 模式，已禁用账号密码注册，请使用访问令牌"}, 401)
        if not auth.registration_open():
            return _js({"ok": False, "error": "注册已关闭：系统已存在账号"}, 403)
        b = await _body(request)
        username = str(b.get("username") or "").strip()
        password = b.get("password") or ""
        if b.get("confirm") is not None and b["confirm"] != password:
            return _js({"ok": False, "error": "两次输入的密码不一致"}, 400)
        err = _wa.password_policy_error(password, username)
        if err:
            return _js({"ok": False, "error": err}, 400)
        try:
            u = auth.create_user(username, password, role="owner")
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        sid, max_age = auth.create_session(
            u["user_id"], remember=True, ip=_client_host(request.scope), user_agent=_ua(request))
        auth.audit("register", username=u["username"], ip=_client_host(request.scope), ok=True)
        return JSONResponse(
            {"ok": True, "user": _public_user(u)}, status_code=201,
            headers={"Set-Cookie": _wa.build_session_cookie(sid, max_age, auth.secure_cookie)},
        )

    async def auth_login(request: Request):
        if auth.auth_mode == "token_only":
            # 回滚模式：密码子系统整体关闭，只认旧令牌。
            # 返回 401（而非 403）：这是鉴权探测端点，401 = "请改用令牌认证"。
            return _js({"ok": False, "error": "当前为 token_only 模式，已禁用账号密码登录，请使用访问令牌"}, 401)
        b = await _body(request)
        ip = _client_host(request.scope)
        user, reason = auth.authenticate(
            str(b.get("username") or "").strip(), b.get("password") or "", ip=ip)
        if user is None:
            # I-5：用户不存在 / 密码错 / 已禁用 一律同一句，防用户枚举
            msg = {"locked": "账号已被临时锁定，请稍后再试",
                   "ip_locked": "尝试过于频繁，请稍后再试"}.get(reason, "用户名或密码错误")
            return _js({"ok": False, "error": msg}, 401)
        sid, max_age = auth.create_session(
            user["user_id"], remember=bool(b.get("remember")), ip=ip, user_agent=_ua(request))
        return JSONResponse(
            {"ok": True, "user": _public_user(user)},
            headers={"Set-Cookie": _wa.build_session_cookie(sid, max_age, auth.secure_cookie)},
        )

    async def auth_logout(request: Request):
        """免鉴权：会话不存在也返回成功（前端清态即可）。I-4：服务端删会话。"""
        sid = _wa.session_id_from_scope(request.scope)
        if sid:
            s = auth.resolve_session(sid)
            auth.revoke_session(sid)
            if s:
                auth.audit("logout", username=s["username"],
                           ip=_client_host(request.scope), ok=True)
        return JSONResponse(
            {"ok": True},
            headers={"Set-Cookie": _wa.clear_session_cookie(auth.secure_cookie)},
        )

    async def auth_me(request: Request):
        a = request.scope.get("af_auth") or {}
        u = auth.get_user(a.get("user_id") or "")
        if not u:
            return _js({"ok": False, "error": "unauthorized"}, 401)
        return _js({"ok": True, "user": _public_user(u)})

    async def auth_change_password(request: Request):
        a = request.scope.get("af_auth") or {}
        u = auth.get_user(a.get("user_id") or "")
        if not u:
            return _js({"ok": False, "error": "unauthorized"}, 401)
        b = await _body(request)
        new = b.get("new_password") or ""
        if b.get("confirm") is not None and b["confirm"] != new:
            return _js({"ok": False, "error": "两次输入的新密码不一致"}, 400)
        # 必须校验旧密码（防止借用遗留会话改密）
        ip = _client_host(request.scope)
        ok_user, _ = auth.authenticate(u["username"], b.get("old_password") or "", ip=ip)
        if not ok_user:
            return _js({"ok": False, "error": "原密码不正确"}, 401)
        try:
            auth.set_password(u["user_id"], new, username=u["username"])
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        # 改密后踢掉该用户其它会话，只保留当前这个（不影响自己当前操作）
        cur = _wa.session_hash(_wa.session_id_from_scope(request.scope))
        n = auth.revoke_user_sessions(u["user_id"], keep_hash=cur)
        auth.audit("change_password", username=u["username"], ip=ip, ok=True,
                   note=f"revoked_other_sessions={n}")
        return _js({"ok": True, "revoked_other_sessions": n})

    async def auth_sessions_list(request: Request):
        a = request.scope.get("af_auth") or {}
        role = a.get("role")
        # owner 看全员的；其余只看自己的（防越权）
        rows = auth.list_sessions(None if role == "owner" else a.get("user_id"))
        return _js({"ok": True, "sessions": rows})

    async def auth_sessions_delete(request: Request):
        a = request.scope.get("af_auth") or {}
        role = a.get("role")
        owner_scope = None if role == "owner" else a.get("user_id")
        ok = auth.revoke_session_by_id(request.path_params["id"], user_id=owner_scope)
        auth.audit("session_revoke", username=a.get("username") or "", ok=bool(ok),
                   note=request.path_params["id"])
        return _js({"ok": ok}, 200 if ok else 404)

    # ── 用户管理（仅 owner，见 PERM_RULES）──
    async def auth_users_list(request: Request):
        return _js({"ok": True, "users": [_public_user(u) for u in auth.list_users()]})

    async def auth_users_create(request: Request):
        b = await _body(request)
        try:
            u = auth.create_user(
                b.get("username") or "", b.get("password") or "",
                role=b.get("role") or "viewer", notes=b.get("notes") or "",
                must_change=1 if b.get("must_change") else 0)
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        auth.audit("user_create", username=u["username"], ok=True, note=u["role"])
        return _js({"ok": True, "user": _public_user(u)}, 201)

    async def auth_users_update(request: Request):
        a = request.scope.get("af_auth") or {}
        tid = request.path_params["id"]
        if tid == a.get("user_id"):
            # 防自锁：不能改自己的角色/状态，否则可能把系统唯一的 owner 弄没
            return _js({"ok": False, "error": "不能修改自己的角色或状态"}, 400)
        b = await _body(request)
        role = b.get("role")
        if role is not None and role == "viewer":
            # 不允许把最后一个 owner 降级
            owners = [u for u in auth.list_users() if u["role"] == "owner"]
            tgt = auth.get_user(tid)
            if tgt and tgt["role"] == "owner" and len(owners) <= 1:
                return _js({"ok": False, "error": "系统至少需要保留一个 owner"}, 400)
        try:
            ok = auth.update_user(tid, role=role, status=b.get("status"),
                                  notes=b.get("notes"), must_change=b.get("must_change"))
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        auth.audit("user_update", username=a.get("username") or "", ok=bool(ok), note=tid)
        return _js({"ok": ok}, 200 if ok else 404)

    async def auth_users_delete(request: Request):
        a = request.scope.get("af_auth") or {}
        tid = request.path_params["id"]
        if tid == a.get("user_id"):
            return _js({"ok": False, "error": "不能删除自己"}, 400)
        tgt = auth.get_user(tid)
        if tgt and tgt["role"] == "owner":
            owners = [u for u in auth.list_users() if u["role"] == "owner"]
            if len(owners) <= 1:
                return _js({"ok": False, "error": "系统至少需要保留一个 owner"}, 400)
        ok = auth.delete_user(tid)
        auth.audit("user_delete", username=a.get("username") or "", ok=bool(ok), note=tid)
        return _js({"ok": ok}, 200 if ok else 404)

    async def auth_users_reset_password(request: Request):
        """owner 给他人重置密码 → 强制下次登录改密（must_change=1）。"""
        a = request.scope.get("af_auth") or {}
        tid = request.path_params["id"]
        tgt = auth.get_user(tid)
        if not tgt:
            return _js({"ok": False, "error": "用户不存在"}, 404)
        b = await _body(request)
        new = b.get("new_password") or ""
        try:
            auth.set_password(tid, new, username=tgt["username"])
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        auth.update_user(tid, must_change=1)
        auth.revoke_user_sessions(tid)
        auth.audit("user_reset_password", username=a.get("username") or "", ok=True,
                   note=tgt["username"])
        return _js({"ok": True, "must_change": True})

    # ── 健康检查 / 配置 ──
    async def health(request: Request):
        return _js({"ok": True, "env": cfg.env, "service": "autoflow-webui"})

    # ── 受控自更新（方案 C）：只读检查 + 触发更新 ──
    async def update_check_endpoint(request: Request):
        """只读：当前提交 / 可用目标 / 是否可更新。仅 owner。"""
        try:
            from . import self_update as _su
            return _js(_su.update_check())
        except Exception as e:
            return _js({"ok": False, "error": f"更新检查失败: {e}"}, 500)

    async def self_update_endpoint(request: Request):
        """触发受控自更新（备份→fetch→checkout→py_compile→重启）。仅 owner。"""
        b = await _body(request)
        ref = (b.get("ref") or "").strip() or None
        mirror = (b.get("mirror") or "").strip() or None
        try:
            from . import self_update as _su
            res = _su.perform_update(ref=ref, mirror=mirror)
        except Exception as e:
            return _js({"ok": False, "error": f"更新失败: {e}"}, 500)
        return _js(res, status=200 if res.get("ok") else 500)

    async def config_view(request: Request):
        from . import tab_organizer as tab_org
        return _js({
            "env": cfg.env,
            "nr_url": cfg.nr_url,
            "hass_server": cfg.hass_server,
            "mcp": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}",
            "mcp_white": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_white_path}",
            "mcp_admin": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_admin_path}",
            "blast_radius_max_flows": cfg.blast_radius_max_flows,
            "elevated_domains": sorted(cfg.elevated_domains),
            "safe_domains": sorted(cfg.safe_domains),
            "task_pool_enabled": is_task_pool_enabled(cfg),
            "raw_node_escape_enabled": is_raw_node_escape_enabled(cfg),
            "deploy_policy": get_deploy_policy(cfg),
            "selfheal_budget": load_feature_flags(cfg).get("selfheal_budget", 3),
            "tab_org_mode": tab_org.get_tab_org_mode(),
        })

    async def settings_update(request: Request):
        """运行时开关：DSL 验证任务池 / 原生节点逃逸 / 部署策略 / Tab组织模式（免重启落盘）。"""
        b = await _body(request)
        tp = b.get("task_pool_enabled")
        if tp is not None:
            if not isinstance(tp, bool):
                return _js({"ok": False, "error": "task_pool_enabled 必须是布尔值"}, 400)
            set_feature_flag(cfg, "task_pool_enabled", tp)
        tom = b.get("tab_org_mode")
        if tom is not None:
            if tom not in ("per_flow", "single_tab"):
                return _js({"ok": False, "error": "tab_org_mode 必须是 per_flow 或 single_tab"}, 400)
            set_feature_flag(cfg, "tab_org_mode", tom)
        rn = b.get("raw_node_escape_enabled")
        if rn is not None:
            if not isinstance(rn, bool):
                return _js({"ok": False, "error": "raw_node_escape_enabled 必须是布尔值"}, 400)
            set_feature_flag(cfg, "raw_node_escape_enabled", rn)
        sg = b.get("submit_run_gate")
        if sg is not None:
            if not isinstance(sg, bool):
                return _js({"ok": False, "error": "submit_run_gate 必须是布尔值"}, 400)
            set_feature_flag(cfg, "submit_run_gate", sg)
        dp = b.get("deploy_policy")
        if dp is not None:
            if dp not in ("review_all", "compiler_auto"):
                return _js({"ok": False, "error": "deploy_policy 必须是 review_all 或 compiler_auto"}, 400)
            try:
                set_deploy_policy(cfg, dp)
            except ValueError as e:
                return _js({"ok": False, "error": str(e)}, 400)
        # 自愈闭环：自愈重试次数（0=禁用自主重试；1~20=单 (agent, flow) 滑动窗口内最多自主重试）
        sb = b.get("selfheal_budget")
        if sb is not None:
            try:
                sb = int(sb)
            except (TypeError, ValueError):
                return _js({"ok": False, "error": "selfheal_budget 必须是整数"}, 400)
            if sb < 0 or sb > 20:
                return _js({"ok": False, "error": "selfheal_budget 必须在 0~20 之间（0=禁用自主重试）"}, 400)
            set_feature_flag(cfg, "selfheal_budget", sb)
        return _js({"ok": True,
                    "task_pool_enabled": is_task_pool_enabled(cfg),
                    "raw_node_escape_enabled": is_raw_node_escape_enabled(cfg),
                    "submit_run_gate": is_submit_gate_enabled(cfg),
                    "deploy_policy": get_deploy_policy(cfg),
                    "selfheal_budget": load_feature_flags(cfg).get("selfheal_budget", 3)})

    # ── Node-RED tab 列表（用于授权码/部署时的目标 tab 选择器）──
    async def nr_tabs(request: Request):
        """返回 Node-RED 中所有 tab 列表（id, label, node_count）。"""
        try:
            flows = gw.nr.list_flows()
            if isinstance(flows, dict):
                flows = flows.get("flows", [])
            flows = flows or []
            # Node-RED flows 是扁平结构：tab 节点不含 nodes 数组，
            # 普通节点通过 z 字段指向所属 tab id。需要按 z 统计。
            tab_map = {}
            node_count_by_tab = {}
            for f in flows:
                if not isinstance(f, dict):
                    continue
                ftype = f.get("type")
                if ftype in (None, "", "tab"):
                    tid = f.get("id", "")
                    if tid:
                        tab_map[tid] = {
                            "id": tid,
                            "label": f.get("label") or tid or "(未命名)",
                            "node_count": 0,
                        }
                        node_count_by_tab[tid] = 0
                elif ftype != "subflow":
                    # 普通节点，按 z 统计
                    z = f.get("z")
                    if z and z in node_count_by_tab:
                        node_count_by_tab[z] += 1
            # 回填节点数
            tabs = []
            for tid, t in tab_map.items():
                t["node_count"] = node_count_by_tab.get(tid, 0)
                tabs.append(t)
            # 按 label 排序
            tabs.sort(key=lambda x: x["label"])
            return _js({"ok": True, "tabs": tabs})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "tabs": []}, 500)

    # ── API Key 管理（v1.5.1）：Agent 身份认证 + 授权范围 ──
    def _api_key_store():
        from .api_keys import APIKeyStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return APIKeyStore(os.path.join(data_dir, "api_keys"))

    def _require_api_key(request, required_perm=None, target_tab=None):
        """认证中间件：从 Authorization header 提取 API Key 并验证。
        返回 (agent_info, None) 或 (None, error_response)。"""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None, _js({"ok": False, "error": "需要 Authorization: Bearer <api_key>"}, 401)
        key = auth[7:].strip()
        store = _api_key_store()
        result = store.validate_key(key, required_perm=required_perm, target_tab=target_tab)
        if not result.get("ok"):
            status = result.get("status", 401)
            return None, _js({"ok": False, "error": result.get("error", "认证失败")}, status)
        return result, None

    async def api_keys_list(request: Request):
        """列出所有 API Key。"""
        try:
            store = _api_key_store()
            keys = store.list_keys(include_revoked=True)
            return _js({"ok": True, "keys": keys})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def api_keys_create(request: Request):
        """创建新的 API Key。"""
        try:
            b = await _body(request)
            name = (b.get("name") or "").strip()
            agent_id = (b.get("agent_id") or "").strip()
            if not name:
                return _js({"ok": False, "error": "name 不能为空"}, 400)
            if not agent_id:
                return _js({"ok": False, "error": "agent_id 不能为空"}, 400)
            authorized_tabs = b.get("authorized_tabs") or []
            permissions = b.get("permissions") or ["read", "deploy"]
            expires_at = b.get("expires_at") or None

            store = _api_key_store()
            result = store.create_key(
                name=name, agent_id=agent_id,
                authorized_tabs=authorized_tabs if isinstance(authorized_tabs, list) else [],
                permissions=permissions if isinstance(permissions, list) else None,
                expires_at=expires_at,
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def api_keys_update(request: Request):
        """更新 API Key 授权范围。"""
        try:
            key_id = request.path_params.get("key_id", "")
            b = await _body(request)
            store = _api_key_store()
            result = store.update_key(
                key_id=key_id,
                name=b.get("name"),
                authorized_tabs=b.get("authorized_tabs"),
                permissions=b.get("permissions"),
            )
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def api_keys_revoke(request: Request):
        """吊销 API Key。"""
        try:
            key_id = request.path_params.get("key_id", "")
            store = _api_key_store()
            result = store.revoke_key(key_id)
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def api_keys_logs(request: Request):
        """获取 API Key 审计日志。"""
        try:
            store = _api_key_store()
            limit = int(request.query_params.get("limit", 100))
            logs = store.get_logs(limit=limit)
            return _js({"ok": True, "logs": logs})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    # ── 模板库（v1.5.2）：Flow 模板复用 ──
    def _template_store():
        from .templates import TemplateStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return TemplateStore(os.path.join(data_dir, "templates"))

    async def templates_list(request: Request):
        """列出模板。"""
        try:
            qp = request.query_params
            store = _template_store()
            templates = store.list_templates(
                category=qp.get("category") or None,
                keyword=qp.get("keyword") or None,
            )
            categories = store.list_categories()
            return _js({"ok": True, "templates": templates, "categories": categories})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_get(request: Request):
        """获取单个模板详情。"""
        try:
            template_id = request.path_params.get("template_id", "")
            store = _template_store()
            t = store.get_template(template_id)
            if not t:
                return _js({"ok": False, "error": "模板不存在"}, 404)
            return _js({"ok": True, "template": t})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_create(request: Request):
        """创建模板。"""
        try:
            b = await _body(request)
            name = (b.get("name") or "").strip()
            dsl = (b.get("dsl") or "").strip()
            if not name or not dsl:
                return _js({"ok": False, "error": "name 和 dsl 不能为空"}, 400)
            store = _template_store()
            result = store.create_template(
                name=name, dsl=dsl,
                description=b.get("description", ""),
                category=b.get("category", "未分类"),
                tags=b.get("tags") or [],
                variables=b.get("variables"),
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_update(request: Request):
        """更新模板。"""
        try:
            template_id = request.path_params.get("template_id", "")
            b = await _body(request)
            store = _template_store()
            result = store.update_template(template_id, **b)
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_delete(request: Request):
        """删除模板。"""
        try:
            template_id = request.path_params.get("template_id", "")
            store = _template_store()
            result = store.delete_template(template_id)
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_render(request: Request):
        """渲染模板，生成 DSL。"""
        try:
            template_id = request.path_params.get("template_id", "")
            b = await _body(request)
            store = _template_store()
            result = store.render_template(
                template_id,
                variables=b.get("variables") or {},
            )
            if not result.get("ok"):
                return _js(result, 400)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──
    async def core_version(request: Request):
        """网关版本 + 兼容性检查。"""
        try:
            version_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
            ver = "unknown"
            if os.path.exists(version_path):
                with open(version_path, "r", encoding="utf-8") as f:
                    ver = f.read().strip()
            return _js({"ok": True, "version": ver, "api_version": "v1",
                        "features": ["propose_dsl", "deploy_raw", "entities", "snapshots", "templates"]})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_health(request: Request):
        """网关健康检查。"""
        try:
            return _js({"ok": True, "status": "healthy", "uptime": getattr(gw, "_start_time", None)})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_propose_dsl(request: Request):
        """【★首选】提交 DSL，编译+闸门校验，返回提案。AutoFlow Pro 主入口。"""
        agent_info, err = _require_api_key(request, required_perm="deploy")
        if err:
            return err
        try:
            b = await _body(request)
            dsl = (b.get("dsl") or "").strip()
            if not dsl:
                return _js({"ok": False, "error": "dsl 不能为空"}, 400)
            agent_id = (b.get("agent_id") or "pro-agent").strip()
            expected = b.get("expected_postconditions") or []
            resolved = b.get("resolved_entities") or []
            deploy_token = (b.get("deploy_token") or "").strip() or None
            preview = bool(b.get("preview", False))

            result = gw.propose_dsl(
                dsl=dsl, agent_id=agent_id,
                expected_postconditions=expected if isinstance(expected, list) else None,
                resolved_entities=resolved if isinstance(resolved, list) else None,
                deploy_token=deploy_token,
            )
            # 附加 telemetry
            result["_telemetry"] = {
                "input_chars": len(dsl),
                "mode": "dsl",
                "agent_id": agent_id,
            }
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_deploy_proposal(request: Request):
        """部署已通过的提案到 NR。"""
        agent_info, err = _require_api_key(request, required_perm="deploy")
        if err:
            return err
        try:
            b = await _body(request)
            pid = (b.get("proposal_id") or b.get("id") or "").strip()
            if not pid:
                return _js({"ok": False, "error": "proposal_id 不能为空"}, 400)
            agent_id = (b.get("agent_id") or "pro-agent").strip()
            target = (b.get("target") or "prod").strip()
            target_tab = (b.get("target_tab") or "").strip() or None
            deploy_token = (b.get("deploy_token") or "").strip() or None

            result = gw.deploy_proposal(
                pid, agent_id=agent_id, target=target,
                target_flow_id=target_tab,
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_deploy_raw(request: Request):
        """【⚠️逃生舱】直接提交 raw Node-RED flow JSON。仅在 DSL 表达不了时使用。"""
        agent_info, err = _require_api_key(request, required_perm="modify")
        if err:
            return err
        try:
            b = await _body(request)
            flow_json = b.get("flow_json")
            if isinstance(flow_json, str):
                import json as _json
                flow_json = _json.loads(flow_json)
            if not flow_json:
                return _js({"ok": False, "error": "flow_json 不能为空"}, 400)
            agent_id = (b.get("agent_id") or "pro-agent").strip()
            label = (b.get("label") or "").strip() or None
            target = (b.get("target") or "staging").strip()

            result = gw.deploy_raw(
                flow_json=flow_json, agent_id=agent_id,
                label=label, target=target,
            )
            # 附加 DSL 转换建议（引导 Agent 用 DSL）
            result["_warning"] = "deploy-raw 是逃生舱，输出冗长且无编译校验。建议改用 propose-dsl 以节省 token。"
            result["_telemetry"] = {
                "mode": "raw",
                "agent_id": agent_id,
            }
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_entities(request: Request):
        """实体目录查询（按域/区域/关键词过滤）。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:
            qp = request.query_params
            result = gw.list_entities(
                domain=qp.get("domain") or None,
                area=qp.get("area") or None,
                keyword=qp.get("keyword") or None,
                limit=int(qp.get("limit", 50)),
                offset=int(qp.get("offset", 0)),
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_resolve_entity(request: Request):
        """自然语言设备名 → entity_id 候选。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:
            qp = request.query_params
            name = (qp.get("name") or "").strip()
            if not name:
                return _js({"ok": False, "error": "name 参数不能为空"}, 400)
            result = gw.resolve_entity(
                name=name,
                area=qp.get("area") or None,
                top_n=int(qp.get("top_n", 5)),
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_snapshots(request: Request):
        """快照列表。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:
            from .snapshot_manager import SnapshotManager
            data_dir = getattr(cfg, "data_dir", None) or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
            mgr = SnapshotManager(os.path.join(data_dir, "snapshots"))
            snapshots = mgr.list_snapshots()
            return _js({"ok": True, "snapshots": snapshots})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def core_rollback(request: Request):
        """回滚到指定快照。"""
        agent_info, err = _require_api_key(request, required_perm="modify")
        if err:
            return err
        try:
            b = await _body(request)
            snapshot_id = (b.get("snapshot_id") or "").strip()
            if not snapshot_id:
                return _js({"ok": False, "error": "snapshot_id 不能为空"}, 400)
            from .snapshot_manager import SnapshotManager
            data_dir = getattr(cfg, "data_dir", None) or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
            mgr = SnapshotManager(os.path.join(data_dir, "snapshots"))
            result = mgr.restore(snapshot_id)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    # ── Tab 组织模式：迁移状态 + 执行迁移（P2）──
    async def tab_org_status(request: Request):
        """获取当前 Tab 组织模式状态和迁移统计。"""
        from . import tab_organizer as tab_org
        try:
            status = tab_org.get_migration_status(gw.state)
        except Exception as e:
            return _js({"ok": False, "error": f"获取状态失败: {e}",
                        "current_mode": tab_org.get_tab_org_mode(),
                        "per_flow_count": 0, "single_tab_count": 0,
                        "total_flows": 0, "warning": None}, 500)
        # P3: 单 tab 节点数预警
        warning = None
        if tab_org.is_single_tab_mode():
            try:
                af_tab = tab_org.get_single_tab(gw.nr)
                if af_tab:
                    node_count = len(af_tab.get("nodes", []))
                    threshold = int(os.environ.get("AF_SINGLE_TAB_WARN_THRESHOLD", "200"))
                    if node_count >= threshold:
                        warning = {
                            "level": "warning",
                            "message": f"AutoFlow tab 已有 {node_count} 个节点（阈值 {threshold}），建议分流或切换回独立 tab 模式",
                            "node_count": node_count,
                            "threshold": threshold,
                        }
            except Exception:
                pass
        status["warning"] = warning
        return _js(status)

    async def tab_org_migrate(request: Request):
        """执行 Tab 组织模式迁移。

        Body: {target_mode: "single_tab" | "per_flow"}
        """
        from . import tab_organizer as tab_org
        b = await _body(request)
        target_mode = b.get("target_mode")
        if target_mode not in ("single_tab", "per_flow"):
            return _js({"ok": False, "error": "target_mode 必须是 single_tab 或 per_flow"}, 400)

        try:
            if target_mode == "single_tab":
                result = tab_org.migrate_per_flow_to_single_tab(gw.nr, gw.state, allow_prod=True)
            else:
                result = tab_org.migrate_single_tab_to_per_flow(gw.nr, gw.state, allow_prod=True)
            return _js(result)
        except Exception as e:
            import traceback
            return _js({"ok": False, "error": f"迁移失败: {e}",
                        "traceback": traceback.format_exc()[-500:]}, 500)

    # ── 部署授权码（P4）：CRUD + 日志 + 快照/回滚 ──
    def _token_store():
        from .deploy_tokens import DeployTokenStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return DeployTokenStore(data_dir)

    def _snap_mgr():
        from .snapshot_manager import SnapshotManager
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return SnapshotManager(os.path.join(data_dir, "snapshots"))

    async def deploy_token_list(request: Request):
        """列出所有授权码（不含密钥）。"""
        include_revoked = request.query_params.get("include_revoked", "false").lower() == "true"
        tokens = _token_store().list_tokens(include_revoked=include_revoked)
        return _js({"ok": True, "tokens": tokens})

    async def deploy_token_create(request: Request):
        """创建授权码。支持 target_tab（单 tab）和 target_tabs（多 tab 列表）。"""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        if not name:
            return _js({"ok": False, "error": "名称不能为空"}, 400)

        # 解析目标 tab：支持单 tab 字符串和多 tab 列表
        # 兼容 URL 格式：从 http://host:1880/#flow/abc123 中提取 abc123
        def _normalize_tab(t):
            t = (t or "").strip()
            if not t:
                return None
            if "#flow/" in t:
                t = t.split("#flow/")[-1].split("/")[0].split("?")[0]
            return t or None

        target_tabs = []
        # 优先使用 target_tabs 列表
        raw_tabs = b.get("target_tabs")
        if isinstance(raw_tabs, list):
            for t in raw_tabs:
                nt = _normalize_tab(t)
                if nt:
                    target_tabs.append(nt)
        # 兼容旧字段 target_tab（单 tab 字符串）
        raw_single = b.get("target_tab")
        if raw_single:
            nt = _normalize_tab(raw_single)
            if nt and nt not in target_tabs:
                target_tabs.append(nt)

        # target_tab 用于向后兼容（取第一个）
        target_tab = target_tabs[0] if target_tabs else None

        try:
            token = _token_store().create_token(
                name=name,
                target_tab=target_tab,
                target_tabs=target_tabs if target_tabs else None,
                expires_in_hours=float(b.get("expires_in_hours", 4)),
                permissions=b.get("permissions") or ["deploy"],
                node_threshold=int(b.get("node_threshold", 50)),
                max_nodes=int(b.get("max_nodes", 500)),
                max_flows=int(b.get("max_flows", 20)),
                rate_limit_per_min=int(b.get("rate_limit_per_min", 10)),
                require_confirm_dangerous=bool(b.get("require_confirm_dangerous", True)),
                bound_agent=b.get("bound_agent") or None,
                created_by="webui",
            )
            return _js({"ok": True, "token": token})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 400)

    async def deploy_token_revoke(request: Request):
        """吊销授权码。"""
        token_id = request.path_params["id"]
        ok = _token_store().revoke_token(token_id, revoked_by="webui")
        if ok:
            return _js({"ok": True, "token_id": token_id, "revoked": True})
        return _js({"ok": False, "error": "授权码不存在"}, 404)

    async def deploy_token_logs(request: Request):
        """获取授权码使用日志。"""
        token_id = request.path_params["id"]
        limit = int(request.query_params.get("limit", 100))
        logs = _token_store().get_logs(token_id, limit=limit)
        return _js({"ok": True, "logs": logs})

    async def deploy_token_snapshots(request: Request):
        """获取授权码快照列表。"""
        token_id = request.path_params["id"]
        snapshot_type = request.query_params.get("type")
        snaps = _snap_mgr().list_snapshots(token_id, snapshot_type=snapshot_type)
        return _js({"ok": True, "snapshots": snaps})

    async def deploy_token_rollback(request: Request):
        """回滚到指定快照。"""
        token_id = request.path_params["id"]
        b = await _body(request)
        snapshot_id = b.get("snapshot_id")
        if not snapshot_id:
            return _js({"ok": False, "error": "snapshot_id 不能为空"}, 400)

        rollback_type = b.get("type", "full")
        if rollback_type == "full":
            result = _snap_mgr().full_rollback(token_id, snapshot_id, gw.nr, allow_prod=True)
        else:
            flow_ids = b.get("flow_ids", [])
            if not flow_ids:
                return _js({"ok": False, "error": "选择性回滚需要 flow_ids"}, 400)
            result = _snap_mgr().selective_rollback(
                token_id, snapshot_id, flow_ids, gw.nr,
                lambda: gw.state.get_flow_catalog(), allow_prod=True)
        return _js(result)

    async def deploy_token_diff(request: Request):
        """对比两个快照的差异。"""
        token_id = request.path_params["id"]
        snap1 = request.query_params.get("snapshot_1")
        snap2 = request.query_params.get("snapshot_2")
        if not snap1 or not snap2:
            return _js({"ok": False, "error": "需要 snapshot_1 和 snapshot_2 参数"}, 400)
        result = _snap_mgr().diff_snapshots(token_id, snap1, snap2)
        return _js(result)

    # ── Lab 沙盒部署（缺陷D修复：实现 /lab/* 路由）──
    def _lab_history_path():
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return os.path.join(data_dir, "lab_deploys.json")

    def _lab_load_history():
        p = _lab_history_path()
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _lab_save_history(entries):
        p = _lab_history_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    async def lab_validate(request: Request):
        """Lab: 验证 flow JSON（lint + schema 校验，不落档）。"""
        b = await _body(request)
        flow_json = b.get("flow_json")
        if not flow_json:
            return _js({"ok": False, "error": "flow_json 不能为空"}, 400)
        try:
            # 用 propose_raw 的 dry_run 模式做校验（不写 NR，不落档到待审批）
            result = gw.propose_raw(flow_json, agent_id="lab", label="lab-validate",
                                     run_gate=False, dry_run=True)
            if result.get("ok"):
                lint_issues = result.get("lint", [])
                errors = [v for v in lint_issues if v["level"] == "error"]
                warnings = [v for v in lint_issues if v["level"] == "warning"]
                return _js({
                    "ok": len(errors) == 0,
                    "node_count": result.get("node_count", 0),
                    "errors": errors,
                    "warnings": warnings,
                    "total_issues": len(lint_issues),
                    "validation": result.get("validation", []),
                })
            else:
                return _js({"ok": False, "error": result.get("error", "校验失败"),
                            "errors": [{"node_id": "", "message": result.get("error", "校验失败")}],
                            "warnings": [], "total_issues": 1})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "errors": [], "warnings": [], "total_issues": 0})

    async def lab_deploy(request: Request):
        """Lab: 直接部署 flow 到 NR（不需要提案审批）。"""
        b = await _body(request)
        flow_json = b.get("flow_json")
        agent_id = b.get("agent_id", "lab")
        label = b.get("label", f"lab-{agent_id}")
        target = b.get("target", "staging")
        target_flow_id = b.get("target_flow_id")
        if not flow_json:
            return _js({"ok": False, "error": "flow_json 不能为空"}, 400)

        try:
            # 先提案（落档），然后自动部署
            propose_result = gw.propose_raw(flow_json, agent_id=agent_id, label=label,
                                             target=target, run_gate=False)
            if not propose_result.get("ok"):
                return _js({"ok": False, "error": propose_result.get("error", "提案失败"),
                            "validation": propose_result.get("validation", [])})

            proposal_id = propose_result.get("proposal_id")
            # 自动部署
            deploy_result = gw.deploy_proposal(proposal_id, agent_id=agent_id,
                                                target_flow_id=target_flow_id, allow_prod=True)

            # 记录历史
            history = _lab_load_history()
            history.insert(0, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent": agent_id,
                "label": label,
                "status": "DEPLOY_OK" if deploy_result.get("ok") else "DEPLOY_FAIL",
                "flow_id": deploy_result.get("flow_id"),
                "node_count": deploy_result.get("node_count", 0),
                "detail": deploy_result.get("error", "") if not deploy_result.get("ok") else "ok",
                "errors": 0 if deploy_result.get("ok") else 1,
                "warnings": 0,
            })
            # 最多保留 50 条
            history = history[:50]
            _lab_save_history(history)

            if deploy_result.get("ok"):
                return _js({
                    "ok": True,
                    "flow_id": deploy_result.get("flow_id"),
                    "node_count": deploy_result.get("node_count", 0),
                    "deployed_at": datetime.now(timezone.utc).isoformat(),
                    "validation": propose_result.get("validation", []),
                })
            else:
                return _js({"ok": False, "error": deploy_result.get("error", "部署失败"),
                            "validation": propose_result.get("validation", [])})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "validation": []})

    async def lab_deploys(request: Request):
        """Lab: 获取部署历史。"""
        history = _lab_load_history()
        return _js({"ok": True, "deploys": history})

    # ── 诊断查看器（P4-C，只读）──
    async def diagnostics_view(request: Request):
        """聚合 env/health、各类计数、最近结构化 trace，供 WebUI 诊断 tab 只读展示。

        trace 来自 _slog 写入的进程内环形缓冲（重启即丢，仅反映最近活动）；
        诊断本质是「瞬时视角」。
        """
        proposals_all = proposals.list()
        by_status: Dict[str, int] = {}
        deployed_prop = 0
        for p in proposals_all:
            st = (p.status or "unknown")
            by_status[st] = by_status.get(st, 0) + 1
            if getattr(p, "deployed_flow_id", None):
                deployed_prop += 1
        try:
            agents_n = len(agents.list_agents())
        except Exception:
            agents_n = 0
        try:
            pending_n = len(await asyncio.to_thread(gw.list_pending))
        except Exception:
            pending_n = 0
        try:
            deployed_n = len(await asyncio.to_thread(gw.list_deployed))
        except Exception:
            deployed_n = 0
        try:
            traces = gw.get_recent_traces(50)
        except Exception:
            traces = []
        return _js({
            "env": cfg.env,
            "nr_url": cfg.nr_url,
            "hass_server": cfg.hass_server,
            "deploy_policy": get_deploy_policy(cfg),
            "mcp": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_path}",
            "mcp_white": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_white_path}",
            "mcp_admin": f"{cfg.mcp_host}:{cfg.mcp_port}{cfg.mcp_admin_path}",
            "counts": {
                "agents": agents_n,
                "pending_ops": pending_n,
                "deployed_flows": deployed_n,
                "proposals_total": len(proposals_all),
                "proposals_deployed": deployed_prop,
                "proposals_by_status": by_status,
            },
            "traces": traces,
            "golden_jobs": gw.list_golden_jobs(),
        })

    # ── 评测工作台（eval）已迁移至 archive/agent-loop-migration/（C4）；此处保留 agents 区 ──
    # ── agents ──
    async def list_agents(request: Request):
        return _js({"agents": [a.to_dict() for a in agents.list_agents()]})

    async def create_agent(request: Request):
        b = await _body(request)
        name = (b.get("name") or "").strip()
        if not name:
            return _js({"ok": False, "error": "name 必填"}, 400)
        tier = b.get("tier", "staging")
        try:
            agent, code = agents.create_agent(
                name, tier, b.get("notes", ""), mode=b.get("mode", "normal")
            )
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 409)
        return _js({"ok": True, "agent": agent.to_dict(include_code=True, code=code),
                    "warn": "身份码仅显示一次，请立即复制进 agent 的 MCP 配置"}, 201)

    async def revoke_agent(request: Request):
        aid = request.path_params["id"]
        ok = agents.revoke_agent(aid)
        return _js({"ok": ok}, 200 if ok else 404)

    async def update_agent(request: Request):
        """编辑已有 agent：name / tier / status / notes / mode（黑箱/白箱/双箱）。"""
        aid = request.path_params["id"]
        b = await _body(request)
        try:
            ok = agents.update_agent(
                aid,
                name=b.get("name"),
                tier=b.get("tier"),
                status=b.get("status"),
                notes=b.get("notes"),
                mode=b.get("mode"),
            )
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        if not ok:
            return _js({"ok": False, "error": "agent 不存在"}, 404)
        a = agents.get_agent(aid)
        return _js({"ok": True, "agent": a.to_dict()})

    async def regen_agent(request: Request):
        aid = request.path_params["id"]
        code = agents.regenerate_code(aid)
        if code is None:
            return _js({"ok": False, "error": "agent 不存在或非 active"}, 404)
        return _js({"ok": True, "agent_id": aid, "identity_code": code,
                    "warn": "旧身份码已失效，新码仅显示一次"}, 200)

    async def delete_agent(request: Request):
        """【真删除】从库里彻底移除整行（含身份码哈希），不可恢复。
        与 revoke（仅置 status=revoked 留历史）区分：吊销后可恢复/保留审计，删除是物理抹除。
        谨慎使用。"""
        aid = request.path_params["id"]
        ok = agents.delete_agent(aid)
        if not ok:
            return _js({"ok": False, "error": "agent 不存在"}, 404)
        return _js({"ok": True, "agent_id": aid,
                    "note": "已从身份库彻底删除（含身份码哈希），不可恢复。"}, 200)

    # ── ACP 对等令牌（kind=acp，前缀 acp_）──
    # 签发面仿 memory-worker /api/mcp/tokens，但独立表 acp_tokens：
    # acp_ 令牌只能进 /acp，进不了 /mcp；af_ 身份码也进不了 /acp（规格 §3/§9 三套隔离）。
    # 本组端点在 WebUI JWT 保护之下（与 /api/agents 同级），不对匿名开放。
    async def list_acp_tokens(request: Request):
        return _js({"tokens": acp_tokens.list_tokens()})

    async def create_acp_token(request: Request):
        b = await _body(request)
        name = (b.get("name") or "").strip()
        if not name:
            return _js({"ok": False, "error": "name 必填"}, 400)
        rec, code = acp_tokens.create_token(name, b.get("notes", ""))
        # 明文仅此一次返回；库里只有 sha256
        return _js({"ok": True, "token": dict(rec, acp_token=code),
                    "warn": "ACP 令牌仅显示一次，请立即写入对端的 AUTOFLOW_ACP_TOKEN"}, 201)

    async def revoke_acp_token(request: Request):
        tid = request.path_params["id"]
        ok = acp_tokens.revoke_token(tid)
        return _js({"ok": ok, "token_id": tid}, 200 if ok else 404)

    async def delete_acp_token(request: Request):
        """【真删除】物理抹除该行（含令牌哈希），不可恢复；与 revoke（留审计）区分。"""
        tid = request.path_params["id"]
        ok = acp_tokens.delete_token(tid)
        if not ok:
            return _js({"ok": False, "error": "acp 令牌不存在"}, 404)
        return _js({"ok": True, "token_id": tid,
                    "note": "已从 acp_tokens 彻底删除（含哈希），不可恢复。"}, 200)

    # ── ACP 启用开关（WebUI「ACP 令牌」页独立开关，运行时经 feature_flags.json 切换免重启）──
    async def get_acp_enabled(request: Request):
        return _js({"enabled": bool(is_acp_enabled(cfg))})

    async def set_acp_enabled(request: Request):
        b = await _body(request)
        v = bool(b.get("enabled", False))
        set_feature_flag(cfg, "acp_enabled", v)
        return _js({"enabled": v,
                    "note": "ACP 开关已更新；关闭后 /acp 停止服务、ACP 工具返回禁用提示（免重启生效）。"})
    # ── LLM 配置（DEV-llm-webui-agent：WebUI 内置 LLM 助手）──
    # 配置落 data/<env>/llm_config.json（与 feature_flags 同目录，免重启读取）；密钥仅存盘不回显。
    def _mask_llm_key(k: str) -> str:
        from .llm_client import _mask_secret
        return _mask_secret(k) if k else ""

    async def get_llm_config(request: Request):
        d = load_llm_config(cfg)
        backends = d.get("backends") or []
        masked_backends = [{
            "url": b.get("url", ""),
            "name": b.get("name", ""),
            "model": b.get("model", ""),
            "api_key": _mask_llm_key(b.get("api_key", "")),
            "enabled": bool(b.get("enabled", True)),
        } for b in backends if isinstance(b, dict)]
        return _js({
            "enabled": bool(d.get("enabled", False)),
            "backends": masked_backends,
            "api_url": d.get("api_url", ""),
            "api_key": _mask_llm_key(d.get("api_key", "")),
            "model": d.get("model", ""),
            "configured": bool(backends or d.get("api_key") or d.get("api_url")),
        })

    async def set_llm_config(request: Request):
        b = await _body(request)
        enabled = bool(b.get("enabled", False))
        raw_backends = b.get("backends")
        if raw_backends is None:
            raw_backends = []
        if not isinstance(raw_backends, list):
            return _js({"ok": False, "error": "backends 必须是数组"}, 400)
        # 防脱敏回写：若 api_key 为空或含 ****（疑似回传了脱敏值），保留文件中原密钥
        old = load_llm_config(cfg)
        old_backends = {x.get("url", ""): x.get("api_key", "") for x in (old.get("backends") or []) if isinstance(x, dict)}

        def _resolve_key(new_key, fallback):
            # 显式空串 → 清空；含 ****（脱敏回传）或字段缺失 → 保留旧值
            v = "" if new_key is None else str(new_key)
            if "****" in v:
                return fallback
            return v

        clean_backends = []
        for x in raw_backends:
            if not isinstance(x, dict):
                continue
            url = str(x.get("url", "")).strip()
            clean_backends.append({
                "url": url,
                "api_key": _resolve_key(x.get("api_key", ""), old_backends.get(url, "")) if "api_key" in x else old_backends.get(url, ""),
                "model": str(x.get("model", "")).strip(),
                "name": str(x.get("name", "")).strip(),
                "enabled": bool(x.get("enabled", True)),
            })
        d = {
            "enabled": enabled,
            "backends": clean_backends,
            "api_url": str(b.get("api_url", "")).strip(),
            "api_key": _resolve_key(b.get("api_key", ""), old.get("api_key", "")) if "api_key" in b else old.get("api_key", ""),
            "model": str(b.get("model", "")).strip(),
        }
        save_llm_config(cfg, d)
        # 免重启：覆盖路由单例（仅当确有后端凭据）
        try:
            from .llm_client import reconfigure_router_from_llm_config
            reconfigure_router_from_llm_config(cfg)
        except Exception as e:
            import logging as _log
            _log.getLogger("webui").warning("LLM 路由未随配置刷新：%s", e)
        return _js({
            "ok": True,
            "enabled": enabled,
            "configured": bool(clean_backends or d["api_key"] or d["api_url"]),
            "note": "LLM 配置已保存（密钥仅存盘不回显）；WebUI 助手免重启生效。",
        })

    async def llm_chat(request: Request):
        """服务端跑 agent 循环：取用户面 MCP 工具 → chat_with_tools 多轮 → 经 FastMCP call_tool 执行回填 → 终态文本。

        新增模式切换：
          · mode=acp：直接把用户消息经 ACP 委派给 memory-agent，不跑本地 agent 循环。
          · backend_index：在 autoflow 模式下指定使用第 N 个后端（供 WebUI「选择模型」）。

        工具暴露遵循身份分层纪律：排除 _DEPLOY_KNIVES（部署/自检刀），WebUI 助手绝不可触。"""
        b = await _body(request)
        user_msg = (b.get("message") or "").strip()
        history = b.get("history") or []
        mode = (b.get("mode") or "autoflow").strip().lower()
        backend_index = b.get("backend_index")
        if not user_msg:
            return _js({"ok": False, "error": "message 必填"}, 400)

        # ── ACP 委派模式：直接外联 memory-agent ──
        if mode == "acp":
            from .acp_client import delegate_to_memory_worker
            res = delegate_to_memory_worker(user_msg, context=None, cfg=cfg)
            if not res.get("ok"):
                return _js({"ok": False, "error": res.get("error") or "memory-agent ACP 调用失败"})
            steps = [{
                "tool": "delegate_to_memory_worker",
                "args": {"task": user_msg[:200]},
                "result": (res.get("text") or "")[:4000],
            }]
            return _js({"ok": True, "text": res.get("text") or "", "steps": steps})

        d = load_llm_config(cfg)
        if not d.get("enabled", False):
            return _js({"ok": False, "error": "LLM 未启用：请在 WebUI「LLM 设置」页开启后重试",
                        "hint": "llm_disabled"})
        if not (d.get("backends") or d.get("api_key") or d.get("api_url")):
            return _js({"ok": False, "error": "LLM 未配置：请在 WebUI「LLM 设置」页填入 OpenAI 兼容后端",
                        "hint": "llm_not_configured"})
        # 路由：用文件配置建独立 router（不污染单例，避免影响 autoflow_ask_llm）
        try:
            from .llm_client import LLMRouter  # 惰性：缺 httpx 时不影响网关启动
            router = LLMRouter.from_dict(d)
        except Exception as e:
            return _js({"ok": False, "error": f"LLM 路由初始化失败: {e}"})
        if not router.configured:
            return _js({"ok": False, "error": "LLM 未配置：后端凭据不完整", "hint": "llm_not_configured"})
        # 若前端指定了后端索引，只使用对应后端
        if isinstance(backend_index, int) and 0 <= backend_index < len(router._providers):
            router._providers = [router._providers[backend_index]]
        # 取用户面工具（过滤部署/自检刀）
        try:
            from . import mcp_server
            mcp = mcp_server.mcp
            knives = mcp_server._DEPLOY_KNIVES
            raw_tools = await mcp.list_tools()
        except Exception as e:
            return _js({"ok": False, "error": f"获取 MCP 工具失败: {e}"})
        tools = []
        for t in raw_tools:
            name = getattr(t, "name", None)
            if not name or name in knives:
                continue
            schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
            tools.append({"type": "function", "function": {
                "name": name,
                "description": getattr(t, "description", "") or "",
                "parameters": schema,
            }})
        if not tools:
            return _js({"ok": False, "error": "无可用工具（部署/自检刀已按纪律隐藏）"})
        # 内置 LLM 助手 agent：直连 FastMCP.call_tool 不经过 HTTP 鉴权中间件，
        # 故在此按用户名解析/创建一个内置 agent，并在 executor 调用前注入 current_agent，
        # 使 autoflow_propose_dsl 等工具能读到有效身份，修复「未识别 agent」。
        agent_store = AgentStore(cfg)
        llm_agent_name = "__builtin_llm_assistant__"
        llm_agent = agent_store.get_agent_by_name(llm_agent_name)
        if llm_agent is None:
            llm_agent, _llm_agent_code = agent_store.create_agent(
                name=llm_agent_name,
                tier="prod",
                notes="AutoFlow 内置 LLM 助手（WebUI 用户态）",
                mode="normal",
            )
        steps = []

        async def executor(name: str, arguments: dict) -> str:
            if name in knives:
                return f"[拒绝：{name} 属部署/自检刀，WebUI 助手不可调用]"
            # 在工具执行前把内置 agent 注入 current_agent（FastMCP.call_tool 保留 contextvar）
            var = mcp_server.get_current_agent_var()
            tok = var.set(llm_agent)
            try:
                res = await mcp.call_tool(name, arguments or {})
                parts = []
                for blk in (res or []):
                    txt = getattr(blk, "text", None)
                    if txt is None:
                        txt = str(blk)
                    parts.append(txt)
                out = "\n".join(parts)
            except Exception as e:
                out = f"[工具 {name} 执行出错: {e}]"
            finally:
                var.reset(tok)
            steps.append({"tool": name, "args": arguments, "result": (out or "")[:4000]})
            return out

        msgs = []
        for h in history:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant", "system"):
                msgs.append({"role": h["role"], "content": str(h.get("content", ""))})
        msgs.append({"role": "user", "content": user_msg})
        system = ("你是 AutoFlow 网关内置助手，可调用网关提供的工具查询与控制智能家居及流程。"
                  "务必先用工具获取真实数据再作答，禁止编造 entity_id 或设备状态；"
                  "若工具返回为空或报错，如实说明并建议用户检查设备/连接。")
        from .llm_client import LLMError  # 惰性导入（同上）
        try:
            text = await router.chat_with_tools(msgs, tools, executor, system=system, max_rounds=6)
        except LLMError as e:
            return _js({"ok": False, "error": str(e), "steps": steps})
        except Exception as e:
            return _js({"ok": False, "error": f"LLM 调用失败: {e}", "steps": steps})
        return _js({"ok": True, "text": text, "steps": steps})

    async def test_llm(request: Request):
        """LLM 连通性测试（WebUI「测试全部」/「测试这条」按钮）。

        - scope=all：用已保存配置建 LLMRouter，ping 所有已配置后端（含停用者，便于确认可用性）。
        - scope=backend：用前端表单构造单后端 LLMProvider 并 ping；若 api_key 含 **** 则从已保存
          配置按 url/name 找回真实 key（防脱敏回写）。"""
        b = await _body(request)
        scope = (b.get("scope") or "all")
        try:
            from .llm_client import LLMRouter, LLMProvider
        except Exception as e:
            return _js({"ok": False, "error": f"LLM 模块加载失败: {e}"})
        if scope == "backend":
            form = b.get("backend") or {}
            if not isinstance(form, dict):
                return _js({"ok": False, "error": "backend 必须是对象"}, 400)
            url = str(form.get("url", "")).strip()
            name = str(form.get("name", "")).strip() or url
            model = str(form.get("model", "")).strip()
            api_key = str(form.get("api_key", ""))
            # 防脱敏：前端回传 ****（未改密钥）→ 从已保存配置按 url/name 找回真实 key
            if "****" in api_key:
                saved = load_llm_config(cfg)
                for sb in (saved.get("backends") or []):
                    if isinstance(sb, dict) and (sb.get("url", "") == url or sb.get("name", "") == name):
                        api_key = sb.get("api_key", "")
                        break
                else:
                    if saved.get("api_url", "") == url:
                        api_key = saved.get("api_key", "")
            if not (url and api_key and model):
                return _js({"ok": False, "error": "后端未完整配置（需 url + api_key + model）",
                            "backends": [{"connected": False, "name": name, "model": model,
                                          "endpoint": url, "error": "缺 url / api_key / model"}]})
            prov = LLMProvider(url=url, api_key=api_key, model=model, name=name)
            res = await prov.ping()
            return _js({"ok": bool(res.get("connected")), "message": "单后端探测完成", "backends": [res]})
        # scope=all（默认）：用已保存配置探测所有后端
        try:
            d = load_llm_config(cfg)
            router = LLMRouter.from_dict(d)
        except Exception as e:
            return _js({"ok": False, "error": f"LLM 路由初始化失败: {e}"})
        if not router.configured:
            return _js({"ok": False, "error": "未配置任何后端（请先在「LLM 设置」页填写）", "backends": []})
        try:
            result = await router.ping()
        except Exception as e:
            return _js({"ok": False, "error": f"LLM 测试失败: {e}", "backends": []})
        return _js(result)

    # ── 确认闸 ──
    async def list_pending(request: Request):
        return _js({"pending": gw.list_pending()})

    async def approve(request: Request):
        op_id = request.path_params["id"]
        result = await asyncio.to_thread(gw.approve, op_id, "human")
        code = 200 if result.get("ok") else 502  # 502 = 上游(NR)执行失败
        return _js(result, status=code)

    async def reject(request: Request):
        op_id = request.path_params["id"]
        b = await _body(request)
        return _js(gw.reject(op_id, "human", b.get("reason")))

    # ── 场景提案（部署候选） ──
    # 注意：经验沉淀(P5)已推迟。promote API 保留兼容，但 UI 不再暴露。
    async def list_proposals(request: Request):
        qp = request.query_params
        agent_id = qp.get("agent_id")
        status = qp.get("status")
        # 分页：默认 100，硬上限 500（W2-1 防止提案区卡加载）
        try:
            limit = int(qp.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        if limit <= 0:
            limit = 100
        limit = min(limit, 500)
        try:
            offset = int(qp.get("offset", "0"))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        # 已归档项默认隐藏（include_archived=1/true 才显示，W2-2 退休语义）
        inc_arch = qp.get("include_archived", "0") in ("1", "true", "True")
        # 阻塞调用移出单 worker 事件循环（DB 读 + 策略文件读），避免卡死其他请求
        policy = await asyncio.to_thread(get_deploy_policy, cfg)
        items = []
        for p in await asyncio.to_thread(
            proposals.list, agent_id=agent_id, status=status,
            limit=limit, offset=offset, include_archived=inc_arch,
        ):
            d = p.to_summary()  # 轻量视图，剔除 content 中体积大的 flow/validation
            # 按当前部署策略 + 提案来源标注是否需人审（前端打「可信/需审」徽章用）
            d["requires_review"] = Gateway.proposal_requires_review(policy, p.source)
            items.append(d)
        total = await asyncio.to_thread(
            proposals.count, agent_id=agent_id, status=status,
            include_archived=inc_arch,
        )
        return _js({
            "proposals": items,
            "deploy_policy": policy,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def submit_proposal(request: Request):
        b = await _body(request)
        title = (b.get("title") or "").strip()
        if not title:
            return _js({"ok": False, "error": "title 必填"}, 400)
        try:
            p = proposals.submit(b.get("agent_id", "human"), title,
                                 b.get("kind", "idea"), b.get("content", ""),
                                 b.get("tags"))
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        return _js({"ok": True, "proposal": p.to_dict()}, 201)

    async def promote_proposal(request: Request):
        pid = request.path_params["id"]
        try:
            p = proposals.promote(pid, "human")
        except (KeyError, ValueError) as e:
            return _js({"ok": False, "error": str(e)}, 400)
        return _js({"ok": True, "proposal": p.to_dict()})

    async def reject_proposal(request: Request):
        pid = request.path_params["id"]
        b = await _body(request)
        try:
            p = proposals.reject(pid, "human", b.get("reason", ""))
        except KeyError as e:
            return _js({"ok": False, "error": str(e)}, 404)
        return _js({"ok": True, "proposal": p.to_dict()})

    async def archive_proposal(request: Request):
        """归档一条提案（退休语义，W2-2）：archived_at=now，默认从 list/count/诊断计数隐藏。
        仅改元数据，不动提案内容/部署关系。"""
        pid = request.path_params["id"]
        p = proposals.archive(pid)
        if p is None:
            return _js({"ok": False, "error": "提案不存在"}, 404)
        return _js({"ok": True, "proposal": p.to_dict()})

    async def unarchive_proposal(request: Request):
        """取消归档（W2-2）：archived_at=NULL，重新进入活跃视图。"""
        pid = request.path_params["id"]
        p = proposals.unarchive(pid)
        if p is None:
            return _js({"ok": False, "error": "提案不存在"}, 404)
        return _js({"ok": True, "proposal": p.to_dict()})

    async def delete_proposal(request: Request):
        """物理删除一条提案（清理垃圾/重复提案）。仅删记录，不自动撤回已部署的 NR flow。"""
        pid = request.path_params["id"]
        try:
            ok = proposals.delete(pid)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)
        if not ok:
            return _js({"ok": False, "error": "提案不存在"}, 404)
        return _js({"ok": True})

    async def deploy_proposal(request: Request):
        """把 DSL 提案送进确认闸（部署到 NR 须人工 approve）。MCP 不暴露此入口。"""
        pid = request.path_params["id"]
        b = await _body(request)
        target = b.get("target", "prod")
        force = bool(b.get("force", False))
        validate = b.get("validate", True)  # 部署前 staging 闸门（vhass 重放断言），默认开
        # require_e2e：显式传 true/false 覆盖提案落档意图；缺省 None → 继承提案 content.require_e2e。
        require_e2e = b.get("require_e2e", None)
        # allow_prod：人手动部署默认 True（显式授权写 prod）；如需强制守卫可传 false。
        allow_prod = b.get("allow_prod", True)
        # P4 混合模式：用户可在 WebUI 部署时指定目标 tab
        target_tab = b.get("target_tab") or None
        if target_tab:
            target_tab = str(target_tab).strip() or None
        try:
            res = await asyncio.to_thread(gw.deploy_proposal, pid, agent_id="human",
                                          target=target, force=force, validate=validate,
                                          require_e2e=require_e2e, allow_prod=allow_prod,
                                          target_tab=target_tab)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 400)
        if not res.get("ok"):
            status = 409 if res.get("conflict") else 400
            return _js(res, status)
        return _js(res)

    # ── 已部署（flow_catalog + 注册表↔NR 分叉对账）──
    async def list_deployed(request: Request):
        return _js({"deployed": await asyncio.to_thread(gw.list_deployed, stale_check=True)})

    async def undeploy_flow(request: Request):
        fid = request.path_params["id"]
        b = await _body(request)
        force = bool(b.get("force", False))
        try:
            res = await asyncio.to_thread(gw.undeploy, fid, force=force)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 400)
        if not res.get("ok"):
            status = 404 if res.get("code") == "not_ours" else 400
            return _js(res, status)
        return _js(res)

    # ── 白盒部署面板已随开源化从网关剥离（C2）；白盒部署能力保留于 MCP 工具 autoflow_deploy_raw ──

    # ── 工作区 plan（总体/当前/最近完成）──
    async def plan_view(request: Request):
        return _js(gw.get_plan())

    async def plan_update(request: Request):
        b = await _body(request)
        overall = b.get("overall")
        current = b.get("current")
        append_completed = b.get("append_completed")
        if not (overall is not None or current is not None or append_completed):
            return _js({"ok": False, "error": "至少传 overall / current / append_completed 之一"}, 400)
        state = gw.update_plan(
            overall=overall,
            current=current,
            append_completed=append_completed,
        )
        return _js({"ok": True, "plan": state})

    # ── 指令收件箱（人类 → deepseek 直达）──
    async def list_commands(request: Request):
        try:
            limit = int(request.query_params.get("limit", "30"))
        except (TypeError, ValueError):
            limit = 30
        return _js({"commands": gw.list_commands(limit=limit)})

    async def submit_command(request: Request):
        b = await _body(request)
        text = (b.get("text") or "").strip()
        if not text:
            return _js({"ok": False, "error": "text 必填"}, 400)
        target = b.get("target", "deepseek")
        res = gw.submit_command(text, target=target)
        return _js(res, 201 if res.get("ok") else 400)

    # ── 多选项决策闸（人类请示）──
    async def list_decisions(request: Request):
        status = request.query_params.get("status")
        return _js({"decisions": gw.list_decisions(status=status)})

    async def resolve_decision(request: Request):
        did = request.path_params["id"]
        b = await _body(request)
        try:
            choice = int(b.get("chosen"))
        except (TypeError, ValueError):
            return _js({"ok": False, "error": "chosen 必须是整数下标"}, 400)
        # resolve 内部可能等待 ds_bridge 空闲并回灌 deepseek，放线程避免阻塞事件循环
        res = await asyncio.to_thread(gw.resolve_decision, did, choice, "human")
        if not res.get("ok"):
            return _js(res, 400)
        return _js(res)

    # ── 笔记 ──
    async def list_notes(request: Request):
        tag = request.query_params.get("tag")
        q = request.query_params.get("q")
        return _js({"notes": [n.to_dict() for n in notes.list(tag=tag, q=q)]})

    async def create_note(request: Request):
        b = await _body(request)
        n = notes.create(b.get("title", ""), b.get("body", ""), b.get("tags"))
        return _js({"ok": True, "note": n.to_dict()}, 201)

    async def update_note(request: Request):
        nid = request.path_params["id"]
        b = await _body(request)
        n = notes.update(nid, title=b.get("title"), body=b.get("body"), tags=b.get("tags"))
        if n is None:
            return _js({"ok": False, "error": "笔记不存在"}, 404)
        return _js({"ok": True, "note": n.to_dict()})

    async def delete_note(request: Request):
        nid = request.path_params["id"]
        ok = notes.delete(nid)
        return _js({"ok": ok}, 200 if ok else 404)

    # ── 子流程注册表（#575/#579）──
    async def list_subflows(request: Request):
        from .api_specs import get_api_spec
        src = request.query_params.get("source_type")
        st = request.query_params.get("status")
        rows = await asyncio.to_thread(gw.tasks.list_subflows,
                                        source_type=src, status=st)
        # A1/A4：link_out / http_api 类（网关 HTTP 桥接）若标记 self_use（豆包系列
        # 等网关自用能力），不进入产品列表；其 spec 定义仍保留在 api_specs.json 仅供
        # 重装，不影响「子流程」Tab（只含 kind=subflow）。这是避免豆包链路在 WebUI
        # 被误装/被默认 tab 生成覆盖的硬约束之一。
        visible = []
        for r in rows:
            kind = r.get("kind") or "subflow"
            if kind in ("link_out", "http_api"):
                spec = get_api_spec(r.get("spec_ref") or r.get("key") or "")
                if spec is not None and getattr(spec, "self_use", False):
                    continue
            visible.append(r)
        # A5(#171)：Bark 安装前置判定——BARK_SERVER/BARK_KEY 都配齐才允许安装按钮启用。
        # 仅给 bark_push 行打标记，前端据此禁用按钮并提示去「连接设置」填写。
        for r in visible:
            if r.get("key") == "bark_push":
                r["bark_ready"] = connections.bark_ready(cfg)
        # #C：给「安装到 Node-RED」的判断依据——只有真正会在 NR 里派生节点的
        # Link API（neids_nr_flow() 为 True，如带 nr_downstream_link_id 的 link_out）
        # 才需要独立安装按钮；http_api（网关内联）与用户从 tab 链接导入的 link_out
        # （指向既有 tab，零 NR 写入）不需要，前端据此隐藏安装按钮。
        for r in visible:
            kind = r.get("kind") or "subflow"
            if kind in ("link_out", "http_api"):
                spec = get_api_spec(r.get("spec_ref") or r.get("key") or "")
                r["needs_nr_flow"] = bool(
                    spec is not None and spec.needs_nr_flow())
            else:
                r["needs_nr_flow"] = False
        return _js({"subflows": visible, "count": len(visible)})

    # ── A2：Link API 配置表单持久化（方案 B：api_configs 表）──
    _ENV_PH_RE = re.compile(r"<([A-Z][A-Z0-9_]+)>")

    # spec 中会被嵌入生成节点、因而需要占位符替换的「表达式字段」。
    # 推导(_config_fields_for_spec) 与替换(_effective_spec) 必须共用这份清单：
    # 只推导不替换 → 用户被要求填一个永远不生效的字段，装出来的 flow 带着裸占位符。
    # 注意不含 description/notes —— 那是给人看的文档，扫了会造出幽灵配置字段。
    _SPEC_EXPR_FIELDS = ("url", "nr_body_template", "extract", "nr_assemble")

    def _config_fields_for_spec(spec) -> list:
        """从 spec 的表达式字段 + headers 提取 <ENV_NAME> 占位符，

        作为该 Link API 需用户填写的配置字段（单一真相源，避免手维护字段清单）。
        A3(#179)：extract / nr_assemble 也会进 change 节点，故一并纳入扫描。
        A25：`<NAS_IP>` 等**系统占位符不进此清单**——那是部署环境事实，网关从
        NR_URL 就能推出来；要用户手填等于把网关的内部知识摊派给使用者。"""
        names: list = []
        seen = set()
        texts = [getattr(spec, f, "") or "" for f in _SPEC_EXPR_FIELDS]
        for v in (spec.nr_headers or {}).values():
            texts.append(str(v))
        for text in texts:
            for m in _ENV_PH_RE.findall(text or ""):
                if m in SYSTEM_PLACEHOLDERS:
                    continue
                if m not in seen:
                    seen.add(m)
                    names.append(m)
        return names

    async def link_api_config_endpoint(request: Request):
        """GET 读配置（含推导 config_fields 与当前值）/ PUT 写配置。

        GET 返回 {ok, name, config_fields, config, self_use}；
        PUT body={config:{ENV: value}}，仅接收 spec 声明的字段（防越权写无关 env），
        写入 api_configs 表。self_use 能力（豆包系列）禁止配置。"""
        name = request.path_params.get("name") or ""
        spec = get_api_spec(name)
        if spec is None:
            return _js({"ok": False, "error": f"未知 Link API: {name}"}, 404)
        if request.method == "GET":
            return _js({
                "ok": True, "name": name,
                "config_fields": _config_fields_for_spec(spec),
                "config": api_configs.get_api_config(name),
                "self_use": getattr(spec, "self_use", False),
            })
        if getattr(spec, "self_use", False):
            return _js({"ok": False, "error": "self_use 能力不可配置"}, 403)
        b = await _body(request)
        cfg_in = b.get("config")
        if not isinstance(cfg_in, dict):
            return _js({"ok": False, "error": "config 必须是对象 {ENV: value}"}, 400)
        allowed = set(_config_fields_for_spec(spec))
        cleaned = {k: str(v) for k, v in cfg_in.items() if k in allowed}
        # 合并而非整体替换：前端对留空的密钥字段(密码框)不发值，
        # 整体替换会误删既有 token；合并保留未在本轮提交的字段。
        existing = api_configs.get_api_config(name)
        merged = {**existing, **cleaned}
        api_configs.set_api_config(name, merged)
        return _js({"ok": True, "name": name, "config": merged})

    async def import_subflow(request: Request):
        """自省导入用户既有 NR 子流程：给定 nr_subflow_id → 抽取前置参数
        （env + input_schema，免手填）→ 注册进 subflow_registry。

        - key（DSL 调用名）必填，避免与内置名撞车由前端/调用方保证。
        - 默认 status=active 立即可被 DSL 编译器调用；可传 status=pending_review
          走人工审核（审批工作流为 Full 阶段后续，#575）。
        """
        b = await _body(request)
        nr_id = (b.get("nr_subflow_id") or "").strip()
        key = (b.get("key") or "").strip()
        if not nr_id:
            return _js({"ok": False, "error": "nr_subflow_id 必填"}, 400)
        if not key:
            return _js({"ok": False, "error": "key（DSL 调用名）必填"}, 400)
        # 1) 自省（走 NR 网络，线程化避免阻塞事件循环）
        info = await asyncio.to_thread(introspect_nr_subflow, gw.nr, nr_id)
        if not info["ok"]:
            return _js({"ok": False, "error": info.get("error", "自省失败")}, 502)
        # 2) 注册校验门：key/标题/撞名 + 自省出的 schema/env 结构
        gv = validate_subflow_registration(
            key=key, nr_subflow_id=nr_id, source_type="imported",
            title=b.get("title") or info.get("title") or key,
            input_schema=info.get("input_schema"),
            env_requirements=[e["name"] for e in info.get("env_requirements", [])],
        )
        if not gv["ok"]:
            return _js({"ok": False, "error": gv["error"]}, 400)
        cleaned = gv["cleaned"]
        # 3) 注册（imported）。
        reg = await asyncio.to_thread(
            gw.tasks.register_subflow, cleaned["key"],
            title=cleaned["title"],
            nr_subflow_id=cleaned["nr_subflow_id"], source_type="imported",
            input_schema=cleaned["input_schema"],
            env_requirements=cleaned["env_requirements"],
            owner=b.get("owner", "webui"), status=b.get("status", "active"),
            spec_ref=None,
        )
        if not reg["ok"]:
            return _js({"ok": False, "error": reg.get("error")}, 400)
        return _js({"ok": True, "key": key, "introspect": info}, 201)

    # ── #C-tab：tab 链接逆生成 Link API ──
    # 用户在 NR 编辑器复制 tab 链接（http://host:port/#flow/<id>），网关只读自省该 tab，
    # 判断能否注册成 link_out 型 Link API（agent DSL 调用时经 link out 命中 tab 入口 link in）。
    # 两步：① import-from-url 检测并返回草稿；② register-from-tab 落注册表。
    def _slugify_key(title: str):
        """把 tab 标题尽量收敛成合法 DSL 标识符；非 ASCII 标题返回 None（让用户手填）。"""
        if not title:
            return None
        s = re.sub(r"[^A-Za-z0-9_]+", "_", str(title).strip().lower())
        s = re.sub(r"_+", "_", s).strip("_")
        if not s or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s):
            return None
        return s or None

    async def import_link_api_from_tab_endpoint(request: Request):
        """POST /link-apis/import-from-url：解析 tab 链接 → 只读自省 → 返回能否注册。

        body: {url, key?}
        返回 {ok, tab_id, registerable, reason, title, entry_kind, entry_id,
              params, suggested_key}。
          - entry_id 是关键的接线目标（link_out 指向它），仅服务端权威给出；
          - suggested_key 由 tab 标题 slugify 得到，用户可在 UI 覆盖；非 ASCII 标题为 None。
        """
        b = await _body(request)
        url = (b.get("url") or "").strip()
        if not url:
            return _js({"ok": False, "error": "url 必填（NR tab 编辑器链接）"}, 400)
        tab_id = _parse_tab_url(url)
        if not tab_id:
            return _js({"ok": False, "error": "无法从链接解析出 tab id（期望 #flow/<id>）"}, 400)
        info = await asyncio.to_thread(introspect_nr_tab, gw.nr, tab_id)
        if not info.get("ok"):
            return _js({"ok": False, "error": info.get("error", "自省失败")}, 502)
        suggested = (b.get("key") or "").strip() or _slugify_key(info.get("title") or "")
        return _js({
            "ok": True,
            "tab_id": tab_id,
            "registerable": info.get("registerable", False),
            "reason": info.get("reason", ""),
            "title": info.get("title", ""),
            "entry_kind": info.get("entry_kind"),
            "entry_id": info.get("entry_id"),
            "params": info.get("params", []),
            "suggested_key": suggested,
        })

    async def register_link_api_from_tab_endpoint(request: Request):
        """POST /link-apis/register-from-tab：把自省出的 tab 注册成 link_out 型 Link API。

        body: {url, key, title?, params?, owner?, status?}
          - key：DSL 调用名（用户填，如 TTS）；必填。
          - params：用户可在 UI 增删/改的入参列表（[{name,required,type,default,enum,desc}]）；
            服务端信任用户编辑后的结构，但仍逐条校验 name 合法性。
          - 服务端**重新**自省 url 取得权威 entry_id（绝不信任客户端给出的接线目标）。
        返回 {ok, key, entry_id}；注册后即出现在 Link API 列表，agent 可调用。
        """
        b = await _body(request)
        url = (b.get("url") or "").strip()
        key = (b.get("key") or "").strip()
        if not url:
            return _js({"ok": False, "error": "url 必填"}, 400)
        if not key:
            return _js({"ok": False, "error": "key（DSL 调用名）必填"}, 400)
        tab_id = _parse_tab_url(url)
        if not tab_id:
            return _js({"ok": False, "error": "无法从链接解析出 tab id"}, 400)
        # 1) 服务端重新自省，取权威 entry_id 并复核可注册性
        info = await asyncio.to_thread(introspect_nr_tab, gw.nr, tab_id)
        if not info.get("ok"):
            return _js({"ok": False, "error": info.get("error", "自省失败")}, 502)
        if not info.get("registerable"):
            return _js({"ok": False,
                        "error": "该 tab 不能注册为 Link API：" + (info.get("reason") or ""),
                        "reason": info.get("reason")}, 400)
        entry_id = info.get("entry_id")
        if not entry_id:
            return _js({"ok": False, "error": "自省未拿到入口 link in 节点 id"}, 502)
        # 2) params 校验（用户可能编辑过）：name 必为合法标识符，结构归一
        params = b.get("params") or info.get("params") or []
        if not isinstance(params, list):
            return _js({"ok": False, "error": "params 必须是列表"}, 400)
        cleaned_params = []
        for i, p in enumerate(params):
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("name"):
                return _js({"ok": False,
                            "error": f"params[{i}] 缺 name 或 name 非字符串"}, 400)
            if not _KEY_RE.match(p["name"]):
                return _js({"ok": False,
                            "error": f"params[{i}].name 必须是合法标识符：{p['name']}"}, 400)
            cleaned_params.append({
                "name": p["name"],
                "required": bool(p.get("required", False)),
                "type": p.get("type", "str") or "str",
                "default": p.get("default"),
                "enum": p.get("enum"),
                "desc": p.get("desc", ""),
            })
        # 3) 注册校验门（link_out 路径：要求 entry_link_id，不要 nr_subflow_id）
        gv = validate_subflow_registration(
            key=key, nr_subflow_id=None, source_type="imported",
            title=b.get("title") or info.get("title") or key,
            input_schema=cleaned_params, env_requirements=[],
            kind="link_out", entry_link_id=entry_id,
        )
        if not gv["ok"]:
            return _js({"ok": False, "error": gv["error"]}, 400)
        cleaned = gv["cleaned"]
        # 4) 注册（imported，link_out，薄桥接；不写用户 NR 流）
        reg = await asyncio.to_thread(
            gw.tasks.register_subflow, cleaned["key"],
            title=cleaned["title"],
            nr_subflow_id=None, source_type="imported",
            input_schema=cleaned["input_schema"],
            env_requirements=cleaned["env_requirements"],
            owner=b.get("owner", "webui"), status=b.get("status", "active"),
            spec_ref=None, kind="link_out", entry_link_id=cleaned["entry_link_id"],
        )
        if not reg["ok"]:
            return _js({"ok": False, "error": reg.get("error")}, 400)
        return _js({"ok": True, "key": key, "entry_id": entry_id,
                    "title": cleaned["title"]}, 201)

    async def set_subflow_status(request: Request):
        """变更子流程状态（active / disabled / pending_review）。

        #711 起：managed 子流程（含 history_* / bark_push）也允许手动启停 ——
        「禁用」是历史子流程唯一的治理手段（它们不允许删除，见 delete 端点），
        原先一刀切 403 导致这类条目在 WebUI 上完全不可操作。禁用后 DSL 编译器
        会给出「子流程已停用」的明确错误，属预期行为，不是数据损坏。
        返回 {ok, key, status}。"""
        key = request.path_params.get("key")
        b = await _body(request)
        new_status = (b.get("status") or "").strip().lower()
        if new_status not in ("active", "disabled", "pending_review"):
            return _js({"ok": False, "error": "status 须为 active/disabled/pending_review"}, 400)
        # #119-fix: 与 delete/ensure 两个端点同构，get_subflow_meta 裸抛会裸 500；纳入 try 返 502。
        # （本处修复原只存在于 NAS 活树，2026-08-05 回补进 git 仓库，防同步时被覆盖丢失。）
        try:
            meta = await asyncio.to_thread(gw.tasks.get_subflow_meta, key)
        except Exception as e:
            return _js({"ok": False, "error": f"读取子流程元数据失败：{e}"}, 502)
        if not meta:
            return _js({"ok": False, "error": f"子流程不存在: {key}"}, 404)
        r = await asyncio.to_thread(gw.tasks.set_subflow_status, key, new_status)
        if not r["ok"]:
            return _js({"ok": False, "error": r.get("error")}, 400)
        return _js({"ok": True, "key": key, "status": new_status})

    async def delete_subflow_endpoint(request: Request):
        """注销一条子流程。#711 起按「谁建的谁负责」区分 NR 侧处置：

        - `history_*`（DSL 内置原语）：**禁止删除**，只能「禁用」。删了编译器就报
          「未注册子流程」，属自伤；且它们会被 ensure_history_subflow 再装回来。
        - managed（网关自建，如 bark_push）：删注册表 **并** 定向删掉 NR 子流程实例
          —— 东西是网关造的，网关负责收尾，不留孤儿。
        - imported（用户自己在 NR 里建的、被网关自省导入）：**只删注册表条目**，
          NR 上的子流程原样保留。网关只是「取消登记」，无权删用户的东西
          （原实现会连用户的 NR 子流程一起删，是越权，已修）。

        返回 {ok, key, nr_removed, nr_kept}。"""
        from .subflows import HISTORY_REGISTRY_KEYS
        key = request.path_params.get("key")
        # #119-fix: 与 ensure_subflow_endpoint 同构，get_subflow_meta 裸抛会裸 500；纳入 try。
        try:
            meta = await asyncio.to_thread(gw.tasks.get_subflow_meta, key)
        except Exception as e:
            return _js({"ok": False, "error": f"读取子流程元数据失败：{e}"}, 502)
        if not meta:
            return _js({"ok": False, "error": f"子流程不存在: {key}"}, 404)
        if key in HISTORY_REGISTRY_KEYS:
            return _js({"ok": False,
                        "error": "历史查询子流程是 DSL 内置能力，不可删除；"
                                 "如需停用请使用「禁用」"}, 403)
        gateway_built = meta.get("source_type") == "managed"
        has_nr_inst = ((meta.get("kind") or "subflow") == "subflow"
                       and bool(meta.get("nr_subflow_id")))
        nr_removed = False
        nr_kept = False
        if has_nr_inst and gateway_built:
            # 网关自建 → 一并删 NR 实例；失败则不碰注册表（避免注册表干净、NR 留孤儿）
            try:
                res = await asyncio.to_thread(
                    gw.nr.delete_flow, meta["nr_subflow_id"], True)
                nr_removed = bool(res.get("deleted"))
            except Exception as e:
                return _js({"ok": False,
                            "error": f"NR 子流程实例删除失败（注册表未动）：{e}"}, 502)
        elif has_nr_inst:
            nr_kept = True  # imported：保留用户在 NR 上的子流程
        r = await asyncio.to_thread(gw.tasks.delete_subflow, key)
        if not r["ok"]:
            return _js({"ok": False, "error": r.get("error")}, 400)
        return _js({"ok": True, "key": key,
                    "nr_removed": nr_removed, "nr_kept": nr_kept})

    async def ensure_subflow_endpoint(request: Request):
        """手动安装/修复网关自有子流程到 NR（#711 的「安装到 NR」按钮）。

        目前覆盖 4 个 history_* —— 它们是 DSL 内置原语，但 NR 侧实例只在部署含
        af_hist_* 的 flow 时才会被 ensure 自动装。这里提供一个「不部署也能提前装」
        的入口，也用于用户在 NR 里手删后一键修复。allow_prod=True（人手动触发）。
        幂等：已存在即 no-op。返回 {ok, key, created, exists, rebuilt}。"""
        from .subflows import HISTORY_REGISTRY_KEYS, ensure_history_subflow
        key = request.path_params.get("key")
        # #119-fix: 原 get_subflow_meta 在 try 之外，注册表/DB 异常会裸抛 500。
        # 纳入 try，DB 读取失败统一返 502（带错误信息），不再裸 500。
        try:
            meta = await asyncio.to_thread(gw.tasks.get_subflow_meta, key)
        except Exception as e:
            return _js({"ok": False, "error": f"读取子流程元数据失败：{e}"}, 502)
        if not meta:
            return _js({"ok": False, "error": f"子流程不存在: {key}"}, 404)
        if key not in HISTORY_REGISTRY_KEYS:
            return _js({"ok": False,
                        "error": "仅历史查询子流程（history_*）支持一键安装到 NR；"
                                 "用户导入的子流程请在 Node-RED 中自行维护"}, 400)
        client = getattr(gw.nr, "client", None)
        if client is None:
            return _js({"ok": False, "error": "NR 未连接，无法安装"}, 503)
        try:
            res = await asyncio.to_thread(ensure_history_subflow, client, True)
        except Exception as e:
            return _js({"ok": False, "error": f"安装失败：{e}"}, 502)
        return _js({"ok": True, "key": key,
                    "created": res.get("created"), "exists": res.get("exists"),
                    "rebuilt": res.get("rebuilt"), "detail": res})

    async def install_bark_subflow_endpoint(request: Request):
        """A5(#171)：安装 Bark 子流程到 NR（幂等），从 connections.json 注入 BARK_* env。

        - 前置校验：BARK_SERVER 与 BARK_KEY 必须都已配置，否则 400 提示先去
          「设置 → 连接配置 → Bark」填写；
        - allow_prod=True（人手动触发，prod 下写 NR 必须）；
        - env 由进程内 os.environ 注入（apply_saved_to_env 在启动/保存时已生效，这里再补一次
          以覆盖「网关运行期间改过连接设置」的情况）；不碰 1880/1990 既有无关节点。
        返回 {ok, key, created, exists, detail}。"""
        if not connections.bark_ready(cfg):
            return _js({"ok": False,
                        "error": "Bark 未配置：请先在「设置 → 连接配置 → Bark」填写 BARK_SERVER 与 BARK_KEY"},
                        400)
        # 确保最新保存的 connections 已注入进程 env（用户可能在网关运行期间改过）
        try:
            connections.apply_saved_to_env(cfg)
        except Exception:
            pass
        client = getattr(gw.nr, "client", None)
        if client is None:
            return _js({"ok": False, "error": "NR 未连接，无法安装"}, 503)
        try:
            from .subflows import ensure_bark_subflow
            res = await asyncio.to_thread(ensure_bark_subflow, client, True)
        except Exception as e:
            return _js({"ok": False, "error": f"安装失败：{e}"}, 502)
        return _js({"ok": True, "key": "bark_push",
                    "created": res.get("created"), "exists": res.get("exists"),
                    "detail": res})

    # ── A3(#170)：安装「AutoFlow API」tab 到 NR（增量合并，绝不整体覆盖）──
    import dataclasses as _dc

    # risk-1(#177)：Node-RED 的 POST /flow 会自行分配 tab id，完全忽略 body 里的
    # "id"。A3 初版每次都拿字面量 "af_api_tab" 去 get_flow 探测 → 恒 404 →
    # existing=[] → 号称「增量合并」实际退化成全量新建：连点两次就多出一个重名
    # 「AutoFlow API」tab，且 af_weather_in 等节点 id 跨 tab 重复（2026-08-05 子流程
    # 重复 id 串台坑）。修法：真实 id 用「本地台账 + list_flows 兜底」解析。
    AF_API_TAB_LABEL = "AutoFlow API"
    AF_API_TAB_SEED_ID = "af_api_tab"   # 仅首次 POST 建壳用的种子，NR 会另发真实 id

    def _af_tab_ledger_path() -> str:
        return os.path.join(cfg.data_dir, "af_api_tab.id")

    def _af_tab_ledger_read() -> Optional[str]:
        """读本地台账里登记的真实 tab id；无台账/读失败返回 None。"""
        try:
            with open(_af_tab_ledger_path(), "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _af_tab_ledger_write(tab_id: str) -> None:
        """原子登记真实 tab id（tmp + os.replace，避免半截写坏台账）。

        台账只是加速缓存：写失败不影响功能，下次靠 list_flows 重扫即可。
        """
        if not tab_id:
            return
        p = _af_tab_ledger_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(tab_id.strip())
            os.replace(tmp, p)
        except OSError:
            pass

    def _af_tab_scan(entry_ids: set):
        """扫 NR 全量 flows，找 label=="AutoFlow API" 的 tab。

        Args:
            entry_ids: 本次要安装的入口节点 id 集合，用于在重名 tab 中挑「真身」。
        Returns:
            (chosen_id | None, matched_ids)：已存在多个重名 tab 时优先选已含我们
            入口节点的那个，避免继续往空壳里叠加。
        """
        try:
            flows = gw.nr.list_flows()
        except Exception:
            return None, []
        if isinstance(flows, dict):      # NR admin API v2：{flows:[...], rev:...}
            flows = flows.get("flows", [])
        flows = flows or []
        matched = [f.get("id") for f in flows
                   if isinstance(f, dict)
                   and f.get("type") in (None, "", "tab")
                   and (f.get("label") or "").strip() == AF_API_TAB_LABEL
                   and f.get("id")]
        if not matched:
            return None, []
        if len(matched) > 1 and entry_ids:
            owners = {n.get("z") for n in flows
                      if isinstance(n, dict) and n.get("id") in entry_ids}
            for fid in matched:
                if fid in owners:
                    return fid, matched
        return matched[0], matched

    def _resolve_af_api_tab_id(entry_ids=None):
        """解析「AutoFlow API」tab 的真实 id（risk-1 / #177）。

        顺序：① 本地台账命中、NR 上仍在**且 label 仍是「AutoFlow API」** → 直接用；
        ② 否则 list_flows 按 label 找，找到就回写台账；③ 都没有 → None（尚未安装，
        走首次创建路径）。

        #178：台账命中必须复验 label。台账可能变脏（换了 NR 实例 / 用户手动改过 /
        原 tab 被删后该 id 被别的 tab 复用），只判「flow 存在」会误命中无关 tab，
        把我们的节点塞进人家的流程里。label 不符则视同台账过期，落到重扫。

        Args:
            entry_ids: 入口节点 id 集合（重名 tab 时用于挑真身）。
        Returns:
            (tab_id | None, matched_ids)。
        """
        entry_ids = entry_ids or set()
        cached = _af_tab_ledger_read()
        if cached:
            try:
                f = gw.nr.get_flow(cached)
                if (isinstance(f, dict) and f.get("id")
                        and (f.get("label") or "").strip() == AF_API_TAB_LABEL):
                    return f.get("id"), [f.get("id")]
            except Exception:
                pass   # 台账过期（tab 被删 / 换了 NR 实例）→ 落到重扫
        fid, matched = _af_tab_scan(entry_ids)
        if fid:
            _af_tab_ledger_write(fid)
        return fid, matched

    def _effective_spec(spec, config: dict) -> ApiSpec:
        """把 spec 表达式字段 + headers 中的 <ENV> 占位符替换为 api_configs 值。

        覆盖面与 _config_fields_for_spec 严格一致（共用 _SPEC_EXPR_FIELDS），
        否则会出现「要求用户填、但装进 NR 的节点里仍是裸占位符」的错位。
        返回替换后的 ApiSpec 副本（不改原 spec）；build_nr_tab_flows 据此生成真值节点。

        A25：用户配置替换完后再过一遍系统占位符解析。顺序不能反——用户若显式
        配了 NAS_IP（老数据里可能有），以用户值优先，系统解析只兜没配的。"""
        sub = lambda m: config.get(m.group(1), m.group(0))
        patch = {f: resolve_system_placeholders(
                     _ENV_PH_RE.sub(sub, getattr(spec, f, "") or ""))
                 for f in _SPEC_EXPR_FIELDS}
        patch["nr_headers"] = {k: resolve_system_placeholders(
                                   _ENV_PH_RE.sub(sub, str(v)))
                               for k, v in (spec.nr_headers or {}).items()}
        return _dc.replace(spec, **patch)

    async def _install_specs_to_nr(specs) -> dict:
        """把给定 ApiSpec 列表增量安装到 NR 的「AutoFlow API」tab，返回 {ok, ...}。

        与 install_link_api_tab_endpoint 共享实现：校验配置→解析真实 tab id→
        增量合并→写 NR→登记台账。_status 键用于通知调用方返回码。
        """
        from .api_specs import build_nr_tab_flows
        if not specs:
            return {"ok": True, "skipped": True, "specs": [],
                    "detail": {"reason": "无待安装 spec"}}
        # 1) 校验配置并派生有效 spec（占位符替换）
        effective, missing = [], []
        for s in specs:
            cfg = api_configs.get_api_config(s.name)
            lack = [f for f in _config_fields_for_spec(s) if not cfg.get(f)]
            if lack:
                missing.append({"name": s.name, "title": s.title, "missing": lack})
                continue
            effective.append(_effective_spec(s, cfg))
        if missing:
            return {
                "ok": False,
                "error": "以下 Link API 缺少必填配置，请先到「Link API」Tab 填写参数",
                "missing": missing,
                "_status": 400,
            }
        # 2) 解析真实 tab id（#177：绝不再用字面量 af_api_tab 探测）
        entry_ids = {(s.entry_link_id or f"{s.name}_in") for s in effective}
        tab_id, matched = await asyncio.to_thread(_resolve_af_api_tab_id, entry_ids)
        # 3) 读既有节点（tab_id 为 None = 尚未安装，无既有节点）
        existing = None
        if tab_id:
            try:
                existing = await asyncio.to_thread(gw.nr.get_flow, tab_id)
            except Exception:
                existing = None
        exist_nodes = (existing or {}).get("nodes", []) if isinstance(existing, dict) else []
        # 4) 生成节点：z 必须对齐真实 tab id；首次创建先用种子 id，
        #    create_or_update_flow 在 POST 拿到真实 id 后会统一改写 z。
        target_id = tab_id or AF_API_TAB_SEED_ID
        nodes = build_nr_tab_flows(target_id, specs=effective)
        gen_by_id = {n.get("id"): n for n in nodes}
        merged, added, updated = [], 0, 0
        for old in exist_nodes:
            gen = gen_by_id.get(old.get("id"))
            if gen is None:
                merged.append(old)          # 别人的节点：原样保留，绝不删改
                continue
            new = {**old, **gen}            # 我们的节点：就地刷新（配置改动才能生效）
            if new != old:
                updated += 1
            merged.append(new)
        exist_ids = {n.get("id") for n in exist_nodes}
        for n in nodes:
            if n.get("id") in exist_ids:
                continue                    # 幂等：已存在的 entry 链不重复生成
            merged.append(n)
            exist_ids.add(n.get("id"))
            added += 1
        # 5) 无新增也无变化 → 完全跳过 NR 写入（最强幂等，也最不打扰 prod）
        if tab_id and added == 0 and updated == 0:
            return {
                "ok": True, "tab_id": tab_id, "skipped": True,
                "specs": [s.name for s in effective],
                "nodes_before": len(exist_nodes), "nodes_added": 0,
                "nodes_updated": 0, "nodes_total": len(merged),
                "duplicate_tabs": matched if len(matched) > 1 else [],
                "detail": {"reason": "tab 已是最新，未写入 NR"},
            }
        flow_data = {
            "id": target_id,
            "label": (existing or {}).get("label") or AF_API_TAB_LABEL,
            "nodes": merged,
            "disabled": bool((existing or {}).get("disabled", False)),
            "info": (existing or {}).get("info", False),
        }
        try:
            res = await asyncio.to_thread(
                gw.nr.create_or_update_flow, target_id, flow_data, True, True)
        except Exception as e:
            return {"ok": False, "error": f"安装 tab 失败：{e}", "_status": 502}
        # 关键：以 NR 返回的真实 id 登记台账（nr_layer docstring 早有此要求）
        real_id = (res or {}).get("id") if isinstance(res, dict) else None
        real_id = real_id or tab_id or target_id
        if real_id and real_id != AF_API_TAB_SEED_ID:
            _af_tab_ledger_write(real_id)
        return {
            "ok": True, "tab_id": real_id,
            "tab_created": bool(isinstance(res, dict) and res.get("created")),
            "specs": [s.name for s in effective],
            "nodes_before": len(exist_nodes), "nodes_added": added,
            "nodes_updated": updated, "nodes_total": len(merged),
            "duplicate_tabs": matched if len(matched) > 1 else [],
            "detail": res,
        }

    async def install_link_api_tab_endpoint(request: Request):
        """A3(#170)：安装「AutoFlow API」tab（id=af_api_tab）到 NR。

        行为（硬约束）：
        1. 取所有 needs_nr_flow() 且非 self_use 的 spec（豆包系列被排除，不会重新生成）；
        2. 用 api_configs 表值替换 url/headers/body_template 的 <ENV> 占位符；
           任一 spec 缺配置 → 400 并指明先去填参数；
        3. 解析「AutoFlow API」tab 的真实 id（台账 + list_flows 兜底，#177）；
        4. build_nr_tab_flows(真实 tab id) 生成节点，z 字段对齐真实 tab；
        5. 增量合并：追加尚不存在的 entry 链，并就地刷新我们自己生成的节点
           （配置改了要能生效），绝不删除既有节点——保护 1990 里用户自用链路的硬约束；
        6. 幂等：无新增且无内容变化 → 直接跳过 NR 写入；allow_prod=True；
           首次创建后把 NR 返回的真实 id 写进台账，下次直接命中。
        """
        from .api_specs import API_SPECS
        candidates = [s for s in API_SPECS
                      if s.needs_nr_flow() and not getattr(s, "self_use", False)]
        result = await _install_specs_to_nr(candidates)
        if not result["ok"]:
            status = result.pop("_status", 400)
            return _js(result, status)
        return _js(result)

    async def install_single_link_api_endpoint(request: Request):
        """#C：给单个 Link API 单独的「安装到 Node-RED」按钮。

        复用 _install_specs_to_nr（与批量安装同一套增量合并/账本/幂等逻辑），
        只装传入的这一个 spec。比批量安装多了三道闸：
        - 未知 name → 404（与删除端点同口径；导入的 tab-link link_out 无 ApiSpec，
          也走 404，前端会隐藏其安装按钮）；
        - self_use 能力 → 403（与配置/删除端点一致）；
        - spec.needs_nr_flow() 为 False（http_api 内联 / 用户导入的 tab-link link_out）
          → 400 并说明「无需安装」（这些本来就不往 NR 写节点）。
        配置缺失同样委托给 _install_specs_to_nr → 400 + missing 清单。
        """
        name = request.path_params.get("name") or ""
        spec = get_api_spec(name)
        if spec is None:
            return _js({"ok": False, "error": f"未知 Link API: {name}"}, 404)
        if getattr(spec, "self_use", False):
            return _js({"ok": False, "error": "self_use 能力（网关自用）不可安装"}, 403)
        if not spec.needs_nr_flow():
            return _js({
                "ok": False,
                "error": "该 Link API 无需安装到 Node-RED（网关内联或指向既有 tab，"
                         "零 NR 写入）",
            }, 400)
        result = await _install_specs_to_nr([spec])
        if not result["ok"]:
            status = result.pop("_status", 400)
            return _js(result, status)
        return _js(result)

    def _derived_node_ids(spec, tab_id: str) -> set:
        """该 spec 在「AutoFlow API」tab 里派生出的节点 id 集合（#182）。

        id 规则不在这里复刻，而是直接调 build_nr_tab_flows 生成一遍取 id ——
        规则只有一处真相源（api_specs.build_nr_tab_flows），避免删除逻辑与安装
        逻辑对 id 命名各自为政，改一处漏一处就会留孤儿节点或误删。

        Args:
            spec: ApiSpec。
            tab_id: 目标 tab id（只影响节点的 z 字段，不影响 id）。
        Returns:
            节点 id 集合；spec 不生成 NR 流时返回空集。
        """
        from .api_specs import build_nr_tab_flows
        if not spec.needs_nr_flow():
            return set()
        return {n.get("id") for n in build_nr_tab_flows(tab_id, specs=[spec])
                if n.get("id")}

    async def delete_link_api_endpoint(request: Request):
        """#182：删除（卸载）一个 Link API。

        语义：Link API 是「网关声明的桥接能力 + 用户填的运行配置 + NR tab 里派生
        的执行链」三件套。删除 = 拆掉后两件（用户资产），spec 声明本身保留在
        api_specs.json（代码级数据，随版本库走，供日后重装），与 self_use 一贯做法一致。

        步骤（顺序有讲究）：
        1. 未知 name → 404；self_use 能力 → 403（与配置端点同口径）；
        2. 先清 NR：定位真实 tab（台账+扫描），移除本 spec 派生的节点后 PUT 回写
           （allow_prod=True）。**NR 写失败即 502 且本地状态一律不动** —— 宁可重试，
           也不能出现「配置已删、NR 里还挂着带旧 token 的孤儿链」；
        3. 再清本地：api_configs 配置行 + subflow_registry 登记（前端列表源自后者，
           删掉它面板才会真的少一条）。

        跨 spec 保护：若两个 spec 的 entry_link_id 撞车，只删「仅本 spec 会生成」
        的 id，其余留给它的主人。

        Returns:
            200 {ok, name, config_removed, registry_removed, nodes_removed,
                 node_ids, tab_id, nr_skipped}
        Raises:
            404 未知 Link API；403 self_use；400 该 key 不是 Link API；502 NR 写失败。
        """
        from .api_specs import API_SPECS
        name = request.path_params.get("name") or ""
        spec = get_api_spec(name)
        try:
            meta = await asyncio.to_thread(gw.tasks.get_subflow_meta, name)
        except Exception as e:
            return _js({"ok": False, "error": f"读取子流程元数据失败：{e}"}, 502)
        if spec is None and not meta:
            return _js({"ok": False, "error": f"未知 Link API: {name}"}, 404)
        if spec is not None and getattr(spec, "self_use", False):
            return _js({"ok": False,
                        "error": "self_use 能力（网关自用）不可删除"}, 403)
        if meta and (meta.get("kind") or "subflow") not in ("link_out", "http_api"):
            return _js({"ok": False,
                        "error": f"{name} 不是 Link API（kind={meta.get('kind')}），"
                                 f"请在「子流程」Tab 删除"}, 400)

        # ── 1) 清 NR：只删本 spec 独有的派生节点 ──
        nodes_removed, removed_ids, tab_id, nr_skipped = 0, [], None, True
        if spec is not None and spec.needs_nr_flow():
            others = [s for s in API_SPECS
                      if s.name != name and s.needs_nr_flow()
                      and not getattr(s, "self_use", False)]
            # 与 install-tab 同口径：只拿非 self_use 的入口 id 作重名 tab 的挑真身依据，
            # 免得豆包（用户自用 tab）的入口把解析引到人家的流程上。
            entry_ids = {(s.entry_link_id or f"{s.name}_in")
                         for s in API_SPECS
                         if s.needs_nr_flow() and not getattr(s, "self_use", False)}
            tab_id, _matched = await asyncio.to_thread(
                _resolve_af_api_tab_id, entry_ids)
            if tab_id:
                probe = tab_id or AF_API_TAB_SEED_ID
                own = _derived_node_ids(spec, probe)
                for s in others:                      # entry_link_id 撞车保护
                    own -= _derived_node_ids(s, probe)
                try:
                    existing = await asyncio.to_thread(gw.nr.get_flow, tab_id)
                except Exception:
                    existing = None
                exist_nodes = ((existing or {}).get("nodes", [])
                               if isinstance(existing, dict) else [])
                kept = [n for n in exist_nodes if n.get("id") not in own]
                removed_ids = [n.get("id") for n in exist_nodes
                               if n.get("id") in own]
                nodes_removed = len(exist_nodes) - len(kept)
                if nodes_removed:
                    flow_data = {
                        "id": tab_id,
                        "label": (existing or {}).get("label") or AF_API_TAB_LABEL,
                        "nodes": kept,
                        "disabled": bool((existing or {}).get("disabled", False)),
                        "info": (existing or {}).get("info", False),
                    }
                    try:
                        await asyncio.to_thread(
                            gw.nr.update_flow_nodes, tab_id, flow_data, True, True)
                    except Exception as e:
                        return _js({"ok": False,
                                    "error": f"清理 NR 派生节点失败（本地配置未动）：{e}"},
                                   502)
                    nr_skipped = False

        # ── 2) 清本地：配置 + 注册表登记 ──
        try:
            config_removed = await asyncio.to_thread(
                api_configs.delete_api_config, name)
        except Exception as e:
            return _js({"ok": False, "error": f"删除配置失败：{e}"}, 502)
        registry_removed = False
        if meta:
            r = await asyncio.to_thread(gw.tasks.delete_subflow, name)
            if not r.get("ok"):
                return _js({"ok": False, "error": r.get("error")}, 400)
            registry_removed = True
        return _js({
            "ok": True, "name": name,
            "config_removed": config_removed,
            "registry_removed": registry_removed,
            "nodes_removed": nodes_removed,
            "node_ids": removed_ids,
            "tab_id": tab_id,
            "nr_skipped": nr_skipped,
        })

    # ── 版本同步已剥离为独立 CLI（C3 / B8）──
    # 后端逻辑保留在 autoflow_gateway/sync.py，由命令行脚本调用；
    # WebUI 不再承载版本同步面板。

    # ── 设置管理界面：连接配置 / 设备保护 / 审计日志（C3/C21/C25）──
    async def connection_test(request: Request):
        """测试当前配置的 NR / HA 连通性（只读探测，不改任何状态）。

        兼容旧前端的扁平返回；新界面请用 /api/settings/connections/test。"""
        res = connections.test_connections(cfg, ["nr", "ha"], gateway=gw)
        return _js(res)

    # ── 连接设置（#45）：HA / Node-RED / Bark 的地址与凭据，界面可填、免重启生效 ──
    async def connections_view(request: Request):
        """列出连接设置。secret 字段只回掩码与来源，永不明文外传。"""
        return _js(connections.describe(cfg))

    async def connections_update(request: Request):
        """保存连接设置：落盘 gitignored 的 data/<env>/connections.json 并即时生效。"""
        b = await _body(request)
        try:
            result = connections.update(cfg, b)
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return _js({"ok": False, "error": f"保存失败: {e}"}, 500)
        payload = {"ok": True, **result}
        payload.update(connections.describe(cfg))
        # Bark 密钥改了之后，NR 里**已经生成过**的 bark_push 子流程仍持有旧值
        # （子流程 env 是生成时快照的）。这里如实提示，不擅自去改用户的 NR。
        touched = list(result.get("changed", [])) + list(result.get("cleared", []))
        # R3(#round4) 同族：NR 地址改了要把 debug 回读桥一起搬过去，否则它继续死连
        # 旧地址，debug_read 恒 connected:false —— 用户完全看不出是「桥没跟着搬」。
        if "NR_URL" in touched:
            try:
                bridge = getattr(gw, "debug_bridge", None)
                if bridge is not None and bridge.retarget(getattr(cfg, "nr_url", "")):
                    payload.setdefault("notices", []).append(
                        "NR 地址已变更，debug 回读桥已切到新地址（重连需数秒）。")
            except Exception:
                pass
        if any(k.startswith("BARK_") for k in touched):
            # 注意用 append 而非直接赋值：同时改了 NR_URL 与 BARK_* 时，
            # 直接赋值会把上面的桥重定向提示整条吞掉。
            payload.setdefault("notices", []).append(
                "Node-RED 中若已生成过 bark_push 子流程，它仍持有旧的 BARK_* 值。"
                "需要同步时：在 Node-RED 里删除该子流程，网关下次部署会用新值自动重建。"
            )
        return _js(payload)

    async def connections_test(request: Request):
        """连通性测试。body: {"targets":["ha","nr","bark"], "send_bark": false}。

        Bark 默认只校验配置完整性；send_bark=true 才真发一条测试推送。"""
        b = await _body(request)
        targets = b.get("targets")
        if targets is not None and not isinstance(targets, list):
            return _js({"ok": False, "error": "targets 必须是数组"}, 400)
        res = connections.test_connections(
            cfg, targets, gateway=gw, send_bark=bool(b.get("send_bark")))
        return _js({"ok": True, "results": res})

    async def device_guard_list(request: Request):
        return _js({"rules": device_guard.list()})

    async def device_guard_upsert(request: Request):
        b = await _body(request)
        try:
            rec = device_guard.upsert(b)
        except ValueError as e:
            return _js({"ok": False, "error": str(e)}, 400)
        return _js({"ok": True, "rule": rec}, 200)

    async def device_guard_delete(request: Request):
        rid = request.path_params["id"]
        ok = device_guard.delete(rid)
        return _js({"ok": ok}, 200 if ok else 404)

    # ── 设备目录 / 实体搜索（安全闸用，与连接配置解耦，不随测试连接触发）──
    async def catalog_view(request: Request):
        """GET /api/catalog → 设备目录摘要 {total, freshness, last_import_at}。"""
        cat = gw.state.get_device_catalog()
        ents = cat.get("entities", {}) or {}
        fresh = cat.get("freshness", "") or ""
        return _js({
            "ok": True,
            "total": len(ents),
            "freshness": fresh,
            "last_import_at": fresh,  # catalog 唯一时间戳即最近导入时刻
        })

    async def catalog_import(request: Request):
        """POST /api/catalog/import → 全量刷新设备目录（仅用户显式点击）。不触发部署。"""
        try:
            res = gw.refresh_catalog(full=True)
        except Exception as e:
            return _js({"ok": False, "error": f"导入失败: {e}"}, status=500)
        return _js({
            "ok": True,
            "total": res.get("entity_total", 0),
            "imported_at": res.get("freshness", ""),
            "mode": res.get("mode", "full"),
            "added": res.get("added", 0),
            "changed": res.get("changed", 0),
            "gone_marked": res.get("gone_marked", 0),
        })

    async def entities_view(request: Request):
        """GET /api/entities?keyword=&limit=20 → 中文/英文模糊搜 entity_id/friendly_name/area。"""
        kw = (request.query_params.get("keyword", "") or "").strip()
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        res = gw.list_entities(keyword=kw, limit=limit)
        return _js({"ok": True, **res})

    async def audit_list(request: Request):
        try:
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        return _js({"audit": audit_store.list(limit)})

    # ── 首次运行免责（C11/C21）──
    async def first_run_state(request: Request):
        flag = os.path.join(cfg.data_dir, ".first_run_accepted")
        return _js({"accepted": os.path.exists(flag)})

    async def first_run_accept(request: Request):
        flag = os.path.join(cfg.data_dir, ".first_run_accepted")
        try:
            with open(flag, "w", encoding="utf-8") as f:
                f.write("accepted")
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)
        return _js({"ok": True, "accepted": True})

    # ── 静态首页 ──
    async def index(request: Request):
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return _js({"ok": False, "error": "前端未构建：缺少 webui/static/index.html"}, 500)

    # ── 人工抽查（spotcheck）已迁移至 archive/agent-loop-migration/（C4）──

    # ── debug 回读（#644，只读，从本地缓冲取，绝不现打 NR）──
    async def debug_read_global(request: Request):
        return _debug_read_impl(request, None)

    async def debug_read(request: Request):
        flow_id = request.path_params.get("flow_id")
        return _debug_read_impl(request, flow_id)

    def _debug_read_impl(request: Request, flow_id):
        try:
            q = request.query_params
            node_id = (q.get("node_id") or "") or None
            since_s = (q.get("since") or "").strip()
            limit_s = (q.get("limit") or "50").strip()
            full = (q.get("full") or "0") in ("1", "true", "True")
            since = int(since_s) if since_s.isdigit() else None
            limit = int(limit_s) if limit_s.isdigit() else 50
            return _js(gw.get_debug_read(
                flow_id=flow_id, node_id=node_id, since=since, limit=limit, full=full))
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    # ── 触发 flow 内所有 inject（#644 debug 回读闭环的「触发」半环；仅点火，不修改 flow）──
    async def trigger_flow_endpoint(request: Request):
        flow_id = request.path_params.get("flow_id")
        try:
            flow = await asyncio.to_thread(gw.nr.get_flow, flow_id)
        except Exception as e:
            return _js({"ok": False, "error": f"获取 flow 失败: {e}"}, 500)
        injects = [n for n in (flow.get("nodes") or []) if n.get("type") == "inject"]
        if not injects:
            return _js({"ok": True, "flow_id": flow_id, "triggered": [], "errors": [],
                        "warning": "该 flow 没有 inject 节点，无触发目标"})
        triggered, errors = [], []
        for n in injects:
            try:
                code = await asyncio.to_thread(gw.nr.trigger_inject, n["id"])
                triggered.append({"id": n["id"], "name": n.get("name", ""), "status": code})
            except Exception as e:
                errors.append({"id": n["id"], "error": str(e)})
        return _js({"ok": True, "flow_id": flow_id, "triggered": triggered, "errors": errors})

    # PWA：manifest / Service Worker 挂在根路径（SW 必须根路径才能管控全站 scope）
    def _serve_static_root(name, media):
        p = os.path.join(static_dir, name)
        async def _handler(request):
            return FileResponse(
                p,
                media_type=media,
                headers={"Cache-Control": "no-store"},
            )
        return _handler

    routes = [
        Route("/manifest.webmanifest", _serve_static_root("manifest.webmanifest", "application/manifest+json")),
        Route("/sw.js", _serve_static_root("sw.js", "application/javascript")),
        Route("/api/health", health, methods=["GET"]),
        Route("/api/debug", debug_read_global, methods=["GET"]),
        Route("/api/debug/{flow_id}", debug_read, methods=["GET"]),
        Route("/api/config", config_view, methods=["GET"]),
        Route("/api/settings", settings_update, methods=["PUT"]),
        # Tab 组织模式（P2 迁移 + P3 预警）
        # 模板库（v1.5.2）
        Route("/api/templates", templates_list, methods=["GET"]),
        Route("/api/templates", templates_create, methods=["POST"]),
        Route("/api/templates/{template_id}", templates_get, methods=["GET"]),
        Route("/api/templates/{template_id}", templates_update, methods=["PUT"]),
        Route("/api/templates/{template_id}", templates_delete, methods=["DELETE"]),
        Route("/api/templates/{template_id}/render", templates_render, methods=["POST"]),
        # API Key 管理（v1.5.1）
        Route("/api/keys", api_keys_list, methods=["GET"]),
        Route("/api/keys", api_keys_create, methods=["POST"]),
        Route("/api/keys/{key_id}", api_keys_update, methods=["PUT"]),
        Route("/api/keys/{key_id}/revoke", api_keys_revoke, methods=["POST"]),
        Route("/api/keys/logs", api_keys_logs, methods=["GET"]),
        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API
        Route("/api/core/version", core_version, methods=["GET"]),
        Route("/api/core/health", core_health, methods=["GET"]),
        Route("/api/core/propose-dsl", core_propose_dsl, methods=["POST"]),
        Route("/api/core/deploy-proposal", core_deploy_proposal, methods=["POST"]),
        Route("/api/core/deploy-raw", core_deploy_raw, methods=["POST"]),
        Route("/api/core/entities", core_entities, methods=["GET"]),
        Route("/api/core/resolve-entity", core_resolve_entity, methods=["GET"]),
        Route("/api/core/snapshots", core_snapshots, methods=["GET"]),
        Route("/api/core/rollback", core_rollback, methods=["POST"]),
        Route("/api/nr/tabs", nr_tabs, methods=["GET"]),
        Route("/api/tab-org/status", tab_org_status, methods=["GET"]),
        Route("/api/tab-org/migrate", tab_org_migrate, methods=["POST"]),
        # 部署授权码（P4）
        Route("/api/deploy-tokens", deploy_token_list, methods=["GET"]),
        Route("/api/deploy-tokens", deploy_token_create, methods=["POST"]),
        Route("/api/deploy-tokens/{id}", deploy_token_revoke, methods=["DELETE"]),
        Route("/api/deploy-tokens/{id}/logs", deploy_token_logs, methods=["GET"]),
        Route("/api/deploy-tokens/{id}/snapshots", deploy_token_snapshots, methods=["GET"]),
        Route("/api/deploy-tokens/{id}/rollback", deploy_token_rollback, methods=["POST"]),
        Route("/api/deploy-tokens/{id}/diff", deploy_token_diff, methods=["GET"]),
        # Lab 沙盒部署（缺陷D修复）
        Route("/api/lab/validate", lab_validate, methods=["POST"]),
        Route("/api/lab/deploy", lab_deploy, methods=["POST"]),
        Route("/api/lab/deploys", lab_deploys, methods=["GET"]),
        # 设置管理界面（C3/C21/C25）
        Route("/api/connection/test", connection_test, methods=["POST"]),
        # 连接设置（#45）：HA / NR / Bark 凭据界面化，避免用户硬编码进脚本
        Route("/api/settings/connections", connections_view, methods=["GET"]),
        Route("/api/settings/connections", connections_update, methods=["PUT"]),
        Route("/api/settings/connections/test", connections_test, methods=["POST"]),
        Route("/api/device-guard", device_guard_list, methods=["GET"]),
        Route("/api/device-guard", device_guard_upsert, methods=["POST"]),
        Route("/api/device-guard/{id}", device_guard_delete, methods=["DELETE"]),
        # 设备目录（安全闸用，连接配置解耦）
        Route("/api/catalog", catalog_view, methods=["GET"]),
        Route("/api/catalog/import", catalog_import, methods=["POST"]),
        Route("/api/entities", entities_view, methods=["GET"]),
        Route("/api/audit", audit_list, methods=["GET"]),
        Route("/api/first-run", first_run_state, methods=["GET"]),
        Route("/api/first-run", first_run_accept, methods=["POST"]),
        Route("/api/diagnostics", diagnostics_view, methods=["GET"]),
        # 受控自更新（方案 C）：仅 owner（开发者）可触发；WebUI RBAC 层把关
        Route("/api/admin/update-check", update_check_endpoint, methods=["GET"]),
        Route("/api/admin/self-update", self_update_endpoint, methods=["POST"]),
        # ── 账号登录 / 会话 / 用户管理（WebUI 专用）──
        # 注意顺序：/api/auth/sessions/{id} 必须排在 /api/auth/sessions 之后
        Route("/api/auth/state", auth_state, methods=["GET"]),
        Route("/api/auth/register", auth_register, methods=["POST"]),
        Route("/api/auth/login", auth_login, methods=["POST"]),
        Route("/api/auth/logout", auth_logout, methods=["POST"]),
        Route("/api/auth/me", auth_me, methods=["GET"]),
        Route("/api/auth/change-password", auth_change_password, methods=["POST"]),
        Route("/api/auth/sessions", auth_sessions_list, methods=["GET"]),
        Route("/api/auth/sessions/{id}", auth_sessions_delete, methods=["DELETE"]),
        Route("/api/auth/users", auth_users_list, methods=["GET"]),
        Route("/api/auth/users", auth_users_create, methods=["POST"]),
        Route("/api/auth/users/{id}", auth_users_update, methods=["PUT"]),
        Route("/api/auth/users/{id}", auth_users_delete, methods=["DELETE"]),
        Route("/api/auth/users/{id}/reset-password", auth_users_reset_password,
              methods=["POST"]),
        # agents
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents", create_agent, methods=["POST"]),
        Route("/api/agents/{id}/revoke", revoke_agent, methods=["POST"]),
        Route("/api/agents/{id}/regen", regen_agent, methods=["POST"]),
        Route("/api/agents/{id}", update_agent, methods=["PUT"]),
        Route("/api/agents/{id}", delete_agent, methods=["DELETE"]),
        # ACP 对等令牌（acp_ 前缀，仅用于 /acp）
        Route("/api/acp/tokens", list_acp_tokens, methods=["GET"]),
        Route("/api/acp/tokens", create_acp_token, methods=["POST"]),
        Route("/api/acp/tokens/{id}/revoke", revoke_acp_token, methods=["POST"]),
        Route("/api/acp/tokens/{id}", delete_acp_token, methods=["DELETE"]),
        # ACP 启用开关（前端「ACP 令牌」页开关调用）
        Route("/api/acp/enabled", get_acp_enabled, methods=["GET"]),
        Route("/api/acp/enabled", set_acp_enabled, methods=["PUT"]),
        # LLM 配置与内置助手（DEV-llm-webui-agent）
        Route("/api/llm/config", get_llm_config, methods=["GET"]),
        Route("/api/llm/config", set_llm_config, methods=["PUT"]),
        Route("/api/llm/chat", llm_chat, methods=["POST"]),
        Route("/api/llm/test", test_llm, methods=["POST"]),
        # 确认闸
        Route("/api/pending", list_pending, methods=["GET"]),
        Route("/api/pending/{id}/approve", approve, methods=["POST"]),
        Route("/api/pending/{id}/reject", reject, methods=["POST"]),
        # 提案
        Route("/api/proposals", list_proposals, methods=["GET"]),
        Route("/api/proposals", submit_proposal, methods=["POST"]),
        Route("/api/proposals/{id}/promote", promote_proposal, methods=["POST"]),
        Route("/api/proposals/{id}/reject", reject_proposal, methods=["POST"]),
        Route("/api/proposals/{id}/delete", delete_proposal, methods=["DELETE"]),
        Route("/api/proposals/{id}/deploy", deploy_proposal, methods=["POST"]),
        Route("/api/proposals/{id}/archive", archive_proposal, methods=["POST"]),
        Route("/api/proposals/{id}/unarchive", unarchive_proposal, methods=["POST"]),
        # 已部署
        Route("/api/deployed", list_deployed, methods=["GET"]),
        Route("/api/deployed/{id}/undeploy", undeploy_flow, methods=["POST"]),
        Route("/api/flows/{flow_id}/trigger", trigger_flow_endpoint, methods=["POST"]),
        # （C2/C4）白盒部署面板与人工抽查端点已剥离，能力迁 archive/agent-loop-migration/

        # 笔记
        Route("/api/notes", list_notes, methods=["GET"]),
        Route("/api/notes", create_note, methods=["POST"]),
        Route("/api/notes/{id}", update_note, methods=["PUT"]),
        Route("/api/notes/{id}", delete_note, methods=["DELETE"]),
        # 子流程注册表（#575/#579）：列出 + 自省导入
        Route("/api/subflows", list_subflows, methods=["GET"]),
        Route("/api/subflows/import", import_subflow, methods=["POST"]),
        Route("/api/subflows/{key}/status", set_subflow_status, methods=["PATCH"]),
        Route("/api/subflows/{key}/ensure", ensure_subflow_endpoint, methods=["POST"]),
        Route("/api/subflows/bark/install", install_bark_subflow_endpoint, methods=["POST"]),
        Route("/api/subflows/{key}", delete_subflow_endpoint, methods=["DELETE"]),
        # A2：Link API 配置读写（GET 读 / PUT 写 api_configs 表）
        Route("/api/link-apis/{name}/config", link_api_config_endpoint,
              methods=["GET", "PUT"]),
        # A3：安装「AutoFlow API」tab 到 NR（增量合并，绝不整体覆盖）
        Route("/api/link-apis/install-tab", install_link_api_tab_endpoint,
              methods=["POST"]),
        # #C：单个 Link API 的「安装到 Node-RED」按钮（复用 _install_specs_to_nr）。
        # 路径比 {name} 多一段 /install，Starlette 不会与下面 {name} DELETE 撞匹配。
        Route("/api/link-apis/{name}/install", install_single_link_api_endpoint,
              methods=["POST"]),
        # #C-tab：tab 链接逆生成 Link API（只读自省 → 注册薄桥接 link_out）
        Route("/api/link-apis/import-from-url", import_link_api_from_tab_endpoint,
              methods=["POST"]),
        Route("/api/link-apis/register-from-tab", register_link_api_from_tab_endpoint,
              methods=["POST"]),
        # #182：删除（卸载）Link API —— 清配置 + 清 tab 内派生节点 + 取消登记。
        # 必须排在 install-tab 之后：`{name}` 会字面匹配 "install-tab"，
        # Starlette 靠方法不同（POST vs DELETE）走 partial-match 继续找，
        # 但顺序摆对更不容易在日后加 POST 时踩坑。
        Route("/api/link-apis/{name}", delete_link_api_endpoint,
              methods=["DELETE"]),

        # 静态
        Route("/", index, methods=["GET"]),
    ]
    if os.path.isdir(static_dir):
        routes.append(Mount("/static", StaticFiles(directory=static_dir), name="static"))

    app = Starlette(routes=routes)

    # ── 禁用前端资源缓存：/ 与 /static/* 一律 no-store ──
    # 避免旧 app.js / style.css 滞留浏览器，导致页面脚本不更新、表现成「加载中… 刷不出来」。
    raw_app = app

    async def no_cache_mw(scope, receive, send):
        if scope["type"] != "http":
            return await raw_app(scope, receive, send)
        p = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                if p == "/" or p.startswith("/static"):
                    h = [(k, v) for (k, v) in message.get("headers", [])
                         if k.lower() != b"cache-control"]
                    h.append((b"cache-control", b"no-store"))
                    message["headers"] = h
            await send(message)

        await raw_app(scope, receive, send_wrapper)

    app = no_cache_mw

    # ── WebUI 鉴权闸门：账号密码会话（主通道）+ 旧令牌（兼容通道）──
    # ★ 三套令牌隔离铁律（I-2）：本闸门只覆盖 WebUI 的 /api。
    #   /mcp* 的 af_ 身份码由 mcp_server 的中间件独立强制，/acp 用 acp_ 令牌，三者互不相认。
    # 判定顺序：白名单 → 会话 Cookie → 旧令牌（仅 both/token_only）→ 拒绝。
    raw = app

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await raw(scope, receive, send)
        p = scope.get("path", "")
        if not p.startswith("/api"):
            return await raw(scope, receive, send)
        method = (scope.get("method") or "GET").upper()

        # 1) 免鉴权白名单：登录/注册/探测必须匿名可达，否则登不进来
        if _wa.is_public_path(p):
            return await raw(scope, receive, send)

        # 2) 主通道：服务端会话（Cookie）。★ token_only 回滚模式：密码子系统关闭，不受理会话
        sess = None if auth.auth_mode == "token_only" else auth.resolve_session(_wa.session_id_from_scope(scope))
        if sess is not None:
            # I-9 第二层：Cookie 是环境权限，写请求必须带同源自定义头，防 CSRF
            if _wa.needs_csrf(method) and not _wa.csrf_ok(scope):
                await JSONResponse(
                    {"ok": False, "error": "csrf: 写请求缺少同源请求头"},
                    status_code=403,
                )(scope, receive, send)
                return
            # RBAC（D1 多用户）：未登记路径的写操作 fail-closed 要求 admin
            need = _wa.required_role_for(method, p)
            if need and _wa.role_rank(sess["role"]) < _wa.role_rank(need):
                await JSONResponse(
                    {"ok": False, "error": f"forbidden: 需要 {need} 权限",
                     "need_role": need, "role": sess["role"]},
                    status_code=403,
                )(scope, receive, send)
                return
            scope["af_auth"] = {
                "mode": "session", "user_id": sess["user_id"],
                "username": sess["username"], "role": sess["role"],
                "session_id": sess["session_id"],
            }
            return await raw(scope, receive, send)

        # 3) 兼容通道：旧令牌。★豁免 CSRF —— 令牌不是环境权限，跨站拿不到，
        #    天然免疫 CSRF；同时给脚本/CI 留活路（I-9 第三层）。
        if auth.resolve_legacy_token(scope):
            scope["af_auth"] = {
                "mode": "token", "user_id": "", "username": "legacy-token",
                "role": "owner", "session_id": "",
            }
            return await raw(scope, receive, send)

        # 4) 无任何身份。
        # ★S-4 止血：token_only 模式，或「未初始化且仍依赖旧令牌体系」时，
        #   远程无认证访问一律 403，防止 Docker 0.0.0.0 把控制面裸奔到公网。
        # password_only 模式下首次注册必须允许远程访问（用户从 LAN 浏览器初始化）；
        # 未命中白名单的非公开路径返回 401，由前端弹出注册向导。
        no_auth_possible = (auth.auth_mode == "token_only") or (
            (not auth.has_users()) and auth.auth_mode != "password_only"
        )
        if no_auth_possible:
            if not _is_loopback(scope):
                await JSONResponse(
                    {"ok": False, "error": "unauthorized: WebUI token required for non-local access"},
                    status_code=403,
                )(scope, receive, send)
                return
            # 本机/回环：沿用既有「未配置认证则本机开放」行为（便于本机完成初始化）
            return await raw(scope, receive, send)

        # 已初始化 → 401（前端据此弹登录框；区别于 403 的「权限不足/来源被拒」）
        await JSONResponse(
            {"ok": False, "error": "unauthorized", "auth_required": True},
            status_code=401,
        )(scope, receive, send)

    return guarded
