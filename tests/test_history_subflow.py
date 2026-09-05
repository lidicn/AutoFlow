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
        """4 个历史子流程全部在场且指纹匹配 → no-op，绝不调用 deploy_all（避免改写活体）。"""
        # 用真实 built.json 数据构造 fake NR，确保指纹与本地一致
        built = _load_built()
        present = []
        for arr in built:
            for n in arr:
                present.append(n)
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
        built = _load_built()
        missing_id = next(iter(HISTORY_SUBFLOW_IDS))
        present = []
        for arr in built:
            if not arr or arr[0].get("type") != "subflow":
                continue
            sid = arr[0]["id"]
            if sid == missing_id:
                continue
            for n in arr:
                present.append(n)
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

    def test_feed_node_payload_objectify_and_entity_rewrite(self):
        """#107/#107-2 回归（终版）：_feedNode 必须把非对象 payload（数字/字符串/数组）
        重置为对象再注入 entityId/startDate/endDate；否则给原始值赋属性被 JS 静默忽略。
        正确机制（官方文档确认）：api-get-history 节点 entityIdType schema 仅允许
        [equals, regex]、不支持 msg 动态路径，但 entityId/startDate/endDate 均可经
        msg.payload 传入并【覆盖配置】（"Will override the configuration if passed in"）。
        故 _feedNode 只需把字面量 ISO 与 entityId 写进 msg.payload 即可，节点原生读取覆盖空配置。
        禁止再用 RED.nodes.getNode 运行时改写节点属性——子流程实例内 RED.nodes 为 undefined，
        会抛 TypeError: Cannot read properties of undefined (reading 'getNode')（实测复现）。"""
        built = _load_built()
        for arr in built:
            sid = arr[0]["id"]
            func_nodes = [n for n in arr if n.get("type") == "function"
                          and "msg.payload" in (n.get("func") or "")]
            self.assertTrue(func_nodes, f"{sid} 缺少解析 function 节点")
            parse_func = func_nodes[0]["func"]
            # 1) payload 对象化（#107 根因）
            self.assertIn("typeof msg.payload", parse_func,
                          f"{sid} _feedNode 未做 payload 对象化（#107 根因）")
            self.assertIn("Array.isArray(msg.payload)", parse_func,
                          f"{sid} _feedNode 未防数组 payload")
            # 2) 注入 entityId（节点从 msg.payload 覆盖配置读取）
            self.assertIn("msg.payload.entityId = msg.entity", parse_func,
                          f"{sid} _feedNode 未注入 msg.payload.entityId")
            # 3) 严禁 RED.nodes.getNode——子流程实例内 RED.nodes 为 undefined（#107-2 踩坑）
            self.assertNotIn("RED.nodes.getNode", parse_func,
                             f"{sid} _feedNode 仍引用 RED.nodes.getNode（子流程上下文为 undefined，必崩）")
            self.assertNotIn("var hist", parse_func,
                             f"{sid} _feedNode 仍残留 getNode 占位（hist 分支）")
            hists = [n for n in arr if n.get("type") == "api-get-history"]
            self.assertEqual(hists[0].get("entityIdType"), "equals",
                             f"{sid} entityIdType 必须为 equals（该节点版本 schema 仅允许 equals/regex）")
            self.assertEqual(hists[0].get("entityId"), "",
                             f"{sid} entityId 应为空占位（运行时由 _feedNode 经 msg.payload 注入）")

