#!/usr/bin/env python3
"""v1.5.5: 修复 propose-dsl target_tab 死参数 + version 返回 unknown"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修复 core_propose_dsl：读取 target_tab，传给 _require_api_key 做授权校验
old = '''    async def core_propose_dsl(request: Request):
        """【★首选】提交 DSL，编译+闸门校验，返回提案。AutoFlow Pro 主入口。"""
        agent_info, err = _require_api_key(request, required_perm="deploy")
        if err:
            return err
        try:
            b = await _body(request)
            dsl = (b.get("dsl") or "").strip()
            if not dsl:
                return _js({"ok": False, "error": "dsl 不能为空"}, 400)
            agent_id = (b.get("agent_id") or "pro-agent").strip()
            expected = b.get("expected_postconditions") or []
            resolved = b.get("resolved_entities") or []
            deploy_token = (b.get("deploy_token") or "").strip() or None
            preview = bool(b.get("preview", False))

            result = gw.propose_dsl(
                dsl=dsl, agent_id=agent_id,
                expected_postconditions=expected if isinstance(expected, list) else None,
                resolved_entities=resolved if isinstance(resolved, list) else None,
                deploy_token=deploy_token,
            )'''

new = '''    async def core_propose_dsl(request: Request):
        """【★首选】提交 DSL，编译+闸门校验，返回提案。AutoFlow Pro 主入口。"""
        # 先读 body 拿 target_tab，再做授权校验（需要 target_tab 做越界检查）
        try:
            b = await _body(request)
        except Exception:
            b = {}
        target_tab = (b.get("target_tab") or "").strip() or None
        agent_info, err = _require_api_key(request, required_perm="deploy", target_tab=target_tab)
        if err:
            return err
        try:
            dsl = (b.get("dsl") or "").strip()
            if not dsl:
                return _js({"ok": False, "error": "dsl 不能为空"}, 400)
            agent_id = (b.get("agent_id") or "pro-agent").strip()
            expected = b.get("expected_postconditions") or []
            resolved = b.get("resolved_entities") or []
            deploy_token = (b.get("deploy_token") or "").strip() or None
            preview = bool(b.get("preview", False))

            result = gw.propose_dsl(
                dsl=dsl, agent_id=agent_id,
                expected_postconditions=expected if isinstance(expected, list) else None,
                resolved_entities=resolved if isinstance(resolved, list) else None,
                deploy_token=deploy_token,
            )
            # 回显 target_tab，便于调用方与审计确认
            if target_tab:
                result["target_tab"] = target_tab
                result["authorized_tabs"] = agent_info.get("authorized_tabs", [])'''

if old in content:
    content = content.replace(old, new, 1)
    print("1. propose-dsl target_tab 校验: OK")
else:
    print("1. propose-dsl target_tab 校验: NOT FOUND")

# 2. 修复 core_version：version 返回 unknown（路径不对）
old_ver = '''    async def core_version(request: Request):
        """网关版本 + 兼容性检查。"""
        try:
            version_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
            ver = "unknown"
            if os.path.exists(version_path):
                with open(version_path, "r", encoding="utf-8") as f:
                    ver = f.read().strip()'''

new_ver = '''    async def core_version(request: Request):
        """网关版本 + 兼容性检查。"""
        try:
            # VERSION 在项目根目录，比 src/autoflow_gateway/ 高两级
            version_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "VERSION")
            ver = "unknown"
            if os.path.exists(version_path):
                with open(version_path, "r", encoding="utf-8") as f:
                    ver = f.read().strip()'''

if old_ver in content:
    content = content.replace(old_ver, new_ver, 1)
    print("2. core_version 路径修复: OK")
else:
    print("2. core_version 路径修复: NOT FOUND")

# 3. 同样修复 core_health 中的 uptime（可选，先不动）

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
