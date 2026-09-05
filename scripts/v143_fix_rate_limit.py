#!/usr/bin/env python3
"""修复频率限制：validate_token 中重置时间窗口后保存"""

DT = r"E:\NAS\autoflow\src\autoflow_gateway\deploy_tokens.py"
with open(DT, "r", encoding="utf-8") as f:
    content = f.read()

old = '''        # 检查频率限制
        now = time.time()
        rate_window_start = stats.get("rate_window_start")
        rate_window_count = stats.get("rate_window_count", 0)
        rate_limit = token_data.get("rate_limit_per_min", DEFAULT_RATE_LIMIT)
        if rate_window_start and (now - rate_window_start) < 60:
            if rate_window_count >= rate_limit:
                return {"ok": False, "error": f"操作频率超限（每分钟最多 {rate_limit} 次），请稍后重试",
                        "token_id": token_id}
        else:
            # 重置时间窗口
            stats["rate_window_start"] = now
            stats["rate_window_count"] = 0

        return {"ok": True, "token_id": token_id, "needs_manual_approval": False}'''

new = '''        # 检查频率限制
        now = time.time()
        rate_window_start = stats.get("rate_window_start")
        rate_window_count = stats.get("rate_window_count", 0)
        rate_limit = token_data.get("rate_limit_per_min", DEFAULT_RATE_LIMIT)
        if rate_window_start and (now - rate_window_start) < 60:
            if rate_window_count >= rate_limit:
                # 频率超限也记录到日志（不通过 record_usage，因为那是成功/失败部署的日志）
                return {"ok": False, "error": f"操作频率超限（每分钟最多 {rate_limit} 次），请稍后重试",
                        "token_id": token_id}
        else:
            # 重置时间窗口 —— 必须保存，否则 rate_window_start 永远为 null，限流永不生效
            stats["rate_window_start"] = now
            stats["rate_window_count"] = 0
            self._save_tokens(data)

        return {"ok": True, "token_id": token_id, "needs_manual_approval": False}'''

if old in content:
    content = content.replace(old, new, 1)
    with open(DT, "w", encoding="utf-8") as f:
        f.write(content)
    print("频率限制修复：validate_token 重置时间窗口后保存")
else:
    print("ERROR: 未找到目标代码")
