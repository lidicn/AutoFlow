"""历史查询子流程幂等 ensure + 引用检测测试（A3 模式，仿 test_bark_subflow）。

运行：python -m unittest tests.test_history_subflow -v
不依赖 live NR —— 用内存 _FakeNR 桩验证 ensure_history_subflow 的两条路径
（活体在场→no-op；缺失→从 subflows_built.json 重建并替换 server 后 deploy_all）。
注意：本文件早期版本把用例写成裸函数，unittest 0 收集、套件假绿；
现统一为 unittest.TestCase，确保被离线套件真实执行。
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.subflows import (
    HISTORY_SUBFLOW_IDS,
    ensure_history_subflow,
    flow_uses_history_subflow,
)

# 直接复用模块内的真值路径，避免测试硬编码副本与实现漂移
# （R2 数据/代码分离时该文件从 nr_subflows/history/ 迁到 data/subflows/nr_defs/，
#  硬编码副本会静默失效——测试读不到就不是在测发布包实际加载的那份）。
from autoflow_gateway.subflows import _HISTORY_BUILT_PATH as _BUILT_PATH  # noqa: E402


def _load_built():
    with open(os.path.abspath(_BUILT_PATH), encoding="utf-8") as f:
        return json.load(f)


class _FakeNR:
    """内存替身：记录 list_flows / get_default_server_id / deploy_all 调用，不触真实 NR。"""

    def __init__(self, flows, default_server="fake-server-id"):
        self._flows = list(flows)
        self._default_server = default_server
        self.deployed = None  # 最后 deploy_all 的 combined 列表

    def list_flows(self):
        return list(self._flows)

    def get_default_server_id(self):
        return self._default_server

    def deploy_all(self, combined, force=False, allow_prod=False, allow_partial=False):
        self.deployed = combined
        return {"deployed": len(combined)}


class TestHistorySubflow(unittest.TestCase):
    def test_ensure_noop_when_all_present(self):
        """4 个历史子流程全部在场 → 必须 no-op，绝不调用 deploy_all（避免改写活体）。"""
        present = []
        for sid in HISTORY_SUBFLOW_IDS:
            present.append({"type": "subflow", "id": sid})
            present.append({"z": sid, "type": "api-get-history"})  # 内部节点：证明非空壳(#607)
        nr = _FakeNR(present)
        res = ensure_history_subflow(nr, allow_prod=False)
        self.assertFalse(res["created"], res)
        self.assertTrue(res["exists"], res)
        self.assertEqual(res["missing"], [], res)
        self.assertEqual(res["rebuilt"], [], res)
        self.assertIsNone(nr.deployed, "已存在时不应 deploy（会触真实 NR / 改写活体）")

    def test_ensure_rebuild_when_missing(self):
        """全部缺失 → 从 subflows_built.json 重建 4 个，server 替换为默认，deploy_all 被调用。"""
        nr = _FakeNR([])
        res = ensure_history_subflow(nr, allow_prod=False)
        self.assertTrue(res["created"], res)
        self.assertFalse(res["exists"], res)
        self.assertEqual(set(res["rebuilt"]), HISTORY_SUBFLOW_IDS, res)
        self.assertIsNotNone(nr.deployed, "缺失时应 deploy")
        # 每个 built 子流程的 def 节点都应出现在 deployed 中
        built = _load_built()
        deployed_ids = {n.get("id") for n in nr.deployed}
        for arr in built:
            self.assertIn(arr[0]["id"], deployed_ids, f"重建缺子流程 {arr[0].get('id')}")
        # 重建的 api-get-history 节点 server 须被替换为默认（非硬编码 e93e1ad9c034e866）
        hist_nodes = [n for n in nr.deployed if n.get("type") == "api-get-history"]
        self.assertTrue(hist_nodes, "应含 api-get-history 节点")
        for h in hist_nodes:
            self.assertEqual(h["server"], "fake-server-id",
                             f"server 应被替换为默认，实得 {h.get('server')}")

    def test_ensure_partial_missing_rebuilds_only_missing(self):
        """仅缺 1 个 → 只重建那 1 个，其余 3 个不受影响，deploy 只含缺失的节点。"""
        missing_id = next(iter(HISTORY_SUBFLOW_IDS))
        present = []
        for sid in HISTORY_SUBFLOW_IDS:
            if sid == missing_id:
                continue
            present.append({"type": "subflow", "id": sid})
            present.append({"z": sid, "type": "api-get-history"})  # 内部节点：证明非空壳(#607)
        nr = _FakeNR(present)
        res = ensure_history_subflow(nr, allow_prod=False)
        self.assertTrue(res["created"], res)
        self.assertEqual(res["rebuilt"], [missing_id], res)
        self.assertEqual(set(res["missing"]), {missing_id}, res)
        # combined = live(在场的3个) + all_entries(缺失的1个)。所以 deployed 含在场子流程是
        # append 安全部署的正常结果；这里只校验「本次新增的条目」恰好等于缺失子流程的节点。
        built = _load_built()
        by_id = {arr[0]["id"]: arr for arr in built if arr and arr[0].get("type") == "subflow"}
        expected_nodes = by_id[missing_id]
        live_ids = {n["id"] for n in present if "id" in n}
        new_in_deploy = [n for n in nr.deployed if n.get("id") not in live_ids]
        new_ids = {n.get("id") for n in new_in_deploy}
        for n in expected_nodes:
            self.assertIn(n["id"], new_ids, f"缺失子流程节点未部署 {n['id']}")
        # 其余子流程的 def 节点不应作为「新增」出现
        for arr in built:
            if not arr or arr[0].get("type") != "subflow":
                continue
            if arr[0]["id"] != missing_id:
                self.assertNotIn(arr[0]["id"], new_ids,
                                 f"不应部署未缺失的子流程 {arr[0]['id']}")

    def test_flow_uses_history_detection(self):
        """deploy_raw 引用检测：兼容 NR5 前缀型与裸型两种写法。"""
        for sid in HISTORY_SUBFLOW_IDS:
            self.assertTrue(flow_uses_history_subflow([{"type": f"subflow:{sid}"}]))
            self.assertTrue(flow_uses_history_subflow([{"type": "subflow", "c": sid}]))
        self.assertFalse(flow_uses_history_subflow([{"type": "subflow:otherid"}]))
        self.assertFalse(flow_uses_history_subflow([{"type": "debug"}]))
        self.assertFalse(flow_uses_history_subflow([]))
        self.assertFalse(flow_uses_history_subflow(None))

    def test_built_json_structure(self):
        """subflows_built.json 结构自洽：每子流程 6 节点、def 端口指向内部、内部节点 z 正确、server 硬编码可替换。"""
        built = _load_built()
        self.assertEqual(len(built), len(HISTORY_SUBFLOW_IDS),
                         f"应有 {len(HISTORY_SUBFLOW_IDS)} 个子流程，实得 {len(built)}")
        for arr in built:
            self.assertTrue(arr and arr[0].get("type") == "subflow", "首节点须为 subflow def")
            sid = arr[0]["id"]
            self.assertIn(sid, HISTORY_SUBFLOW_IDS, f"未知子流程 id {sid}")
            # 9 节点：def + n_parse + n_hist(api-get-history) + n_catch + n_err + n_calc
            #        + n_dbg_parse / n_dbg_hist / n_dbg_calc（G1 #644：build 期预置 debug 探针）
            self.assertEqual(len(arr), 9, f"{sid} 应有 9 节点，实得 {len(arr)}")
            dbg_nodes = {n.get("id") for n in arr if n.get("type") == "debug"}
            self.assertEqual(dbg_nodes, {"n_dbg_parse", "n_dbg_hist", "n_dbg_calc"},
                             f"{sid} G1 debug 探针缺失：{dbg_nodes}")
            # def 的 in/out 是端口对象列表，各 1 个端口
            self.assertEqual(len(arr[0].get("in", [])), 1, f"{sid} def.in 应有 1 端口")
            self.assertEqual(len(arr[0].get("out", [])), 1, f"{sid} def.out 应有 1 端口")
            # 内部节点 z 指向 subflow_id
            for n in arr[1:]:
                self.assertEqual(n.get("z"), sid,
                                 f"{sid} 内部节点 {n.get('id')} 的 z 应为 {sid}，实得 {n.get('z')}")
            # 至少有一个 api-get-history，且 server 是硬编码占位（可被 ensure 替换为默认）
            hists = [n for n in arr if n.get("type") == "api-get-history"]
            self.assertTrue(hists, f"{sid} 应含 api-get-history")
            self.assertEqual(hists[0].get("server"), "e93e1ad9c034e866",
                             f"{sid} 硬编码 server 占位不符预期：{hists[0].get('server')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
