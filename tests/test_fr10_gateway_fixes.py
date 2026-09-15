# -*- coding: utf-8 -*-
"""F-R10 网关修复守卫（docs/04_test/findings-ledger.md §B R10 三缺陷）。

- **F-R10-GATE-01（P1）**：可控设备做状态触发（如「当 switch2 开 → 开灯」）时，
  seed_overrides（断言目标反态翻转，F-R8-04）在触发注入**之后**覆盖同一实体，
  把分支判定条件抹掉 → server-state-changed 比不中 → 0 HA 意图 → t07 结构性失效。
  修法：触发实体上的种子覆盖**前置**应用（先种子、后触发事件，后注入者胜）；
  「期望态 == 注入触发态」的世界态断言剔除（由触发事件保证，非 flow 副作用证据）。
- **F-R10-B1-01（P2）**：submit 缺 agent_id 曾静默落成 locked_by="arena-agent"。
  修法：HTTP 层 400 + ArenaManager 深度防御。
- **F-R10-T0（P3）**：题面要状态触发、DSL 却 inject-only → 触发保真降级
  （fully_verified=False，非硬拦）。
- **F-R10-KB-01（P2）**：error_kb / experience 分区隔离开关
  （AUTOFLOW_ARENA_KB_ISOLATION=1），杜绝 A/B 两臂串答案。

全程离线、零运行时副作用。运行：pytest tests/test_fr10_gateway_fixes.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from autoflow_gateway.arena import ArenaManager
from autoflow_gateway.config import reset_config


@pytest.fixture(autouse=True)
def _fr10_isolated_env():
    """F-R10 守卫需 staging env + 独立 data dir。用 fixture 隔离并在结束时还原 env
    与 gateway config 单例，杜绝 import 期/测试期写 os.environ 污染整轮会话
    （曾导致 49 个其他测试假 error）。"""
    saved = {k: os.environ.get(k) for k in ("AUTOFLLOW_ENV", "AUTOFLLOW_DATA_DIR")}
    os.environ["AUTOFLLOW_ENV"] = "staging"
    os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_fr10_")
    reset_config()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_config()  # env 已还原 → config 单例退回默认，避免泄漏到后续测试


def _fresh_mgr(gateway=None):
    d = tempfile.mkdtemp(prefix="af_fr10mgr_")
    return ArenaManager(d, gateway=gateway if gateway is not None else object())


def _store(*rows):
    from autoflow_gateway import vhass as VH
    st = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    st.areas = seed["areas"]
    st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
    return st


# ══════════════════════════════════════════════════════════════════
# F-R10-GATE-01：可控设备状态触发 × 种子反态覆盖
# ══════════════════════════════════════════════════════════════════

def test_fr10_gate01_switch_trigger_produces_intents():
    """t07 场景：触发实体 switch 本身被过度推断为断言目标（期望 on）。

    种子覆盖（反态 off）若按旧序后置覆盖触发实体 → 触发态被抹 → 分支判不中 →
    0 意图 + 断言失败。修复后：触发事件后注入者胜（世界=on）→ 分支激活 →
    light.turn_on 被重放；switch 的「期望 on」断言与触发事件同态，剔除并告警。
    """
    from autoflow_gateway import gateway as G

    GW = G.Gateway()
    GW.state.add_mapping("switch.study_sw", "switch.study_sw")
    GW.state.add_mapping("light.study", "light.study")

    st = _store(("switch.study_sw", "书房开关", "书房", "off", {}),
                ("light.study", "书房台灯", "书房", "off", {}))

    dsl = ('场景: 开关开灯\n'
           '触发: switch.study_sw on\n'
           '动作: light.turn_on(light.study)\n')
    # 第二条是「过度推断」的世界态断言（R10 t07 实况）；
    # 期望 on → 种子反态 off（与触发态同值 off）
    expected = [{"entity_id": "light.study", "state": "on"},
                {"entity_id": "switch.study_sw", "state": "on"}]
    g = GW.run_staging_gate(dsl, expected, vhass_store=st, branch_aware=True,
                            seed_overrides={"light.study": "off",
                                            "switch.study_sw": "off"})
    assert g["passed"] is True, g
    assert g["fully_verified"] is True, g
    assert any("light.turn_on(light.study)" in r for r in g["replayed_services"]), g
    # 触发保真：触发事件最后落定，世界=on（旧实现此处为 off）
    assert st.get_state("switch.study_sw")["state"] == "on", st.get_state("switch.study_sw")
    # 世界态断言（与触发事件同态）被剔除，且有显式告警
    assert not [a for a in g["assertions"]
                if a.get("entity_id") == "switch.study_sw"], g["assertions"]
    assert any("触发态断言剔除" in w for w in g["warnings"]), g["warnings"]


def test_fr10_gate01_different_state_assertion_kept():
    """期望态 **不同于** 触发态时不断言剔除（如「开关开 → 关灯」族的触发实体翻转题）。

    这里验证剔除规则只吃「同态」断言：构造期望 switch=off（触发态 on）→
    断言保留，且因触发实体种子前置为反态 on→…——本例只验证断言未被剔除。
    """
    from autoflow_gateway import gateway as G

    GW = G.Gateway()  # 经 autouse fixture 获取隔离 data dir
    GW.state.add_mapping("switch.study_sw", "switch.study_sw")
    GW.state.add_mapping("light.study", "light.study")

    st = _store(("switch.study_sw", "书房开关", "书房", "off", {}),
                ("light.study", "书房台灯", "书房", "off", {}))
    dsl = ('场景: 开关开灯\n'
           '触发: switch.study_sw on\n'
           '动作: light.turn_on(light.study)\n')
    expected = [{"entity_id": "light.study", "state": "on"},
                {"entity_id": "switch.study_sw", "state": "off"}]  # ≠ 触发态 on → 保留
    g = GW.run_staging_gate(dsl, expected, vhass_store=st, branch_aware=True,
                            seed_overrides={"light.study": "off",
                                            "switch.study_sw": "on"})
    assert any(a.get("entity_id") == "switch.study_sw" for a in g["assertions"]), g
    assert not any("触发态断言剔除" in w for w in g["warnings"]), g["warnings"]


# ══════════════════════════════════════════════════════════════════
# F-R10-B1-01：agent_id 强制校验
# ══════════════════════════════════════════════════════════════════

def test_fr10_b101_submit_rejects_empty_agent_id():
    mgr = _fresh_mgr()
    r = mgr.submit_flow("study_room", "task_x", "场景: x\n触发: inject\n动作: light.a turn_on", "")
    assert r["ok"] is False and "agent_id" in str(r.get("error")), r
    # 防御顺序：agent_id 校验先于题目查找（避免空归属路径先吃掉任务状态）


# ══════════════════════════════════════════════════════════════════
# F-R10-T0：inject-only 触发保真降级（arena 层，题面上下文）
# ══════════════════════════════════════════════════════════════════

class _RecGW:
    def __init__(self):
        self.calls = []

    def propose_dsl(self, **kw):
        self.calls.append(kw)
        return {"ok": True, "gate": {"passed": True, "fully_verified": True,
                                     "verdict": "放行"}}


def _task(title, desc):
    return {"id": "t1", "arena_id": "study_room", "title": title,
            "description": desc,
            "entity_ids": ["light.desk_lamp", "switch.desk_sw"]}


def test_fr10_t0_inject_only_state_task_degrades():
    gw = _RecGW()
    d = tempfile.mkdtemp(prefix="af_fr10t0_")  # ArenaManager 显式路径，不经 env
    mgr = ArenaManager(d, gateway=gw)
    task = _task("开关开灯", "当书房开关打开时，自动打开书房台灯。")
    dsl = ('场景: 开灯\n触发: inject(payload={"cmd":"开灯"})\n'
           '动作: light.turn_on(light.desk_lamp)\n')
    r = mgr._verify_flow("study_room", task, dsl, "a1")
    assert r["ok"] is True
    assert r["gate"]["fully_verified"] is False, r["gate"]
    assert any("触发保真降级" in w for w in r["gate"]["warnings"]), r["gate"]


def test_fr10_t0_state_trigger_dsl_not_degraded():
    gw = _RecGW()
    d = tempfile.mkdtemp(prefix="af_fr10t0b_")  # ArenaManager 显式路径，不经 env
    mgr = ArenaManager(d, gateway=gw)
    task = _task("开关开灯", "当书房开关打开时，自动打开书房台灯。")
    dsl = ('场景: 开灯\n触发: switch.desk_sw on\n'
           '动作: light.turn_on(light.desk_lamp)\n')
    r = mgr._verify_flow("study_room", task, dsl, "a1")
    assert r["gate"]["fully_verified"] is True, r["gate"]


def test_fr10_t0_manual_task_inject_not_degraded():
    """真·手动题（题面无状态触发信号词）用 inject 不降级，不误伤。"""
    gw = _RecGW()
    d = tempfile.mkdtemp(prefix="af_fr10t0c_")  # ArenaManager 显式路径，不经 env
    mgr = ArenaManager(d, gateway=gw)
    task = _task("一键开灯", "手动一键打开书房台灯。")
    dsl = ('场景: 开灯\n触发: inject(payload={"cmd":"开灯"})\n'
           '动作: light.turn_on(light.desk_lamp)\n')
    r = mgr._verify_flow("study_room", task, dsl, "a1")
    assert r["gate"]["fully_verified"] is True, r["gate"]


# ══════════════════════════════════════════════════════════════════
# F-R10-KB-01：经验库分区隔离开关
# ══════════════════════════════════════════════════════════════════

def test_fr10_kb01_default_shared_and_isolated_switch(monkeypatch):
    mgr = _fresh_mgr()
    # 缺省：全局共享（旧行为）
    assert mgr._kb_store_for("lr_arm_a") is mgr._error_kb
    monkeypatch.setenv("AUTOFLOW_ARENA_KB_ISOLATION", "1")
    sa = mgr._kb_store_for("lr_arm_a")
    sb = mgr._kb_store_for("lr_arm_b")
    assert sa is not sb
    assert sa is not mgr._error_kb
    # 无分区标识 → 回全局
    assert mgr._kb_store_for("") is mgr._error_kb
    # A 臂写入的经验对 B 臂不可见（隔离生效）
    sa.record(dsl="dsl-a", error_msg="boom-isolated", stage="gate_rejected",
              agent_id="arm-a")
    data_b = sb._load()
    assert all("boom-isolated" not in (e.get("error") or "")
               for e in data_b.get("errors", []))