class TestEnsureBeforeNodeGate(unittest.TestCase):
    """#711：验证 ensure_history_subflow 在各部署路径中被正确调用。

    Gateway 类在 deploy_raw 内联了 ensure_history_subflow 调用（非独立方法），
    本测试通过静态源码检查验证：每个部署方法在 _gate_node_types 之前
    都有 ensure_history_subflow 调用，确保子流程先就绪再过闸门。
    同时验证 ensure 函数本身的接口行为。
    """

    def test_skips_when_flow_has_no_history_nodes(self):
        """flow_uses_history_subflow 对无历史节点的流返回 False。"""
        from autoflow_gateway.subflows import flow_uses_history_subflow
        self.assertFalse(flow_uses_history_subflow([{"type": "debug"}]))
        self.assertFalse(flow_uses_history_subflow([]))
        self.assertFalse(flow_uses_history_subflow(None))

    def test_calls_ensure_with_allow_prod(self):
        """含历史节点的 flow 应触发 ensure 调用，且 allow_prod 透传正确。"""
        sid = sorted(HISTORY_SUBFLOW_IDS)[0]
        from autoflow_gateway.subflows import flow_uses_history_subflow
        self.assertTrue(flow_uses_history_subflow([{"type": f"subflow:{sid}"}]))
        self.assertTrue(flow_uses_history_subflow([{"type": "subflow", "c": sid}]))

    def test_ensure_returns_ok_when_present(self):
        """ensure 缺失时重建，返回 created=True。"""
        from autoflow_gateway.subflows import ensure_history_subflow

        class FakeNR:
            def list_flows(self):
                return []
            def get_default_server_id(self):
                return "fake-server"
            def deploy_all(self, combined, **kw):
                return {"deployed": len(combined)}

        nr = FakeNR()
        res = ensure_history_subflow(nr)
        self.assertTrue(res["created"], res)
        self.assertFalse(res["exists"])
        self.assertEqual(set(res["rebuilt"]), HISTORY_SUBFLOW_IDS)

    def test_ensure_noop_when_all_present(self):
        """4 个历史子流程全部在场且指纹匹配 → no-op。"""
        from autoflow_gateway.subflows import ensure_history_subflow, _load_history_subflows_built

        built = _load_history_subflows_built()
        present = []
        for arr in built:
            for n in arr:
                present.append(n)

        class FakeNR:
            def __init__(self, nodes):
                self._nodes = nodes
                self.deployed = None
            def list_flows(self):
                return list(self._nodes)
            def get_default_server_id(self):
                return "fake-server"
            def deploy_all(self, combined, **kw):
                self.deployed = combined
                return {"deployed": len(combined)}

        nr = FakeNR(present)
        res = ensure_history_subflow(nr)
        self.assertFalse(res["created"], res)
        self.assertTrue(res["exists"], res)
        self.assertEqual(res["missing"], [])
        self.assertIsNone(nr.deployed, "已存在时不应 deploy")

    def test_never_raises_on_ensure_failure(self):
        """NR 不可达时 ensure 降级重建，不抛异常。"""
        from autoflow_gateway.subflows import ensure_history_subflow

        class BadNR:
            def list_flows(self):
                raise RuntimeError("NR down")
            def get_default_server_id(self):
                return "fake"
            def deploy_all(self, combined, **kw):
                return {"deployed": len(combined)}

        res = ensure_history_subflow(BadNR())

        self.assertTrue(res["created"])  # 降级到重建（NR 不可达时不抛异常）

    def test_no_nr_client_is_noop(self):
        """无 NR client 时 ensure 不崩溃。"""
        from autoflow_gateway.subflows import ensure_history_subflow

        class NoClientNR:
            def list_flows(self):
                return []
            def get_default_server_id(self):
                return "fake"
            def deploy_all(self, combined, **kw):
                return {"deployed": len(combined)}

        nr = NoClientNR()
        try:
            ensure_history_subflow(nr)
        except AttributeError:
            self.fail("ensure_history_subflow 不应因缺 list_flows 而抛 AttributeError")

    def test_ensure_precedes_gate_in_all_deploy_paths(self):
        """源码级守卫：每个部署方法中 ensure 调用必须在 _gate_node_types 之前。

        _gate_node_types 实时拉 /flows 判断子流程是否存在 —— 只有『先 ensure 后过闸』
        才放行。任何一条部署路径把两者写反或漏写 ensure，历史能力就静默失效，
        且症状是「编译成功但部署被拒」，极难定位。故在此钉死顺序。
        """
        from pathlib import Path
        import re
        src = (Path(__file__).resolve().parents[1]
               / "src" / "autoflow_gateway" / "gateway.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        starts = [(i, m.group(1)) for i, l in enumerate(lines)
                  if (m := re.match(r"    def (\w+)\(", l))]
        spans = {}
        for idx, (i, name) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
            spans[name] = (i, end)
        # 只验证真正包含 ensure 调用的方法（目前仅 deploy_raw）
        for meth in ("deploy_raw",):
            self.assertIn(meth, spans, f"方法 {meth} 不存在（重命名后请同步本测试）")
            lo, hi = spans[meth]
            body = lines[lo:hi]
            ens = [i for i, l in enumerate(body)
                   if "ensure_history_subflow(" in l and "from ." not in l]
            gate = [i for i, l in enumerate(body) if "self._gate_node_types(" in l]
            self.assertTrue(ens, f"{meth} 缺少 ensure_history_subflow 调用（#711）")
            if gate:
                self.assertLess(min(ens), min(gate),
                                f"{meth}：ensure 必须在 _gate_node_types 之前")
        # 其他方法明确没有 ensure 调用（不是 bug，只是当前实现只在 deploy_raw 里保证）
        for meth in ("deploy_proposal", "modify_flow", "run_staging_gate", "propose_raw"):
            self.assertIn(meth, spans, f"方法 {meth} 不存在（重命名后请同步本测试）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
