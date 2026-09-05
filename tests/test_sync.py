#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync.py 离线单元测试 —— NR(dev) -> 1880(prod) promotion 护栏。

覆盖：
  1) 标签纯函数（parse_stage/parse_version/make_label_block/set_label_in_info），不触网。
  2) 参数化守卫（resolve_id 解析 / dst_has_subflows 子流程依赖预检 = promotion 冲突检测）。
  3) 核心 promotion（scan_stages / push_one / push_release / set_stage），
     通过 monkeypatch sync.NodeRedClient 为 FakeNRClient，模拟 src/dst 双实例，
     验证「release 才推 / 版本去重 / dry_run 不落盘 / 强制 enabled / 存在则更新否则创建」。

全程离线：STATE_FILE 指向临时文件，绝不写 ~/.workbuddy；绝不连真实 NR。
（注：sync.py 不含占位符/remap 逻辑——那是 gateway.deploy_raw 的职责，
 本测试只覆盖 sync.py 实际拥有的 promotion 逻辑。）
"""
import os
import sys
import json
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import autoflow_gateway.sync as sync


# ── Fake NR 客户端（按 url 区分 src/dst，记录调用，绝不触网）──────────
class FakeNRClient:
    # url -> {"flows": {id: flow}, "list": [entries]}
    REGISTRY = {}
    calls = []  # [("update"|"create", url, fid), ...]

    def __init__(self, url=None):
        self.url = url

    def list_flows(self):
        return list(self.REGISTRY.get(self.url, {}).get("list", []))

    def get_flow(self, fid):
        flows = self.REGISTRY.get(self.url, {}).get("flows", {})
        if fid in flows:
            return flows[fid]
        # 未知 key：返回「无 id」的占位，使 resolve_id 走 list 扫描分支
        return {"type": "tab", "label": "", "nodes": [], "info": ""}

    def update_flow(self, fid, flow, force=False):
        FakeNRClient.calls.append(("update", self.url, fid))
        self.REGISTRY.setdefault(self.url, {"flows": {}, "list": []})["flows"][fid] = flow
        return {"ok": True}

    def create_flow(self, flow, force=False):
        fid = flow.get("id")
        FakeNRClient.calls.append(("create", self.url, fid))
        self.REGISTRY.setdefault(self.url, {"flows": {}, "list": []})["flows"][fid] = flow
        return {"ok": True}


def _seed(url, flows):
    """把一个 {id: flow} 字典种入 REGISTRY，并构造对应 list 视图（含 tab/subflow 条目）。"""
    entries = []
    for fid, fl in flows.items():
        t = fl.get("type", "tab")
        entries.append({"id": fid, "type": t, "label": fl.get("label", "")})
    FakeNRClient.REGISTRY[url] = {"flows": dict(flows), "list": entries}


SRC_URL = "http://dev:1880"
DST_URL = "http://prod:1880"


# ── 1) 标签纯函数 ───────────────────────────────────────────
class TestLabelParsing(unittest.TestCase):
    def test_parse_stage_release(self):
        self.assertEqual(sync.parse_stage("autoflow-stage: release"), "release")

    def test_parse_stage_case_insensitive(self):
        self.assertEqual(sync.parse_stage("AUTOFLOW-STAGE: Dev"), "dev")

    def test_parse_stage_default_dev(self):
        # 缺省 = dev（安全：绝不自动推 prod）
        self.assertEqual(sync.parse_stage(""), "dev")
        self.assertEqual(sync.parse_stage("nothing here"), "dev")

    def test_parse_version(self):
        self.assertEqual(sync.parse_version("autoflow-version: 1.2.3"), "1.2.3")
        # 版本号必须以数字开头（正则 [0-9][\w.]*）；非数字开头（如 v2）回退默认
        self.assertEqual(sync.parse_version("autoflow-version: 2.0"), "2.0")
        self.assertEqual(sync.parse_version("autoflow-version: v2"), "0.0.0")

    def test_parse_version_default(self):
        self.assertEqual(sync.parse_version("no version"), "0.0.0")

    def test_make_label_block(self):
        self.assertEqual(
            sync.make_label_block("release", "1.0.0"),
            "autoflow-stage: release\nautoflow-version: 1.0.0",
        )

    def test_set_label_in_info_preserves_other(self):
        info = "我的书房场景\nautoflow-stage: dev\nautoflow-version: 0.1.0\n"
        out = sync.set_label_in_info(info, "release", "1.0.0")
        self.assertIn("我的书房场景", out)
        self.assertNotIn("autoflow-stage: dev", out)
        self.assertIn("autoflow-stage: release", out)
        self.assertIn("autoflow-version: 1.0.0", out)

    def test_set_label_in_info_empty(self):
        out = sync.set_label_in_info("", "dev", "0.0.1")
        self.assertEqual(out, "autoflow-stage: dev\nautoflow-version: 0.0.1")

    def test_set_label_idempotent_roundtrip(self):
        # 连续两次写入同一标签，不重复堆叠 autoflow-* 行
        out1 = sync.set_label_in_info("x", "release", "2.0.0")
        out2 = sync.set_label_in_info(out1, "release", "2.0.0")
        self.assertEqual(out1, out2)
        self.assertEqual(out1.count("autoflow-stage:"), 1)


# ── 2) 参数化守卫（不依赖全局 NodeRedClient）─────────────────
class TestResolveId(unittest.TestCase):
    def setUp(self):
        FakeNRClient.REGISTRY.clear()
        FakeNRClient.calls.clear()
        _seed(SRC_URL, {
            "abc123": {"id": "abc123", "type": "tab", "label": "书房夜灯"},
            "def456": {"id": "def456", "type": "tab", "label": "客厅窗帘"},
        })

    def test_resolve_by_exact_id(self):
        nr = FakeNRClient(url=SRC_URL)
        self.assertEqual(sync.resolve_id(nr, "abc123"), "abc123")

    def test_resolve_by_label_substring(self):
        nr = FakeNRClient(url=SRC_URL)
        self.assertEqual(sync.resolve_id(nr, "夜灯"), "abc123")

    def test_resolve_multiple_match_raises(self):
        nr = FakeNRClient(url=SRC_URL)
        # 注入第二个含「书房」的 tab，使按标签子串解析命中 2 个 → 应抛 SystemExit
        FakeNRClient.REGISTRY[SRC_URL]["flows"]["study2"] = {
            "id": "study2", "type": "tab", "label": "书房台灯"}
        FakeNRClient.REGISTRY[SRC_URL]["list"].append(
            {"id": "study2", "type": "tab", "label": "书房台灯"})
        with self.assertRaises(SystemExit):
            sync.resolve_id(nr, "书房")

    def test_resolve_no_match_raises(self):
        nr = FakeNRClient(url=SRC_URL)
        with self.assertRaises(SystemExit):
            sync.resolve_id(nr, "不存在的关键词zzz")


class TestDstHasSubflows(unittest.TestCase):
    def _dst(self, subs):
        class FakeDst:
            def __init__(self, s):
                self._s = s
            def list_flows(self):
                return [{"type": "subflow", "id": x} for x in self._s]
        return FakeDst(subs)

    def test_all_present(self):
        flow = {"nodes": [
            {"type": "subflow:abc", "z": "f"},
            {"type": "inject", "z": "f", "flow": "abc"},
        ]}
        ok, missing = sync.dst_has_subflows(self._dst({"abc"}), flow)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_missing_subflow_type(self):
        flow = {"nodes": [{"type": "subflow:xyz", "z": "f"}]}
        ok, missing = sync.dst_has_subflows(self._dst({"abc"}), flow)
        self.assertFalse(ok)
        self.assertEqual(missing, ["xyz"])

    def test_missing_node_flow_ref(self):
        flow = {"nodes": [{"type": "api-call-service", "z": "f", "flow": "sub1"}]}
        ok, missing = sync.dst_has_subflows(self._dst(set()), flow)
        self.assertFalse(ok)
        self.assertEqual(missing, ["sub1"])


# ── 3) 核心 promotion（monkeypatch NodeRedClient）────────────
class TestPromotion(unittest.TestCase):
    def setUp(self):
        FakeNRClient.REGISTRY.clear()
        FakeNRClient.calls.clear()
        # 临时 STATE_FILE，避免触碰 ~/.workbuddy
        self._tmp = tempfile.NamedTemporaryFile(
            prefix="af_sync_state_", suffix=".json", delete=False
        )
        self._tmp.close()
        self._orig_state_file = sync.STATE_FILE
        self._orig_nrc = sync.NodeRedClient
        sync.STATE_FILE = self._tmp.name
        sync.NodeRedClient = FakeNRClient

        # src：两个 tab（一个 release，一个 dev）
        self._src_flows = {
            "rel1": {"id": "rel1", "type": "tab", "label": "发布流",
                     "info": "autoflow-stage: release\nautoflow-version: 1.0.0",
                     "nodes": [{"type": "subflow:abc", "z": "rel1", "wires": []},
                               {"type": "inject", "z": "rel1", "wires": []}]},
            "dev1": {"id": "dev1", "type": "tab", "label": "开发流",
                     "info": "autoflow-stage: dev\nautoflow-version: 0.3.0",
                     "nodes": [{"type": "inject", "z": "dev1", "wires": []}]},
        }
        _seed(SRC_URL, self._src_flows)
        # dst：含 src 引用的子流程 abc
        _seed(DST_URL, {
            "abc": {"id": "abc", "type": "subflow", "label": "Bark"},
        })

    def tearDown(self):
        sync.STATE_FILE = self._orig_state_file
        sync.NodeRedClient = self._orig_nrc
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_scan_stages_marks_release(self):
        r = sync.scan_stages(src_url=SRC_URL)
        by_id = {f["id"]: f for f in r["flows"]}
        self.assertTrue(by_id["rel1"]["would_push"])
        self.assertFalse(by_id["dev1"]["would_push"])
        self.assertEqual(by_id["rel1"]["stage"], "release")
        self.assertEqual(by_id["rel1"]["version"], "1.0.0")
        # 排序：先按 stage 再按 label
        stages = [f["stage"] for f in r["flows"]]
        self.assertEqual(stages, sorted(stages))

    def test_push_one_dry_run_no_write(self):
        r = sync.push_one("rel1", dry_run=True, src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["action"], "would push (enabled)")
        self.assertEqual(FakeNRClient.calls, [])  # dry-run 绝不落盘

    def test_push_one_forces_enabled_and_creates_when_absent(self):
        # dst 没有 rel1 → 走 create_flow
        self._src_flows["rel1"]["disabled"] = True
        r = sync.push_one("rel1", src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "pushed (enabled)")
        # 发送前强制 enabled
        sent = FakeNRClient.REGISTRY[DST_URL]["flows"].get("rel1")
        self.assertIsNotNone(sent)
        self.assertFalse(sent.get("disabled", True))
        self.assertIn(("create", DST_URL, "rel1"), FakeNRClient.calls)

    def test_push_one_updates_when_present(self):
        # 先让 dst 已存在 rel1（同时保留 abc 子流程，否则子流程预检会拦截）
        _seed(DST_URL, {
            "rel1": {"id": "rel1", "type": "tab", "label": "旧版"},
            "abc": {"id": "abc", "type": "subflow", "label": "Bark"},
        })
        r = sync.push_one("rel1", src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "pushed (enabled)")
        self.assertIn(("update", DST_URL, "rel1"), FakeNRClient.calls)

    def test_push_one_blocks_missing_subflow(self):
        # dst 删掉子流程 abc → 应被拦截
        FakeNRClient.REGISTRY[DST_URL] = {"flows": {}, "list": []}
        r = sync.push_one("rel1", src_url=SRC_URL, dst_url=DST_URL)
        self.assertFalse(r["ok"])
        self.assertIn("子流程", r["error"])

    def test_push_release_only_release_and_version_dedup(self):
        # 预置 state：rel1 已推 1.0.0 → 同版本不重复推
        with open(sync.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"rel1": {"version": "1.0.0"}}, f)
        r = sync.push_release(src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        # 没有需要推送的（dev1 非 release；rel1 已是最新）
        self.assertEqual(r["pushed"], [])
        self.assertIn("没有需要推送", r["message"])

    def test_push_release_pushes_newer_version(self):
        # 预置 state：rel1 已推 0.9.0 → 1.0.0 更新，应推送
        with open(sync.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"rel1": {"version": "0.9.0"}}, f)
        r = sync.push_release(src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        pushed_ids = [p["id"] for p in r["pushed"] if p.get("ok")]
        self.assertEqual(pushed_ids, ["rel1"])
        # state 已更新到 1.0.0
        state = sync.load_state()
        self.assertEqual(state["rel1"]["version"], "1.0.0")

    def test_push_release_dry_run_preview(self):
        r = sync.push_release(dry_run=True, src_url=SRC_URL, dst_url=DST_URL)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["pushed"]), 1)
        self.assertTrue(r["pushed"][0]["dry_run"])
        self.assertEqual(FakeNRClient.calls, [])  # dry-run 不落盘
        # dry-run 不应写 state
        self.assertEqual(sync.load_state(), {})

    def test_set_stage_invalid_rejected(self):
        r = sync.set_stage("rel1", "bogus", src_url=SRC_URL)
        self.assertFalse(r["ok"])
        self.assertIn("stage", r["error"])

    def test_set_stage_valid_writes_label(self):
        r = sync.set_stage("rel1", "dev", "1.1.0", src_url=SRC_URL)
        self.assertTrue(r["ok"])
        # update_flow 被调用，且 info 已写入新标签
        self.assertIn(("update", SRC_URL, "rel1"), FakeNRClient.calls)
        sent = FakeNRClient.REGISTRY[SRC_URL]["flows"]["rel1"]
        self.assertIn("autoflow-stage: dev", sent["info"])
        self.assertIn("autoflow-version: 1.1.0", sent["info"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
