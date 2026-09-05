#!/usr/bin/env python3
"""修复缺陷D: 实现 /lab/* 路由（validate/deploy/deploys）"""

WU = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WU, "r", encoding="utf-8") as f:
    content = f.read()

# 在 deploy_token_diff 函数之后增加 /lab/* API
old_marker = '''    async def deploy_token_diff(request: Request):
        """对比两个快照的差异。"""
        token_id = request.path_params["id"]
        snap1 = request.query_params.get("snapshot_1")
        snap2 = request.query_params.get("snapshot_2")
        if not snap1 or not snap2:
            return _js({"ok": False, "error": "需要 snapshot_1 和 snapshot_2 参数"}, 400)
        result = _snap_mgr().diff_snapshots(token_id, snap1, snap2)
        return _js(result)

    # ── 诊断查看器（P4-C，只读）──'''

new_marker = '''    async def deploy_token_diff(request: Request):
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

    # ── 诊断查看器（P4-C，只读）──'''

if old_marker in content:
    content = content.replace(old_marker, new_marker, 1)
    print("1. 增加 /lab/* API 函数")
else:
    print("WARNING: 未找到 deploy_token_diff 标记")

# 增加路由注册
old_routes = '''        Route("/api/deploy-tokens/{id}/diff", deploy_token_diff, methods=["GET"]),'''

new_routes = '''        Route("/api/deploy-tokens/{id}/diff", deploy_token_diff, methods=["GET"]),
        # Lab 沙盒部署（缺陷D修复）
        Route("/lab/validate", lab_validate, methods=["POST"]),
        Route("/lab/deploy", lab_deploy, methods=["POST"]),
        Route("/lab/deploys", lab_deploys, methods=["GET"]),'''

if old_routes in content:
    content = content.replace(old_routes, new_routes, 1)
    print("2. 增加 /lab/* 路由注册")
else:
    print("WARNING: 未找到路由注册位置")

with open(WU, "w", encoding="utf-8") as f:
    f.write(content)

print("\nwebui.py /lab/* 路由实现完成")
