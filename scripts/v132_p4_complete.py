#!/usr/bin/env python3
"""P4 修复：propose_raw 增加 target_tab 参数 + content 字段 + MCP 调用传递"""

GW = r"E:\NAS\autoflow\src\autoflow_gateway\gateway.py"
with open(GW, "r", encoding="utf-8") as f:
    content = f.read()

# 1. propose_raw 函数签名增加 target_tab
old_sig = '''    def propose_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                    label: Optional[str] = None,
                    target: str = "staging", force: bool = False,
                    run_gate: bool = True, dry_run: bool = False,
                    require_e2e: bool = False) -> Dict[str, Any]:'''

new_sig = '''    def propose_raw(self, flow_json: Dict, agent_id: str = "unknown-agent",
                    label: Optional[str] = None,
                    target: str = "staging", force: bool = False,
                    run_gate: bool = True, dry_run: bool = False,
                    require_e2e: bool = False,
                    target_tab: Optional[str] = None) -> Dict[str, Any]:'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. propose_raw 签名增加 target_tab")
else:
    print("WARNING: 未找到 propose_raw 签名")

# 2. 提案 content 增加 target_tab 字段
old_content = '''            "node_count": len(nodes),
            "require_e2e": bool(require_e2e),
            "validation": validation,'''

new_content = '''            "node_count": len(nodes),
            "require_e2e": bool(require_e2e),
            "target_tab": target_tab,
            "validation": validation,'''

if old_content in content:
    content = content.replace(old_content, new_content, 1)
    print("2. 提案 content 增加 target_tab 字段")
else:
    print("WARNING: 未找到提案 content 位置")

with open(GW, "w", encoding="utf-8") as f:
    f.write(content)

# 3. MCP 工具调用 propose_raw 时传递 target_tab
MCP = r"E:\NAS\autoflow\src\autoflow_gateway\mcp_server.py"
with open(MCP, "r", encoding="utf-8") as f:
    content = f.read()

old_call = '''    return _js(_gw().propose_raw(data, agent_id=aid, label=label or None,
                                 target=target, force=force, require_e2e=require_e2e))'''

new_call = '''    return _js(_gw().propose_raw(data, agent_id=aid, label=label or None,
                                 target=target, force=force, require_e2e=require_e2e,
                                 target_tab=target_tab or None))'''

if old_call in content:
    content = content.replace(old_call, new_call, 1)
    print("3. MCP 调用传递 target_tab")
else:
    print("WARNING: 未找到 MCP 调用位置")

with open(MCP, "w", encoding="utf-8") as f:
    f.write(content)

print("\nP4 target_tab 完整修复完成")
