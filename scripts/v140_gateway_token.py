#!/usr/bin/env python3
"""修改 gateway.py: 增加授权码验证 + 自动部署逻辑"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 1. propose_dsl 函数签名增加 deploy_token 参数
old_sig1 = '''    def propose_dsl(self, dsl: str, agent_id: str,
                    expected_postconditions: Optional[List[Dict]] = None,
                    resolved_entities: Optional[List[str]] = None,
                    vhass_store=None, strict: bool = False,
                    require_e2e: bool = False) -> Dict[str, Any]:'''

new_sig1 = '''    def propose_dsl(self, dsl: str, agent_id: str,
                    expected_postconditions: Optional[List[Dict]] = None,
                    resolved_entities: Optional[List[str]] = None,
                    vhass_store=None, strict: bool = False,
                    require_e2e: bool = False,
                    deploy_token: Optional[str] = None) -> Dict[str, Any]:'''

if old_sig1 in content:
    content = content.replace(old_sig1, new_sig1, 1)
    print("1. propose_dsl 增加 deploy_token 参数")
else:
    print("WARNING: 未找到 propose_dsl 签名")

# 2. 在 propose_dsl 提案落档后增加自动部署逻辑
old_after_submit = '''        _slog(_tid, "propose_dsl.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, gate_passed=bool(gate.get("passed")))
        # lint 摘要已在 lint 阶段统一计算（lint_summary / lint_error_count / lint_warning_count）
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "snapshot": snap,
            "_trace_id": _tid,'''

new_after_submit = '''        # P4 授权码自动部署：如果 deploy_token 有效且闸门通过，自动部署
        auto_deploy_result = None
        if deploy_token and proposal_id and gate.get("passed"):
            try:
                auto_deploy_result = self._try_auto_deploy_with_token(
                    deploy_token, proposal_id, agent_id, len(flow.get("nodes", [])),
                    operation="deploy")
            except Exception as _ade:
                auto_deploy_result = {"ok": False, "error": f"自动部署异常: {_ade}",
                                      "fallback": "manual"}

        _slog(_tid, "propose_dsl.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, gate_passed=bool(gate.get("passed")),
              auto_deploy=auto_deploy_result.get("ok") if auto_deploy_result else None)
        # lint 摘要已在 lint 阶段统一计算（lint_summary / lint_error_count / lint_warning_count）
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "snapshot": snap,
            "_trace_id": _tid,
            "auto_deploy": auto_deploy_result,'''

if old_after_submit in content:
    content = content.replace(old_after_submit, new_after_submit, 1)
    print("2. propose_dsl 增加自动部署逻辑")
else:
    print("WARNING: 未找到 propose_dsl 提案落档后位置")

# 3. propose_raw 函数签名增加 deploy_token 参数
old_sig2 = '''    def propose_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                    label: Optional[str] = None,
                    target: str = "staging", force: bool = False,
                    run_gate: bool = True, dry_run: bool = False,
                    require_e2e: bool = False,
                    target_tab: Optional[str] = None) -> Dict[str, Any]:'''

new_sig2 = '''    def propose_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                    label: Optional[str] = None,
                    target: str = "staging", force: bool = False,
                    run_gate: bool = True, dry_run: bool = False,
                    require_e2e: bool = False,
                    target_tab: Optional[str] = None,
                    deploy_token: Optional[str] = None) -> Dict[str, Any]:'''

if old_sig2 in content:
    content = content.replace(old_sig2, new_sig2, 1)
    print("3. propose_raw 增加 deploy_token 参数")
else:
    print("WARNING: 未找到 propose_raw 签名")

# 4. 在 propose_raw 提案落档后增加自动部署逻辑
# 先找到 propose_raw 的返回位置
old_raw_return = '''        _slog(_tid, "propose_raw.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, node_count=len(nodes))
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),'''

new_raw_return = '''        # P4 授权码自动部署
        auto_deploy_result = None
        if deploy_token and proposal_id:
            try:
                auto_deploy_result = self._try_auto_deploy_with_token(
                    deploy_token, proposal_id, agent_id, len(nodes),
                    operation="deploy", target_tab=target_tab)
            except Exception as _ade:
                auto_deploy_result = {"ok": False, "error": f"自动部署异常: {_ade}",
                                      "fallback": "manual"}

        _slog(_tid, "propose_raw.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, node_count=len(nodes),
              auto_deploy=auto_deploy_result.get("ok") if auto_deploy_result else None)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),
            "auto_deploy": auto_deploy_result,'''

if old_raw_return in content:
    content = content.replace(old_raw_return, new_raw_return, 1)
    print("4. propose_raw 增加自动部署逻辑")
else:
    print("WARNING: 未找到 propose_raw 返回位置")

# 5. 增加 _try_auto_deploy_with_token 辅助函数（在 deploy_proposal 函数之前）
old_helper_pos = '''    def deploy_proposal(self, pid: str, agent_id: str = "human",'''

new_helper = '''    def _try_auto_deploy_with_token(self, token_plaintext: str, proposal_id: str,
                                      agent_id: str, node_count: int,
                                      *, operation: str = "deploy",
                                      target_tab: Optional[str] = None) -> Dict[str, Any]:
        """尝试用授权码自动部署提案。

        返回 {ok, deployed, flow_id?, error?, fallback?}
        fallback=manual 表示授权码无效或需要人工审批，回退到人工审批流程。
        """
        from .deploy_tokens import DeployTokenStore, PERM_DEPLOY
        from .snapshot_manager import SnapshotManager

        try:
            data_dir = getattr(self.cfg, "data_dir", None) or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
            token_store = DeployTokenStore(data_dir)
            snap_mgr = SnapshotManager(os.path.join(data_dir, "snapshots"))

            # 验证授权码
            validation = token_store.validate_token(
                token_plaintext, operation=operation, agent_id=agent_id,
                node_count=node_count)

            if not validation.get("ok"):
                # 授权码无效，回退到人工审批
                token_store.record_usage(
                    validation.get("token_id", "unknown"),
                    operation=operation, agent_id=agent_id,
                    success=False, error=validation.get("error", "授权码验证失败"))
                return {"ok": False, "fallback": "manual",
                        "reason": validation.get("error", "授权码无效")}

            token_id = validation["token_id"]
            token_data = token_store.get_token(token_id)
            target_tab_from_token = token_data.get("target_tab") if token_data else None

            # 如果需要人工审批（节点数超阈值），回退
            if validation.get("needs_manual_approval"):
                return {"ok": False, "fallback": "manual",
                        "reason": validation.get("reason", "需要人工审批")}

            # 确定目标 tab：授权码绑定的 target_tab 优先级最高
            effective_target_tab = target_tab_from_token or target_tab

            # 部署前做快照
            try:
                if effective_target_tab:
                    # 查找目标 tab 的 flow_id
                    all_flows = self.nr.list_flows()
                    target_flow = None
                    for f in all_flows:
                        if f.get("label") == effective_target_tab or f.get("id") == effective_target_tab:
                            target_flow = f
                            break
                    if target_flow:
                        tab_data = self.nr.get_flow(target_flow["id"])
                        snap_mgr.create_incremental_snapshot(
                            token_id, target_flow["id"], tab_data, [],
                            operation="auto_deploy",
                            label=f"自动部署前快照（提案 {proposal_id}）",
                            created_by=agent_id)
            except Exception:
                pass  # 快照失败不阻断部署

            # 执行自动部署
            deploy_result = self.deploy_proposal(
                proposal_id, agent_id=agent_id,
                target_tab=effective_target_tab,
                allow_prod=True)

            if deploy_result.get("ok"):
                token_store.record_usage(
                    token_id, operation=operation, agent_id=agent_id,
                    flow_id=deploy_result.get("flow_id"),
                    flow_label=deploy_result.get("label"),
                    node_count=node_count, success=True)
                return {"ok": True, "deployed": True,
                        "flow_id": deploy_result.get("flow_id"),
                        "label": deploy_result.get("label"),
                        "token_id": token_id,
                        "target_tab": effective_target_tab}
            else:
                token_store.record_usage(
                    token_id, operation=operation, agent_id=agent_id,
                    success=False, error=deploy_result.get("error", "部署失败"))
                return {"ok": False, "fallback": "manual",
                        "reason": deploy_result.get("error", "部署失败")}

        except Exception as e:
            return {"ok": False, "fallback": "manual",
                    "reason": f"自动部署异常: {e}"}

    def deploy_proposal(self, pid: str, agent_id: str = "human",'''

if old_helper_pos in content:
    content = content.replace(old_helper_pos, new_helper, 1)
    print("5. 增加 _try_auto_deploy_with_token 辅助函数")
else:
    print("WARNING: 未找到 deploy_proposal 函数位置")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

print("\ngateway.py 修改完成")
