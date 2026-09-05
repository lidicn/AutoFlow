#!/usr/bin/env python3
"""v1.5.2: webui.py 增加模板库 API"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

# 在 API Key 管理函数之后插入模板库函数
insert_marker = '''    # ── AutoFlow Pro：/api/core/* 轻量 Agent 客户端 REST API（v1.5.0）──'''

template_code = '''    # ── 模板库（v1.5.2）：Flow 模板复用 ──
    def _template_store():
        from .templates import TemplateStore
        data_dir = getattr(cfg, "data_dir", None) or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return TemplateStore(os.path.join(data_dir, "templates"))

    async def templates_list(request: Request):
        """列出模板。"""
        try:
            qp = request.query_params
            store = _template_store()
            templates = store.list_templates(
                category=qp.get("category") or None,
                keyword=qp.get("keyword") or None,
            )
            categories = store.list_categories()
            return _js({"ok": True, "templates": templates, "categories": categories})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_get(request: Request):
        """获取单个模板详情。"""
        try:
            template_id = request.path_params.get("template_id", "")
            store = _template_store()
            t = store.get_template(template_id)
            if not t:
                return _js({"ok": False, "error": "模板不存在"}, 404)
            return _js({"ok": True, "template": t})
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_create(request: Request):
        """创建模板。"""
        try:
            b = await _body(request)
            name = (b.get("name") or "").strip()
            dsl = (b.get("dsl") or "").strip()
            if not name or not dsl:
                return _js({"ok": False, "error": "name 和 dsl 不能为空"}, 400)
            store = _template_store()
            result = store.create_template(
                name=name, dsl=dsl,
                description=b.get("description", ""),
                category=b.get("category", "未分类"),
                tags=b.get("tags") or [],
                variables=b.get("variables"),
            )
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_update(request: Request):
        """更新模板。"""
        try:
            template_id = request.path_params.get("template_id", "")
            b = await _body(request)
            store = _template_store()
            result = store.update_template(template_id, **b)
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_delete(request: Request):
        """删除模板。"""
        try:
            template_id = request.path_params.get("template_id", "")
            store = _template_store()
            result = store.delete_template(template_id)
            if not result.get("ok"):
                return _js(result, 404)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

    async def templates_render(request: Request):
        """渲染模板，生成 DSL。"""
        try:
            template_id = request.path_params.get("template_id", "")
            b = await _body(request)
            store = _template_store()
            result = store.render_template(
                template_id,
                variables=b.get("variables") or {},
            )
            if not result.get("ok"):
                return _js(result, 400)
            return _js(result)
        except Exception as e:
            return _js({"ok": False, "error": str(e)}, 500)

'''

if insert_marker in content:
    content = content.replace(insert_marker, template_code + insert_marker, 1)
    print("1. 模板库函数插入: OK")
else:
    print("1. 模板库函数插入: NOT FOUND")

# 在路由表中注册模板库路由
route_marker = '''        # API Key 管理（v1.5.1）'''

template_routes = '''        # 模板库（v1.5.2）
        Route("/api/templates", templates_list, methods=["GET"]),
        Route("/api/templates", templates_create, methods=["POST"]),
        Route("/api/templates/{template_id}", templates_get, methods=["GET"]),
        Route("/api/templates/{template_id}", templates_update, methods=["PUT"]),
        Route("/api/templates/{template_id}", templates_delete, methods=["DELETE"]),
        Route("/api/templates/{template_id}/render", templates_render, methods=["POST"]),
        # API Key 管理（v1.5.1）'''

if route_marker in content:
    content = content.replace(route_marker, template_routes, 1)
    print("2. 模板库路由注册: OK")
else:
    print("2. 模板库路由注册: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
