"""A5 runtime observe-correct 闭环（tap 风格）单元测试。

不依赖 live HA/NR —— 注入可控假 HA（self.ha.get_state），验证：
  1. observe_postconditions：预期 vs HA 实际状态比对，覆盖 匹配/不匹配/实体缺失/空预期。
  2. observe_after_deploy：组合 tap（HA 断言 + 尽力 NR 快照），返回合并观测报告，
     含断言明细、flow_id 透传、NR 未授权时的非阻塞提示。

旧版轮询式 observe_after_deploy_loop（window/poll_interval/observed/早停）已在
W2-5「D3 串行缓解」首刀中移除（见 gateway.py:4889 注释），本文件对应新 tap-API。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway.gateway import Gateway


class _FakeHA:
    """get_state(eid) 返回预设状态；未预设实体返回 None（模拟 HA 无该实体）。"""

    def __init__(self, states: dict):
        self._states = states

    def get_state(self, eid):
        return self._states.get(eid)


def _patch(gw, states: dict):
    gw.ha = _FakeHA(states)


def test_observe_postconditions_match():
    gw = Gateway()
    _patch(gw, {"light.a": {"state": "on"}})
    res = gw.observe_postconditions([{"entity_id": "light.a", "state": "on"}])
    assert res["ok"] is True
    assert res["source"] == "ha"
    assert res["failures"] == []
    assert res["assertions"][0] == {"entity_id": "light.a", "expected": "on",
                                     "actual": "on", "ok": True}


def test_observe_postconditions_mismatch():
    gw = Gateway()
    _patch(gw, {"light.a": {"state": "off"}})
    res = gw.observe_postconditions([{"entity_id": "light.a", "state": "on"}])
    assert res["ok"] is False
    assert len(res["failures"]) == 1
    f = res["failures"][0]
    assert f["entity_id"] == "light.a" and f["expected"] == "on" and f["actual"] == "off"
    assert res["assertions"][0]["ok"] is False


def test_observe_postconditions_missing_entity():
    gw = Gateway()
    _patch(gw, {})  # HA 无该实体
    res = gw.observe_postconditions([{"entity_id": "light.a", "state": "on"}])
    assert res["ok"] is False
    assert res["failures"][0]["actual"] is None
    assert res["assertions"][0]["actual"] is None


def test_observe_postconditions_empty_expected_is_vacuous_pass():
    gw = Gateway()
    _patch(gw, {})
    res = gw.observe_postconditions([])
    assert res["ok"] is True
    assert res["assertions"] == []
    assert res["failures"] == []


def test_observe_after_deploy_tap_match():
    gw = Gateway()
    _patch(gw, {"light.a": {"state": "on"}})
    res = gw.observe_after_deploy([{"entity_id": "light.a", "state": "on"}],
                                  flow_id="fid1")
    assert res["ok"] is True
    assert res["ha"]["ok"] is True
    assert res["flow_id"] == "fid1"
    # 无 nr capture 能力时给非阻塞提示，不抛错
    assert res.get("nr_note") is None or isinstance(res.get("nr_note"), str)


def test_observe_after_deploy_tap_mismatch_reports_failure():
    gw = Gateway()
    _patch(gw, {"light.a": {"state": "off"}})
    res = gw.observe_after_deploy([{"entity_id": "light.a", "state": "on"}],
                                  flow_id="fid1")
    assert res["ok"] is False
    assert res["ha"]["failures"][0]["entity_id"] == "light.a"


if __name__ == "__main__":
    test_observe_postconditions_match()
    test_observe_postconditions_mismatch()
    test_observe_postconditions_missing_entity()
    test_observe_postconditions_empty_expected_is_vacuous_pass()
    test_observe_after_deploy_tap_match()
    test_observe_after_deploy_tap_mismatch_reports_failure()
    print("✅ test_observe_deploy 全部通过（tap-API）")
