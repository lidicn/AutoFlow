#!/usr/bin/env python3
"""修复撤回 fail-atomic：NR 写失败时不清账本，保持状态一致"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # NR 侧删除/更新失败后，仍清账本：避免 mutation 半残导致网关注册表永远卡死。
        # 原则：get_flow 阶段已确认这是本网关部署的 tab；mutation 失败通常是 NR 侧
        # 瞬时/权限问题，保留 ledger 会让用户无法重试/重部署。返回 nr_warning 供人审。
        self.state.remove_flow(flow_id)
        src = meta.get("source_proposal")
        if src:
            try:
                ProposalStore(self.cfg).clear_deployed(src)
            except Exception:
                pass
        if not nr_ok:
            return {"ok": True, "action": action, "flow_id": flow_id, "label": label,
                    "gateway_nodes_removed": g_removed, "user_nodes_preserved": u_preserved,
                    "nr_warning": nr_err,
                    "note": "NR 侧撤回调用失败，已清网关账本；NR 可能有残留，请手动确认"}
        return {"ok": True, "action": action, "flow_id": flow_id, "label": label,
                "gateway_nodes_removed": g_removed, "user_nodes_preserved": u_preserved}'''

new = '''        # fail-atomic：NR 侧删除/更新失败时，不清账本，保持注册表与 NR 实际状态一致。
        # 之前的逻辑是失败仍清账本，导致"注册表说已删但 NR 实际没删"的不一致，
        # 出现孤儿边界注释残留、网关节点实际未删等问题。
        # 现在失败时返回明确错误，用户可修复 NR 侧问题后重试撤回。
        if not nr_ok:
            return {"ok": False, "action": action, "flow_id": flow_id, "label": label,
                    "gateway_nodes_removed": 0, "user_nodes_preserved": u_preserved,
                    "error": f"NR 侧撤回失败，账本未清理（保持状态一致）：{nr_err}",
                    "note": "NR 写操作失败，网关账本未变更。请检查 NR 侧问题后重试撤回。"}

        # NR 写成功 → 清账本
        self.state.remove_flow(flow_id)
        src = meta.get("source_proposal")
        if src:
            try:
                ProposalStore(self.cfg).clear_deployed(src)
            except Exception:
                pass
        return {"ok": True, "action": action, "flow_id": flow_id, "label": label,
                "gateway_nodes_removed": g_removed, "user_nodes_preserved": u_preserved}'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("撤回 fail-atomic 修复：NR 写失败时不清账本")
else:
    print("ERROR: 未找到目标代码")
