"""A24(#round5)：apply 回执 decision_id 与库错位守护 + 一致性回归。

背景：原报告 iss_fb16973875 实测回执 id 与库「一位之差」（dec_83d…3aea vs dec_83d…7aea），
apply→get_decision 闭环静默断掉。R4 已在 request_decision 加「读回自检」fail-safe：
回执 id 必须能从库里查回，查不回即 ok=False 并如实说明，绝不把死 id 当成功回执发出去。

A24 调查结论：全仓库「dec_」唯一生成点是 decision_store.create 的
`did = "dec_" + uuid.uuid4().hex[:12]`，落库与回查均走同一 did（_row_to_dict 不改 id）。
request_decision 回执 decision_id = did = rec["id"] = DB 行 id，三处同源、无中间改写/截断；
调用方均派生自同一 id，脆弱剥壳最多取 None 不会「一位之差」。故原始一位之差无法在当前代码
任何路径复现——判定为历史传输/显示偶发，非代码缺陷。处置：保留 R4 读回自检为最终兜底，
并把「复现不出」固化为可执行守护（错位注入 → ok:False + 300 轮 create→get→回执 id 一致性）。

运行：python -m pytest tests/test_decision_id_consistency.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway.gateway import Gateway


class _FakeNR:
    """最小 NR 桩：拦截点发生在部署前，本测试不会触达。"""

    def list_flows(self):
        return []

    def get_default_server_id(self):
        return ""

    def get_flow(self, fid):
        return None

    def create_or_update_flow(self, fid, flow_data, force=False, allow_prod=False):
        return {"id": fid, "created": True}


def _make_gw(monkeypatch, tmp_path):
    """不触网的 Gateway：侧车落 tmp_path，Bark 行为 noop。"""
    monkeypatch.chdir(tmp_path)
    gw = Gateway()
    gw.nr = _FakeNR()
    gw._bark_push = lambda *a, **k: None  # 避免后台 Bark 线程触网
    return gw


class _MismatchDecisionStore:
    """模拟 DecisionStore：create 回执 id 与 get 读回 id 故意不一致（注入一位之差）。"""

    def __init__(self, mismatch_id):
        self._id = "dec_real00001a"
        self.mismatch_id = mismatch_id

    def create(self, question, options, source="deepseek"):
        return {"id": self._id, "question": question,
                "options": list(options), "status": "pending"}

    def get(self, did):
        return {"id": self.mismatch_id, "question": "q",
                "options": ["a"], "status": "pending"}


def test_mismatched_decision_id_rejected(monkeypatch, tmp_path):
    """回执 id 与库读回不一致 → ok=False 且如实说明，绝不发死 id（守护 R4 读回自检）。"""
    gw = _make_gw(monkeypatch, tmp_path)
    gw.decisions = _MismatchDecisionStore(mismatch_id="dec_wrong0000b")  # 一位之差
    res = gw.request_decision("开灯吗？", ["开", "关"])
    assert res["ok"] is False, f"错位必须 ok=False：{res}"
    assert "读回自检" in res.get("error", ""), "必须点明读回自检失败"
    # 回执 decision_id 仍是内存权威 did，并非被污染的错 id
    assert res["decision_id"] == "dec_real00001a", "回执 decision_id 应为权威 did"
    assert res["decision_id"] != "dec_wrong0000b", "不得回吐错 id"


def test_decision_id_300_round_consistency(monkeypatch, tmp_path):
    """正常路径 300 轮 create→get→回执 id 一致性：store 层零错位（单一真相源 did）。"""
    gw = _make_gw(monkeypatch, tmp_path)
    for i in range(300):
        res = gw.request_decision(f"请确认操作 #{i}", ["批准", "拒绝"])
        assert res["ok"] is True, f"轮 {i} 应 ok=True：{res}"
        did = res["decision_id"]
        assert did.startswith("dec_"), f"轮 {i} decision_id 格式异常：{did}"
        stored = gw.decisions.get(did)
        assert stored is not None, f"轮 {i} 库里查不回 {did}"
        assert stored["id"] == did, (
            f"轮 {i} 库 id 与回执 decision_id 错位：{stored['id']!r} vs {did!r}")


def test_request_decision_flat_decision_id_is_single_source(monkeypatch, tmp_path):
    """回执 decision_id 与 decision.id 同源（R4 平铺字段，调用方应优先取 decision_id）。"""
    gw = _make_gw(monkeypatch, tmp_path)
    res = gw.request_decision("亮度？", ["80", "30"])
    assert res["ok"] is True
    assert res["decision_id"] == res["decision"]["id"], "decision_id 必须等于 decision.id"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\ndecision_id_consistency: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
