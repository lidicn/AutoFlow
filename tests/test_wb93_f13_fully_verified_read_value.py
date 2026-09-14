"""WB93 §1.3 收口测试：读值-label 流的 fully_verified 语义边界。

结论（实证，见 wb93_f13_probe2.py）：
- 读值实体在 staging store 中且为数字 → 重放按逻辑求值；**且须提供证明状态转变的
  expected（种子取反态）**，否则 B20/F-R2-01「零断言 / 前置已满足」会诚实降级。
- 读值实体缺失 / 状态不可解析为数字 → 保守降级 fully_verified=False（安全 fail-closed，预期行为）。
本测试锁定这两条边界，防止回归。

注：B20/F-R2-01（4343e50 弱验证加固）后，「fully_verified=True」还要求 expected 证明世界态
转变；仅「选对分支、动作被重放」不足以充分验证。故正向用例须传 expected 且把灯种子取反态。
"""
import os, sys, tempfile
import pytest

sys.path.insert(0, r"E:\NAS\autoflow\src")
from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.dsl_engine import parse, compile as C


def _gw():
    gw = G.Gateway()
    for eid in ("light.lamp", "sensor.lumi"):
        gw.state.add_mapping(eid, eid)
    return gw


def _store(include_lumi=True, lumi_state="50", lamp_state="off"):
    st = VH.VHassStore()
    rows = [("light.lamp", "灯", "书房", lamp_state, {})]
    if include_lumi:
        rows.append(("sensor.lumi", "光照", "书房", lumi_state, {}))
    seed = VH.build_seed_from_entities(rows)
    st.areas = seed["areas"]
    st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
    return st


DSL = (
    "场景: 书房光照\n触发: inject\n取值: sensor.lumi 光照\n"
    "分支: $number(光照) < 10\n  动作: light.turn_on(light.lamp)\n"
    "否则:\n  动作: light.turn_off(light.lamp)\n"
)


@pytest.fixture
def flow():
    return C(parse(DSL))


class TestF13ReadValueFullyVerified:
    def test_numeric_entity_gives_fully_verified(self, flow):
        """读值实体为数字 → 重放按逻辑求值；种子取反态(on)证明 50>=10 走 turn_off 的状态转变。"""
        r = _gw().run_staging_gate(
            dsl="", expected=[{"entity_id": "light.lamp", "state": "off"}], flow=flow,
            vhass_store=_store(lumi_state="50", lamp_state="on"))
        assert r["verdict"] == "放行", r
        assert r["fully_verified"] is True, r
        assert r["replayed_services"] == ["light.turn_off(light.lamp)"], r

    def test_numeric_entity_low_triggers_then(self, flow):
        """光照=5（<10）→ 走 分支(then)=turn_on；种子取反态(off)证明 then 分支的符号化求值。"""
        r = _gw().run_staging_gate(
            dsl="", expected=[{"entity_id": "light.lamp", "state": "on"}], flow=flow,
            vhass_store=_store(lumi_state="5", lamp_state="off"))
        assert r["fully_verified"] is True, r
        assert r["replayed_services"] == ["light.turn_on(light.lamp)"], r

    def test_missing_entity_is_safe_failclosed(self, flow):
        """读值实体不在 staging store → 无法符号化求值 → 保守降级（安全 fail-closed，预期）。"""
        r = _gw().run_staging_gate(dsl="", expected=[], flow=flow,
                                   vhass_store=_store(include_lumi=False))
        assert r["fully_verified"] is False, r
        assert r["verdict"] == "未充分验证", r
        joined = " ".join(r.get("warnings", []))
        assert "无法本地求值" in joined or "保守" in joined, r

    def test_non_numeric_entity_is_safe_failclosed(self, flow):
        """读值实体状态不可解析为数字 → 保守降级（安全 fail-closed，预期）。"""
        r = _gw().run_staging_gate(dsl="", expected=[], flow=flow,
                                   vhass_store=_store(lumi_state="unknown"))
        assert r["fully_verified"] is False, r
        assert r["verdict"] == "未充分验证", r
