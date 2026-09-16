# -*- coding: utf-8 -*-
"""#18 · 防假绿四件套 + 幽灵实体「旁路即失败」固化守卫。

路线图 v2.3.0 #18：把 verify_flow 闸的「诚实降级 / fail-closed」行为钉死为确定性回归测试，
证明每一类「旁路（bypass）」场景都**不得**伪造绿（fully_verified 必须为 False，且 verdict
绝非「放行」）——要么 fail-closed 拦截，要么诚实降级「未充分验证」。

覆盖矩阵（#18 新增 / 加固项）：
- B22（外部调用驱动的分支不可求值）：flow 含 link out / 子流程，其返回值驱动 switch 分支判定，
  导致「未激活分支」的跳过结论不可信；若同时重放了与期望相反的动作 → 视为未充分验证。
  （此前无专门回归测试，属真缺口。）
- F-R6.5（数值条件触发净化）：`> 28` 类数值条件触发，世界态必须注入满足条件的合成值（29）
  而非条件原文（"> 28"），否则 `$number(payload.x)` 拿到垃圾、数值分支误判。集成验证：
  数值触发流经闸门后动作被正确重放、真转变路径 fully_verified=True（净化失效则触发不点火 → 0 重放）。
- 幽灵实体（两层）：① 引用不存在的 entity_id → 闸门实体校验 fail-closed 拦截
  （stage=entity_check / verdict=拦截）；② 取值节点引用的实体不在 staging store →
  `_seed_read_value_entities_from_ha` 绝不借道创建幽灵实体（维持 fail-closed）。

已存在的同级守卫（不重复，仅引用）：
- B20 ①前置已满足 / ②零断言 → tests/test_b20_vacuous_assertions.py
- F-R5-01（gate 放行但 fully_verified=False 不得锁题）→ tests/test_b23_b24_examiner.py
- F-R6.5 单元（_sanitize_trigger_state）→ tests/test_gate_jsonata_eval.py
- 幽灵实体 wires 形状 / 种子覆盖 → tests/test_gate_vhass_deepen.py / test_fr8_arena_lockable.py

全程离线、零运行时副作用；证据落临时目录。
运行：pytest tests/test_false_green_pinned.py
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_fgp_")
os.environ.pop("AUTOFLOW_REPLAY_ZERO_POLICY", None)  # 默认 fail_closed

from autoflow_gateway.gateway import Gateway
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config

reset_config()

TAB = "tab_fgp"


# ───────────── 白箱直通 harness（复用 FalseGreen 家族的 shim 模式）─────────────
def _mk_shim(seed_states):
    class _Shim:
        def _gate_node_types(self, flow):
            return None

        def _ensure_history_subflow_for(self, flow, allow_prod, tid=None, where="deploy"):
            return {"skipped": "stub_no_nr"}

        def _check_entities_known(self, scene):
            return []  # 白箱直通口不经目录校验（幽灵实体校验由 DSL 路径专门测）

        def _seed_read_value_entities_from_ha(self, flow, store):
            return None  # 自管种子

        def _build_vhass_from_staging(self):
            store = VH.VHassStore()
            store.areas = {"area_0": "房间"}
            store.entities = {}
            for eid, st in seed_states.items():
                store.entities[eid] = VH.VHassStore._normalize({
                    "entity_id": eid, "state": st,
                    "attributes": {"friendly_name": eid}, "area": "房间"})
            return store
    return _Shim()


def _run(nodes, seed_states, expected):
    flow = {"id": "tf_fgp", "label": "[T004-FGP]", "nodes": nodes}
    gw = _mk_shim(seed_states)
    return Gateway.run_staging_gate(gw, dsl="", expected=expected, flow=flow,
                                    branch_aware=True)


# ───────────── 节点构造助手 ─────────────
def _inject(nid, out, payload):
    return {"id": nid, "type": "inject", "z": TAB, "name": "触发",
            "props": [{"p": "payload"}], "repeat": "", "crontab": "", "once": False,
            "payloadType": "json", "payload": payload, "wires": [[out]]}


def _switch(nid, out0, out1):
    # rule0: cmd==off → 走 on 分支（turn_off）；rule1: else → 走 off 分支（turn_on）
    return {"id": nid, "type": "switch", "z": TAB, "property": "payload.cmd",
            "propertyType": "msg",
            "rules": [{"t": "eq", "v": "off", "vt": "str"}, {"t": "else"}],
            "checkall": "true", "outputs": 2, "wires": [[out0], [out1]]}


def _action(nid, target, svc):
    return {"id": nid, "type": "api-call-service", "z": TAB, "name": "动作",
            "server": "srv", "domain": "light", "service": svc,
            "entityId": [target], "data": "", "wires": [[]]}


def _link_out(nid):
    return {"id": nid, "type": "link out", "z": TAB, "name": "外部调用", "wires": []}


# ============================== B22 ==============================
def test_b22_external_branch_unverified_degrades():
    """B22：flow 含 link out（外部调用），其返回值驱动 switch 分支；触发走 on 分支重放
    turn_off，而期望（else 分支意图）= on 落进未激活分支 → 反置动作被重放、分支判定不可求值
    → 结论降级为未充分验证（绝不假绿）。"""
    ENTITY = "light.b22_lamp"
    nodes = [
        _inject("n1", "sw", json.dumps({"cmd": "off"})),
        _switch("sw", "off_n", "on_n"),
        _action("off_n", ENTITY, "turn_off"),   # 激活：与期望相反的动作被重放
        _action("on_n", ENTITY, "turn_on"),     # 未激活：期望后置条件来源
        _link_out("lo"),                          # 外部调用 → 分支判定不可求值
    ]
    expected = [{"entity_id": ENTITY, "state": "on"}]  # else 分支意图
    r = _run(nodes, {ENTITY: "off"}, expected)
    assert r.get("passed") is True, r  # 不硬拦（沿 A22 诚实降级模式）
    assert r.get("fully_verified") is False, r  # ★ 旁路即失败：不得假绿
    assert r.get("verdict") == "未充分验证", r
    assert any("分支判定不可求值" in w or "B22" in w for w in r.get("warnings", [])), r.get("warnings")


# ============================== F-R6.5 ==============================
def test_fr65_numeric_trigger_world_injected_as_number():
    """F-R6.5 集成：server-state-changed 数值条件触发 `ifState='> 28'`，世界态须注入满足条件的
    合成值 '29'（而非条件原文 '> 28'）。净化生效 → 数值触发正确点火、下游动作被重放、
    真转变 fully_verified=True。若净化失效，world='> 28' → $number 取到垃圾 → 触发不点火
    → 0 重放 → fail-closed 拦截（同样不假绿，但丢失正确验证能力）。"""
    TEMP = "sensor.temp"
    LAMP = "light.temp_lamp"
    nodes = [
        {"id": "ssc", "type": "server-state-changed", "z": TAB,
         "entities": {"entity": [TEMP]}, "ifState": ">28",
         "wires": [["act"]]},
        _action("act", LAMP, "turn_on"),
    ]
    expected = [{"entity_id": LAMP, "state": "on"}]
    # 种子：温度 25（<28，未达触发），灯 off（期望 on → 真转变）
    r = _run(nodes, {TEMP: "25", LAMP: "off"}, expected)
    # 净化后 world(sensor.temp)=29 > 28 → 触发点火 → turn_on 重放 → 真转变
    assert r.get("passed") is True, r
    assert r.get("fully_verified") is True, r  # 真转变，不是净化失败导致的假绿/假拦
    assert r.get("verdict") == "放行", r
    assert any("light.turn_on(" + LAMP + ")" in x for x in (r.get("replayed_services") or [])), r


# ============================== 幽灵实体 ① 网关级 fail-closed ==============================
def test_ghost_entity_check_blocks_at_gate():
    """幽灵实体（引用不存在的 entity_id）→ 闸门实体校验阶段 fail-closed 拦截，
    verdict=拦截、passed=False，绝不静默放行。"""
    gw = Gateway()
    dsl = "场景: 幽灵\n触发: inject(payload={})\n动作: light.turn_on(light.ghost_999_does_not_exist)"
    r = gw.run_staging_gate(dsl, [])
    assert r.get("passed") is False, r
    assert r.get("verdict") == "拦截", r
    assert r.get("stage") == "entity_check", r
    assert "light.ghost_999_does_not_exist" in (r.get("failures") or []), r


# ============================== 幽灵实体 ② store 不创建幽灵 ==============================
def test_ghost_entity_seed_does_not_create_ghost():
    """`_seed_read_value_entities_from_ha`：取值节点引用的实体不在 staging store（幽灵）→
    绝不借道创建幽灵实体，维持 fail-closed（HA 不可达 / 不存在即跳过）。"""
    gw = Gateway()
    # 自管 HA：对任意实体返回合法 state dict（模拟「HA 里真的有这个实体」）
    gw.ha = types.SimpleNamespace(get_state=lambda eid: {"state": "on"})
    store = VH.VHassStore()
    store.entities = {
        "light.real_lamp": VH.VHassStore._normalize({
            "entity_id": "light.real_lamp", "state": "off",
            "attributes": {"friendly_name": "真灯"}, "area": "房间"}),
    }
    ghost = "sensor.ghost_read"
    flow = {"id": "f", "label": "l", "nodes": [
        {"id": "rd", "type": "api-current-state", "z": TAB,
         "entityId": ghost, "halt_if": "", "wires": [[]]},
    ]}
    gw._seed_read_value_entities_from_ha(flow, store)
    assert store.get_state(ghost) is None, "不得创建幽灵实体"
    assert "light.real_lamp" in store.entities, "既存实体不受影响"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
