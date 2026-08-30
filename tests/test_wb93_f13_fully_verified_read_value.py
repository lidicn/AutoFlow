"""WB93 §1.3 收口测试：读值-label 流的 fully_verified 语义边界。

结论（实证，见 wb93_f13_probe2.py）：
- 读值实体在 staging store 中且为数字 → 重放按逻辑求值、fully_verified=True（已验证）。
- 读值实体缺失 / 状态不可解析为数字 → 保守降级 fully_verified=False（安全 fail-closed，预期行为）。
本测试锁定这两条边界，防止回归。
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


def _store(include_lumi=True, lumi_state="50"):
    st = VH.VHassStore()
    rows = [("light.lamp", "灯", "书房", "off", {})]
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
        """读值实体为数字 → 重放按逻辑求值，fully_verified=True（O1+§1.3 正常路径已解）。"""
        r = _gw().run_staging_gate(dsl="", expected=[], flow=flow,
                                   vhass_store=_store(lumi_state="50"))
        assert r["verdict"] == "放行", r
        assert r["fully_verified"] is True, r
        assert r["replayed_services"] == ["light.turn_off(light.lamp)"], r

    def test_numeric_entity_low_triggers_then(self, flow):
        """光照=5（<10）→ 走 否则 反向？不，走 分支(then)=turn_on。验证 then 分支也被符号化求值。"""
        r = _gw().run_staging_gate(dsl="", expected=[], flow=flow,
                                   vhass_store=_store(lumi_state="5"))
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
