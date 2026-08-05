# -*- coding: utf-8 -*-
"""API 能力声明式单一真相源（tab api spec）。

设计目的（回应"一个完整功能分成 NR + 网关两处手搓"的架构债）：
- 每个 API 能力**只在这里定义一次**。
- `to_subflow_spec()` 派生出网关侧的 `SubflowSpec`（供 dsl_engine 编译 / dsl_help 指导 / 黑箱调用）。
- `build_nr_tab_flows()` 从同一份 spec **生成** NR「AutoFlow API」tab 的真实 flow
  （link_out 类能力需要一个后端 flow 真正干活；http_api 类由网关内联，不生成 NR 节点）。
- 网关注册与 NR tab 都从这份 spec 派生 → 不再有"两处手搓、改一处漏一处"的 split。

kind 取值：
- "http_api" ：网关编译期内联 change(设参)→http request→change(取 reply)；
              agent 不碰 URL，reply 落在 msg.payload.reply。不生成 NR 节点。
- "link_out" ：网关只发一个 link out 到 `entry_link_id`（NR tab 入口）；
              `build_nr_tab_flows` 在 tab 里生成「入口→http→取reply→组装→link out」链真正干活。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .subflows import Param, SubflowSpec


@dataclass
class ApiSpec:
    name: str
    title: str
    kind: str  # "http_api" | "link_out"
    # —— 请求描述（http_api 用于网关内联；link_out 用于生成 NR tab 内的 http 节点）——
    url: str = ""
    method: str = "POST"
    extract: str = "payload.reply"  # reply 在 http 响应里的取值表达式（JSONata）
    # —— link_out 专用：网关 link out 目标 + NR tab 生成所需 ——
    entry_link_id: str = ""          # NR tab 入口 link in 的节点 id（网关 link out 指向它）
    nr_downstream_link_id: str = ""  # 生成链末端 link out 指向的下游入口（如 TTS 队列）
    nr_assemble: str = ""            # 组装下游入参的 JSONata 表达式（如 {"text": payload.reply, ...}）
    # —— 帮助信息（原样透传给 dsl_help / 黑箱）——
    params: dict[str, Param] = field(default_factory=dict)
    description: str = ""
    notes: str = ""
    # —— AutoFlow API tab 生成增强（build_nr_tab_flows，回应"太简陋/缺校验与错误处理"）——
    nr_tab: bool = False               # 是否在本 tab 生成后端流（即便非 link_out 也生成）
    nr_headers: dict = field(default_factory=dict)   # 自定义请求头
    nr_body_template: str = ""         # 请求体 JSONata 模板（覆盖默认"透传 payload"）
    nr_out_links: list = field(default_factory=list)  # 出口 link out 目标（空=用户自连；say 指向 TTS）
    nr_debug: bool = True              # 生成 debug 节点便于观测返回
    self_use: bool = False             # True=豆包等网关自用能力：不进 WebUI 产品列表、不参与默认 tab 生成，但 spec 定义保留（保重装性）

    def to_subflow_spec(self) -> SubflowSpec:
        """派生网关侧 SubflowSpec（供 dsl_engine / dsl_help）。"""
        if self.kind == "http_api":
            call = {
                "type": "http_api",
                "url": self.url,
                "method": self.method,
                "extract": self.extract,
            }
        elif self.kind == "link_out":
            call = {"type": "link_out", "entry_link_id": self.entry_link_id}
        else:
            raise ValueError(f"ApiSpec {self.name}: 未知 kind={self.kind}")
        return SubflowSpec(
            name=self.name,
            title=self.title,
            call=call,
            params=dict(self.params),
            description=self.description,
            notes=self.notes,
            param_style="payload",
        )

    def needs_nr_flow(self) -> bool:
        """是否需要在 NR AutoFlow API tab 生成后端 flow。"""
        return self.nr_tab or (self.kind == "link_out" and bool(self.nr_downstream_link_id))


# ── 单一真相源：所有 API 能力在此登记一次 ────────────────────────────────
# 豆包对话中枢（NR d5a38c4777f84f35）已自带按 人设+场景 的对话记忆。
def _load_api_specs() -> list[ApiSpec]:
    """从 data/api_specs.json 加载 API 能力声明（数据/代码分离）。

    代码只保留 ApiSpec 结构与派生逻辑；具体能力清单是数据，存于
    src/autoflow_gateway/data/api_specs.json（占位符，无密钥，进版本库）。
    """
    path = os.path.join(os.path.dirname(__file__), "data", "api_specs.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    specs: list[ApiSpec] = []
    for d in raw:
        params = {k: Param(**pv) for k, pv in (d.get("params") or {}).items()}
        rest = {k: v for k, v in d.items() if k != "params"}
        specs.append(ApiSpec(params=params, **rest))
    return specs


API_SPECS = _load_api_specs()


def get_api_spec(name: str) -> Optional[ApiSpec]:
    for s in API_SPECS:
        if s.name == name:
            return s
    return None


def _gen_validate_js(required: list) -> str:
    """生成「校验必填参数」function 节点代码（输出2路：合法→0，缺失→1）。"""
    req = json.dumps(required, ensure_ascii=False)
    return (
        "// 自动生成：校验必填参数\n"
        "const required = " + req + ";\n"
        "let missing = required.filter(function (k) {\n"
        "  const v = (msg.payload && msg.payload[k] !== undefined) ? msg.payload[k] : undefined;\n"
        "  return v === undefined || v === null || v === '';\n"
        "});\n"
        "if (missing.length > 0) {\n"
        "  msg.payload = { reply: '参数缺失: ' + missing.join(', ') };\n"
        "  msg._skip = true;\n"
        "  return [null, msg];\n"
        "}\n"
        "return [msg, null];\n"
    )


def _gen_error_js() -> str:
    """生成「错误处理 + 取返回值兜底」function 节点代码（输出1路）。"""
    return (
        "// 自动生成：错误处理 + 取返回值兜底\n"
        "if (msg._skip) return msg;\n"
        "const sc = msg.statusCode || (msg.response && msg.response.statusCode);\n"
        "if (sc && sc >= 400) {\n"
        "  msg.payload = { reply: '调用失败 (HTTP ' + sc + ')' };\n"
        "  return msg;\n"
        "}\n"
        "if (msg.payload && (msg.payload.status === 'failed' || msg.payload.error)) {\n"
        "  const m = (msg.payload.message) || (msg.payload.error) || '未知错误';\n"
        "  msg.payload = { reply: '调用失败: ' + m };\n"
        "  return msg;\n"
        "}\n"
        "if (msg.payload && msg.payload.reply === undefined) {\n"
        "  msg.payload = { reply: msg.payload };\n"
        "}\n"
        "return msg;\n"
    )


def build_nr_tab_flows(tab_id: str, specs: Optional[list[ApiSpec]] = None) -> list[dict]:
    """从 spec 生成「AutoFlow API」tab 的 NR 节点（健壮版：校验 + 错误处理 + 观测）。

    每个在 `needs_nr_flow()` 中为 True 的能力，生成一条完整链：
        link in(入口)
          → function(校验必填参数：缺失则置 payload.reply=错误并跳到出口)
          → [change(构造请求体/透传，GET 则直连 http)]
          → http request(带 nr_headers，ret=obj)
          → change(取返回值：payload.reply = extract JSONata)
          → function(错误处理：statusCode>=400 / 业务错误 → 置 reply=错误)
          → [change(组装下游入参 nr_assemble，仅 link_out 有)]
          → link out(→ nr_out_links 或 nr_downstream_link_id)
          → debug(观测 payload.reply，可选)

    相比旧版新增：① 必填参数校验；② HTTP 状态/业务错误兜底；
    ③ debug 节点观测；④ 支持自定义请求头与 JSON-RPC 请求体模板。
    `tab_id` 用于填充每个节点的 `z` 字段。返回扁平 nodes 列表，可直接 PUT 进对应 tab flow。
    """
    specs = specs if specs is not None else API_SPECS
    nodes: list[dict] = []
    x = 120
    for idx, spec in enumerate(specs):
        if not spec.needs_nr_flow():
            continue
        eid = spec.entry_link_id or f"{spec.name}_in"
        y = 160 + idx * 320
        bid = f"{eid}_body"
        hid = f"{eid}_http"
        vid = f"{eid}_validate"
        xid = f"{eid}_extract"
        fid = f"{eid}_err"
        aid = f"{eid}_assemble"
        oid = f"{eid}_out"
        gid = f"{eid}_dbg"
        method = spec.method.upper()
        required = [n for n, p in spec.params.items() if getattr(p, "required", False)]

        # 出口 link 目标：优先 nr_out_links，其次 link_out 的下游（如 say→TTS）
        out_links = list(spec.nr_out_links)
        if not out_links and spec.kind == "link_out" and spec.nr_downstream_link_id:
            out_links = [spec.nr_downstream_link_id]

        # 校验合法出口的目标：经 body 节点，或 GET 直连 http
        if spec.nr_body_template or method != "GET":
            valid_target = bid
        else:
            valid_target = hid

        # 0) 入口 link in
        nodes.append({
            "id": eid, "type": "link in", "z": tab_id,
            "name": f"{spec.title} 入口", "links": [],
            "x": x, "y": y, "wires": [[vid]],
        })
        # 1) 校验必填参数（output0=合法→valid_target，output1=缺失→出口+debug）
        nodes.append({
            "id": vid, "type": "function", "z": tab_id,
            "name": "校验必填参数", "func": _gen_validate_js(required),
            "outputs": 2, "x": x + 260, "y": y,
            "wires": [[valid_target], [oid, gid]],
        })
        # 2) 构造请求体 / 透传
        if spec.nr_body_template:
            nodes.append({
                "id": bid, "type": "change", "z": tab_id,
                "name": "构造请求体",
                "rules": [
                    {"t": "set", "p": "payload", "pt": "msg",
                     "to": spec.nr_body_template, "tot": "jsonata"},
                    {"t": "set", "p": "headers", "pt": "msg",
                     "to": json.dumps(spec.nr_headers, ensure_ascii=False), "tot": "json"},
                ],
                "x": x + 520, "y": y, "wires": [[hid]],
            })
        elif method != "GET":
            nodes.append({
                "id": bid, "type": "change", "z": tab_id,
                "name": "透传 payload",
                "rules": [{"t": "set", "p": "payload", "pt": "msg",
                           "to": "payload", "tot": "jsonata"}],
                "x": x + 520, "y": y, "wires": [[hid]],
            })
        # GET 无 body：校验合法出口直连 http（不碰 payload，避免误带 body）
        # 3) http request
        http = {
            "id": hid, "type": "http request", "z": tab_id,
            "name": f"→ {spec.title}", "method": method,
            "ret": "obj", "paytoqs": "ignore", "url": spec.url,
            "x": x + 780, "y": y, "wires": [[xid]],
        }
        if spec.nr_headers and not spec.nr_body_template:
            http["headers"] = [{"key": k, "value": v} for k, v in spec.nr_headers.items()]
        nodes.append(http)
        # 4) 取返回值（extract JSONata → payload.reply）
        nodes.append({
            "id": xid, "type": "change", "z": tab_id,
            "name": "取返回值",
            "rules": [{"t": "set", "p": "payload.reply", "pt": "msg",
                       "to": spec.extract, "tot": "jsonata"}],
            "x": x + 1040, "y": y, "wires": [[fid]],
        })
        # 5) 错误处理
        err = {
            "id": fid, "type": "function", "z": tab_id,
            "name": "错误处理", "func": _gen_error_js(),
            "outputs": 1, "x": x + 1300, "y": y, "wires": [[oid, gid]],
        }
        # 6) 组装下游入参（仅 link_out 带 nr_assemble，如 say→TTS）
        if spec.nr_assemble:
            nodes.append({
                "id": aid, "type": "change", "z": tab_id,
                "name": "组装下游入参",
                "rules": [{"t": "set", "p": "payload", "pt": "msg",
                           "to": spec.nr_assemble, "tot": "jsonata"}],
                "x": x + 1560, "y": y, "wires": [[oid, gid]],
            })
            err["wires"] = [[aid]]
        nodes.append(err)
        # 7) 出口 link out
        nodes.append({
            "id": oid, "type": "link out", "z": tab_id,
            "name": "→ 下游", "links": out_links,
            "x": x + 1820, "y": y,
        })
        # 8) debug 观测（可选）
        if spec.nr_debug:
            nodes.append({
                "id": gid, "type": "debug", "z": tab_id,
                "name": f"{spec.title} 返回", "active": True,
                "tosidebar": True, "console": False,
                "complete": "payload.reply", "targetType": "msg",
                "x": x + 1820, "y": y + 40, "wires": [[]],
            })
    return nodes
