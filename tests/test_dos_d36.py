"""D36（WB83 P1）回归测试：DSL 复杂度退化 DoS 修复。

根因：propose_dsl 的 staging 闸逐实体调 _resolve_best / _entity_attribute_names，
而每个都调 get_device_catalog()（读盘 + json.load 大目录）与 resolve_entity（全目录模糊扫描）
→ O(N·目录解析) 串行阻塞 DoS（10 层嵌套即数秒、数百层卡死，拖垮整个串行 MCP 面）。

修复：
  1) _check_entities_known 单次取目录+映射，entity_id 形态引用走内联快路径（不读盘、不模糊）；
  2) resolve_entity 结果 LRU 缓存；
  3) SharedState._load 按 (mtime,size) 缓存解析结果，消除同一请求内 O(N) 次读盘。
"""
import os
import tempfile
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoflow_gateway import dsl_engine
from autoflow_gateway import gateway as G
from autoflow_gateway import state as ST


# ── 1) entity_id 形态判定（决定走快路径还是模糊解析）──
def test_entity_id_shape_regex():
    ok = ["light.kitchen_ceiling", "switch.xxx", "light.fake_1", "binary_sensor.motion_2"]
    bad = ["书房吊灯", "Backup 自动备份", "Living Room Light", "light.kitchen ceiling", ""]
    for s in ok:
        assert G._ENTITY_ID_SHAPE_RE.match(s), f"应判为 entity_id 形态: {s}"
    for s in bad:
        assert not G._ENTITY_ID_SHAPE_RE.match(s), f"不应判为 entity_id 形态: {s!r}"


# ── 2) SharedState._load 缓存：未变更命中、变更即失效 ──
def test_sharedstate_load_cache():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "device_catalog.json")
    ST.SharedState._files = {  # 仅测试用，隔离
        "device_catalog": p,
        "flow_catalog": os.path.join(d, "flow_catalog.json"),
        "entity_mapping": os.path.join(d, "entity_mapping.json"),
        "intent_log": os.path.join(d, "intent_log.json"),
    }
    ss = ST.SharedState.__new__(ST.SharedState)
    ss.cfg = None
    ss.base = d
    ss._files = ST.SharedState._files
    ss._load_cache = {}
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"entities": {"light.a": {"friendly_name": "A"}}}')
    a = ss._load("device_catalog")
    b = ss._load("device_catalog")  # 命中缓存（同一解析对象）
    assert a is b, "未变更应命中缓存（同一对象引用）"
    # 改写文件 → 失效重新读
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"entities": {"light.b": {"friendly_name": "B"}}}')
    c = ss._load("device_catalog")
    assert c is not a
    assert c["entities"].get("light.b")


# ── 3) _check_entities_known 短路：entity_id 形态引用不触发模糊解析 ──
class _FakeState:
    def __init__(self, ents):
        self._ents = ents
    def get_device_catalog(self):
        return {"version": 1, "freshness": "", "entities": self._ents}
    def get_entity_mapping(self):
        return {"mappings": {}}


class _FakeGW:
    _MAX_ENTITY_REFS = G._MAX_ENTITY_REFS
    def __init__(self, ents):
        self.state = _FakeState(ents)
        self.fuzzy_calls = []
    def resolve_entity(self, name, area=None, domain=None):
        self.fuzzy_calls.append(name)
        return {"ok": True, "candidates": []}  # 无模糊命中
    _check_entities_known = G.Gateway._check_entities_known
    _scene_entity_refs = G.Gateway._scene_entity_refs


def _nest_dsl(depth, ent="light.fake"):
    lines = [f"触发: switch.xxx 状态变化", f"分支: payload = a", f"  动作: light.turn_on({ent}_a)"]
    for i in range(1, depth + 1):
        lines.append(f"否则如果: payload = x{i}")
        lines.append(f"  动作: light.turn_on({ent}_{i})")
    return "\n".join(lines)


def test_check_entities_known_short_circuit_no_fuzzy():
    scene = dsl_engine.parse(_nest_dsl(80))
    gw = _FakeGW({})  # 空目录：所有引用均未知
    import time
    t0 = time.perf_counter()
    unknown = gw._check_entities_known(scene)
    dt = time.perf_counter() - t0
    assert len(unknown) == 82  # 1 触发(switch.xxx) + 81 动作实体，全部未知
    # 关键：entity_id 形态引用不应触发任何模糊解析（DoS 根因）
    assert gw.fuzzy_calls == [], f"entity_id 形态不应触发模糊解析，却调了 {gw.fuzzy_calls[:3]}…"
    assert dt < 1.0, f"_check_entities_known 应 <1s，实耗 {dt:.2f}s（DoS 未根除）"


def test_check_entities_known_fuzzy_only_for_natural_names():
    # 真实实体在目录 → 已知；中文名 → 走模糊（被调一次）；entity_id 形态编造 → 短路未知
    scene = dsl_engine.parse(
        "触发: light.real 状态变化\n"
        "分支: payload = on\n  动作: light.turn_on(light.real)\n"
        "否则如果: payload = 1\n  动作: light.turn_on(灯.客厅)\n"  # 非 entity_id 形态 → 模糊
    )
    gw = _FakeGW({"light.real": {"friendly_name": "Real"}})
    unknown = gw._check_entities_known(scene)
    # light.real 已知；「灯.客厅」非 entity_id 形态（含中文）→ 走模糊（返回空候选→未知）
    assert "light.real" not in unknown
    assert gw.fuzzy_calls == ["灯.客厅"], f"仅中文名应触发模糊，实得 {gw.fuzzy_calls}"


if __name__ == "__main__":
    test_entity_id_shape_regex()
    test_sharedstate_load_cache()
    test_check_entities_known_short_circuit_no_fuzzy()
    test_check_entities_known_fuzzy_only_for_natural_names()
    print("ALL D36 REGRESSION TESTS PASSED ✅")
