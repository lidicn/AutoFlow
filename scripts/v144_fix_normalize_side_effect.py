#!/usr/bin/env python3
"""修复 server_resolved 副作用：_normalize_api_call_service 不反向补全 domain/service，避免改写用户节点"""

NC = r"E:\NAS\autoflow\src\autoflow_gateway\lib\nr_client.py"
with open(NC, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # action ⇄ domain/service 双向补全（编译器契约 dsl_engine.py:2148）
        action = n.get("action")
        action = action.strip() if isinstance(action, str) else ""
        domain = n.get("domain")
        domain = domain.strip() if isinstance(domain, str) else ""
        service = n.get("service")
        service = service.strip() if isinstance(service, str) else ""
        if not action and domain and service:
            action = f"{domain}.{service}"
            n["action"] = action
        elif action and "." in action:
            _d, _, _s = action.partition(".")
            if not domain and _d:
                n["domain"] = _d
            if not service and _s:
                n["service"] = _s'''

new = '''        # action ⇄ domain/service 补全（编译器契约 dsl_engine.py:2148）
        # 修复：只正向补全 action（v7 必需），不反向补全 domain/service。
        # 之前双向补全会改写用户手动添加的 api-call-service 节点（如「💾 存档」节点
        # 的空 domain/service 被补为 input_text/set_value），属部署副作用。
        # v7 格式只需要 action 字段，domain/service 是可选的，反向补全非必须。
        action = n.get("action")
        action = action.strip() if isinstance(action, str) else ""
        domain = n.get("domain")
        domain = domain.strip() if isinstance(domain, str) else ""
        service = n.get("service")
        service = service.strip() if isinstance(service, str) else ""
        if not action and domain and service:
            action = f"{domain}.{service}"
            n["action"] = action
        # 不再反向补全 domain/service：避免改写用户节点的原始字段'''

if old in content:
    content = content.replace(old, new, 1)
    with open(NC, "w", encoding="utf-8") as f:
        f.write(content)
    print("server_resolved 副作用修复：不再反向补全 domain/service")
else:
    print("ERROR: 未找到目标代码")
