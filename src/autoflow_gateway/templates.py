#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板库模块 —— AutoFlow Pro 的 Flow 模板复用。

设计原则：
  · 模板 = DSL 文本 + 变量占位符 + 元数据
  · 渲染 = 变量替换，生成可直接 propose-dsl 的 DSL
  · 从提案保存 = 把已通过编译的 DSL 保存为模板
  · 分类管理：按场景/设备/触发方式分类

存储：data/<env>/templates.json
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TemplateStore:
    """模板存储管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.templates_file = os.path.join(data_dir, "templates.json")
        os.makedirs(data_dir, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        if os.path.isfile(self.templates_file):
            try:
                with open(self.templates_file, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"templates": []}
        # 内置示例模板
        return {"templates": self._builtin_templates()}

    def _builtin_templates(self) -> List[Dict[str, Any]]:
        """内置示例模板。"""
        now = _utcnow_iso()
        return [
            {
                "id": "tpl_motion_light",
                "name": "人体感应开灯",
                "description": "检测到人体传感器触发后打开灯，延时关闭",
                "category": "照明",
                "tags": ["motion", "light", "auto"],
                "dsl": "场景: 人体感应开灯\n触发: {{motion_sensor}} on\n动作: light.turn_on({{light}})\n延时: {{delay_seconds|300}}秒\n动作: light.turn_off({{light}})",
                "variables": [
                    {"name": "motion_sensor", "description": "人体传感器 entity_id", "example": "binary_sensor.living_room_motion"},
                    {"name": "light", "description": "灯 entity_id", "example": "light.living_room_light"},
                    {"name": "delay_seconds", "description": "延时秒数", "default": 300},
                ],
                "created_at": now,
                "updated_at": now,
                "use_count": 0,
                "builtin": True,
            },
            {
                "id": "tpl_door_notify",
                "name": "门窗开启通知",
                "description": "门窗传感器开启时发送通知",
                "category": "安防",
                "tags": ["door", "notify", "security"],
                "dsl": "场景: 门窗开启通知\n触发: {{door_sensor}} on\n动作: notify.send({{notify_target}}, title=\"门窗开启\", message=\"{{door_name}}被打开了\")",
                "variables": [
                    {"name": "door_sensor", "description": "门窗传感器 entity_id", "example": "binary_sensor.front_door"},
                    {"name": "notify_target", "description": "通知目标", "example": "mobile_app_user"},
                    {"name": "door_name", "description": "门窗名称", "default": "门"},
                ],
                "created_at": now,
                "updated_at": now,
                "use_count": 0,
                "builtin": True,
            },
            {
                "id": "tpl_temperature_control",
                "name": "温度自动控制",
                "description": "温度超过阈值时开启空调/风扇",
                "category": "环境",
                "tags": ["temperature", "climate", "auto"],
                "dsl": "场景: 温度自动控制\n触发: {{temperature_sensor}} above {{threshold|28}}\n动作: climate.turn_on({{climate}}, temperature={{target_temp|24}})\n触发: {{temperature_sensor}} below {{target_temp|24}}\n动作: climate.turn_off({{climate}})",
                "variables": [
                    {"name": "temperature_sensor", "description": "温度传感器 entity_id", "example": "sensor.living_room_temperature"},
                    {"name": "climate", "description": "空调 entity_id", "example": "climate.living_room_ac"},
                    {"name": "threshold", "description": "开启阈值", "default": 28},
                    {"name": "target_temp", "description": "目标温度", "default": 24},
                ],
                "created_at": now,
                "updated_at": now,
                "use_count": 0,
                "builtin": True,
            },
        ]

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.templates_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.templates_file)

    def list_templates(self, category: Optional[str] = None,
                       keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出模板，可按分类和关键词过滤。"""
        data = self._load()
        templates = data.get("templates", [])
        if category:
            templates = [t for t in templates if t.get("category") == category]
        if keyword:
            kw = keyword.lower()
            templates = [t for t in templates
                         if kw in (t.get("name", "").lower()
                                   or kw in (t.get("description", "").lower())
                                   or any(kw in tag.lower() for tag in t.get("tags", [])))]
        # 返回时不包含 dsl 全文（列表页不需要），减少传输量
        return [{
            "id": t["id"],
            "name": t["name"],
            "description": t.get("description", ""),
            "category": t.get("category", "未分类"),
            "tags": t.get("tags", []),
            "variables": t.get("variables", []),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "use_count": t.get("use_count", 0),
            "builtin": t.get("builtin", False),
        } for t in templates]

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """获取单个模板详情。"""
        data = self._load()
        for t in data.get("templates", []):
            if t["id"] == template_id:
                return t
        return None

    def create_template(self, name: str, dsl: str,
                        description: str = "", category: str = "未分类",
                        tags: Optional[List[str]] = None,
                        variables: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """创建新模板。"""
        template_id = "tpl_" + uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        entry = {
            "id": template_id,
            "name": name,
            "description": description,
            "category": category,
            "tags": tags or [],
            "dsl": dsl,
            "variables": variables or self._extract_variables(dsl),
            "created_at": now,
            "updated_at": now,
            "use_count": 0,
            "builtin": False,
        }
        data = self._load()
        data["templates"].append(entry)
        self._save(data)
        return {"ok": True, "template": entry}

    def update_template(self, template_id: str, **kwargs) -> Dict[str, Any]:
        """更新模板。"""
        data = self._load()
        for t in data.get("templates", []):
            if t["id"] == template_id:
                for key in ["name", "description", "category", "tags", "dsl", "variables"]:
                    if key in kwargs and kwargs[key] is not None:
                        t[key] = kwargs[key]
                t["updated_at"] = _utcnow_iso()
                self._save(data)
                return {"ok": True, "template": t}
        return {"ok": False, "error": "模板不存在"}

    def delete_template(self, template_id: str) -> Dict[str, Any]:
        """删除模板（内置模板不可删除）。"""
        data = self._load()
        for i, t in enumerate(data.get("templates", [])):
            if t["id"] == template_id:
                if t.get("builtin"):
                    return {"ok": False, "error": "内置模板不可删除"}
                data["templates"].pop(i)
                self._save(data)
                return {"ok": True}
        return {"ok": False, "error": "模板不存在"}

    def render_template(self, template_id: str,
                        variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """渲染模板，替换变量，生成 DSL。"""
        t = self.get_template(template_id)
        if not t:
            return {"ok": False, "error": "模板不存在"}

        dsl = t["dsl"]
        variables = variables or {}
        missing = []

        # 替换 {{var}} 和 {{var|default}}
        def replace_var(match):
            var_expr = match.group(1).strip()
            if "|" in var_expr:
                var_name, default = var_expr.split("|", 1)
                var_name = var_name.strip()
                default = default.strip()
            else:
                var_name = var_expr
                default = None
            if var_name in variables and variables[var_name] != "":
                return str(variables[var_name])
            if default is not None:
                return default
            missing.append(var_name)
            return "{{" + var_name + "}}"

        rendered = re.sub(r"\{\{([^}]+)\}\}", replace_var, dsl)

        # 增加使用计数
        data = self._load()
        for tt in data.get("templates", []):
            if tt["id"] == template_id:
                tt["use_count"] = tt.get("use_count", 0) + 1
                self._save(data)
                break

        if missing:
            return {
                "ok": False,
                "error": "缺少变量: " + ", ".join(missing),
                "missing_variables": missing,
                "rendered": rendered,
            }
        return {"ok": True, "dsl": rendered, "template_id": template_id}

    def save_from_proposal(self, name: str, dsl: str,
                           description: str = "", category: str = "未分类") -> Dict[str, Any]:
        """从已通过的提案保存为模板。"""
        return self.create_template(
            name=name, dsl=dsl, description=description,
            category=category, tags=["from_proposal"],
        )

    def list_categories(self) -> List[str]:
        """列出所有分类。"""
        data = self._load()
        categories = set()
        for t in data.get("templates", []):
            categories.add(t.get("category", "未分类"))
        return sorted(categories)

    @staticmethod
    def _extract_variables(dsl: str) -> List[Dict[str, str]]:
        """从 DSL 中提取变量定义。"""
        variables = []
        seen = set()
        for match in re.finditer(r"\{\{([^}]+)\}\}", dsl):
            var_expr = match.group(1).strip()
            if "|" in var_expr:
                var_name, default = var_expr.split("|", 1)
                var_name = var_name.strip()
                default = default.strip()
            else:
                var_name = var_expr
                default = None
            if var_name not in seen:
                seen.add(var_name)
                entry = {"name": var_name, "description": ""}
                if default is not None:
                    entry["default"] = default
                variables.append(entry)
        return variables
