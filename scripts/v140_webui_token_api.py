#!/usr/bin/env python3
"""修改 webui.py: 增加授权码 CRUD API + 快照/回滚 API"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

# 在 tab_org_migrate 函数后面增加授权码 API
old_marker = '''    # ── 诊断查看器（P4-C，只读）──
    async def diagnostics_view(request: Request):'''

new_marker = '''    # ── 部署授权码（P4）：CRUD + 日志 + 快照/回滚 ──
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
        """创建授权码。"""
        b = await _body(request)
        name = (b.get("name") or "").strip()
        target_tab = (b.get("target_tab") or "").strip()
        if not name:
            return _js({"ok": False, "error": "名称不能为空"}, 400)
        if not target_tab:
            return _js({"ok": False, "error": "目标 tab 不能为空"}, 400)

        try:
            token = _token_store().create_token(
                name=name,
                target_tab=target_tab,
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

    # ── 诊断查看器（P4-C，只读）──
    async def diagnostics_view(request: Request):'''

if old_marker in content:
    content = content.replace(old_marker, new_marker, 1)
    print("1. 增加授权码 CRUD API 函数")
else:
    print("WARNING: 未找到诊断查看器标记")

# 增加路由注册
old_routes = '''        Route("/api/tab-org/status", tab_org_status, methods=["GET"]),
        Route("/api/tab-org/migrate", tab_org_migrate, methods=["POST"]),'''

new_routes = '''        Route("/api/tab-org/status", tab_org_status, methods=["GET"]),
        Route("/api/tab-org/migrate", tab_org_migrate, methods=["POST"]),
        # 部署授权码（P4）
        Route("/api/deploy-tokens", deploy_token_list, methods=["GET"]),
        Route("/api/deploy-tokens", deploy_token_create, methods=["POST"]),
        Route("/api/deploy-tokens/{id}", deploy_token_revoke, methods=["DELETE"]),
        Route("/api/deploy-tokens/{id}/logs", deploy_token_logs, methods=["GET"]),
        Route("/api/deploy-tokens/{id}/snapshots", deploy_token_snapshots, methods=["GET"]),
        Route("/api/deploy-tokens/{id}/rollback", deploy_token_rollback, methods=["POST"]),
        Route("/api/deploy-tokens/{id}/diff", deploy_token_diff, methods=["GET"]),'''

if old_routes in content:
    content = content.replace(old_routes, new_routes, 1)
    print("2. 增加授权码 API 路由注册")
else:
    print("WARNING: 未找到 tab-org 路由注册位置")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

print("\nwebui.py 授权码 API 完成")
