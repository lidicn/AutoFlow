#!/usr/bin/env python3
"""v1.5.8: 网关 raw-to-dsl API + nr_client 离线降级"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 /api/core/* 区域增加 raw-to-dsl handler
insert_marker = '''    async def core_deploy_raw(request: Request):'''

raw_to_dsl_code = '''    async def core_raw_to_dsl(request: Request):
        """将 raw JSON flow 转换为 DSL 参考（引导 Agent 使用 DSL）。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:
            b = await _body(request)
            flow_json = b.get("flow_json")
            if not flow_json:
                return _js({"ok": False, "error": "flow_json 不能为空"}, 400)
            if isinstance(flow_json, str):
                flow_json = json.loads(flow_json)

            # 分析 flow 结构，生成 DSL 草稿
            nodes = flow_json if isinstance(flow_json, list) else flow_json.get("nodes", [])
            triggers = []
            actions = []
            connections = []

            for node in nodes:
                ntype = node.get("type", "")
                nid = node.get("id", "")
                name = node.get("name", "") or node.get("topic", "") or nid
                # 识别触发节点
                if any(k in ntype for k in ["inject", "mqtt in", "http in", "websocket in",
                                             "server-state-changed", "trigger", "cronplus",
                                             "schedex", "bigtimer"]):
                    triggers.append({"id": nid, "type": ntype, "name": name, "node": node})
                # 识别动作节点
                elif any(k in ntype for k in ["call-service", "api-call-service", "ha-entity",
                                              "switch", "light", "output", "http request",
                                              "function", "change", "template"]):
                    actions.append({"id": nid, "type": ntype, "name": name, "node": node})

            # 分析连接关系
            for node in nodes:
                nid = node.get("id", "")
                wires = node.get("wires", [])
                for wire_group in wires:
                    for target in wire_group:
                        connections.append({"from": nid, "to": target})

            # 生成 DSL 草稿
            dsl_lines = ["# AutoFlow DSL 草稿（从 raw JSON 转换，仅供参考）", ""]
            for i, trig in enumerate(triggers):
                trig_name = trig["name"] or trig["type"]
                dsl_lines.append(f"trigger: {trig_name}")
                # 找这个触发节点连接的动作
                downstream = [c["to"] for c in connections if c["from"] == trig["id"]]
                for did in downstream:
                    act = next((a for a in actions if a["id"] == did), None)
                    if act:
                        act_name = act["name"] or act["type"]
                        # 尝试提取 HA 服务调用
                        ha_domain = act["node"].get("domain", "")
                        ha_service = act["node"].get("service", "")
                        if ha_domain and ha_service:
                            dsl_lines.append(f"  action: {ha_domain}.{ha_service}")
                        else:
                            dsl_lines.append(f"  action: {act_name}")
                dsl_lines.append("")

            if not triggers:
                dsl_lines.append("# 未识别到触发节点，请手动补充")
                dsl_lines.append("trigger: <请填写触发条件>")
                for act in actions[:3]:
                    act_name = act["name"] or act["type"]
                    dsl_lines.append(f"  action: {act_name}")

            dsl_draft = "\\n".join(dsl_lines)

            return _js({
                "ok": True,
                "dsl_draft": dsl_draft,
                "analysis": {
                    "total_nodes": len(nodes),
                    "triggers": len(triggers),
                    "actions": len(actions),
                    "connections": len(connections),
                },
                "note": "这是自动生成的 DSL 草稿，可能不完整，请根据实际需求调整。建议优先使用 DSL 而非 raw JSON。",
            })
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

'''

if insert_marker in content:
    content = content.replace(insert_marker, raw_to_dsl_code + insert_marker, 1)
    print("1. raw-to-dsl handler: OK")
else:
    print("1. raw-to-dsl handler: NOT FOUND")

# 2. 注册路由
route_marker = '''        Route("/api/core/deploy-raw", core_deploy_raw, methods=["POST"]),'''
new_route = '''        Route("/api/core/raw-to-dsl", core_raw_to_dsl, methods=["POST"]),
        Route("/api/core/deploy-raw", core_deploy_raw, methods=["POST"]),'''
if route_marker in content:
    content = content.replace(route_marker, new_route, 1)
    print("2. raw-to-dsl 路由: OK")
else:
    print("2. raw-to-dsl 路由: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
