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

    def test_ensure_self_heals_id_collision(self):
        """碰撞自愈：线上 4 子流程内部 id 跨子流程重名（满壳但已坏）→ 必须强制全部重建。

        #711 衍生 bug：4 子流程曾共用 n_parse/n_hist/n_catch/n_err/n_calc/n_dbg_* 8 个同名
        id，deploy_all 拼进同一命名空间后 NR 全局索引按 id 互相覆盖 → 仅 1 个能跑、其余
        sendEvent.destination.node.receive is not a function。若 ensure 只看「内部节点数>0
        就算 OK」，这种满壳坏部署永不自愈。本测试用共享内部 id 模拟该坏部署，断言 ensure
        检测到重复并重建全部 4 个，且重建产物使用 sid__ 前缀的唯一 id。
        """
        # 模拟坏部署：4 子流程都在场，但内部 id 互相重名（n_parse/n_hist/n_calc）
        broken = []
        for sid in HISTORY_SUBFLOW_IDS:
            broken.append({"type": "subflow", "id": sid})
            broken.append({"z": sid, "type": "function", "id": "n_parse"})
            broken.append({"z": sid, "type": "api-get-history", "id": "n_hist"})
            broken.append({"z": sid, "type": "function", "id": "n_calc"})
        nr = _FakeNR(broken)
        res = ensure_history_subflow(nr, allow_prod=False)
        self.assertTrue(res["created"], f"碰撞部署应触发重建，res={res}")
        self.assertEqual(set(res["rebuilt"]), HISTORY_SUBFLOW_IDS,
                         f"应强制重建全部 4 个，res={res}")
        self.assertIsNotNone(nr.deployed, "碰撞时应 deploy")
        # 重建产物使用 sid__ 前缀的唯一 id（不再出现裸 n_parse 等共享名）
        deployed_ids = {n.get("id") for n in nr.deployed}
        from collections import Counter
        dup = {k: v for k, v in Counter(deployed_ids).items() if v > 1}
        self.assertFalse(dup, f"重建后仍有重复 id：{dup}")
        self.assertNotIn("n_parse", deployed_ids, "重建不应保留共享裸 id")
        for sid in HISTORY_SUBFLOW_IDS:
            self.assertIn(f"{sid}__n_parse", deployed_ids,
                          f"重建缺前缀 id {sid}__n_parse")

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
            # 9 节点：def + 8 个内部节点（解析/取数/降级/计算/3 个 debug 探针）。
            # 内部节点 id 已加 sid__ 前缀（#711 衍生 bug 修复：跨子流程同名 id 在 NR 全局
            # 节点索引互相覆盖，导致仅 1 个子流程能跑、其余 receive is not a function）。
            self.assertEqual(len(arr), 9, f"{sid} 应有 9 节点，实得 {len(arr)}")
            dbg_nodes = {n.get("id") for n in arr if n.get("type") == "debug"}
            self.assertEqual(dbg_nodes,
                             {f"{sid}__n_dbg_parse", f"{sid}__n_dbg_hist", f"{sid}__n_dbg_calc"},
                             f"{sid} G1 debug 探针缺失/未加前缀：{dbg_nodes}")
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

    def test_built_json_internal_ids_global_unique(self):
        """回归守卫：4 子流程内部节点 id 必须全局唯一。

        #711 衍生 bug 根因：4 子流程曾共用 n_parse/n_hist/n_catch/n_err/n_calc/
        n_dbg_parse/n_dbg_hist/n_dbg_calc 这 8 个同名 id，deploy_all 拼进同一命名空间后
        NR 按 id 建全局索引互相覆盖 → 仅 1 个子流程能跑、其余
        sendEvent.destination.node.receive is not a function。此测试确保以后不再复发。
        """
        built = _load_built()
        all_ids: list = []
        for arr in built:
            for n in arr:
                if n.get("type") == "subflow":
                    continue
                all_ids.append(n["id"])
        from collections import Counter
        dup = {k: v for k, v in Counter(all_ids).items() if v > 1}
        self.assertFalse(dup, f"内部节点 id 跨子流程重复（碰撞根因）：{dup}")
        # 同时确认每个 id 都带 sid__ 前缀
        sids = {arr[0]["id"] for arr in built}
        for i in all_ids:
            self.assertTrue(any(i.startswith(s + "__") for s in sids),
                             f"内部节点 id 未带子流程前缀：{i}")

    def test_feed_node_payload_objectify_and_entity_msg(self):
        """#107 回归：_feedNode 必须把非对象 payload（数字/字符串/数组）重置为对象再注入
        entityId/startDate/endDate；否则给原始值赋属性被 JS 静默忽略 → api-get-history
        读不到 entityId → ValidationError: entityId is not allowed to be empty（实测复现）。
        同时 api-get-history 节点必须 entityIdType:msg + entityId:entity（原生从 msg.entity 读），
        不依赖脆弱的运行时属性改写。"""
        built = _load_built()
        for arr in built:
            sid = arr[0]["id"]
            func_nodes = [n for n in arr if n.get("type") == "function"
                          and "msg.payload" in (n.get("func") or "")]
            self.assertTrue(func_nodes, f"{sid} 缺少解析 function 节点")
            parse_func = func_nodes[0]["func"]
            self.assertIn("typeof msg.payload", parse_func,
                          f"{sid} _feedNode 未做 payload 对象化（#107 根因）")
            self.assertIn("Array.isArray(msg.payload)", parse_func,
                          f"{sid} _feedNode 未防数组 payload")
            self.assertIn("msg.payload.entityId = msg.entity", parse_func,
                          f"{sid} _feedNode 未注入 entityId")
            hists = [n for n in arr if n.get("type") == "api-get-history"]
            self.assertEqual(hists[0].get("entityIdType"), "msg",
                             f"{sid} entityIdType 应为 msg（原生从 msg.entity 读）")
            self.assertEqual(hists[0].get("entityId"), "entity",
                             f"{sid} entityId 字段应为 entity（msg.entity 路径）")

