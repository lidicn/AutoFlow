#!/usr/bin/env python3
"""v1.5.3: webui.py 增加 Token 统计 API + 调用记录"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在模板库函数之后增加 token stats 辅助函数
insert_marker = '''    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──'''

token_code = '''    # ── Token 统计（v1.5.3）──
    def _token_stats_store():
        from .token_stats import TokenStatsStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return TokenStatsStore(os.path.join(data_dir, "token_stats"))

    def _record_token(endpoint: str, agent_id: str, input_chars: int,
                      output_chars: int, mode: str = "dsl"):
        """记录一次 API 调用的 Token 消耗（异步非阻塞，失败不影响主流程）。"""
        try:
            _token_stats_store().record(endpoint, agent_id, input_chars, output_chars, mode)
        except Exception:
            pass

    async def core_token_stats(request: Request):
        """Token 消耗统计。"""
        agent_info, err = _require_api_key(request, required_perm="read")
        if err:
            return err
        try:
            days = int(request.query_params.get("days", 7))
            store = _token_stats_store()
            stats = store.get_stats(days=days)
            return _js({"ok": True, "stats": stats})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

'''

if insert_marker in content:
    content = content.replace(insert_marker, token_code + insert_marker, 1)
    print("1. Token 统计函数插入: OK")
else:
    print("1. Token 统计函数插入: NOT FOUND")

# 2. 在 propose-dsl 返回前记录 token
old_propose_return = '''            # 附加 telemetry
            result["_telemetry"] = {
                "input_chars": len(dsl),
                "mode": "dsl",
                "agent_id": agent_id,
            }
            return _js(result)'''

new_propose_return = '''            # 附加 telemetry + 记录 token
            output_chars = len(json.dumps(result, ensure_ascii=False))
            result["_telemetry"] = {
                "input_chars": len(dsl),
                "output_chars": output_chars,
                "estimated_tokens": (len(dsl) + output_chars) // 4,
                "mode": "dsl",
                "agent_id": agent_id,
            }
            _record_token("propose-dsl", agent_id, len(dsl), output_chars, "dsl")
            return _js(result)'''

if old_propose_return in content:
    content = content.replace(old_propose_return, new_propose_return, 1)
    print("2. propose-dsl token 记录: OK")
else:
    print("2. propose-dsl token 记录: NOT FOUND")

# 3. 在 deploy-raw 返回前记录 token
old_raw_return = '''            # 附加 DSL 转换建议（引导 Agent 用 DSL）
            result["_warning"] = "deploy-raw 是逃生舱，输出冗长且无编译校验。建议改用 propose-dsl 以节省 token。"
            result["_telemetry"] = {
                "mode": "raw",
                "agent_id": agent_id,
            }
            return _js(result)'''

new_raw_return = '''            # 附加 DSL 转换建议 + 记录 token
            input_chars = len(json.dumps(flow_json, ensure_ascii=False))
            output_chars = len(json.dumps(result, ensure_ascii=False))
            result["_warning"] = "deploy-raw 是逃生舱，输出冗长且无编译校验。建议改用 propose-dsl 以节省 token。"
            result["_telemetry"] = {
                "input_chars": input_chars,
                "output_chars": output_chars,
                "estimated_tokens": (input_chars + output_chars) // 4,
                "mode": "raw",
                "agent_id": agent_id,
            }
            _record_token("deploy-raw", agent_id, input_chars, output_chars, "raw")
            return _js(result)'''

if old_raw_return in content:
    content = content.replace(old_raw_return, new_raw_return, 1)
    print("3. deploy-raw token 记录: OK")
else:
    print("3. deploy-raw token 记录: NOT FOUND")

# 4. 在路由表中注册 token-stats
route_marker = '''        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API'''

token_route = '''        # Token 统计（v1.5.3）
        Route("/api/core/token-stats", core_token_stats, methods=["GET"]),
        # AutoFlow Pro: /api/core/* 轻量 Agent 客户端 API'''

if route_marker in content:
    content = content.replace(route_marker, token_route, 1)
    print("4. token-stats 路由注册: OK")
else:
    print("4. token-stats 路由注册: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
