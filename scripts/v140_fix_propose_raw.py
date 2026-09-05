#!/usr/bin/env python3
"""修复 propose_raw 自动部署逻辑"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        _slog(_tid, "propose_raw.done", elapsed=round(time.perf_counter() - _t0, 3),
              proposal_id=proposal_id, label=flow.get("label", ""))
        return {
            "ok": True,
            # WB24 NEW-F5（透明性）：回显归一化后的 flow_json（已完成 HA server 注入/占位符回退），
            # 便于 autoflow_deploy_raw 调用方核对归一化结果（如 trigger-state 的 version/entities 改写），
            # 无需等用户在 WebUI 部署后才知情。
            "flow_json": flow,
            "proposal_id": proposal_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),'''

new = '''        # P4 授权码自动部署
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
              proposal_id=proposal_id, label=flow.get("label", ""),
              auto_deploy=auto_deploy_result.get("ok") if auto_deploy_result else None)
        return {
            "ok": True,
            # WB24 NEW-F5（透明性）：回显归一化后的 flow_json（已完成 HA server 注入/占位符回退），
            # 便于 autoflow_deploy_raw 调用方核对归一化结果（如 trigger-state 的 version/entities 改写），
            # 无需等用户在 WebUI 部署后才知情。
            "flow_json": flow,
            "proposal_id": proposal_id,
            "label": flow.get("label", ""),
            "node_count": len(nodes),
            "auto_deploy": auto_deploy_result,'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("propose_raw 自动部署逻辑已修复")
else:
    print("ERROR: 未找到目标代码")
