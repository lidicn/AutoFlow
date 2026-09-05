#!/usr/bin/env python3
"""修复 deploy_tokens.py: 频率限制失败也计数"""

DT = r"E:\NAS\autoflow\src\autoflow_gateway\deploy_tokens.py"
with open(DT, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        if success:
            if operation == PERM_DEPLOY:
                stats["deploy_count"] = stats.get("deploy_count", 0) + 1
                stats["nodes_deployed"] = stats.get("nodes_deployed", 0) + node_count
                stats["flows_deployed"] = stats.get("flows_deployed", 0) + 1
            elif operation == PERM_MODIFY:
                stats["modify_count"] = stats.get("modify_count", 0) + 1
            elif operation == PERM_UNDEPLOY:
                stats["undeploy_count"] = stats.get("undeploy_count", 0) + 1
            # 频率计数
            stats["rate_window_count"] = stats.get("rate_window_count", 0) + 1
        else:
            stats["failed_count"] = stats.get("failed_count", 0) + 1'''

new = '''        if success:
            if operation == PERM_DEPLOY:
                stats["deploy_count"] = stats.get("deploy_count", 0) + 1
                stats["nodes_deployed"] = stats.get("nodes_deployed", 0) + node_count
                stats["flows_deployed"] = stats.get("flows_deployed", 0) + 1
            elif operation == PERM_MODIFY:
                stats["modify_count"] = stats.get("modify_count", 0) + 1
            elif operation == PERM_UNDEPLOY:
                stats["undeploy_count"] = stats.get("undeploy_count", 0) + 1
        else:
            stats["failed_count"] = stats.get("failed_count", 0) + 1
        # 频率计数：成功和失败都计数，防止失败风暴绕过限流
        stats["rate_window_count"] = stats.get("rate_window_count", 0) + 1'''

if old in content:
    content = content.replace(old, new, 1)
    with open(DT, "w", encoding="utf-8") as f:
        f.write(content)
    print("deploy_tokens.py 频率限制失败也计数")
else:
    print("ERROR: 未找到目标代码")
