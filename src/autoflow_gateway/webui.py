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
import json
import asyncio
import secrets
import hmac
import tempfile
from typing import Optional, Dict, Any

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, FileResponse
from starlette.staticfiles import StaticFiles
from starlette.requests import Request

from .config import get_config, is_task_pool_enabled, is_raw_node_escape_enabled, is_submit_gate_enabled, set_feature_flag, get_deploy_policy, set_deploy_policy
from .gateway import Gateway
from .identity import AgentStore
from .proposals import ProposalStore
from .notes import NoteStore
from .device_guard import DeviceGuardStore
from .audit import AuditStore
from .subflows import introspect_nr_subflow, validate_subflow_registration
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
    proposals = ProposalStore(cfg)
    notes = NoteStore(cfg)
    device_guard = DeviceGuardStore(cfg)
    audit_store = AuditStore(gw)
    static_dir = os.path.join(os.path.dirname(__file__), "webui", "static")
    index_path = os.path.join(static_dir, "index.html")

    # ── 助手：解析 JSON body ──
    async def _body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    # ── 健康检查 / 配置 ──
    async def health(request: Request):
        return _js({"ok": True, "env": cfg.env, "service": "autoflow-webui"})

    async def config_view(request: Request):
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
        })

    async def settings_update(request: Request):
        """运行时开关：DSL 验证任务池 / 原生节点逃逸 / 部署策略（免重启落盘）。"""
        b = await _body(request)
        tp = b.get("task_pool_enabled")
        if tp is not None:
            if not isinstance(tp, bool):
                return _js({"ok": False, "error": "task_pool_enabled 必须是布尔值"}, 400)
            set_feature_flag(cfg, "task_pool_enabled", tp)
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
        return _js({"ok": True,
                    "task_pool_enabled": is_task_pool_enabled(cfg),
                    "raw_node_escape_enabled": is_raw_node_escape_enabled(cfg),
                    "submit_run_gate": is_submit_gate_enabled(cfg),
                    "deploy_policy": get_deploy_policy(cfg)})

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
                name, tier, b.get("notes", ""), mode=b.get("mode", "both")
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
        try:
            res = await asyncio.to_thread(gw.deploy_proposal, pid, agent_id="human",
                                          target=target, force=force, validate=validate,
                                          require_e2e=require_e2e, allow_prod=allow_prod)
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
        src = request.query_params.get("source_type")
        st = request.query_params.get("status")
        rows = await asyncio.to_thread(gw.tasks.list_subflows,
                                        source_type=src, status=st)
        return _js({"subflows": rows, "count": len(rows)})

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
        meta = await asyncio.to_thread(gw.tasks.get_subflow_meta, key)
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
        if any(k.startswith("BARK_") for k in touched):
            payload["notices"] = [
                "Node-RED 中若已生成过 bark_push 子流程，它仍持有旧的 BARK_* 值。"
                "需要同步时：在 Node-RED 里删除该子流程，网关下次部署会用新值自动重建。"
            ]
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

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/debug", debug_read_global, methods=["GET"]),
        Route("/api/debug/{flow_id}", debug_read, methods=["GET"]),
        Route("/api/config", config_view, methods=["GET"]),
        Route("/api/settings", settings_update, methods=["PUT"]),
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
        # agents
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents", create_agent, methods=["POST"]),
        Route("/api/agents/{id}/revoke", revoke_agent, methods=["POST"]),
        Route("/api/agents/{id}/regen", regen_agent, methods=["POST"]),
        Route("/api/agents/{id}", update_agent, methods=["PUT"]),
        Route("/api/agents/{id}", delete_agent, methods=["DELETE"]),
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
        Route("/api/subflows/{key}", delete_subflow_endpoint, methods=["DELETE"]),

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

    # ── 可选 WebUI token 闸门 ──
    # 每次请求实时解析令牌（支持：放文件/改环境变量后立即生效，无需重启网关）。
    # 令牌缺失则整段跳过（仅本机/可信网络开放并打告警，符合原有默认行为）。
    raw = app

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await raw(scope, receive, send)
        p = scope.get("path", "")
        if not p.startswith("/api"):
            return await raw(scope, receive, send)
        token = _resolve_webui_token(cfg)
        if not token:
            # ★S-4 止血：未配置 token 时仅放行本机/回环；远程无认证访问一律拒（防 Docker 0.0.0.0 暴露）。
            if not _is_loopback(scope):
                await JSONResponse(
                    {"ok": False, "error": "unauthorized: WebUI token required for non-local access"},
                    status_code=403,
                )(scope, receive, send)
                return
            return await raw(scope, receive, send)
        headers = dict(scope.get("headers", []))
        provided = None
        auth = headers.get(b"authorization")
        if auth:
            provided = auth.decode().removeprefix("Bearer ").strip()
        if not provided:
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode())
            provided = (qs.get("token") or [None])[0]
        if not provided:
            provided = _cookie_token(headers)
        # ★S-1 安全修复：常量时间比较，避免时序攻击逐字节猜 token
        if not hmac.compare_digest(provided or "", token or ""):
            await JSONResponse({"ok": False, "error": "unauthorized"}, status_code=403)(scope, receive, send)
            return
        await raw(scope, receive, send)

    return guarded
