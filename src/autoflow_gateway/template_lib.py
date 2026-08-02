#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量模板库：复用 node-red-ai 的 skill 格式（YAML frontmatter + markdown 正文），
但正文是**可填充的 DSL 模板**（{{var}} / {{var|default}}），让 agent 填空即可生成合规 DSL，
避免每次从零写、降低幻觉面。

格式（与 node-red-ai skills 兼容，可未来直接 install_skill_from_url 拉社区技能）：
    ---
    name: motion_to_light
    description: 人体传感器触发开灯（带可选亮度）
    tags: [lighting, motion, 基础]
    params: sensor, light, brightness
    ---
    场景: {{room}}人体感应开灯
    触发: {{sensor}} 有人
    动作: light.turn_on({{light}}, brightness={{brightness|100}})
    预期:
      {{light}} = on

- params 为逗号分隔的占位符名（可选带默认值，用 | 分隔）。
- 正文里的 {{var}} 在 render 时被 values 替换；缺省且无默认则留空。
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


@dataclass
class Template:
    name: str
    description: str
    tags: List[str] = field(default_factory=list)
    params: List[str] = field(default_factory=list)
    body: str = ""
    path: str = ""

    def to_summary(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "params": self.params,
        }


class TemplateValidationError(Exception):
    """渲染模板时必填参数缺失或存在未知参数。

    attributes:
        missing: 缺失的必填占位符名（正文里无默认值的 {{x}}，且未提供值）
        unknown: 调用方多传的、模板正文里不存在的参数名
    """

    def __init__(self, missing: Optional[List[str]] = None,
                 unknown: Optional[List[str]] = None):
        self.missing = missing or []
        self.unknown = unknown or []
        parts = []
        if self.missing:
            parts.append("缺失必填参数: " + ", ".join(self.missing))
        if self.unknown:
            parts.append("未知参数: " + ", ".join(self.unknown))
        super().__init__("; ".join(parts) if parts else "模板参数校验失败")


def _parse_frontmatter(text: str):
    """极简 YAML frontmatter 解析（无第三方依赖）。

    支持：顶层 key: value；值可用 [a, b] 或 a, b 表示列表；params 为逗号列表。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text.strip()
    fm = lines[1:end]
    body = "\n".join(lines[end + 1:]).strip()
    data: Dict = {}
    for ln in fm:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = v[1:-1]
        if "," in v:
            data[k] = [x.strip() for x in v.split(",") if x.strip()]
        else:
            data[k] = v
    return data, body


_VAR_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _var_names(body: str):
    """从模板正文提取占位符名。

    返回 (required, all_vars)：
      - required：无默认值的 {{x}}（缺值会产坏 DSL）
      - all_vars：全部占位符名（去重、保序）
    """
    required: List[str] = []
    all_vars: List[str] = []
    seen = set()
    for m in _VAR_RE.finditer(body):
        inner = m.group(1).strip()
        if "|" in inner:
            name = inner.split("|", 1)[0].strip()
        else:
            name = inner.strip()
        if name in seen:
            continue
        seen.add(name)
        all_vars.append(name)
        if "|" not in inner:
            required.append(name)
    return required, all_vars


def _render(body: str, values: Dict[str, str]) -> str:
    def repl(m):
        inner = m.group(1).strip()
        if "|" in inner:
            name, default = inner.split("|", 1)
            name, default = name.strip(), default.strip()
        else:
            name, default = inner.strip(), ""
        val = values.get(name, "")
        if val in (None, ""):
            val = default
        return "" if val is None else str(val)

    return _VAR_RE.sub(repl, body)


def load_templates(directory: Optional[str] = None) -> List[Template]:
    """扫描目录下的 *.md，解析为 Template 列表。"""
    d = directory or TEMPLATES_DIR
    out: List[Template] = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        fm, body = _parse_frontmatter(text)
        name = fm.get("name") or fn[:-3]
        out.append(Template(
            name=name,
            description=fm.get("description", ""),
            tags=fm.get("tags", []) if isinstance(fm.get("tags"), list) else [],
            params=fm.get("params", []) if isinstance(fm.get("params"), list) else [],
            body=body,
            path=path,
        ))
    return out


def list_templates(directory: Optional[str] = None) -> List[Dict]:
    return [t.to_summary() for t in load_templates(directory)]


def render_template(name: str, values: Dict[str, str],
                    directory: Optional[str] = None,
                    strict: bool = True) -> str:
    """按名渲染模板，返回填充后的 DSL 文本。找不到模板抛 KeyError。

    strict=True（默认）时，渲染前校验：
      - 必填参数（正文里无默认值的 {{x}}）是否都提供了非空值，否则抛
        TemplateValidationError(missing=[...])，杜绝静默产出空占位符的坏 DSL；
      - 是否存在模板正文里没有的未知参数，若有则 TemplateValidationError(unknown=[...])。
    strict=False 时退化为旧行为（缺值留空、多传参数被忽略），用于历史自检等场景。
    """
    for t in load_templates(directory):
        if t.name == name:
            values = values or {}
            if strict:
                required, all_vars = _var_names(t.body)
                missing = [v for v in required if not str(values.get(v, "")).strip()]
                unknown = [k for k in values.keys() if k not in all_vars]
                if missing or unknown:
                    raise TemplateValidationError(missing=missing, unknown=unknown)
            return _render(t.body, values)
    raise KeyError(f"模板不存在: {name}")
