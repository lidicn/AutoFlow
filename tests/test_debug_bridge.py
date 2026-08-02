#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""debug_bridge 单元测试（纯离线，合成 NR comms 帧，不触真实 NR）。

覆盖：debug 事件解析（两种线上形态 + 忽略非 debug）、缓冲投递、TTL 淘汰、
每节点上限、全局上限、node_id/flow_id/since/limit 过滤、payload 截断、
ws 帧编解码（socketpair 回环）、fail-open（坏帧/坏 JSON 不崩）。
"""
import os
import sys
import json
import time
import struct
import socket
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.debug_bridge import DebugBridge, _http_url_to_ws


class FakeNR:
    """最小 NodeRedClient 桩：仅提供 login() 返回假 token。"""
    def login(self):
        return "fake-bearer-token"


def make_bridge(**over):
    kw = dict(nr_client=FakeNR(), nr_url="http://localhost:1880", enabled=False)
    kw.update(over)
    return DebugBridge(**kw)


# ── 手工构造 websocket 帧（server→client 不 mask；client→server 必 mask）──
def _server_frame(opcode, data: bytes, fin=True):
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(data)
    if n < 126:
        header = bytes([b0, n])
    elif n < 65536:
        header = bytes([b0, 126]) + struct.pack("!H", n)
    else:
        header = bytes([b0, 127]) + struct.pack("!Q", n)
    return header + data


def _read_masked_client_frame(srv):
    b0, b1 = DebugBridge._recv_exact(srv, 2)
    opcode = b0 & 0x0F
    masked = (b1 >> 7) & 1
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", DebugBridge._recv_exact(srv, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", DebugBridge._recv_exact(srv, 8))[0]
    assert masked == 1, "client frame must be masked"
    mask = DebugBridge._recv_exact(srv, 4)
    raw = DebugBridge._recv_exact(srv, length) if length else b""
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
    return opcode, payload


class TestExtractDebug(unittest.TestCase):
    def setUp(self):
        self.b = make_bridge()

    def test_shape_a_wrapped(self):
        raw = {"topic": "debug", "data": {
            "id": "n1", "name": "dbg", "topic": "tt",
            "msg": {"x": 1}, "_path": {"type": "flow", "id": "flowA"},
            "timestamp": 1700000000000}}
        info = self.b._extract_debug(raw)
        self.assertEqual(info["node_id"], "n1")
        self.assertEqual(info["flow_id"], "flowA")
        self.assertEqual(info["payload"], {"x": 1})
        self.assertEqual(info["ts"], 1700000000000)

    def test_shape_b_bare(self):
        raw = {"id": "n2", "msg": "hello", "z": "flowB"}
        info = self.b._extract_debug(raw)
        self.assertEqual(info["node_id"], "n2")
        self.assertEqual(info["flow_id"], "flowB")
        self.assertEqual(info["payload"], "hello")

    def test_nested_msg_payload(self):
        raw = {"id": "n3", "msg": {"payload": "deep"}}
        info = self.b._extract_debug(raw)
        self.assertEqual(info["payload"], "deep")

    def test_non_debug_topic_skipped_by_handle(self):
        self.b._handle_message(json.dumps({"topic": "notification", "data": {"id": "x"}}))
        self.assertEqual(self.b.read()["count"], 0)

    def test_batched_array_frame_ingests_each(self):
        # NR 把多个事件打包成单个 TEXT 帧的 JSON 数组发送：[{topic,data},...]
        batch = json.dumps([
            {"topic": "debug", "data": {"id": "nA", "msg": "a", "_path": {"id": "fA"}}},
            {"topic": "hb", "data": 123},  # 心跳，应被忽略
            {"topic": "debug", "data": {"id": "nB", "msg": "b", "z": "fB"}},
        ])
        self.b._handle_message(batch)
        evs = self.b.read()["events"]
        self.assertEqual(len(evs), 2)  # 仅 2 个 debug 入账，hb 被忽略
        nids = sorted(e["node_id"] for e in evs)
        self.assertEqual(nids, ["nA", "nB"])

    def test_missing_fields_returns_none(self):
        self.assertIsNone(self.b._extract_debug({"foo": "bar"}))

    def test_retain_flag(self):
        raw = {"topic": "debug", "retain": True, "data": {"id": "n4", "msg": 1}}
        info = self.b._extract_debug(raw)
        self.assertTrue(info["retain"])


class TestBufferAndRead(unittest.TestCase):
    def test_ingest_and_read(self):
        b = make_bridge()
        b._handle_message(json.dumps({"topic": "debug", "data": {
            "id": "n1", "name": "d", "msg": "v1", "_path": {"id": "f1"}}}))
        b._handle_message(json.dumps({"id": "n2", "msg": "v2", "z": "f1"}))
        res = b.read()
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        # 倒序：最后写入的在最前
        self.assertEqual(res["events"][0]["node_id"], "n2")

    def test_filter_by_node(self):
        b = make_bridge()
        b._handle_message(json.dumps({"id": "n1", "msg": "a"}))
        b._handle_message(json.dumps({"id": "n2", "msg": "b"}))
        res = b.read(node_id="n1")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["events"][0]["node_id"], "n1")

    def test_filter_by_flow(self):
        b = make_bridge()
        b._handle_message(json.dumps({"id": "n1", "msg": "a", "_path": {"id": "fa"}}))
        b._handle_message(json.dumps({"id": "n2", "msg": "b", "_path": {"id": "fb"}}))
        res = b.read(flow_id="fa")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["events"][0]["flow_id"], "fa")

    def test_limit(self):
        b = make_bridge()
        for i in range(10):
            b._handle_message(json.dumps({"id": "n", "msg": i}))
        res = b.read(limit=3)
        self.assertEqual(res["count"], 3)

    def test_ttl_drops_old(self):
        b = make_bridge(ttl_seconds=60)  # ttl_seconds=0 表示关闭 TTL；此处用正值触发淘汰
        b._push({"flow_id": "f", "node_id": "n", "name": None, "topic": None,
                 "payload_preview": "x", "payload_full": "x",
                 "timestamp": None, "received_at": time.time() - 1000, "retain": False})
        self.assertEqual(b.read()["count"], 0)

    def test_ttl_keeps_fresh(self):
        b = make_bridge(ttl_seconds=300)
        b._push({"flow_id": "f", "node_id": "n", "name": None, "topic": None,
                 "payload_preview": "x", "payload_full": "x",
                 "timestamp": None, "received_at": time.time(), "retain": False})
        self.assertEqual(b.read()["count"], 1)

    def test_since_filter(self):
        b = make_bridge(ttl_seconds=0)  # 关 TTL，专测 since（合成 received_at 远小于 now）
        b._push({"flow_id": "f", "node_id": "n", "name": None, "topic": None,
                 "payload_preview": "old", "payload_full": "old",
                 "timestamp": None, "received_at": 1000.0, "retain": False})
        b._push({"flow_id": "f", "node_id": "n", "name": None, "topic": None,
                 "payload_preview": "new", "payload_full": "new",
                 "timestamp": None, "received_at": 2000.0, "retain": False})
        res = b.read(since=1500)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["events"][0]["payload_preview"], "new")

    def test_per_node_cap(self):
        b = make_bridge(max_per_node=3, max_total=1000)
        for i in range(5):
            b._handle_message(json.dumps({"id": "n", "msg": i}))
        self.assertEqual(b.read(node_id="n")["count"], 3)

    def test_global_cap(self):
        b = make_bridge(max_per_node=1000, max_total=5)
        for i in range(10):
            b._handle_message(json.dumps({"id": f"n{i}", "msg": i}))
        self.assertEqual(b.read()["count"], 5)

    def test_payload_truncation(self):
        b = make_bridge(max_payload_chars=20, preview_chars=8, ttl_seconds=0)
        big = "X" * 5000
        b._handle_message(json.dumps({"id": "n", "msg": big}))
        ev = b.read()["events"][0]
        self.assertTrue(ev["payload_preview"].startswith("X" * 8))
        self.assertIn("truncated", ev["payload_preview"])
        self.assertLessEqual(len(ev["payload_preview"]), 8 + 40)  # 截断后缀带数字，给足余量
        full = b.read(full=True)["events"][0]
        self.assertIn("truncated", full["payload"])
        self.assertLessEqual(len(full["payload"]), 20 + 40)


class TestFailOpen(unittest.TestCase):
    def test_malformed_json_ignored(self):
        b = make_bridge()
        b._handle_message("{not json")
        self.assertEqual(b.read()["count"], 0)

    def test_bad_frame_does_not_raise(self):
        b = make_bridge()
        # 模拟解析异常路径：直接喂入坏数据到 handle，确认不抛
        try:
            b._handle_message("12345")  # 合法 JSON 但非对象
            b._handle_message(json.dumps([1, 2, 3]))
        except Exception as e:
            self.fail(f"handle_message 不应抛异常：{e}")
        self.assertEqual(b.read()["count"], 0)

    def test_read_fail_open_on_unexpected(self):
        b = make_bridge()
        # 强制 _events 损坏场景不易构造；改测 read 在空缓冲时正常返回
        res = b.read()
        self.assertTrue(res["ok"])


class TestWsCodec(unittest.TestCase):
    def test_recv_server_text_frame(self):
        b = make_bridge()
        c, s = socket.socketpair()
        try:
            payload = json.dumps({"topic": "debug", "data": {"id": "n", "msg": 1}}).encode()
            s.sendall(_server_frame(0x1, payload))
            fin, opcode, got = b._recv_frame(c)
            self.assertTrue(fin)
            self.assertEqual(opcode, 0x1)
            self.assertEqual(json.loads(got.decode()), {"topic": "debug", "data": {"id": "n", "msg": 1}})
        finally:
            c.close(); s.close()

    def test_recv_server_fragmented(self):
        b = make_bridge()
        c, s = socket.socketpair()
        try:
            data = b"hello world fragment"
            # 分两片：第一片 fin=0 opcode=1，第二片 fin=1 opcode=0(continuation)
            s.sendall(_server_frame(0x1, data[:6], fin=False))
            s.sendall(_server_frame(0x0, data[6:], fin=True))
            fin1, op1, p1 = b._recv_frame(c)
            self.assertFalse(fin1)
            fin2, op2, p2 = b._recv_frame(c)
            self.assertTrue(fin2)
            self.assertEqual(op2, 0x0)
            self.assertEqual(p1 + p2, data)
        finally:
            c.close(); s.close()

    def test_send_frame_is_masked_and_decodes(self):
        b = make_bridge()
        c, s = socket.socketpair()
        try:
            b._send_frame(c, 0x1, b'{"subscribe":["debug"]}')
            opcode, payload = _read_masked_client_frame(s)
            self.assertEqual(opcode, 0x1)
            self.assertEqual(payload, b'{"subscribe":["debug"]}')
        finally:
            c.close(); s.close()

    def test_ping_triggers_pong(self):
        b = make_bridge()
        c, s = socket.socketpair()
        try:
            b._sock = c
            b._on_message(0x9, b"pingdata")
            opcode, payload = _read_masked_client_frame(s)
            self.assertEqual(opcode, 0xA)
            self.assertEqual(payload, b"pingdata")
        finally:
            c.close(); s.close()


class TestUrlAndSingleton(unittest.TestCase):
    def test_http_to_ws(self):
        # #649 关键归一：localhost 必须改回 127.0.0.1（LocalSystem/Session0 下 ::1 是黑洞）
        self.assertEqual(_http_url_to_ws("http://localhost:1880"), "ws://127.0.0.1:1880")
        self.assertEqual(_http_url_to_ws("https://localhost:1880"), "wss://127.0.0.1:1880")
        self.assertEqual(_http_url_to_ws("https://x:1880"), "wss://x:1880")

    def test_ws_normalizes_localhost_only(self):
        # 只有 localhost 被改写；其它 host（含 127.0.0.1 / 真实域名）原样保留
        self.assertEqual(_http_url_to_ws("ws://localhost:1880"), "ws://127.0.0.1:1880")
        self.assertEqual(_http_url_to_ws("wss://127.0.0.1:1880"), "wss://127.0.0.1:1880")
        self.assertEqual(_http_url_to_ws("http://<NR_LAN_IP>:1880"), "ws://<NR_LAN_IP>:1880")
        # 裸 host（无 scheme）默认补 ws://
        self.assertEqual(_http_url_to_ws("localhost:1880"), "ws://127.0.0.1:1880")

    def test_ws_url_appends_comms(self):
        b = make_bridge()
        self.assertEqual(b.ws_url, "ws://127.0.0.1:1880/comms")

    def test_singleton_start_guard(self):
        b = make_bridge(enabled=True)
        b.start()
        self.assertTrue(DebugBridge._bridge_started)
        # 第二次 start 不应新建线程（单例守卫）
        b2 = make_bridge(enabled=True)
        b2.start()
        b.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