class TestEnsureBeforeNodeGate(unittest.TestCase):
    """#711：ensure 必须在节点闸门【之前】跑，且要拿到正确的 allow_prod。

    历史 bug：ensure_history_subflow 只挂在 deploy_raw 且写死 allow_prod=False。
    生产 docker-compose 设 AUTOFLLOW_ENV=prod → 所有 NR 写都要 allow_prod=True →
    4 个 af_hist_* 永远装不上 → _gate_node_types 把所有历史类 flow 判「节点类型未注册」
    全拦（MCP propose_dsl history_duration 实测 gate_passed=false）。
    """

    def setUp(self):
        import autoflow_gateway.subflows as sf_mod
        self.sf_mod = sf_mod
        self._orig = sf_mod.ensure_history_subflow
        self.calls = []
        sf_mod.ensure_history_subflow = lambda nr, allow_prod=False: (
            self.calls.append({"nr": nr, "allow_prod": allow_prod})
            or {"exists": True, "created": False, "rebuilt": []})

    def tearDown(self):
        self.sf_mod.ensure_history_subflow = self._orig

    @staticmethod
    def _stub_self(client="fake-client"):
        import types
        return types.SimpleNamespace(nr=types.SimpleNamespace(client=client))

    def _helper(self):
        from autoflow_gateway.gateway import Gateway
        return Gateway._ensure_history_subflow_for

    def test_skips_when_flow_has_no_history_nodes(self):
        res = self._helper()(self._stub_self(),
                             {"nodes": [{"type": "debug"}]}, True)
        self.assertEqual(res, {"skipped": "not_used"})
        self.assertEqual(self.calls, [])

    def test_calls_ensure_with_allow_prod(self):
        sid = sorted(HISTORY_SUBFLOW_IDS)[0]
        flow = {"nodes": [{"type": f"subflow:{sid}"}]}
        res = self._helper()(self._stub_self(), flow, True)
        self.assertTrue(res.get("exists"))
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0]["allow_prod"], True)   # ★ prod 也要能装
        self.assertEqual(self.calls[0]["nr"], "fake-client")

    def test_never_raises_on_ensure_failure(self):
        """ensure 失败不得抛：后续节点闸门会给出准确的『未注册』错误，语义一致。"""
        def _boom(nr, allow_prod=False):
            raise RuntimeError("NR down")
        self.sf_mod.ensure_history_subflow = _boom
        sid = sorted(HISTORY_SUBFLOW_IDS)[0]
        res = self._helper()(self._stub_self(),
                             {"nodes": [{"type": f"subflow:{sid}"}]}, True)
        self.assertEqual(res.get("skipped"), "error")

    def test_no_nr_client_is_noop(self):
        sid = sorted(HISTORY_SUBFLOW_IDS)[0]
        res = self._helper()(self._stub_self(client=None),
                             {"nodes": [{"type": f"subflow:{sid}"}]}, True)
        self.assertEqual(res, {"skipped": "no_nr_client"})
        self.assertEqual(self.calls, [])

    def test_ensure_precedes_gate_in_all_deploy_paths(self):
        """顺序不变式的源码级守卫。

        _gate_node_types 实时拉 /flows 判断子流程是否存在 —— 只有『先 ensure 后过闸』
        才放行。任何一条部署路径把两者写反或漏写 ensure，历史能力就静默失效，
        且症状是「编译成功但部署被拒」，极难定位。故在此钉死顺序。
        """
        from pathlib import Path
        import re
        src = (Path(__file__).resolve().parents[1]
               / "src" / "autoflow_gateway" / "gateway.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        # 找出每个方法的起止行，只在同一方法内比较顺序
        starts = [(i, m.group(1)) for i, l in enumerate(lines)
                  if (m := re.match(r"    def (\w+)\(", l))]
        spans = {}
        for idx, (i, name) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
            spans[name] = (i, end)
        for meth in ("deploy_proposal", "modify_flow", "run_staging_gate",
                     "deploy_raw", "propose_raw"):
            self.assertIn(meth, spans, f"方法 {meth} 不存在（重命名后请同步本测试）")
            lo, hi = spans[meth]
            body = lines[lo:hi]
            ens = [i for i, l in enumerate(body) if "_ensure_history_subflow_for(" in l]
            gate = [i for i, l in enumerate(body) if "self._gate_node_types(" in l]
            self.assertTrue(ens, f"{meth} 缺少 _ensure_history_subflow_for 调用（#711）")
            if gate:
                self.assertLess(min(ens), min(gate),
                                f"{meth}：ensure 必须在 _gate_node_types 之前")


if __name__ == "__main__":
    unittest.main(verbosity=2)
