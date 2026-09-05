"""WB93 §1.3 方案A 守卫：verify 时从真实 HA 只读播种取值实体当前态（'播种桥'）。

隔离真实 HA（用 FakeHA 注入当前态），验证方案A 的桥接逻辑：
1. ha_layer 返回数字态 → 播种成功 → 取值-label 分支符号化求值 → fully_verified=True。
2. ha_layer 返回非数字态 → 仍 fail-closed（安全不变）。
3. ha_layer 不可达（抛异常）→ 跳过播种，维持 fail-closed。
4. 取值实体是幽灵（不在 staging store）→ 不造实体，维持 fail-closed。

底层机制（rec['state'] 注入后求值）已由 test_wb93_f13_fully_verified_read_value.py
（注入 store）锁定；本文件只锁「从 HA 读取 → 注入 store」这一段桥。
"""
import os, sys
sys.path.insert(0, r"E:\NAS\autoflow\src")
from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.dsl_engine import parse, compile as C


class FakeHA:
    """只读 HA 桩：states[eid]=状态串；特殊值 __raise__ 抛异常、__none__ 返回 None。"""
    def __init__(self, states):
        self.states = dict(states)
        self.calls = []
    def get_state(self, eid):
        self.calls.append(eid)
        v = self.states.get(eid, "__none__")
        if v == "__raise__":
            raise RuntimeError("HA unreachable")
        if v == "__none__":
            return None
        return {"entity_id": eid, "state": v, "attributes": {}}


def _store(lumi_state="0"):
    st = VH.VHassStore()
    rows = [("light.lamp", "灯", "书房", "off", {}),
            ("sensor.lumi", "光照", "书房", lumi_state, {})]
    seed = VH.build_seed_from_entities(rows)
    st.areas = seed["areas"]
    st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
    return st


DSL = (
    "场景: 书房光照\n触发: inject\n取值: sensor.lumi 光照\n"
    "分支: $number(光照) < 10\n  动作: light.turn_on(light.lamp)\n"
    "否则:\n  动作: light.turn_off(light.lamp)\n"
)

GHOST_DSL = (
    "场景: 书房光照\n触发: inject\n取值: sensor.ghost 幽灵\n"
    "分支: $number(幽灵) < 10\n  动作: light.turn_on(light.lamp)\n"
    "否则:\n  动作: light.turn_off(light.lamp)\n"
)


def _gw(ha, store):
    gw = G.Gateway(ha_layer=ha)
    for eid in ("light.lamp", "sensor.lumi", "sensor.ghost"):
        gw.state.add_mapping(eid, eid)
    # 强制默认 staging store 用我们构造的（含 sensor.lumi），以触发播种桥
    gw._build_vhass_from_staging = lambda: store
    return gw


class TestF13SeedFromHA:
    def test_seed_numeric_makes_fully_verified(self):
        ha = FakeHA({"sensor.lumi": "50"})
        store = _store(lumi_state="0")  # 目录态为 0，被实时态 50 覆盖
        r = _gw(ha, store).run_staging_gate(dsl="", expected=[], flow=C(parse(DSL)), vhass_store=None)
        assert r["verdict"] == "放行", r
        assert r["fully_verified"] is True, r
        assert r["replayed_services"] == ["light.turn_off(light.lamp)"], r
        assert ha.calls == ["sensor.lumi"], ha.calls

    def test_seed_non_numeric_stays_failclosed(self):
        ha = FakeHA({"sensor.lumi": "unknown"})
        store = _store(lumi_state="0")
        r = _gw(ha, store).run_staging_gate(dsl="", expected=[], flow=C(parse(DSL)), vhass_store=None)
        assert r["fully_verified"] is False, r
        assert r["verdict"] == "未充分验证", r

    def test_ha_unreachable_stays_failclosed(self):
        ha = FakeHA({"sensor.lumi": "__raise__"})
        store = _store(lumi_state="unknown")  # 目录态也非数字 → 不播种时仍 fail-closed
        r = _gw(ha, store).run_staging_gate(dsl="", expected=[], flow=C(parse(DSL)), vhass_store=None)
        assert r["fully_verified"] is False, r
        assert r["verdict"] == "未充分验证", r

    def test_ghost_entity_not_created_failclosed(self):
        ha = FakeHA({"sensor.ghost": "50"})
        store = _store(lumi_state="0")  # 不含 sensor.ghost
        r = _gw(ha, store).run_staging_gate(dsl="", expected=[], flow=C(parse(GHOST_DSL)), vhass_store=None)
        assert r["fully_verified"] is False, r
        assert r["verdict"] == "未充分验证", r
        # 幽灵实体绝不被凭空创建
        assert store.get_state("sensor.ghost") is None, "幽灵实体不应被创建"
