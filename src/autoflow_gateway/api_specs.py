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
API_SPECS: list[ApiSpec] = [
    ApiSpec(
        name="llm_doubao_chat",
        title="豆包大模型对话",
        kind="http_api",
        url="http://<NAS_IP>:1880/llm/chat",
        method="POST",
        extract="payload.reply",
        params={
            "user_msg": Param("user_msg", required=True, desc="用户消息内容"),
            "user": Param("user", required=False, default="大佬",
                          desc="对话人设：大佬/凯文/爱美丽"),
            "scenario": Param("scenario", required=False, default="书房",
                              desc="场景标签，影响记忆隔离与语气（中枢按 人设+场景 存对话历史）"),
            "model": Param("model", required=False, default="doubao", desc="模型名"),
        },
        description="调用豆包中枢生成中文回复（闲聊/提醒/总结/文生图描述等）。网关自动拼装请求体、调用、提取 reply，"
                    "agent 完全不接触 URL/鉴权/JSON 解析。返回 reply 字段（msg.payload.reply）供后续 提取/调用子流程 复用。",
        notes=(
            "底层走家中豆包中枢（NR d5a38c4777f84f35，已自带按 人设+场景 的对话记忆）。"
            "调用后 reply 在 msg.payload.reply，例：提取: 回复 = payload.reply → 调用子流程: demo_notify(text=`回复`)。"
            "kind=http_api 由编译器内联为 change(设参)→http request→(reply 落在 msg.payload)，无需 NR 子流程定义。"
        ),
    ),

    ApiSpec(
        name="llm_doubao_say",
        title="豆包对话并语音播报",
        kind="link_out",
        url="http://<NAS_IP>:1880/llm/chat",
        method="POST",
        extract="payload.reply",
        entry_link_id="af_apisay_in",
        nr_downstream_link_id="b595563939283231",  # TTS 队列入口
        nr_assemble="{'text': payload.reply, 'room': payload.scenario, 'level': '一般'}",
        params={
            "user_msg": Param("user_msg", required=True, desc="用户消息内容"),
            "user": Param("user", required=False, default="大佬", desc="对话人设：大佬/凯文/爱美丽"),
            "scenario": Param("scenario", required=False, default="书房", desc="场景标签（同时作为 TTS 播报房间）"),
            "model": Param("model", required=False, default="doubao", desc="模型名"),
        },
        description="调用豆包对话并把回复自动语音播报（fire-and-forget）。底层 flow 位于「AutoFlow API」tab，"
                    "经 link out 进 TTS 队列。agent 无需接触 URL/鉴权，一行即可「聊天+播报」。",
        notes=(
            "与 llm_doubao_chat 区别：say 是 fire-and-forget（自动播报、不回传 reply）；"
            "chat 是 request-response（回传 reply 供 提取/链式调用）。两者都走家中豆包中枢。"
            "entry_link_id=af_apisay_in 指向「AutoFlow API」tab 内的 llm_doubao_say 入口；"
            "该入口及后端 http/组装/link out 链由 api_specs.build_nr_tab_flows 从本 spec 自动生成，"
            "不再手搓（避免 网关注册 与 NR flow 两处不一致）。"
        ),
    ),

    ApiSpec(
        name="llm_doubao_image",
        title="豆包文生图",
        kind="http_api",
        url="http://<NAS_IP>:1880/llm/image",
        method="POST",
        extract="payload.image_url",
        params={
            "prompt": Param("prompt", required=True,
                            desc="图像生成提示词（画面描述，如 '一只赛博朋克风格的猫'）"),
        },
        description="调用豆包中枢生成图片，返回图片 URL（规整进 msg.payload.reply）。网关自动拼装请求体并提取 image_url，"
                    "agent 不接触 URL/鉴权。例：调用子流程: llm_doubao_image(prompt=`一只赛博朋克风格的猫`) "
                    "→ 提取: 图片链接 = payload.reply。",
        notes=(
            "底层走家中豆包中枢（NR d5a38c4777f84f35）的 /llm/image 端点。"
            "中枢把入参 body.prompt 作为提示词（缺省 '一只可爱的猫'），响应体 {image_url}；"
            "网关把 image_url 规整进 msg.payload.reply，与对话类能力返回值位置一致。"
            "kind=http_api 由编译器内联为 change(设参)→http request→(image_url 落在 msg.payload.reply)，无需 NR 子流程。"
        ),
    ),

    ApiSpec(
        name="llm_doubao_vision",
        title="豆包图生文（视觉理解）",
        kind="http_api",
        url="http://<NAS_IP>:1880/llm/vision",
        method="POST",
        extract="payload.reply",
        params={
            "prompt": Param("prompt", required=True,
                            desc="针对图片的提问/指令（如 '描述这张图'、'图里有几只猫'）"),
            "image": Param("image", required=True,
                           desc="图片来源：http(s) URL，或 base64 字符串（中枢自动加 data:image/jpeg;base64, 前缀）"),
            "model": Param("model", required=False, default="doubao",
                           desc="视觉模型名（中枢缺省 'doubao'）"),
        },
        description="调用豆包中枢做视觉理解（图生文），返回文字描述（msg.payload.reply）。"
                    "网关自动拼装 {prompt, image, model} 请求体并提取 reply，agent 不接触 URL/鉴权。"
                    "例：调用子流程: llm_doubao_vision(prompt=`描述这张图`, image=`https://...`) "
                    "→ 提取: 回复 = payload.reply。",
        notes=(
            "底层走家中豆包中枢（NR d5a38c4777f84f35）的 /llm/vision 端点。"
            "中枢编排：body.prompt（缺省 '描述这张图片'）、body.image（URL 或 base64）、body.model（缺省 'doubao'）→ "
            "POST doubao2api 视觉接口，响应体 {reply}；网关把 reply 规整进 msg.payload.reply。"
            "kind=http_api 由编译器内联为 change(设参)→http request→(reply 落在 msg.payload.reply)，无需 NR 子流程。"
        ),
    ),

    ApiSpec(
        name="llm_caiyun_weather",
        title="彩云天气",
        kind="link_out",
        url="https://api.caiyunapp.com/v2.7/<CAIYUN_TOKEN>/<CAIYUN_LON>,<CAIYUN_LAT>/weather?alert=true",
        method="GET",
        extract="payload.result",
        entry_link_id="af_weather_in",
        nr_tab=True,
        nr_out_links=[],
        params={},  # 坐标/密钥经 <CAIYUN_TOKEN>/<CAIYUN_LON>,<CAIYUN_LAT> 占位，部署时替换
        description="查询彩云天气（默认坐标占位，含预警 alert=true）。返回 result 对象"
                    "（实时温度/天气/未来小时与每日预报/预警）落在 msg.payload.reply。",
        notes=(
            "GET 接口、无请求体。网关 AutoFlow API tab 后端做「必填校验 + HTTP 状态错误处理」，"
            "result 经 link out 转发（用户可接 debug 观测或接下游）。黑箱可 调用子流程: llm_caiyun_weather() 触发。"
            "⚠️ URL 内 <CAIYUN_TOKEN>/<CAIYUN_LON>,<CAIYUN_LAT> 均为占位符，部署前需替换为你的彩云 token 与坐标。"
        ),
    ),

    ApiSpec(
        name="anysearch_batch",
        title="AnySearch 资讯搜索",
        kind="link_out",
        url="https://api.anysearch.com/mcp",
        method="POST",
        extract="payload.result",
        entry_link_id="af_anysearch_in",
        nr_tab=True,
        nr_headers={
            "Authorization": "Bearer <ANYSEARCH_API_KEY>",
            "Content-Type": "application/json",
        },
        # JSON-RPC 信封：把 keywords(逗号分隔) 展开成多个查询（各加" 最新资讯"），封进 batch_search
        nr_body_template=(
            r"{'jsonrpc':'2.0','id':1,'method':'tools/call',"
            r"'params':{'name':'batch_search',"
            r"'arguments':{'queries':"
            r'$split(payload.keywords, /\s*,\s*/)'
            r".{'query': $ & ' 最新资讯', 'max_results': (payload.max_results or 5)}}}}"
        ),
        nr_out_links=[],
        params={
            "keywords": Param("keywords", required=True,
                              desc="要搜索的关键词，逗号分隔（如 'mac mini m5, 小米智能存储, 苹果眼镜'）"),
            "max_results": Param("max_results", required=False, default=5,
                                 desc="每个词返回条数（默认 5，anysearch 上限 5）"),
        },
        description="调用 AnySearch MCP 批量搜索资讯。keywords 逗号分隔，自动展开为多个查询"
                    "（各加'最新资讯'）。返回 result 落在 msg.payload.reply。",
        notes=(
            "POST JSON-RPC 到 api.anysearch.com/mcp，Bearer 鉴权（<ANYSEARCH_API_KEY> 占位，部署前替换）；"
            "网关 tab 后端自动构造信封 + 校验必填 + HTTP 错误处理。"
            "黑箱可 调用子流程: anysearch_batch(keywords=`mac mini m5, 苹果眼镜`)。"
        ),
    ),
]


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
