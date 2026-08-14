#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow Gateway — 自带 LLM 客户端（OpenAI 兼容 /chat/completions，多后端 fallback）。

设计（只读参考 memory-worker LLMRouter，不改兄弟项目）：
  · 多后端：llm_backends（JSON 数组 [{url,api_key,model,name?}...]）优先；缺失回落单
    llm_api_key/url/model。仅 env 驱动，绝不硬编码密钥（P-2 门禁）。
  · fallback：某后端 429 / 5xx / 超时 / 鉴权失败(401/403) → 自动下一后端；全部失败 → 抛 LLMError。
  · 脱敏：_mask_secret 用于日志，绝不落明文密钥。
  · 对外方法对齐 memory-worker：chat / stream_chat / ping / reconfigure / close。
  · 传输用 httpx 异步（与 memory-worker 一致）；ask_llm 工具在网关既有事件循环内直接 await。

所有失败一律抛 LLMError（带可读中文文案），由上层工具转成 {ok:False, error} 友好返回，不崩。
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("autoflow.llm")


class LLMError(Exception):
    """LLM 调用统一异常（含所有后端均失败的情况）。"""


def _mask_secret(secret: str) -> str:
    """日志脱敏：保留前 4 位 + 末 4 位，中间 ****；空/过短整体遮。密钥绝不落明文日志。"""
    if not secret:
        return "‹空›"
    s = str(secret)
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def normalize_chat_url(url: str) -> str:
    """把后端 base url 归一成 …/chat/completions。

    接受 https://api.openai.com/v1 / https://x/v1/chat / https://x 三种写法。"""
    if not url:
        return ""
    u = url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/chat/completions"


def _num(s: Any, default: Any) -> Any:
    """安全转数字：失败回落 default（兼容 None / 非法串）。"""
    if s is None:
        return default
    try:
        return type(default)(s)
    except (TypeError, ValueError):
        return default


