#!/usr/bin/env python3
"""v1.5.7: 错误知识库 API + propose-dsl 失败自动记录 + token-stats 无需认证"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 token stats 函数之后增加错误知识库函数
insert_marker = '''    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──'''

ek_code = '''    # ── 错误知识库（v1.5.7）──
    def _error_knowledge_store():
        from .error_knowledge import ErrorKnowledgeStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return ErrorKnowledgeStore(os.path.join(data_dir, "error_knowledge"))

    async def error_knowledge_list(request: Request):
        """列出错误案例。"""
        try:
            qp = request.query_params
            store = _error_knowledge_store()
            result = store.list_errors(
                error_type=qp.get("error_type") or None,
                keyword=qp.get("keyword") or None,
                agent_id=qp.get("agent_id") or None,
                limit=int(qp.get("limit", 50)),
                offset=int(qp.get("offset", 0)),
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def error_knowledge_stats(request: Request):
        """错误统计。"""
        try:
            days = int(request.query_params.get("days", 7))
            store = _error_knowledge_store()
            result = store.get_stats(days=days)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

'''

if insert_marker in content:
    content = content.replace(insert_marker, ek_code + insert_marker, 1)
    print("1. 错误知识库函数插入: OK")
else:
    print("1. 错误知识库函数插入: NOT FOUND")

# 2. 在 propose-dsl 失败时自动记录错误
old_propose_end = '''            # 附加 telemetry + 记录 token
            output_chars = len(json.dumps(result, ensure_ascii=False))
            result["_telemetry"] = {
                "input_chars": len(dsl),
                "output_chars": output_chars,
                "estimated_tokens": (len(dsl) + output_chars) // 4,
                "mode": "dsl",
                "agent_id": agent_id,
            }
            _record_token("propose-dsl", agent_id, len(dsl), output_chars, "dsl")
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)'''

new_propose_end = '''            # 附加 telemetry + 记录 token
            output_chars = len(json.dumps(result, ensure_ascii=False))
            result["_telemetry"] = {
                "input_chars": len(dsl),
                "output_chars": output_chars,
                "estimated_tokens": (len(dsl) + output_chars) // 4,
                "mode": "dsl",
                "agent_id": agent_id,
            }
            _record_token("propose-dsl", agent_id, len(dsl), output_chars, "dsl")
            # 失败时自动记录到错误知识库
            if not result.get("ok"):
                try:
                    _error_knowledge_store().record(
                        dsl=dsl,
                        error_msg=result.get("error", "") or result.get("gate", {}).get("reason", ""),
                        stage=result.get("stage", ""),
                        agent_id=agent_id,
                        proposal_id=result.get("proposal_id", ""),
                    )
                except Exception:
                    pass
            return _js(result)
        except Exception as e:
            # 异常也记录
            try:
                _error_knowledge_store().record(
                    dsl=dsl, error_msg=str(e), stage="exception",
                    agent_id=agent_id,
                )
            except Exception:
                pass
            return _js({"ok": False, "error": str(e)}, 500)'''

if old_propose_end in content:
    content = content.replace(old_propose_end, new_propose_end, 1)
    print("2. propose-dsl 错误自动记录: OK")
else:
    print("2. propose-dsl 错误自动记录: NOT FOUND")

# 3. 注册错误知识库路由
route_marker = '''        # Token 统计（v1.5.3）'''

ek_routes = '''        # 错误知识库（v1.5.7）
        Route("/api/errors", error_knowledge_list, methods=["GET"]),
        Route("/api/errors/stats", error_knowledge_stats, methods=["GET"]),
        # Token 统计（v1.5.3）'''

if route_marker in content:
    content = content.replace(route_marker, ek_routes, 1)
    print("3. 错误知识库路由注册: OK")
else:
    print("3. 错误知识库路由注册: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
