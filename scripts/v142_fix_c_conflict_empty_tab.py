#!/usr/bin/env python3
"""修复缺陷C: 冲突检查增加空tab判断"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # ── 冲突检测：同名 flow 已存在且不是本网关部署的 → 拒绝覆盖用户已有 flow ──
        existing = None
        for f in self.nr.list_flows():
            if f.get("label") == label:
                existing = f
                break
        if existing and existing.get("id") not in self.state.get_flow_catalog().get("flows", {}):
            if not force:
                return {
                    "ok": False, "conflict": True,
                    "error": f"NR 中已存在同名 flow「{label}」({existing.get('id')})，且非本网关部署，避免覆盖。可改名后重试，或 force=true 以新建副本。",
                    "existing": {"id": existing.get("id"), "label": label},
                }'''

new = '''        # ── 冲突检测：同名 flow 已存在且不是本网关部署的 → 拒绝覆盖用户已有 flow ──
        # 缺陷C修复：撤回后空 tab（只有 tab 节点）可覆盖，避免网关自管流撤回后无法幂等重部署
        existing = None
        for f in self.nr.list_flows():
            if f.get("label") == label:
                existing = f
                break
        if existing and existing.get("id") not in self.state.get_flow_catalog().get("flows", {}):
            # 检查是否为空 tab（撤回后残留）：只有 tab 节点，无其他节点
            is_empty_tab = False
            try:
                _existing_flow = self.nr.get_flow(existing["id"])
                _non_tab_nodes = [n for n in _existing_flow.get("nodes", []) if n.get("type") != "tab"]
                is_empty_tab = len(_non_tab_nodes) == 0
            except Exception:
                pass
            if is_empty_tab:
                # 空 tab 是撤回后残留，允许覆盖（视为本网关之前部署的）
                pass
            elif not force:
                return {
                    "ok": False, "conflict": True,
                    "error": f"NR 中已存在同名 flow「{label}」({existing.get('id')})，且非本网关部署，避免覆盖。可改名后重试，或 force=true 以新建副本。",
                    "existing": {"id": existing.get("id"), "label": label},
                }'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("缺陷C修复: 冲突检查增加空tab判断")
else:
    print("ERROR: 未找到目标代码")
