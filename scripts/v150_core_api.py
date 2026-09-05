#!/usr/bin/env python3
"""v1.5.0: 添加 /api/core/* 系列 REST API（AutoFlow Pro 入口）"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 在 /api/nr/tabs 之前插入 core API 处理函数
insert_marker = '''    # ── Tab 组织模式：迁移状态 + 执行迁移（P2）──'''

core_apis = '''    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──
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

'''

if insert_marker in content:
    content = content.replace(insert_marker, core_apis + insert_marker, 1)
    print("1. core API 处理函数插入: OK")
else:
    print("1. core API 处理函数插入: NOT FOUND")

# 在路由表中注册 core API 路由
route_marker = '''        Route("/api/nr/tabs", nr_tabs, methods=["GET"]),'''

core_routes = '''        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API
        Route("/api/core/version", core_version, methods=["GET"]),
        Route("/api/core/health", core_health, methods=["GET"]),
        Route("/api/core/propose-dsl", core_propose_dsl, methods=["POST"]),
        Route("/api/core/deploy-proposal", core_deploy_proposal, methods=["POST"]),
        Route("/api/core/deploy-raw", core_deploy_raw, methods=["POST"]),
        Route("/api/core/entities", core_entities, methods=["GET"]),
        Route("/api/core/resolve-entity", core_resolve_entity, methods=["GET"]),
        Route("/api/core/snapshots", core_snapshots, methods=["GET"]),
        Route("/api/core/rollback", core_rollback, methods=["POST"]),
        Route("/api/nr/tabs", nr_tabs, methods=["GET"]),'''

if route_marker in content:
    content = content.replace(route_marker, core_routes, 1)
    print("2. core API 路由注册: OK")
else:
    print("2. core API 路由注册: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
