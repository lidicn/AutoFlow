# -*- coding: utf-8 -*-
"""debug_bridge — Node-RED 5.x 原生 debug 事件旁路采集桥（两条热路径都不碰）。

设计铁律（见 autoflow 架构决策 #644）：
- 采集：网关后台 daemon 线程订阅 NR5.0.1 原生 websocket 事件流
  ws://<nr>/comms（Bearer 认证，101 握手已实测通过）。
  **绝不往任何 flow 插采集/debug 节点**——那是侵入式改造，把干净的观测变成脏数据。
- 呈现：网关只读 REST 端点 + autoflow_debug_read MCP 工具，只从本地内存缓冲读，
  per-read 不现打 NR、不触发任何节点。
- 缓冲护栏（T5 零炸裂半径）：每节点有界环形缓冲 + 全局有界缓冲 + TTL 淘汰 +
  单条 payload 截断上限。任何维度溢出都只丢最旧数据，绝不 OOM。
- 容错：帧解析 / 连接异常一律 fail-open——断线自动退避重连，绝不抛错上抛到网关主流程。
"""
from __future__ import annotations

import os
import re
import json
import time
import base64
import struct
import socket
import threading
import logging
import collections
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("autoflow.debug_bridge")

# ── 默认护栏（均可经 env 覆盖；属内部调参，不动 config.py）──
DEFAULT_TTL_SECONDS = int(os.environ.get("AUTOFLLOW_DEBUG_TTL", "300"))
DEFAULT_MAX_PER_NODE = int(os.environ.get("AUTOFLLOW_DEBUG_MAX_PER_NODE", "200"))
DEFAULT_MAX_TOTAL = int(os.environ.get("AUTOFLLOW_DEBUG_MAX_TOTAL", "5000"))
DEFAULT_MAX_PAYLOAD_CHARS = int(os.environ.get("AUTOFLLOW_DEBUG_MAX_PAYLOAD", "2000"))
DEFAULT_PREVIEW_CHARS = int(os.environ.get("AUTOFLLOW_DEBUG_PREVIEW", "160"))
DEFAULT_RECONNECT_BASE = float(os.environ.get("AUTOFLLOW_DEBUG_RECONN_BASE", "2"))
DEFAULT_RECONNECT_MAX = float(os.environ.get("AUTOFLLOW_DEBUG_RECONN_MAX", "30"))
DEFAULT_WS_PATH = "/comms"
DEBUG_TOPICS = ("debug",)  # 只关心 debug 主题；其余（notification/event）一律忽略


def _make_ws_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _http_url_to_ws(http_url: str) -> str:
    u = (http_url or "").rstrip("/")
    if u.startswith("https://"):
        scheme, rest = "wss", u[len("https://"):]
    elif u.startswith("http://"):
        scheme, rest = "ws", u[len("http://"):]
    elif u.startswith("wss://"):
        scheme, rest = "wss", u[len("wss://"):]
    elif u.startswith("ws://"):
        scheme, rest = "ws", u[len("ws://"):]
    else:
        scheme, rest = "ws", u
    # 关键归一（#649 根因修复）：Windows 服务以 LocalSystem/Session0 身份运行，
    # 该会话下 localhost 优先解析到 IPv6 `::1`，而 Session0 的 IPv6 回环是黑洞，
    # 导致 ws 握手 socket.create_connection 超时（timed out）。交互会话 `::1` 恰能通，
    # 造成「用户态诊断能连、服务态桥连不上」的假象。统一改回 127.0.0.1 走 IPv4 回环
    # （沙箱只走回环 127.0.0.1:port，与 gateway.py 的 nr_url 约定一致）。
    try:
        parsed = urllib.parse.urlparse(scheme + "://" + rest)
        if parsed.hostname == "localhost":
            netloc = "127.0.0.1"
            if parsed.port:
                netloc += ":" + str(parsed.port)
            return f"{scheme}://{netloc}"
    except Exception:
        pass
    return f"{scheme}://{rest}"


class _WsClose(Exception):
    """websocket 收到 close 帧的干净退出信号。"""


