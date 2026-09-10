# -*- coding: utf-8 -*-
"""F-R8-01/02/03/04/05 守卫：竞技场「清题」主刀 + 配套修复。

R8 实况（docs/04_test/findings-ledger.md §B）：13 道 challenge 题团队判「死路」。
负责人复核后定性——不是死路，是**四个可修的产品缺陷**：

- **F-R8-04（P1，主根因）**：静态种子极性冲突。种子文件被手工调成「灯=off」，
  于是「关门关灯」族的期望(off) 与种子态(off) 同态 → `pre_satisfied` →
  `fully_verified=false` → 不落锁。单一静态种子无法同时服务 turn_on / turn_off
  两类题。**修法**：验收入口按断言目标把种子翻成**反态**（网关新增 seed_overrides
  通道，在触发注入之后、重放采样之前生效）。
- **F-R8-02（P2）**：期望推断把**提交的 DSL** 也算进去 → flow 自证语义
  （题面说「开学习灯」但表里没 open 词 → 提交 turn_off 反而让期望推成 off →
  反向 flow 通过）。**修法**：只从题面推导 + 最右方向标记（解触发侧反向动词）+ 扩充关键词。
- **F-R8-03（P2）**：`entity_ids` 只列传感器 → 推不出动作目标 → 零断言死题。
  **修法**：propose 要求 entity_ids 至少含 1 个可控域设备。
- **F-R8-05（P3）**：LLM 考官把「环境量触发执行器」当因果谬误误拒（传感器驱动
  执行器是家居自动化主流形态）。**修法**：校准 prompt——只有环境量出现在**动作侧**才算硬伤。

全程离线、零运行时副作用。运行：pytest tests/test_fr8_arena_lockable.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
os.environ["AUTOFLLOW_DATA_DIR"] = tempfile.mkdtemp(prefix="af_fr8_")

import pytest

from autoflow_gateway.arena import (ArenaManager, _infer_direction_from_text,
                                    _reverse_state_for, _CONTROLLABLE_DOMAINS)
from autoflow_gateway.config import reset_config

reset_config()

DEVICES = [
    {"entity_id": "light.study", "friendly_name": "台灯", "state": "off", "domain": "light"},
    {"entity_id": "sensor.temp", "friendly_name": "书房温度", "state": "24.5", "domain": "sensor"},
    {"entity_id": "climate.ac", "friendly_name": "书房空调", "state": "off", "domain": "climate"},
]


def _fresh_mgr(gateway=None):
    d = tempfile.mkdtemp(prefix="af_fr8mgr_")
    return ArenaManager(d, gateway=gateway if gateway is not None else object())


# ══════════════════════════════════════════════════════════════════
# F-R8-04：验收入口按断言目标自动翻种子为反态
# ══════════════════════════════════════════════════════════════════

def test_fr804_reverse_state_table():
    """`_reverse_state_for`：给定期望态 → 反态；非可控域/空值 → None（不翻）。"""
    assert _reverse_state_for("light.x", "on") == "off"
    assert _reverse_state_for("light.x", "off") == "on"
    assert _reverse_state_for("media_player.x", "off") == "playing"
    assert _reverse_state_for("media_player.x", "playing") == "off"
    assert _reverse_state_for("climate.x", "off") == "on"
    assert _reverse_state_for("cover.x", "closed") == "open"
    assert _reverse_state_for("lock.x", "locked") == "unlocked"
    # 传感器是只读的：没有「反态」概念，绝不翻
    assert _reverse_state_for("sensor.x", "on") is None
    assert _reverse_state_for("binary_sensor.x", "on") is None
    assert _reverse_state_for("light.x", "") is None
    assert _reverse_state_for("light.x", None) is None


def test_fr804_gate_seed_overrides_makes_reversal_verifiable():
    """核心：种子=off、期望=off、flow=turn_off。

    不给覆盖 → 前置已满足（pre_satisfied）→ fully_verified=false（R8 死路现象）。
    给覆盖（种子翻成 on）→ 重放真的把状态翻回 off → changed_by_replay=true →
    fully_verified=true（可落锁）。
    """
    from autoflow_gateway import gateway as G
    from autoflow_gateway import vhass as VH

    data = tempfile.mkdtemp(prefix="af_fr804_")
    os.environ["AUTOFLLOW_DATA_DIR"] = data
    reset_config()
    GW = G.Gateway()
    GW.state.add_mapping("light.study_main", "light.study_main")

    def _store(state):
        rows = (("light.study_main", "书房主灯", "书房", state, {}),)
        st = VH.VHassStore()
        seed = VH.build_seed_from_entities(rows)
        st.areas = seed["areas"]
        st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}
        return st

    dsl = ('场景: 关灯\n'
           '触发: inject(payload={"cmd":"关灯"})\n'
           '动作: light.turn_off(light.study_main)\n')
    expected = [{"entity_id": "light.study_main", "state": "off"}]

    # ① 不给覆盖：R8 死路现象
    g0 = GW.run_staging_gate(dsl, expected, vhass_store=_store("off"), branch_aware=True)
    assert g0["passed"] is True, g0
    assert g0["fully_verified"] is False, g0          # ★ 不可落锁
    st0 = [a for a in g0["assertions"] if a.get("entity_id") == "light.study_main"][0]
    assert st0.get("pre_satisfied") is True and st0.get("changed_by_replay") is False, st0

    # ② 给覆盖：翻转成反态 → 可验证
    g1 = GW.run_staging_gate(dsl, expected, vhass_store=_store("off"), branch_aware=True,
                            seed_overrides={"light.study_main": "on"})
    assert g1["passed"] is True, g1
    assert g1["fully_verified"] is True, g1           # ★ 可落锁
    st1 = [a for a in g1["assertions"] if a.get("entity_id") == "light.study_main"][0]
    assert st1.get("changed_by_replay") is True, st1
    assert st1.get("pre_state") == "on", st1
    assert g1.get("seed_overrides") == {"light.study_main": "on"}, g1.get("seed_overrides")


def test_fr804_seed_overrides_never_creates_ghost_entity():
    """覆盖只作用于 store 中**已存在**的实体——不得借道创建幽灵实体。"""
    from autoflow_gateway import gateway as G
    from autoflow_gateway import vhass as VH

    data = tempfile.mkdtemp(prefix="af_fr804g_")
    os.environ["AUTOFLLOW_DATA_DIR"] = data
    reset_config()
    GW = G.Gateway()
    GW.state.add_mapping("light.study_main", "light.study_main")
    st = VH.VHassStore()
    seed = VH.build_seed_from_entities((("light.study_main", "书房主灯", "书房", "off", {}),))
    st.areas = seed["areas"]
    st.entities = {e["entity_id"]: VH.VHassStore._normalize(e) for e in seed["entities"]}

    dsl = ('场景: 开灯\n触发: inject(payload={"cmd":"开灯"})\n'
           '动作: light.turn_on(light.study_main)\n')
    GW.run_staging_gate(dsl, [{"entity_id": "light.study_main", "state": "on"}],
                        vhass_store=st, branch_aware=True,
                        seed_overrides={"light.does_not_exist": "on"})
    assert st.get_state("light.does_not_exist") is None, "不得创建幽灵实体"


def test_fr804_arena_verify_flow_wires_seed_overrides():
    """接线守卫：`_verify_flow` 必须把「期望的反态」作为 seed_overrides 传给闸门。"""
    class _RecGW:
        def __init__(self):
            self.calls = []

        def propose_dsl(self, **kw):
            self.calls.append(kw)
            return {"ok": True, "gate": {"passed": True, "fully_verified": True,
                                        "verdict": "放行"}}

    d = tempfile.mkdtemp(prefix="af_fr804w_")
    os.environ["AUTOFLLOW_DATA_DIR"] = d
    reset_config()
    gw = _RecGW()
    mgr = ArenaManager(d, gateway=gw)
    arena = mgr._get_arena("study_room")          # DEFAULT_ARENAS 占位种子
    mgr._rewrite_seed("study_room", arena)        # 把 arena.devices 落成 vhass 种子

    task = {"id": "t1", "arena_id": "study_room", "title": "关门自动关闭台灯",
            "description": "当书房门窗传感器检测到门关闭时，自动关闭书房台灯。",
            "entity_ids": ["light.desk_lamp", "binary_sensor.study_motion"]}
    mgr._verify_flow("study_room", task, "动作: light.turn_off(light.desk_lamp)", "a1")

    assert gw.calls, "闸门未被调用"
    call = gw.calls[0]
    assert call.get("expected_postconditions") == [
        {"entity_id": "light.desk_lamp", "state": "off"}], call.get("expected_postconditions")
    # 期望 off → 种子必须被翻成 on（反态）
    assert call.get("seed_overrides") == {"light.desk_lamp": "on"}, call.get("seed_overrides")


def test_fr804_seed_health_marks_auto_corrected():
    """种子健康检查里被自动翻转的 pre_satisfied_seed 项要标 auto_corrected，避免误导。"""
    class _RecGW:
        def __init__(self):
            self.calls = []

        def propose_dsl(self, **kw):
            self.calls.append(kw)
            return {"ok": True, "gate": {"passed": True, "fully_verified": True}}

    d = tempfile.mkdtemp(prefix="af_fr804h_")
    os.environ["AUTOFLLOW_DATA_DIR"] = d
    reset_config()
    mgr = ArenaManager(d, gateway=_RecGW())
    arena = mgr._get_arena("study_room")
    # 强制把台灯种子置 off（R8 实况：种子被手工调成 off）
    for dev in arena["devices"]:
        if dev["entity_id"] == "light.desk_lamp":
            dev["state"] = "off"
    mgr._save_json(mgr.arenas_file, {"arenas": mgr._load_arenas()})
    mgr._rewrite_seed("study_room", arena)

    task = {"id": "t2", "arena_id": "study_room", "title": "关门自动关闭台灯",
            "description": "门关闭时自动关闭书房台灯。",
            "entity_ids": ["light.desk_lamp"]}
    r = mgr._verify_flow("study_room", task, "动作: light.turn_off(light.desk_lamp)", "a2")
    issues = ((r or {}).get("seed_health") or {}).get("issues") or []
    hit = [i for i in issues if i.get("issue") == "pre_satisfied_seed"]
    assert hit and hit[0].get("auto_corrected") is True, issues
    assert hit[0].get("corrected_seed_state") == "on", hit[0]


# ══════════════════════════════════════════════════════════════════
# F-R8-02：期望推断只从题面来（flow 不得自证）+ 最右方向标记
# ══════════════════════════════════════════════════════════════════

def test_fr802_dsl_cannot_self_justify():
    """核心：题面说「开学习灯」，即使提交 turn_off 的 DSL，期望也必须是 on。"""
    mgr = _fresh_mgr()
    task = {"id": "task_c9d4", "title": "人在书房且门关闭时开学习灯",
            "description": "当书房人在书房且门关闭时，自动执行对应操作。",
            "entity_ids": ["light.study"]}
    exp = mgr._infer_postconditions(
        task, "触发: x off\n动作: light.turn_off(light.study)")
    assert exp == [{"entity_id": "light.study", "state": "on"}], exp


def test_fr802_title_authoritative_over_description():
    """标题与描述方向冲突时以标题为准（描述含目的从句/反向动词不翻转）。"""
    mgr = _fresh_mgr()
    task = {"id": "task_a", "title": "人离开书房延时20分钟再关空调电脑",
            "description": "当人在传感器显示书房无人时，先等待 20 分钟确认不是短暂离开，"
                           "再自动关闭书房空调和电脑。延时缓冲是为避免有人短暂出门"
                           "导致空调电脑被误关、回来还要重新启动空调和电脑。",
            "entity_ids": ["climate.ac", "switch.pc"]}
    exp = mgr._infer_postconditions(task, "触发: x off\n动作: climate.turn_on(climate.ac)")
    by_id = {e["entity_id"]: e["state"] for e in exp}
    assert by_id.get("climate.ac") == "off", exp       # 「离开」里的 开 不是动作
    assert by_id.get("switch.pc") == "off", exp


def test_fr802_trigger_side_verb_does_not_flip():
    """触发侧反向动词不污染动作方向：门「打开」→ 关灯 应是 off。"""
    mgr = _fresh_mgr()
    task = {"id": "task_e398", "title": "门开即关书房台灯",
            "description": "当书房门窗传感器检测到门打开时，立即关闭书房台灯以节约电能。",
            "entity_ids": ["light.study"]}
    exp = mgr._infer_postconditions(task)
    assert exp == [{"entity_id": "light.study", "state": "off"}], exp


def test_fr802_no_direction_returns_empty():
    """题面完全无方向信号 → 返回空断言集（不猜，闸门按零断言 fail-closed）。"""
    mgr = _fresh_mgr()
    task = {"id": "task_junk", "title": "历史分支取证2", "description": "T12 取证2",
            "entity_ids": ["light.study"]}
    assert mgr._infer_postconditions(task, "动作: light.turn_on(light.study)") == []


@pytest.mark.parametrize("title,expect", [
    # R8 真实题库（13 道待清题）逐条回归方向判定
    ("关门自动关闭显示器挂灯", "close"),
    ("空调今日运行时长超3小时自动关闭", "close"),
    ("人在书房且门关闭时开学习灯", "open"),       # 旧实现在此推成 off（F-R8-02 实锤）
    ("高湿环境自动关闭学习灯防空潮", "close"),
    ("有人进入书房时自动打开电脑", "open"),
    ("人离开书房后延时关闭台灯", "close"),        # 「离开」含 开，不得误判 open
    ("开门延时关闭书房台灯", "close"),            # 「开门」含 开，最右是 关闭
    ("门开即关书房台灯", "close"),
    ("关门自动关闭书房台灯防误关", "close"),      # 最末的「误关」也是 close
    ("人在书房且温度过低自动关闭空调", "close"),
    ("工作时段光照不足并行开启台灯与挂灯", "open"),
    ("有人进书房自动开灯", "open"),
    ("光照不足开显示器挂灯", "open"),
    ("关灯", "close"),
    ("打开电视看新闻", "open"),
    ("湿度回落退出除湿", None),                   # 标题无方向 → None（描述兜底在调用方）
    ("", None),
])
def test_fr802_direction_table(title, expect):
    assert _infer_direction_from_text(title) == expect, title


def test_fr802_media_player_playing_kept():
    """回归：media_player 语义态仍是 playing/off（F-R7-03 不退化）。"""
    mgr = _fresh_mgr()
    t1 = {"title": "打开电视看新闻", "description": "当有人时打开电视并播放新闻。",
          "entity_ids": ["media_player.tv"]}
    assert mgr._infer_postconditions(t1) == [{"entity_id": "media_player.tv", "state": "playing"}]
    t2 = {"title": "关闭电视", "description": "当门关闭时关闭电视。",
          "entity_ids": ["media_player.tv"]}
    assert mgr._infer_postconditions(t2) == [{"entity_id": "media_player.tv", "state": "off"}]


# ══════════════════════════════════════════════════════════════════
# F-R8-03：entity_ids 必须含可控设备
# ══════════════════════════════════════════════════════════════════

def test_fr803_reject_sensor_only_entity_ids():
    """只有传感器的题 → 零断言死题 → 拒题。"""
    mgr = _fresh_mgr()
    r = mgr.propose_task(
        "study_room", "门窗打开时提醒我", "当书房门窗传感器检测到门打开时，推送一条提醒消息。",
        ["binary_sensor.study_motion", "sensor.study_temperature"], "agent-fr803")
    assert r.get("ok") is False, r
    assert r.get("reason") == "no_controllable_device", r


def test_fr803_accept_task_with_controllable_device():
    """含可控设备的题正常受理（不被新守卫误杀）。"""
    mgr = _fresh_mgr()
    r = mgr.propose_task(
        "study_room", "有人进书房自动开台灯",
        "当书房人体感应检测到有人进入时，自动打开书房台灯并调到阅读亮度。",
        ["light.desk_lamp", "binary_sensor.study_motion"], "agent-fr803b")
    assert r.get("ok") is True, r
    assert r.get("task_id"), r


def test_fr803_controllable_domains_cover_expected_set():
    for dom in ("light", "switch", "climate", "fan", "media_player", "cover", "input_boolean"):
        assert dom in _CONTROLLABLE_DOMAINS, dom
    for dom in ("sensor", "binary_sensor"):
        assert dom not in _CONTROLLABLE_DOMAINS, dom


# ══════════════════════════════════════════════════════════════════
# F-R8-05：LLM 考官不得把「环境量触发执行器」当因果谬误
# ══════════════════════════════════════════════════════════════════

def test_fr805_llm_prompt_allows_env_trigger_cross_domain(monkeypatch):
    llm_client = pytest.importorskip("autoflow_gateway.llm_client")
    captured = {}

    def _fake_chat(messages, **kw):
        captured["prompt"] = messages[0]["content"]
        return '{"logic_ok": true, "issues": [], "novelty": 0.4}'

    monkeypatch.setattr(llm_client, "chat_sync", _fake_chat, raising=False)
    from autoflow_gateway.arena import _llm_logic_review

    r = _llm_logic_review(
        "高湿环境自动关闭学习灯防空潮",
        "当书房湿度传感器检测到相对湿度超过 70% 时，自动关闭米家桌面学习灯。",
        ["light.study", "sensor.humidity"], DEVICES)
    assert r and r["logic_ok"] is True, r
    prompt = captured.get("prompt", "")
    # 旧的错误绝对规则（环境量只能同域）必须已被移除
    assert "环境量只能因果关联同域设备" not in prompt, prompt
    # 新的正确口径：只有动作侧不可控才算硬伤
    assert "动作侧" in prompt, prompt
    assert "不得" in prompt, prompt
