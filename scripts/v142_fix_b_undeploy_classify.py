#!/usr/bin/env python3
"""修复缺陷B: 撤回节点分类，增加网关节点特征判断"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        live_nodes = live.get("nodes", [])
        tab_node = next((n for n in live_nodes if n.get("type") == "tab"), None)
        gateway_nodes = [n for n in live_nodes if n.get("id") in deployed_ids]
        user_nodes = [n for n in live_nodes
                      if n.get("id") not in deployed_ids and n.get("type") != "tab"]'''

new = '''        live_nodes = live.get("nodes", [])
        tab_node = next((n for n in live_nodes if n.get("type") == "tab"), None)
        # 缺陷B修复：网关节点判定不仅靠 deployed_ids（可能遗漏边界 comment），
        # 还增加特征判断：AF_START/AF_END 边界 comment、af_scene_ 前缀节点
        def _is_gateway_node(n):
            if n.get("id") in deployed_ids:
                return True
            # 边界 comment 节点（AF_START/AF_END）
            if n.get("type") == "comment":
                _name = (n.get("name") or "") + " " + (n.get("text") or "")
                if "AF_START" in _name or "AF_END" in _name:
                    return True
            # af_scene_ 前缀的网关节点
            if str(n.get("name", "")).startswith("af_scene_"):
                return True
            return False

        gateway_nodes = [n for n in live_nodes if _is_gateway_node(n)]
        user_nodes = [n for n in live_nodes
                      if not _is_gateway_node(n) and n.get("type") != "tab"]'''

if old in content:
    content = content.replace(old, new, 1)
    with open(GW, "w", encoding="utf-8") as f:
        f.write(content)
    print("缺陷B修复: 撤回节点分类增加网关节点特征判断")
else:
    print("ERROR: 未找到目标代码")