class DebugBridge:
    """旁路采集 Node-RED debug 事件，落本地内存环形缓冲，供只读端点消费。

    线程模型：单例 daemon 线程跑 _loop（模块级 _bridge_started 守卫，避免多个
    Gateway 实例重复建连）。缓冲区访问加 self._lock，read() 线程安全。
    """

    # 模块级单例守卫（对齐 gateway._start_watchdog 的模式）
    _bridge_lock = threading.Lock()
    _bridge_started = False

    def __init__(self, nr_client, nr_url: str, enabled: bool = True,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_per_node: int = DEFAULT_MAX_PER_NODE,
                 max_total: int = DEFAULT_MAX_TOTAL,
                 max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS,
                 preview_chars: int = DEFAULT_PREVIEW_CHARS,
                 ws_path: str = DEFAULT_WS_PATH,
                 reconnect_base: float = DEFAULT_RECONNECT_BASE,
                 reconnect_max: float = DEFAULT_RECONNECT_MAX):
        # nr_client：NodeRedClient 实例（仅用于取 Bearer token，不直接复用其 HTTP 会话）
        self.nr_client = nr_client
        self.nr_url = nr_url
        self.ws_url = _http_url_to_ws(nr_url) + ws_path
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.max_per_node = max_per_node
        self.max_total = max_total
        self.max_payload_chars = max_payload_chars
        self.preview_chars = preview_chars
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max

        # 缓冲：全局有界 deque（maxlen 自动淘汰最旧）；per_node_count 记账
        self._events: collections.deque = collections.deque(maxlen=max_total)
        self._per_node_count: Dict[str, int] = {}
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._sock: Optional[socket.socket] = None
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._frag_opcode = 0
        self._frag_payload = b""

    # ───────────── 生命周期 ─────────────

    def start(self) -> None:
        global _bridge_started
        if not self.enabled:
            logger.info("debug_bridge 未启用（AUTOFLLOW_DEBUG_BRIDGE=0）。")
            return
        with DebugBridge._bridge_lock:
            if DebugBridge._bridge_started:
                return
            DebugBridge._bridge_started = True
        self._thread = threading.Thread(target=self._loop, name="debug-bridge", daemon=True)
        self._thread.start()
        self._started_at = time.time()
        logger.info("debug_bridge 后台线程已启动，订阅 %s", self.ws_url)

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def retarget(self, nr_url: str) -> bool:
        """运行期改指目标 NR（连接设置在 WebUI 被改过时调用）。返回是否真的换了地址。

        R3(#round4) 同族：`ws_url` 原先只在 __init__ 算一次，用户在网关运行期间把
        NR_URL 从 A 改到 B 后，桥仍死连 A —— 表现同样是 connected:false 恒真，
        且**无任何提示**。这里换址后主动踹断当前 socket，让 _loop 的重连逻辑
        用新地址重建（桥本就是无限重连模型，不需要额外线程）。
        """
        try:
            new_ws = _http_url_to_ws(nr_url or "") + DEFAULT_WS_PATH
        except Exception:
            return False
        if not nr_url or new_ws == self.ws_url:
            return False
        old = self.ws_url
        self.ws_url = new_ws
        self.nr_url = nr_url
        logger.info("debug_bridge 目标切换：%s → %s（断开旧连接，等重连生效）", old, new_ws)
        sock = self._sock
        if sock is not None:
            try:
                sock.close()  # 触发 _connect_and_run 抛错 → _loop 用新 ws_url 重连
            except Exception:
                pass
        return True

    # ───────────── 主循环 ─────────────

    def _loop(self) -> None:
        backoff = self.reconnect_base
        while not self._stop.is_set():
            try:
                self._connect_and_run()
                backoff = self.reconnect_base
            except _WsClose:
                logger.info("debug_bridge 收到 close，重连中…")
            except Exception as e:  # fail-open：任何异常都重连，绝不崩网关
                self._last_error = str(e)
                logger.warning("debug_bridge 连接异常：%s", e)
            finally:
                self._connected = False
                self._sock = None
            if self._stop.is_set():
                break
            time.sleep(min(backoff, self.reconnect_max))
            backoff = min(backoff * 2, self.reconnect_max)

    def _connect_and_run(self) -> None:
        sock = self._connect()
        self._sock = sock
        self._connected = True
        self._last_error = None
        self._frag_opcode = 0
        self._frag_payload = b""
        try:
            while not self._stop.is_set():
                fin, opcode, payload = self._recv_frame(sock)
                if opcode == 0x0:  # continuation
                    self._frag_payload += payload
                    if fin:
                        full = self._frag_payload
                        self._frag_payload = b""
                        self._on_message(self._frag_opcode, full)
                    continue
                if opcode in (0x1, 0x2):  # text / binary
                    if fin:
                        self._on_message(opcode, payload)
                    else:
                        self._frag_opcode = opcode
                        self._frag_payload = payload
                    continue
                # 控制帧（close/ping/pong）在 _on_message 处理
                self._on_message(opcode, payload)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _on_message(self, opcode: int, payload: bytes) -> None:
        if opcode == 0x8:  # close
            raise _WsClose()
        if opcode == 0x9:  # ping -> pong（client→server 必须 mask）
            try:
                if self._sock is not None:
                    self._send_frame(self._sock, 0xA, payload)
            except Exception:
                pass
            return
        if opcode == 0xA:  # pong（忽略）
            return
        if opcode in (0x1, 0x2):
            text = payload.decode("utf-8", "replace")
            try:
                self._handle_message(text)
            except Exception as e:
                logger.warning("debug_bridge 帧解析跳过（fail-open）：%s", e)

    # ───────────── websocket 传输（零依赖标准库）─────────────

    def _get_token(self) -> Optional[str]:
        try:
            return self.nr_client.login()
        except Exception as e:
            logger.warning("debug_bridge 取 token 失败：%s", e)
            return None

    def _open_handshake(self, host: str, port: int, path: str,
                        token: Optional[str]) -> socket.socket:
        """建 TCP + 发 ws upgrade + 校验 101。token=None 表示裸握手（不带 Bearer 头）。"""
        sock = socket.create_connection((host, port), timeout=10)
        sock.settimeout(10)
        try:
            sock.sendall(self._handshake_request(host, port, path, token))
            self._read_http_response(sock)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        return sock

    def _connect(self) -> socket.socket:
        u = urllib.parse.urlparse(self.ws_url)
        host = u.hostname or "localhost"
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        token = self._get_token()
        # R3(#round4) iss_da3b4a5aa7 —— 桥恒 connected:false 的真根因。
        # NR 5.x 的 /comms upgrade handler **不接受 `Authorization` 头**：带 Bearer
        # 握手会被服务端直接 destroy socket（连 HTTP 响应都不回，客户端只看到
        # 「响应前连接已关闭」），于是 _loop 无限重连、缓冲恒空、debug_read 永远
        # connected:false/count:0。实测（NR 5 @1990）：带 Bearer → 空关闭；
        # 裸握手 → HTTP/1.1 101 Switching Protocols。
        # NR 官方 editor 客户端本就是两段式：**裸 ws 握手 + 应用层 {"auth": token}**。
        # 故默认裸握手；仅当裸握手失败时才回退「带 Bearer」重试一次，
        # 兼容可能存在的反向代理 / 旧版把鉴权放在 HTTP 头的部署。
        attempts: list = [None]
        if token:
            attempts.append(token)
        sock = None
        last_exc: Optional[Exception] = None
        for hs_token in attempts:
            try:
                sock = self._open_handshake(host, port, path, hs_token)
                if hs_token is not None:
                    logger.info("debug_bridge 裸握手失败，已回退 Bearer 头握手成功。")
                break
            except Exception as e:
                last_exc = e
                sock = None
        if sock is None:
            raise last_exc or RuntimeError("comms：握手失败（无可用方式）")
        # NR5 /comms 二次鉴权：ws 握手通过后，须先发 {"auth": <token>}
        # 应用消息，否则 NR 回 {"auth":"fail"} 并立即断开连接。
        if token:
            try:
                self._send_frame(sock, 0x1, json.dumps({"auth": token}).encode("utf-8"))
                # 消费 {"auth":"ok"} 回执（5s 内未到则忽略，fail-open）。
                # 注意：仅在本次读取时临时收紧超时；读完后必须还原为阻塞，
                # 否则主接收循环会继承 5s 超时，在两次心跳（约 15s）之间的静默期
                # 误判为断开而反复重连（#649 稳定性 bug）。
                sock.settimeout(5)
                self._recv_frame(sock)
            except Exception as e:
                logger.warning("debug_bridge 应用层鉴权回执读取跳过（fail-open）：%s", e)
            finally:
                sock.settimeout(None)  # 还原阻塞模式：主循环靠 recv 返回空字节感知真实断连
        self._subscribe(sock)
        logger.info("debug_bridge 已连上 ws %s (订阅 %s)", self.ws_url, DEBUG_TOPICS[0])
        return sock

    def _handshake_request(self, host: str, port: int, path: str, token: Optional[str]) -> bytes:
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {_make_ws_key()}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    def _read_http_response(self, sock: socket.socket) -> str:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("comms：响应前连接已关闭")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
        first = head.splitlines()[0] if head.splitlines() else ""
        if not (first.startswith("HTTP/1.1 101") or first.startswith("HTTP/1.0 101")):
            raise RuntimeError(f"comms：期望 101，实得 {first!r}")
        return head

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("comms：帧读取中途连接关闭")
            buf += chunk
        return buf

    def _recv_frame(self, sock: socket.socket) -> Tuple[int, int, bytes]:
        b0, b1 = self._recv_exact(sock, 2)
        fin = (b0 >> 7) & 1
        opcode = b0 & 0x0F
        masked = (b1 >> 7) & 1
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(sock, 8))[0]
        if masked:
            mask_key = self._recv_exact(sock, 4)
            raw = self._recv_exact(sock, length) if length else b""
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
        else:
            payload = self._recv_exact(sock, length) if length else b""
        return fin, opcode, payload

    def _send_frame(self, sock: socket.socket, opcode: int, data: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(data)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        sock.sendall(header + mask + masked)

    def _subscribe(self, sock: socket.socket) -> None:
        # NR comms.subscribe 接收单个字符串 topic（非数组），数组会触发
        # `opts.topic.replace is not a function` 报错。客户端连上后本就被自动
        # 订阅到全部话题，这里仅显式触发 retained 重放，格式必须正确。
        msg = json.dumps({"subscribe": DEBUG_TOPICS[0]}).encode("utf-8")
        self._send_frame(sock, 0x1, msg)

    # ───────────── 解析 + 缓冲 ─────────────

    def _handle_message(self, text: str) -> None:
        try:
            raw = json.loads(text)
        except Exception:
            return  # 非 JSON（如 keepalive 文本）直接忽略
        # NR 把多个事件打包成单个 TEXT 帧的 JSON 数组发送：[{topic,data},...]
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    self._handle_one(item)
            return
        if isinstance(raw, dict):
            self._handle_one(raw)

    def _handle_one(self, raw: Dict[str, Any]) -> None:
        topic = raw.get("topic")
        # 顶层 topic 存在且非 debug → 明确不是我们要的，跳过（如 hb 心跳）
        if isinstance(topic, str) and topic not in DEBUG_TOPICS:
            return
        info = self._extract_debug(raw)
        if info is None:
            return
        self._ingest(**info)

    @staticmethod
    def _first_present(d: Dict[str, Any], *keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    def _extract_debug(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """容错抽取 debug 事件。兼容两种线上形态：
        A) {topic:"debug", data:{id,name,topic,msg,_path,timestamp,...}}
        B) 裸 debug 对象 {id,name,topic,msg,_path,timestamp,...}
        返回 None 表示无法识别为 debug 事件。
        """
        data = raw
        if isinstance(raw.get("data"), dict):
            data = raw["data"]
        node_id = self._first_present(data, "id", "node", "nodeId")
        # flow_id 归属：优先 _path.id（NR 运行时注入的 flow 作用域）
        flow_id = None
        p = self._first_present(data, "_path", "path")
        if isinstance(p, dict):
            flow_id = self._first_present(p, "id", "flow", "z")
        if not flow_id:
            flow_id = self._first_present(data, "flow_id", "z")
        name = self._first_present(data, "name", "nodeName")
        mtopic = data.get("topic")
        # payload：NR 把 msg.payload 放在 data.msg；兼容 data.payload 与嵌套
        payload = self._first_present(data, "msg", "payload")
        if isinstance(payload, dict) and "payload" in payload and "msg" not in payload:
            payload = payload.get("payload")
        ts = self._first_present(data, "timestamp", "ts")
        retain = bool(self._first_present(raw, "retain", default=False)
                      or self._first_present(data, "retain", default=False))
        if node_id is None and payload is None:
            return None
        return {
            "node_id": node_id,
            "flow_id": flow_id,
            "name": name,
            "topic": mtopic,
            "payload": payload,
            "ts": ts,
            "retain": retain,
        }

    def _ingest(self, node_id, flow_id, name, topic, payload, ts, retain) -> None:
        try:
            if isinstance(payload, (dict, list)):
                try:
                    payload_str = json.dumps(payload, ensure_ascii=False)
                except Exception:
                    payload_str = str(payload)
            else:
                payload_str = str(payload) if payload is not None else ""
            preview = self._truncate(payload_str, self.preview_chars)
            full = self._truncate(payload_str, self.max_payload_chars)
            ts_num = ts if isinstance(ts, (int, float)) else None
            ev = {
                "flow_id": flow_id,
                "node_id": node_id,
                "name": name,
                "topic": topic,
                "payload_preview": preview,
                "payload_full": full,
                "payload_truncated": len(payload_str) > self.max_payload_chars,
                "payload_full_length": len(payload_str),
                "timestamp": ts_num,
                "received_at": time.time(),
                "retain": retain,
            }
            self._push(ev)
        except Exception as e:
            logger.warning("debug_bridge ingest 跳过（fail-open）：%s", e)

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        """截断字符串；若内容像 JSON（{ / [ 开头），保证截断结果仍是合法 JSON。

        Bug（深度测试报告 Bug 2）：旧实现对 payload 中段切断再追加
        ``(truncated,N chars)`` 文本，当 payload 本身是 JSON 字符串时，
        截断结果不再是合法 JSON，调用方 json.loads 必失败。
        修复：JSON 场景闭合未完成的定界符/字符串字面量，并把截断标记以
        结构内字段 ``__truncated__`` 注入（如 ``{"a":1,"__truncated__":true}``），
        使 json.loads 永远成功且能识别截断。边缘情况（如切断在悬挂 key 后）
        若仍不合法，退回 ``{"__truncated__":true,"head":"<原始前缀>"}`` 包裹，
        依旧合法 JSON——彻底杜绝「截断后无法解析」。
        非 JSON 场景保持原行为，尾部追加 ``(truncated,N chars)`` 人类可读标记。"""
        if s is None:
            return ""
        if len(s) <= n:
            return s
        head = s[:n]
        stripped = head.lstrip()
        first = stripped[:1]
        if first in ("{", "[", '"'):
            depth: List[str] = []
            in_str = False
            esc = False
            for ch in head:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch in "{[":
                        depth.append(ch)
                    elif ch in "}]":
                        if depth:
                            depth.pop()
            if in_str:
                head += '"'                       # 闭合当前字符串字面量
            while depth:
                d = depth.pop()
                head += "}" if d == "{" else "]"  # 闭合未完成的容器
            head = head.rstrip()
            # 去掉闭合后残留的尾随 , / :（如 [1,2,3, 或 {"a":1,"b":）
            while head.endswith(",") or head.endswith(":"):
                head = head[:-1].rstrip()
            if first == '"':
                # 裸字符串：闭合后已是合法 JSON 字符串，直接返回
                try:
                    json.loads(head)
                    return head
                except Exception:
                    return json.dumps({"__truncated__": True, "length": len(s),
                                       "head": head}, ensure_ascii=False)
            # 对象/数组：把截断标记作为末位字段注入（插在末位定界符之前）
            # 对象用 key:value；数组不能出现 ":"，改为追加一个完整对象元素
            cand = (head[:-1] + ',"__truncated__":true}') if first == "{" \
                else (head[:-1] + ',{"__truncated__":true}]')
            try:
                json.loads(cand)                  # 合法才用；否则退回包裹（永远合法）
                return cand
            except Exception:
                return json.dumps({"__truncated__": True, "length": len(s),
                                   "head": head}, ensure_ascii=False)
        return head + f"...(truncated,{len(s) - n} chars)"

    def _push(self, ev: Dict[str, Any]) -> None:
        with self._lock:
            nid = ev.get("node_id") or "__unknown__"
            # 全局上限：deque maxlen 会在 append 时自动丢最旧；先给被挤出的节点记账
            if len(self._events) >= self.max_total:
                old = self._events[0]
                onid = old.get("node_id") or "__unknown__"
                self._per_node_count[onid] = max(0, self._per_node_count.get(onid, 1) - 1)
            self._events.append(ev)
            c = self._per_node_count.get(nid, 0) + 1
            if c > self.max_per_node:
                # 单节点超额：从全局 deque 剔除该节点最旧的一条
                for i, e in enumerate(self._events):
                    if (e.get("node_id") or "__unknown__") == nid:
                        del self._events[i]
                        break
                c -= 1
            self._per_node_count[nid] = c

    # ───────────── 只读查询 ─────────────

    def read(self, flow_id: Optional[str] = None, node_id: Optional[str] = None,
             since: Optional[int] = None, limit: Optional[int] = None,
             full: bool = False) -> Dict[str, Any]:
        """从本地缓冲读 debug 事件。**绝不**访问 NR。fail-open。

        过滤：node_id / flow_id 精确匹配；since=received_at>=since（Unix 秒）；
        TTL 自动淘汰超龄事件；结果按 received_at 倒序；limit 截断。
        full=True 时附完整 payload_full（默认只返回 payload_preview 保护传输）。"""
        try:
            with self._lock:
                evs = list(self._events)
            now = time.time()
            out: List[Dict[str, Any]] = []
            for e in evs:
                if node_id is not None and (e.get("node_id") or "") != node_id:
                    continue
                if flow_id is not None and (e.get("flow_id") or "") != flow_id:
                    continue
                if since is not None and (e.get("received_at") or 0) < since:
                    continue
                if self.ttl_seconds and (now - (e.get("received_at") or 0)) > self.ttl_seconds:
                    continue
                item = {k: v for k, v in e.items() if k != "payload_full"}
                if full:
                    item["payload"] = e.get("payload_full")
                out.append(item)
            out.sort(key=lambda x: x.get("received_at") or 0, reverse=True)
            if limit:
                out = out[:limit]

            # D4-#3（C7）：消灭 count:0 三义。
            # count:0 可能意味着：① 缓冲完全为空；② 有数据但全被过滤/过期；
            # ③ 过滤后确实没有匹配事件。status 字段区分三种情况。
            total_buffered = len(evs)
            if total_buffered == 0:
                status = "empty"
            elif len(out) == 0:
                status = "filtered"
            else:
                status = "ok"

            # D4-#1（C8）：回显 expires_at。
            # 最新返回事件的 received_at + ttl_seconds，告知调用方数据窗口截止时间。
            expires_at: Optional[float] = None
            if out and self.ttl_seconds:
                newest_ts = out[0].get("received_at") or 0
                if newest_ts:
                    expires_at = newest_ts + self.ttl_seconds

            resp: Dict[str, Any] = {
                "ok": True,
                "source": "debug_bridge_buffer",
                "enabled": self.enabled,
                "connected": self._connected,
                "flow_id": flow_id,
                "node_id": node_id,
                "status": status,
                "count": len(out),
                "events": out,
            }
            if expires_at is not None:
                resp["expires_at"] = expires_at
            if self.ttl_seconds:
                resp["ttl_seconds"] = self.ttl_seconds
            return resp
        except Exception as ex:
            return {
                "ok": True,
                "source": "error",
                "enabled": self.enabled,
                "connected": self._connected,
                "error": f"debug_bridge read 失败（fail-open）: {ex}",
                "events": [],
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._events)
            node_count = len(self._per_node_count)
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "started_at": self._started_at,
            "total_events": total,
            "node_count": node_count,
            "ttl_seconds": self.ttl_seconds,
            "max_per_node": self.max_per_node,
            "max_total": self.max_total,
            "ws_url": self.ws_url,
            "last_error": self._last_error,
        }
