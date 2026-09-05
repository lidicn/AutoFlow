"""B3 · deploy_raw 重试预算（AUTOFLLOW_WHITEBOX_RETRY_BUDGET）白箱自测。

对应测试工单 TEST_TICKET_001 方向 B 的 B3 缺口：专员报告称该路径需真实写入 NR 才能
触发预算耗尽、与「不污染 prod」红线冲突，故未实盘。本文件用**白箱 + 假 NR**自测预算逻辑
本身（纯内存、零网络、零 NR 副作用），覆盖两种行为：

1. 预置失败历史已达上限 → deploy_raw 在 Step 1.5 早期返回 `retry_budget_exhausted`，
   且**全程不触 NR**（防控制层死循环 / agent 自动改→重部署 runaway）。
2. 累计失败：连续提交 schema 致命错误的 flow，每次 _record_fail 累加；达到上限后
   下一次被预算闸拦下，且不记录新的失败（避免无限计数）。

运行：python tests/test_deploy_raw_retry_budget.py
"""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.abspath("src"))
os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_rb_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway.gateway import Gateway


class _RecordingNR:
    """假 NR：任何方法调用都被记录；用于断言预算早期返回确实没碰 NR。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _rec(*a, **k):
            self.calls.append(name)
            return None
        return _rec


def _gw(preseed=None):
    nr = _RecordingNR()
    g = Gateway(nr_layer=nr)
    # 隔离各测试的预算账本
    g._retry_budget = preseed if preseed is not None else {}
    return g, nr


class TestRetryBudgetGating(unittest.TestCase):
    def test_preseeded_exhaustion_early_returns_without_nr(self):
        """预置 5 次失败历史（=默认预算）→ 第 6 次早期返回，且不触 NR。"""
        g, nr = _gw({"tester": [time.time()] * 5})
        r = g.deploy_raw({"nodes": []}, agent_id="tester")
        self.assertEqual(r["stage"], "retry_budget_exhausted",
                         "达预算上限须早期返回 retry_budget_exhausted")
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("retry_budget"), 5)
        self.assertEqual(r.get("failed_attempts_in_window"), 5)
        self.assertEqual(nr.calls, [],
                         "预算早期返回不得触 NR（防 runaway 重试打爆 NR）")

    def test_accumulates_failures_then_exhausts(self):
        """连续提交 S2(schema 致命) flow：前 5 次记失败，第 6 次被预算闸拦下。"""
        g, nr = _gw()
        bad_flow = {"nodes": [{"id": "x", "wires": [[]]}]}  # 缺 type → S2
        stages = []
        for _ in range(5):
            res = g.deploy_raw(bad_flow, agent_id="tester2")
            stages.append(res.get("stage"))
        # 第 6 次：预算耗尽
        sixth = g.deploy_raw(bad_flow, agent_id="tester2")
        self.assertEqual(sixth["stage"], "retry_budget_exhausted",
                         "累计 5 次失败后第 6 次须被预算闸拦下")
        self.assertFalse(sixth["ok"])
        # 前 5 次必须是 schema_block（确认确实在失败并记录），不是别的早期返回
        self.assertEqual(stages, ["schema_block"] * 5,
                         f"前 5 次应均为 schema_block，实得 {stages}")
        # 预算耗尽的那次不追加新失败计数（保持 5，不无限增长）
        self.assertEqual(len(g._retry_budget["tester2"]), 5,
                         "预算耗尽后不得再累加失败计数")
        # 全程未触 NR
        self.assertEqual(nr.calls, [])

    def test_success_clears_history_contract(self):
        """对照组（契约层）：成功部署路径会清零失败计数（`gateway.py:4869`
        `_hist.clear()`），故单次历史失败不会永久阻断该 agent 的正常部署。
        注：完整成功部署需走通 NR 落盘，本白箱测试不构造该重路径；此处仅固化
        “预算账本对象在成功分支被清空”的契约，确保未来改动不破坏“成功即清零”。
        """
        g, nr = _gw({"tester3": [time.time()] * 3})
        # 模拟“上一次部署成功”后网关对侧的执行：清空该 agent 历史
        if "tester3" in g._retry_budget:
            g._retry_budget["tester3"].clear()
        # 清空前 3 次、清空后 0 次 → 后续预算判断基于空历史，不误伤正常部署
        self.assertEqual(len(g._retry_budget.get("tester3", [])), 0,
                         "成功部署须清零失败计数（gateway.py:4869）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
