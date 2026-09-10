#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFlow Gateway — ACP 客户端（autoflow 侧调用对端 memory-worker 的 /acp）

规格（memory-worker ACP 文档 §2/§7/§8）：
  · 传输 JSON-RPC 2.0 over HTTP+SSE；POST /acp，Content-Type application/json。
  · 非流式方法（initialize/cancel/session.*）→ application/json 响应。
  · prompt → text/event-stream，每事件 `event: message\\ndata: <JSON-RPC 通知>`。
    流结束 = 本轮会话结束，不再单独发 JSON-RPC result；完成态经 session_update.status 表达。
  · 鉴权头：`Authorization: Bearer acp_xxx` 或 `x-acp-token: acp_xxx`。

仅用标准库（urllib.request）手写 SSE 行解析，零新依赖，与网关既有 HTTP 风格一致。
所有对外调用失败一律返回 {ok:False, error:...}（不抛异常），便于上层给 agent 友好提示。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


def _acp_error(message: str) -> Dict[str, Any]:
    return {"ok": False, "error": message}


def _acp_error_from_jsonrpc(raw: bytes, what: str) -> Dict[str, Any]:
    """把对端返回的非 SSE 包体（通常是 JSON-RPC error，如鉴权失败 -32000）转成
    统一的 {ok:False, error:...}。

    为什么必须归一：规格要求鉴权失败走 HTTP 200 + JSON-RPC error，若直接把解析结果
    透传给上层，包体里没有 `ok` 字段，调用方 `res["ok"]` 会 KeyError 或被当成
    「无错误」静默放过。这里统一收口成带 error 文案的失败对象。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return _acp_error(f"ACP {what} 返回非 JSON 包体（对端可能未实现 /acp）")
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        return _acp_error(
            f"ACP {what} 被对端拒绝：{err.get('message') or '未知错误'}"
            f"（code={err.get('code')}）"
        )
    return _acp_error(f"ACP {what} 未返回 SSE 流，对端响应：{str(obj)[:200]}")


def call_acp(url: str, token: str, method: str,
             params: Optional[Dict[str, Any]] = None,
             *, timeout: float = 300.0) -> Dict[str, Any]:
    """发一个非流式 ACP JSON-RPC 请求（initialize/cancel/session.*）。

    返回解析后的 JSON-RPC 响应体（可能含 error 分支）；网络/解析/鉴权失败返回
    {ok:False, error:...}（不抛异常）。鉴权失败（HTTP 200 + JSON-RPC -32000）由服务端
    包体携带，这里原样透传。"""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "x-acp-token": token,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        # 服务端常把鉴权失败（§3）包成 HTTP 200 + JSON-RPC error(-32000) → 这里走的是非 200 分支
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return _acp_error(f"ACP HTTP {e.code}: {e.reason}")
    except Exception as e:  # 网络不可达 / 超时 / 非法 JSON
        return _acp_error(f"ACP 请求失败: {e}")


def prompt_acp(url: str, token: str, messages: List[Dict[str, str]],
               context: Optional[Dict[str, Any]] = None,
               session_id: Optional[str] = None,
               *, timeout: float = 300.0) -> Dict[str, Any]:
    """调对端 /acp prompt 并流式消费 SSE，提取最终 completed 的 text 返回。

    返回 {ok, session_id, status, text, blocks}；网络/鉴权失败返回 {ok:False, error} 友好提示。
    - blocks：按出现顺序累积的 content 块（tool_call/text/status），便于上层取结构化结果。
    - text：最后一个 type=text 块的内容（通常即最终答案）。
    - status：末次 session_update 的 status（completed/aborted/error）。
    """
    params: Dict[str, Any] = {"messages": messages}
    if session_id:
        params["sessionId"] = session_id
    if context:
        params["context"] = context
    payload = {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": params}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {token}",
        "x-acp-token": token,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    blocks: List[Dict[str, Any]] = []
    final_text = ""
    sid = session_id
    status = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # ★ 对端鉴权失败按规格返回 HTTP 200 + application/json 的 JSON-RPC error（非 SSE）。
            # 若不在此拦截，下面按行找 "data:" 会一无所获，最终误报 ok=True、text=""（静默假成功）。
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in ctype:
                raw_body = resp.read()
                return _acp_error_from_jsonrpc(raw_body, "prompt")
            event = ""
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_line = line.split(":", 1)[1].strip()
                    if event != "message":
                        continue
                    try:
                        msg = json.loads(data_line)
                    except Exception:
                        continue
                    p = msg.get("params", {}) or {}
                    if not sid and p.get("sessionId"):
                        sid = p.get("sessionId")
                    if p.get("status"):
                        status = p.get("status")
                    for blk in (p.get("content") or []):
                        blocks.append(blk)
                        if blk.get("type") == "text":
                            final_text = blk.get("text", "")
                    if p.get("status") in ("completed", "aborted", "error"):
                        break
    except urllib.error.HTTPError as e:
        try:
            return _acp_error_from_jsonrpc(e.read(), "prompt")
        except Exception:
            return _acp_error(f"ACP prompt HTTP {e.code}: {e.reason}")
    except Exception as e:  # 网络不可达 / 超时
        return _acp_error(f"ACP prompt 失败: {e}")
    return {"ok": True, "session_id": sid, "status": status,
            "text": final_text, "blocks": blocks}


def delegate_to_memory_worker(task: str,
                              context: Optional[Dict[str, Any]] = None,
                              cfg: Any = None) -> Dict[str, Any]:
    """autoflow → memory-worker 委派（规格 §8）。

    读 MEMORY_WORKER_ACP_URL / MEMORY_WORKER_ACP_TOKEN；未配置返回友好提示（不抛）。
    成功返回对端 completed 的 text（经 prompt_acp 流式消费）。"""
    from .config import get_config
    cfg = cfg or get_config()
    url = (cfg.memory_worker_acp_url or "").rstrip("/")
    token = cfg.memory_worker_acp_token or ""
    if not url or not token:
        return {"ok": False,
                "error": "未配置 memory-worker 委派通道（MEMORY_WORKER_ACP_URL / MEMORY_WORKER_ACP_TOKEN 缺失）。"
                         "请在网关连接设置或环境变量中填入 memory-worker 的 /acp 地址与 acp_ 令牌后重试。",
                "blocks": []}
    if not url.endswith("/acp"):
        url = url + "/acp"
    messages = [{"role": "user", "content": task}]
    return prompt_acp(url, token, messages, context=context)


# ── 竞技场 ↔ memory-agent 消费点（ROADMAP #4）──────────────────────────────
# 网关用**独立的 arena_ 令牌**访问对端窄接口，与 acp_ 委派链路隔离：
#   · 读灵感：ACP 工具 get_arena_inspiration（对端「洞察 + LLM 包装」）
#   · 落战报：ACP 工具 record_arena_result（带 used_memory_tools 遥测）
#   · 读快照：HTTP GET /api/arena/snapshots/{id}（确定性，作灵感降级源）
# 三条路径共用同一 arena_ 令牌；令牌只从 config/env 读，绝不入仓库（P-2 门禁）。

# 对端 prompt 是 LLM 中介（设计即为「洞察 + LLM 包装」）：不加约束时模型可能改写
# 结果。这里要求逐字回传工具 JSON，便于网关确定性解析；解析失败则上层降级。
_VERBATIM = "只把工具返回的原始 JSON 完整贴出来，不要任何解释、不要改写、不要增删字段。"


def _arena_ctx(cfg: Any = None):
    """返回 (base_url, token)。base 优先 arena_url，缺省由 acp_url 推导（同一实例）。"""
    from .config import get_config
    cfg = cfg or get_config()
    url = (getattr(cfg, "memory_agent_arena_url", "") or "").strip().rstrip("/")
    if not url:
        url = (getattr(cfg, "memory_worker_acp_url", "") or "").strip().rstrip("/")
        if url.endswith("/acp"):
            url = url[: -len("/acp")]
    token = (getattr(cfg, "memory_agent_arena_token", "") or "").strip()
    return url, token


def arena_configured(cfg: Any = None) -> bool:
    """竞技场联动通道是否已配置（base + arena_ 令牌齐备）。"""
    base, token = _arena_ctx(cfg)
    return bool(base and token)


def _tool_result(blocks: List[Dict[str, Any]], name: str) -> Optional[Any]:
    """从 ACP blocks 取指定工具最后一次 tool_call 的 result，并尝试解析 JSON。"""
    found = None
    for blk in blocks or []:
        if blk.get("type") == "tool_call" and blk.get("name") == name:
            found = blk
    if not found:
        return None
    res = found.get("result")
    if isinstance(res, (dict, list)):
        return res
    if isinstance(res, str):
        try:
            return json.loads(res)
        except Exception:  # noqa: BLE001
            # 模型可能包了 ```json 围栏或加了前后缀，退一步做首尾大括号截取
            s = res.strip()
            i, j = s.find("{"), s.rfind("}")
            if 0 <= i < j:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:  # noqa: BLE001
                    return None
            return None
    return None


def arena_fetch_inspiration(arena_id: str, limit: int = 5, cfg: Any = None,
                            timeout: float = 180.0) -> Dict[str, Any]:
    """读对端灵感：ACP get_arena_inspiration。未配置/失败返回 {ok:False,...}（不抛）。"""
    base, token = _arena_ctx(cfg)
    if not base or not token:
        return {"ok": False, "error": "未配置竞技场联动通道（MEMORY_AGENT_ARENA_TOKEN 缺失）", "items": []}
    ask = (f'请调用工具 get_arena_inspiration，参数 arena_id="{arena_id}"，limit={int(limit)}。' + _VERBATIM)
    res = prompt_acp(base + "/acp", token, [{"role": "user", "content": ask}], timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "ACP 调用失败", "items": [],
                "blocks": res.get("blocks") or []}
    data = _tool_result(res.get("blocks") or [], "get_arena_inspiration")
    if not isinstance(data, dict) or not data.get("items"):
        return {"ok": False, "error": "对端未返回可用灵感", "items": [],
                "text": res.get("text") or ""}
    items = data.get("items") or []
    return {"ok": True, "arena_id": arena_id, "items": items,
            "count": data.get("count", len(items))}


def arena_record_result(arena_id: str, task_title: str, task_description: str,
                        flow_dsl: str, success: bool, token_used: int, agent_id: str,
                        used_memory_tools: Optional[List[str]] = None,
                        cfg: Any = None, timeout: float = 180.0) -> Dict[str, Any]:
    """落战报到对端：ACP record_arena_result。未配置/失败返回 {ok:False,...}（不抛）。"""
    base, token = _arena_ctx(cfg)
    if not base or not token:
        return {"ok": False, "error": "未配置竞技场联动通道（MEMORY_AGENT_ARENA_TOKEN 缺失）"}
    args = {
        "arena_id": arena_id,
        "task_title": task_title,
        "task_description": task_description,
        "flow_dsl": flow_dsl,
        "success": bool(success),
        "token_used": int(token_used or 0),
        "agent_id": agent_id,
        "used_memory_tools": list(used_memory_tools or []),
    }
    ask = ("请调用工具 record_arena_result，参数为下面这个 JSON 对象（8 个字段缺一不可，逐字照传）：\n"
           + json.dumps(args, ensure_ascii=False) + "\n" + _VERBATIM)
    res = prompt_acp(base + "/acp", token, [{"role": "user", "content": ask}], timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "ACP 调用失败"}
    data = _tool_result(res.get("blocks") or [], "record_arena_result")
    if isinstance(data, dict) and data.get("ok"):
        return {"ok": True, "insight_id": data.get("insight_id"), "raw": data}
    return {"ok": False,
            "error": (data or {}).get("error") if isinstance(data, dict) else "对端未确认落库",
            "raw": data}


def arena_fetch_snapshot(arena_id: str, cfg: Any = None, timeout: float = 15.0) -> Dict[str, Any]:
    """读对端快照（**确定性 HTTP**，非 LLM 中介）：GET /api/arena/snapshots/{arena_id}。

    返回对端原始体 {ok, snapshot:{...}}；snapshot["snapshot_json"] 为 JSON 字符串，
    其 entities[].original 即真实 entity_id（label 为脱敏别名）。
    """
    base, token = _arena_ctx(cfg)
    if not base or not token:
        return {"ok": False, "error": "未配置竞技场联动通道（MEMORY_AGENT_ARENA_TOKEN 缺失）"}
    url = f"{base}/api/arena/snapshots/{urllib.parse.quote(arena_id)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读取快照失败：{exc}"}
