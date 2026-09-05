#!/usr/bin/env python3
"""v1.5.1: webui.py 增加 API Key 管理 API + /api/core/* 认证中间件"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 core API 处理函数之前，增加 API Key store 和认证辅助函数
insert_marker = '''    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──'''

api_key_code = '''    # ── API Key 管理（v1.5.1）：Agent 身份认证 + 授权范围 ──
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

'''

if insert_marker in content:
    content = content.replace(insert_marker, api_key_code + insert_marker, 1)
    print("1. API Key 管理函数插入: OK")
else:
    print("1. API Key 管理函数插入: NOT FOUND")

# 2. 给 core API 增加认证（修改 propose-dsl、deploy-raw、deploy-proposal、rollback）
# propose-dsl 需要 deploy 权限
old_propose = '''    async def core_propose_dsl(request: Request):
        """【★首选】提交 DSL，编译+闸门校验，返回提案。AutoFlow Pro 主入口。"""
        try:
            b = await _body(request)'''

new_propose = '''    async def core_propose_dsl(request: Request):
        """【★首选】提交 DSL，编译+闸门校验，返回提案。AutoFlow Pro 主入口。"""
        agent_info, err = _require_api_key(request, required_perm="deploy")
        if err:
            return err
        try:
            b = await _body(request)'''

if old_propose in content:
    content = content.replace(old_propose, new_propose, 1)
    print("2. propose-dsl 认证: OK")
else:
    print("2. propose-dsl 认证: NOT FOUND")

# deploy-raw 需要 modify 权限
old_raw = '''    async def core_deploy_raw(request: Request):
        """【⚠️逃生舱】直接提交 raw Node-RED flow JSON。仅在 DSL 表达不了时使用。"""
        try:
            b = await _body(request)'''

new_raw = '''    async def core_deploy_raw(request: Request):
        """【⚠️逃生舱】直接提交 raw Node-RED flow JSON。仅在 DSL 表达不了时使用。"""
        agent_info, err = _require_api_key(request, required_perm="modify")
        if err:
            return err
        try:
            b = await _body(request)'''

if old_raw in content:
    content = content.replace(old_raw, new_raw, 1)
    print("3. deploy-raw 认证: OK")
else:
    print("3. deploy-raw 认证: NOT FOUND")

# deploy-proposal 需要 deploy 权限
old_dp = '''    async def core_deploy_proposal(request: Request):
        """部署已通过的提案到 NR。"""
        try:
            b = await _body(request)'''

new_dp = '''    async def core_deploy_proposal(request: Request):
        """部署已通过的提案到 NR。"""
        agent_info, err = _require_api_key(request, required_perm="deploy")
        if err:
            return err
        try:
            b = await _body(request)'''

if old_dp in content:
    content = content.replace(old_dp, new_dp, 1)
    print("4. deploy-proposal 认证: OK")
else:
    print("4. deploy-proposal 认证: NOT FOUND")

# rollback 需要 modify 权限
old_rb = '''    async def core_rollback(request: Request):
        """回滚到指定快照。"""
        try:
            b = await _body(request)'''

new_rb = '''    async def core_rollback(request: Request):
        """回滚到指定快照。"""
        agent_info, err = _require_api_key(request, required_perm="modify")
        if err:
            return err
        try:
            b = await _body(request)'''

if old_rb in content:
    content = content.replace(old_rb, new_rb, 1)
    print("5. rollback 认证: OK")
else:
    print("5. rollback 认证: NOT FOUND")

# entities 和 resolve-entity 需要 read 权限
old_ent = '''    async def core_entities(request: Request):
        """实体目录查询（按域/区域/关键词过滤）。"""
        try:'''

new_ent = '''    async def core_entities(request: Request):
        """实体目录查询（按域/区域/关键词过滤）。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:'''

if old_ent in content:
    content = content.replace(old_ent, new_ent, 1)
    print("6. entities 认证: OK")
else:
    print("6. entities 认证: NOT FOUND")

old_re = '''    async def core_resolve_entity(request: Request):
        """自然语言设备名 → entity_id 候选。"""
        try:'''

new_re = '''    async def core_resolve_entity(request: Request):
        """自然语言设备名 → entity_id 候选。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:'''

if old_re in content:
    content = content.replace(old_re, new_re, 1)
    print("7. resolve-entity 认证: OK")
else:
    print("7. resolve-entity 认证: NOT FOUND")

# snapshots 需要 read 权限
old_snap = '''    async def core_snapshots(request: Request):
        """快照列表。"""
        try:'''

new_snap = '''    async def core_snapshots(request: Request):
        """快照列表。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:'''

if old_snap in content:
    content = content.replace(old_snap, new_snap, 1)
    print("8. snapshots 认证: OK")
else:
    print("8. snapshots 认证: NOT FOUND")

# 3. 在路由表中注册 API Key 管理路由
route_marker = '''        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API'''

api_key_routes = '''        # API Key 管理（v1.5.1）
        Route("/api/keys", api_keys_list, methods=["GET"]),
        Route("/api/keys", api_keys_create, methods=["POST"]),
        Route("/api/keys/{key_id}", api_keys_update, methods=["PUT"]),
        Route("/api/keys/{key_id}/revoke", api_keys_revoke, methods=["POST"]),
        Route("/api/keys/logs", api_keys_logs, methods=["GET"]),
        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API'''

if route_marker in content:
    content = content.replace(route_marker, api_key_routes, 1)
    print("9. API Key 路由注册: OK")
else:
    print("9. API Key 路由注册: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