class LLMProvider:
    """单个 OpenAI 兼容后端。"""

    def __init__(self, url: str, api_key: str, model: str, name: str = "", enabled: bool = True):
        self.url = normalize_chat_url(url)
        self.api_key = api_key
        self.model = model
        self.name = name or url
        self.enabled = bool(enabled)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key and self.model)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    async def _post(self, client: httpx.AsyncClient, payload: Dict[str, Any], timeout: float) -> httpx.Response:
        return await client.post(self.url, headers=self._headers(), json=payload, timeout=timeout)

    async def chat(self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
                   temperature: float = 0.7, max_tokens: int = 4096, timeout: float = 120,
                   tools: Optional[List[Dict[str, Any]]] = None) -> str:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient() as client:
                resp = await self._post(client, payload, timeout)
        except httpx.HTTPError as e:
            # 连接失败 / 超时 / DNS / TLS 等传输层异常 → 包装成 LLMError 触发 fallback（避免裸 500）
            raise LLMError(f"后端 {self.name} 连接异常：{e}")
        return self._parse_text(resp)

    async def chat_raw(self, messages: List[Dict[str, str]], *, tools: Optional[List[Dict[str, Any]]] = None,
                       model: Optional[str] = None, temperature: float = 0.7,
                       max_tokens: int = 4096, timeout: float = 120) -> Dict[str, Any]:
        """发 messages（含可选 tools），返回 OpenAI 原始 assistant message dict。

        含 role / content / tool_calls（若有）。供 LLMRouter.chat_with_tools 跑 agent 循环，
        由调用方拿到 tool_calls 后自行执行并回填。多后端 fallback 由上层 LLMRouter 负责。"""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient() as client:
                resp = await self._post(client, payload, timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"后端 {self.name} 连接异常：{e}")
        return self._parse_message(resp)

    async def stream_chat(self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
                          temperature: float = 0.7, max_tokens: int = 4096, timeout: float = 120):
        """逐块 yield 文本 delta；某后端失败则交由上层切下一后端。"""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", self.url, headers=self._headers(),
                                         json=payload, timeout=timeout) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except httpx.HTTPError as e:
            raise LLMError(f"后端 {self.name} 连接异常：{e}")

    def _raise_if_error(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise LLMError(f"后端 {self.name} 鉴权失败（{resp.status_code}），请检查 api_key")
        if resp.status_code == 429:
            raise LLMError(f"后端 {self.name} 限流（429），尝试下一后端")
        if resp.status_code >= 500:
            raise LLMError(f"后端 {self.name} 服务端错误（{resp.status_code}），尝试下一后端")
        if resp.status_code >= 400:
            # 其它 4xx（如 400 请求体非法）一般不值得重试，直接报错
            raise LLMError(f"后端 {self.name} 客户端错误（{resp.status_code}）：{self._resp_text(resp)[:200]}")

    def _parse_text(self, resp: httpx.Response) -> str:
        self._raise_if_error(resp)
        try:
            obj = resp.json()
        except Exception:
            raise LLMError(f"后端 {self.name} 返回非 JSON：{self._resp_text(resp)[:200]}")
        return (obj.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    def _parse_message(self, resp: httpx.Response) -> Dict[str, Any]:
        """解析 OpenAI 响应为 assistant message dict（含 role/content/tool_calls）。"""
        self._raise_if_error(resp)
        try:
            obj = resp.json()
        except Exception:
            raise LLMError(f"后端 {self.name} 返回非 JSON：{self._resp_text(resp)[:200]}")
        msg = (obj.get("choices") or [{}])[0].get("message", {}) or {}
        if not isinstance(msg, dict):
            msg = {}
        return msg

    @staticmethod
    def _resp_text(resp: httpx.Response) -> str:
        try:
            return resp.text
        except Exception:
            return ""

    async def ping(self, timeout: float = 15) -> Dict[str, Any]:
        """单后端连通性探测：发一条极短请求，返回结构化结果（供 WebUI「测试这条」按钮）。

        成功 → {connected:True, name, model, endpoint}；失败 → {connected:False, ..., error}。"""
        if not self.configured:
            return {"connected": False, "name": self.name, "model": self.model,
                    "endpoint": self.url, "error": "后端未完整配置（缺 url/api_key/model）"}
        try:
            async with httpx.AsyncClient() as client:
                resp = await self._post(client,
                    {"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                     "max_tokens": 1}, timeout)
            self._raise_if_error(resp)
            return {"connected": True, "name": self.name, "model": self.model, "endpoint": self.url}
        except LLMError as e:
            return {"connected": False, "name": self.name, "model": self.model,
                    "endpoint": self.url, "error": str(e)}
        except httpx.HTTPError as e:
            return {"connected": False, "name": self.name, "model": self.model,
                    "endpoint": self.url, "error": f"连接异常：{e}"}


class LLMRouter:
    """多后端路由：按序 fallback，首个成功即返回。"""

    def __init__(self, cfg: Any = None):
        self._providers: List[LLMProvider] = []
        self._cfg_ref: Any = None
        if cfg is not None:
            self.reconfigure(cfg)

    def reconfigure(self, cfg: Any) -> None:
        self._cfg_ref = cfg
        backends = list(getattr(cfg, "llm_backends", []) or [])
        providers: List[LLMProvider] = []
        if backends:
            for b in backends:
                if isinstance(b, dict):
                    providers.append(LLMProvider(
                        url=b.get("url", ""),
                        api_key=b.get("api_key", ""),
                        model=b.get("model", "") or getattr(cfg, "llm_model", ""),
                        name=b.get("name", ""),
                        enabled=bool(b.get("enabled", True)),
                    ))
        # 回落单后端（llm_api_key/url/model）
        single = LLMProvider(
            url=getattr(cfg, "llm_api_url", ""),
            api_key=getattr(cfg, "llm_api_key", ""),
            model=getattr(cfg, "llm_model", ""),
            name="single",
        )
        if single.configured:
            providers.append(single)
        self._providers = providers

    def reconfigure_dict(self, d: Dict[str, Any]) -> None:
        """从 WebUI 保存的 llm_config.json 字典重建后端（与 env 配置同源，结构不同）。

        d 字段：backends([{url,api_key,model,name?}]) 优先；缺失回落单 api_url/api_key/model。
        与 reconfigure(cfg) 互斥：调此即改由文件驱动（WebUI 免重启生效）。"""
        self._cfg_ref = None
        providers: List[LLMProvider] = []
        for b in list((d or {}).get("backends") or []):
            if isinstance(b, dict):
                providers.append(LLMProvider(
                    url=b.get("url", ""),
                    api_key=b.get("api_key", ""),
                    model=b.get("model", "") or (d or {}).get("model", ""),
                    name=b.get("name", ""),
                    enabled=bool(b.get("enabled", True)),
                ))
        single = LLMProvider(
            url=(d or {}).get("api_url", ""),
            api_key=(d or {}).get("api_key", ""),
            model=(d or {}).get("model", ""),
            name="single",
        )
        if single.configured:
            providers.append(single)
        self._providers = providers

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LLMRouter":
        r = cls()
        r.reconfigure_dict(d)
        return r

    @property
    def configured(self) -> bool:
        return any(p.configured for p in self._providers)

    def _params(self, cfg, temperature, max_tokens, timeout):
        if cfg is not None:
            return (_num(temperature, getattr(cfg, "llm_temperature", 0.7)),
                    _num(max_tokens, getattr(cfg, "llm_max_tokens", 4096)),
                    _num(timeout, getattr(cfg, "llm_timeout", 120)))
        return (_num(temperature, 0.7), _num(max_tokens, 4096), _num(timeout, 120))

    async def chat(self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
                   system: Optional[str] = None, temperature: Any = None,
                   max_tokens: Any = None, timeout: Any = None) -> str:
        if not self.configured:
            raise LLMError("LLM 未配置：请在网关连接设置或环境变量填入 AUTOFLOW_LLM_BACKENDS "
                           "或 AUTOFLOW_LLM_API_KEY/URL/MODEL")
        cfg = self._cfg_ref
        msgs: List[Dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        temp, mt, to = self._params(cfg, temperature, max_tokens, timeout)
        last_err: Optional[LLMError] = None
        for p in self._providers:
            if not p.configured:
                continue
            if not p.enabled:
                continue
            try:
                return await p.chat(msgs, model=model or p.model, temperature=temp,
                                    max_tokens=mt, timeout=to)
            except LLMError as e:
                last_err = e
                logger.warning("LLM 后端 %s 失败，尝试下一后端：%s", p.name, _mask_secret(str(e)))
                continue
        raise LLMError(f"所有 LLM 后端均失败：{last_err}" if last_err else "所有 LLM 后端均未配置")

    async def chat_with_tools(self, messages: List[Dict[str, str]], tools: List[Dict[str, Any]],
                              executor, *, model: Optional[str] = None, system: Optional[str] = None,
                              max_rounds: int = 6, temperature: Any = None,
                              max_tokens: Any = None, timeout: Any = None) -> str:
        """跑 agent 循环（OpenAI function-calling 协议）：每轮发 tools，无 tool_calls → 返回文本（终态）；
        有 tool_calls → 经 executor(name, arguments) 执行并回填 {role:tool}，再进下一轮；最多 max_rounds 防死循环。

        - executor：调用方注入的异步函数 async (name:str, arguments:dict) -> str，负责真正执行工具
          （如 webui.py 内复用 FastMCP call_tool）。llm_client 不绑定任何工具分发逻辑，保持单一真相源。
        - tools：OpenAI function 格式 [{"type":"function","function":{"name","description","parameters"}}]。
        - 保持多后端 fallback（沿用 LLMProvider.chat_raw 的 401/429/5xx 重试）。
        - 返回终态文本；达 max_rounds 仍带 tool_calls 则强制返回最后一轮文本并告警（防死循环）。"""
        if not self.configured:
            raise LLMError("LLM 未配置：请在 WebUI「LLM 设置」页填入 OpenAI 兼容 base_url/api_key/model")
        cfg = self._cfg_ref
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        temp, mt, to = self._params(cfg, temperature, max_tokens, timeout)
        last_err: Optional[LLMError] = None
        rounds = max(1, int(max_rounds))
        last_assistant = None
        for _ in range(rounds):
            assistant = None
            for p in self._providers:
                if not p.configured:
                    continue
                if not p.enabled:
                    continue
                try:
                    assistant = await p.chat_raw(msgs, tools=tools, model=model or p.model,
                                                 temperature=temp, max_tokens=mt, timeout=to)
                    break
                except LLMError as e:
                    last_err = e
                    logger.warning("LLM 后端 %s 失败，尝试下一后端：%s", p.name, _mask_secret(str(e)))
                    continue
            if assistant is None:
                raise LLMError(f"所有 LLM 后端均失败：{last_err}" if last_err else "所有 LLM 后端均未配置")
            last_assistant = assistant
            msgs.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return assistant.get("content", "") or ""
            # ── 有 tool_calls：由调用方 executor 执行并回填 ──
            for tc in tool_calls:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                try:
                    result = await executor(name, args)
                except Exception as e:  # 工具执行异常不应中断循环，回填错误文本让 LLM 自我纠正
                    logger.warning("LLM 工具 %s 执行异常：%s", name, e)
                    result = f"[工具 {name} 执行出错: {e}]"
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": str(result)})
        logger.warning("LLM agent 循环达 max_rounds=%s 上限，强制终态返回", max_rounds)
        return (last_assistant or {}).get("content", "") or ""

    async def stream_chat(self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
                          system: Optional[str] = None, temperature: Any = None,
                          max_tokens: Any = None, timeout: Any = None):
        if not self.configured:
            raise LLMError("LLM 未配置")
        cfg = self._cfg_ref
        msgs: List[Dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        temp, mt, to = self._params(cfg, temperature, max_tokens, timeout)
        last_err: Optional[LLMError] = None
        for p in self._providers:
            if not p.configured:
                continue
            if not p.enabled:
                continue
            try:
                async for piece in p.stream_chat(msgs, model=model or p.model, temperature=temp,
                                                 max_tokens=mt, timeout=to):
                    yield piece
                return
            except LLMError as e:
                last_err = e
                logger.warning("LLM 后端 %s 流式失败，尝试下一后端：%s", p.name, _mask_secret(str(e)))
                continue
        raise LLMError(f"所有 LLM 后端均失败（流式）：{last_err}" if last_err else "所有 LLM 后端均未配置")

    async def ping(self) -> Dict[str, Any]:
        """探测所有已配置后端连通性，返回结构化结果（供 WebUI「测试全部」按钮）。

        返回 {ok, message, backends:[{connected,name,model,endpoint,error?}]}；
        ok 表示至少一个后端连通。停用(disabled)的后端仍被测（便于用户确认其可用性）。"""
        results = []
        for p in self._providers:
            if not p.configured:
                results.append({"connected": False, "name": p.name, "model": p.model,
                                "endpoint": p.url, "error": "后端未完整配置（缺 url/api_key/model）"})
                continue
            results.append(await p.ping())
        connected = [r for r in results if r.get("connected")]
        ok = len(connected) > 0
        message = (f"LLM 代理池可用：{len(connected)}/{len(results)} 个后端连通"
                   if results else "未配置任何后端")
        return {"ok": ok, "message": message, "backends": results}

    def close(self) -> None:
        # 每调用独立建 AsyncClient，无长连接需关；保留接口对齐 memory-worker
        self._providers = []


# ── 模块单例（仿 acp_client 风格，避免 webui.py 逐层透传）──
_ROUTER: Optional[LLMRouter] = None


def get_llm_router(cfg: Any = None) -> LLMRouter:
    global _ROUTER
    if _ROUTER is None:
        if cfg is not None:
            _ROUTER = LLMRouter(cfg)
        else:
            from .config import get_config
            _ROUTER = LLMRouter(get_config())
    elif cfg is not None:
        _ROUTER.reconfigure(cfg)
    return _ROUTER


def reconfigure_router_from_llm_config(cfg: Any) -> bool:
    """若 data/<env>/llm_config.json 存在且含后端配置，用其覆盖路由单例（WebUI 设置免重启生效）。

    仅在文件确实提供了后端凭据时才覆盖；否则保留 env 配置（autoflow_ask_llm 不受影响）。
    返回是否发生了覆盖。"""
    from .config import load_llm_config
    d = load_llm_config(cfg)
    if d and (d.get("backends") or d.get("api_key") or d.get("api_url")):
        get_llm_router(cfg).reconfigure_dict(d)
        return True
    return False


def chat_sync(messages: List[Dict[str, str]], **kw) -> str:
    """同步便捷封装（仅用于独立脚本/测试；网关工具内请直接用 async chat 以免事件循环冲突）。"""
    return asyncio.run(get_llm_router().chat(messages, **kw))


def ping_sync() -> Dict[str, Any]:
    return asyncio.run(get_llm_router().ping())
