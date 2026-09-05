#!/usr/bin/env python3
"""修复 /api/nr/tabs 节点数统计 + 支持多 tab 授权"""

# 1. 修复 webui.py 中 /api/nr/tabs 的节点数统计
WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    async def nr_tabs(request: Request):
        """返回 Node-RED 中所有 tab 列表（id, label, node_count）。"""
        try:
            flows = gw.nr.list_flows()
            if isinstance(flows, dict):
                flows = flows.get("flows", [])
            flows = flows or []
            tabs = []
            for f in flows:
                if not isinstance(f, dict):
                    continue
                if f.get("type") not in (None, "", "tab"):
                    continue
                if f.get("type") == "subflow":
                    continue
                tabs.append({
                    "id": f.get("id", ""),
                    "label": f.get("label") or f.get("id") or "(未命名)",
                    "node_count": len(f.get("nodes", [])) if isinstance(f.get("nodes"), list) else 0,
                })
            return _js({"ok": True, "tabs": tabs})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "tabs": []}, 500)'''

new = '''    async def nr_tabs(request: Request):
        """返回 Node-RED 中所有 tab 列表（id, label, node_count）。"""
        try:
            flows = gw.nr.list_flows()
            if isinstance(flows, dict):
                flows = flows.get("flows", [])
            flows = flows or []
            # Node-RED flows 是扁平结构：tab 节点不含 nodes 数组，
            # 普通节点通过 z 字段指向所属 tab id。需要按 z 统计。
            tab_map = {}
            node_count_by_tab = {}
            for f in flows:
                if not isinstance(f, dict):
                    continue
                ftype = f.get("type")
                if ftype in (None, "", "tab"):
                    tid = f.get("id", "")
                    if tid:
                        tab_map[tid] = {
                            "id": tid,
                            "label": f.get("label") or tid or "(未命名)",
                            "node_count": 0,
                        }
                        node_count_by_tab[tid] = 0
                elif ftype != "subflow":
                    # 普通节点，按 z 统计
                    z = f.get("z")
                    if z and z in node_count_by_tab:
                        node_count_by_tab[z] += 1
            # 回填节点数
            tabs = []
            for tid, t in tab_map.items():
                t["node_count"] = node_count_by_tab.get(tid, 0)
                tabs.append(t)
            # 按 label 排序
            tabs.sort(key=lambda x: x["label"])
            return _js({"ok": True, "tabs": tabs})
        except Exception as e:
            return _js({"ok": False, "error": str(e), "tabs": []}, 500)'''

if old in content:
    content = content.replace(old, new, 1)
    print("1. /api/nr/tabs 节点数统计修复: OK")
else:
    print("1. /api/nr/tabs 节点数统计修复: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
