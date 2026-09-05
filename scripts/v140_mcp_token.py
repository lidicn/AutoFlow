#!/usr/bin/env python3
"""修改 mcp_server.py: autoflow_deploy_raw 和 autoflow_propose_dsl 增加 deploy_token 参数"""

MCP = r"E:\NAS\autoflow\src\autoflow_gateway\mcp_server.py"
with open(MCP, "r", encoding="utf-8") as f:
    content = f.read()

# 1. autoflow_deploy_raw 增加 deploy_token 参数
old_sig1 = '''def autoflow_deploy_raw(flow_json: str, label: str = "", target: str = "staging",
                        force: bool = False, require_e2e: bool = False,
                        target_tab: str = "") -> str:'''

new_sig1 = '''def autoflow_deploy_raw(flow_json: str, label: str = "", target: str = "staging",
                        force: bool = False, require_e2e: bool = False,
                        target_tab: str = "", deploy_token: str = "") -> str:'''

if old_sig1 in content:
    content = content.replace(old_sig1, new_sig1, 1)
    print("1. autoflow_deploy_raw 增加 deploy_token 参数")
else:
    print("WARNING: 未找到 autoflow_deploy_raw 签名")

# 2. autoflow_deploy_raw 文档增加 deploy_token 说明
old_doc1 = '''    - target_tab：【P4 混合模式】指定部署到哪个 Node-RED tab（按 tab id 或 label 匹配，不存在则自动创建）。
      留空（默认）则按当前 Tab 组织模式部署（per_flow=独立tab / single_tab=AutoFlow集中tab）。
      示例：target_tab="客厅" → 该 flow 部署到「客厅」tab 中，与其他 flow 共存。'''

new_doc1 = '''    - target_tab：【P4 混合模式】指定部署到哪个 Node-RED tab（按 tab id 或 label 匹配，不存在则自动创建）。
      留空（默认）则按当前 Tab 组织模式部署（per_flow=独立tab / single_tab=AutoFlow集中tab）。
      示例：target_tab="客厅" → 该 flow 部署到「客厅」tab 中，与其他 flow 共存。
    - deploy_token：【P4 授权码自动部署】部署授权码。如果提供且有效，提案将自动通过审批并直接部署到 NR，
      无需用户在 WebUI 手动确认。授权码由用户在 WebUI「授权码管理」页面创建，绑定目标 tab、有效期、权限等。
      授权码无效或需要人工审批时，自动回退到正常人工审批流程（不会拒绝部署）。
      示例：deploy_token="dt_xxxxxxxxxxxxxxxx" → 自动部署到授权码绑定的 tab。'''

if old_doc1 in content:
    content = content.replace(old_doc1, new_doc1, 1)
    print("2. autoflow_deploy_raw 文档增加 deploy_token 说明")
else:
    print("WARNING: 未找到 autoflow_deploy_raw target_tab 文档")

# 3. autoflow_deploy_raw 调用 propose_raw 时传递 deploy_token
old_call1 = '''    return _js(_gw().propose_raw(data, agent_id=aid, label=label or None,
                                 target=target, force=force, require_e2e=require_e2e,
                                 target_tab=target_tab or None))'''

new_call1 = '''    return _js(_gw().propose_raw(data, agent_id=aid, label=label or None,
                                 target=target, force=force, require_e2e=require_e2e,
                                 target_tab=target_tab or None,
                                 deploy_token=deploy_token or None))'''

if old_call1 in content:
    content = content.replace(old_call1, new_call1, 1)
    print("3. autoflow_deploy_raw 调用传递 deploy_token")
else:
    print("WARNING: 未找到 autoflow_deploy_raw 调用位置")

# 4. autoflow_propose_dsl 增加 deploy_token 参数
old_sig2 = '''def autoflow_propose_dsl(dsl: str, expected_postconditions_json: str = "",
                         resolved_entities_json: str = "", require_e2e: bool = False) -> str:'''

new_sig2 = '''def autoflow_propose_dsl(dsl: str, expected_postconditions_json: str = "",
                         resolved_entities_json: str = "", require_e2e: bool = False,
                         deploy_token: str = "") -> str:'''

if old_sig2 in content:
    content = content.replace(old_sig2, new_sig2, 1)
    print("4. autoflow_propose_dsl 增加 deploy_token 参数")
else:
    print("WARNING: 未找到 autoflow_propose_dsl 签名")

# 5. autoflow_propose_dsl 文档增加 deploy_token 说明
old_doc2 = '''    - agent_id 由已认证身份自动注入；提案进入 raw，等待用户在 WebUI 审核升格。
    - 返回 {ok, proposal_id, scene_name, gate, flow, ...}；gate 实际含'''

new_doc2 = '''    - agent_id 由已认证身份自动注入；提案进入 raw，等待用户在 WebUI 审核升格。
    - deploy_token：【P4 授权码自动部署】部署授权码。如果提供且有效且闸门通过，提案将自动部署到 NR，
      无需用户在 WebUI 手动确认。授权码无效或需要人工审批时，自动回退到正常人工审批流程。
    - 返回 {ok, proposal_id, scene_name, gate, flow, ..., auto_deploy}；gate 实际含'''

if old_doc2 in content:
    content = content.replace(old_doc2, new_doc2, 1)
    print("5. autoflow_propose_dsl 文档增加 deploy_token 说明")
else:
    print("WARNING: 未找到 autoflow_propose_dsl 文档位置")

# 6. autoflow_propose_dsl 调用 propose_dsl 时传递 deploy_token
old_call2 = '''        result = gw.propose_dsl(dsl, agent_id=aid,
                                 expected_postconditions=expected,
                                 resolved_entities=resolved,
                                 require_e2e=require_e2e)'''

new_call2 = '''        result = gw.propose_dsl(dsl, agent_id=aid,
                                 expected_postconditions=expected,
                                 resolved_entities=resolved,
                                 require_e2e=require_e2e,
                                 deploy_token=deploy_token or None)'''

if old_call2 in content:
    content = content.replace(old_call2, new_call2, 1)
    print("6. autoflow_propose_dsl 调用传递 deploy_token")
else:
    print("WARNING: 未找到 autoflow_propose_dsl 调用位置")

with open(MCP, "w", encoding="utf-8") as f:
    f.write(content)

print("\nmcp_server.py 修改完成")
