"""Test apply_flow / apply_rollback（WB1-F / #694）：自愈闭环（Self-Healing Loop）编排核心。

覆盖铁律：
  - mode A/C（改 flow，高风险）：**默认自动写回**（不进人审闸），先落回滚点再 modify_flow；
    受 per-(agent, flow) 滑动窗口失败预算（自愈重试次数，默认 3）有界保护，耗尽即停止
    （stage=selfheal_budget_exhausted）并转报告/人工，防自动修复死循环；中间一次成功即清零计数；
  - mode B（落状态，低风险）：本层 audit auto-pass，透传 commit_ha_service（其自带确认闸），
    全程不碰 flow，不计入自愈预算；
  - #607：目标 tab 禁用态 → tab_disabled + 显式告警（不阻塞）；
  - 回滚：apply_rollback(trace_id) 从 apply 前快照还原，默认自动执行、计入同一自愈预算；
  - 审计：同一 trace_id 复用同一回滚点，轨迹可追。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
import pytest
from autoflow_gateway import gateway as gwmod
from autoflow_gateway.gateway import Gateway


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """隔离的 Gateway：轨迹目录落 tmp，NR / 决策闸 / 快照全部替身，绝不触真机。"""
    g = Gateway()
    monkeypatch.setattr(gwmod, "_apply_trace_dir", lambda: str(tmp_path / "apply_traces"))
    return g


def _base_flow(disabled=False):
    return {
        "id": "f_apply", "label": "书房迎宾", "disabled": disabled,
        "nodes": [
            {"id": "n1", "type": "inject", "z": "f_apply", "wires": [["n2"]]},
            {"id": "n2", "type": "api-call-service", "z": "f_apply",
             "name": "开灯", "wires": [[]]},
        ],
    }


@pytest.fixture
def stub(gw, tmp_path, monkeypatch):
    """把 apply 依赖的四个外部动作换成可观测替身，返回调用记录。"""
    calls = {"modify": [], "commit": [], "decision": [], "deploy": []}

    monkeypatch.setattr(gw.nr, "get_flow", lambda fid: _base_flow())

    def _modify(flow_id, dsl=None, node_patches=None, agent_id="x", force=False,
                allow_prod=False):
        calls["modify"].append({"flow_id": flow_id, "dsl": dsl,
                                "node_patches": node_patches, "agent_id": agent_id,
                                "allow_prod": allow_prod})
        return {"ok": True, "flow_id": flow_id, "label": "书房迎宾",
                "changed_nodes": 1, "node_count": 2, "mode": "node_patches"}
    monkeypatch.setattr(gw, "modify_flow", _modify)

    def _commit(domain, service, data, agent_id):
        calls["commit"].append({"domain": domain, "service": service,
                                "data": data, "agent_id": agent_id})
        return {"ok": True, "pending_id": "pend-1", "risk": "low",
                "needs_approval": True, "blast_radius": 1}
    monkeypatch.setattr(gw, "commit_ha_service", _commit)

    def _decide(question, options, source="deepseek"):
        calls["decision"].append({"question": question, "options": options,
                                  "source": source})
        return {"ok": True, "decision": {"id": f"dec-{len(calls['decision'])}",
                                         "question": question, "options": options}}
    monkeypatch.setattr(gw, "request_decision", _decide)

    def _snap(agent_id, kind, label, flow, **kw):
        p = tmp_path / f"snap_{len(calls['deploy'])}_{kind}.json"
        p.write_text(json.dumps({"flow": flow, "label": label, **kw},
                                ensure_ascii=False), encoding="utf-8")
        return str(p)
    monkeypatch.setattr(gwmod, "snapshot_flow", _snap)

    def _deploy(flow_id, flow, force=False, allow_prod=False):
        calls["deploy"].append({"flow_id": flow_id, "flow": flow,
                                 "force": force, "allow_prod": allow_prod})
        return {"id": flow_id}
    monkeypatch.setattr(gw.nr, "create_or_update_flow", _deploy)
    monkeypatch.setattr(gw, "_gate_node_types", lambda f: None)
    return calls


# ───────────── 入参守卫 ─────────────

def test_bad_mode_rejected(gw, stub):
    r = gw.apply_flow("f_apply", {"dsl": "x"}, mode="Z")
    assert r["ok"] is False and r["stage"] == "input"
    assert "A/B/C" in r["error"]
    assert not stub["modify"] and not stub["decision"]


def test_mode_a_requires_correction(gw, stub):
    r = gw.apply_flow("f_apply", {"reason": "只写了理由"}, mode="A")
    assert r["ok"] is False and r["stage"] == "input"
    assert not stub["decision"]


def test_mode_a_requires_flow_id(gw, stub):
    r = gw.apply_flow("", {"dsl": "场景: x"}, mode="A")
    assert r["ok"] is False and "flow_id" in r["error"]


# ───────────── A/C 段：自愈闭环自动写回 ─────────────

def test_mode_a_applies_without_approval(gw, stub):
    """自愈闭环：不传 auto_approve 也直接写回（无闸），返回 applied/pending=False。"""
    r = gw.apply_flow("f_apply", {"dsl": "场景: 修正版", "reason": "观测到灯没亮"},
                      mode="A", agent_id="wb1")
    assert r["ok"] is True
    assert r["applied"] is True and r["pending"] is False
    assert r["stage"] == "modify_flow" and r["gate"] == "selfheal_auto_write" and r["risk"] == "high"
    assert r["snapshot_path"] and os.path.exists(r["snapshot_path"])
    assert len(stub["modify"]) == 1            # 默认即写回
    assert stub["decision"] == []              # 不再进人审闸


def test_mode_a_approved_applies(gw, stub):
    r = gw.apply_flow("f_apply", {"dsl": "场景: 修正版", "reason": "r"},
                      mode="A", agent_id="wb1", auto_approve=True)
    assert r["ok"] is True and r["applied"] is True and r["pending"] is False
    assert r["stage"] == "modify_flow" and r["gate"] == "selfheal_auto_write"
    assert len(stub["modify"]) == 1
    assert stub["modify"][0]["dsl"] == "场景: 修正版"
    assert stub["decision"] == []          # 已批准不再重复请示


def test_mode_c_node_patches_applied(gw, stub):
    patches = [{"match": {"id": "n2"}, "set": {"name": "开灯v2"}}]
    r = gw.apply_flow("f_apply", {"node_patches": patches, "reason": "热补丁"},
                      mode="C", agent_id="cb", auto_approve=True)
    assert r["applied"] is True and r["mode"] == "C"
    assert stub["modify"][0]["node_patches"] == patches
    assert stub["modify"][0]["dsl"] is None


def test_modify_failure_surfaces_error(gw, stub, monkeypatch):
    monkeypatch.setattr(gw, "modify_flow",
                        lambda *a, **k: {"ok": False, "stage": "node_gate",
                                         "error": "未注册节点类型"})
    r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                      mode="C", auto_approve=True)
    assert r["ok"] is False and r["applied"] is False
    assert "未注册节点类型" in r["error"]


# ───────────── #607：禁用 tab 显式告警 ─────────────

def test_disabled_tab_warns_but_not_blocked(gw, stub, monkeypatch):
    monkeypatch.setattr(gw.nr, "get_flow", lambda fid: _base_flow(disabled=True))
    r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                      mode="C", auto_approve=True)
    assert r["tab_disabled"] is True
    assert any("#607" in w for w in r["warnings"])
    assert r["applied"] is True            # 告警不阻塞


def test_enabled_tab_has_no_607_warning(gw, stub):
    r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                      mode="C", auto_approve=True)
    assert r.get("tab_disabled") is not True
    assert not any("#607" in w for w in r["warnings"])


# ───────────── B 段：落状态 audit auto-pass ─────────────

def test_mode_b_auto_pass_to_commit(gw, stub):
    r = gw.apply_flow("", {"domain": "light", "service": "turn_on",
                           "data": {"entity_id": "light.study"},
                           "reason": "回读显示灯是关的"},
                      mode="B", agent_id="cb")
    assert r["ok"] is True and r["gate"] == "audit_auto_pass" and r["risk"] == "low"
    assert r["pending"] is True and r["pending_id"] == "pend-1"   # 由 commit 自带确认闸
    assert r["applied"] is False
    assert stub["commit"] == [{"domain": "light", "service": "turn_on",
                               "data": {"entity_id": "light.study"}, "agent_id": "cb"}]
    # B 段绝不碰 flow：不请示改流、不部署
    assert stub["decision"] == [] and stub["modify"] == [] and stub["deploy"] == []


def test_mode_b_missing_service_rejected(gw, stub):
    r = gw.apply_flow("", {"domain": "light"}, mode="B")
    assert r["ok"] is False and r["stage"] == "input"
    assert stub["commit"] == []


def test_mode_b_commit_failure_surfaces(gw, stub, monkeypatch):
    monkeypatch.setattr(gw, "commit_ha_service",
                        lambda *a, **k: {"ok": False, "errors": ["defense: 超出爆炸半径"]})
    r = gw.apply_flow("", {"domain": "light", "service": "turn_on", "data": {}}, mode="B")
    assert r["ok"] is False and "爆炸半径" in r["error"]


# ───────────── 审计轨迹 + 两阶段复用回滚点 ─────────────

def test_trace_persists_same_trace_id_reuses_rollback_point(gw, stub):
    """自愈闭环：同一 trace_id 两次 apply 复用同一回滚点（不再有 pending 阶段，默认即写回）。"""
    p1 = gw.apply_flow("f_apply", {"dsl": "场景: v2", "reason": "r"}, mode="A")
    tid, snap = p1["trace_id"], p1["snapshot_path"]
    p2 = gw.apply_flow("f_apply", {"dsl": "场景: v2", "reason": "r"}, mode="A",
                       auto_approve=True, trace_id=tid)
    assert p2["trace_id"] == tid and p2["applied"] is True
    tr = gwmod._read_apply_trace(tid)
    assert tr is not None
    assert len(tr["events"]) == 2
    assert tr["events"][0]["applied"] is True and tr["events"][1]["applied"] is True
    # 顶层回滚点取首个非空 → 仍指向 apply 前那一份
    assert tr["snapshot_path"] == snap
    assert tr["flow_id"] == "f_apply"


# ───────────── 回滚 ─────────────

def test_rollback_unknown_trace(gw, stub):
    r = gw.apply_rollback("ap_nope")
    assert r["ok"] is False and "找不到" in r["error"]


def test_rollback_restores_directly(gw, stub):
    """自愈闭环：apply_rollback 默认自动还原（无闸），写回快照里的原始节点。"""
    a = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"},
                                                    "set": {"name": "v2"}}],
                                  "reason": "热补丁"},
                      mode="C", auto_approve=True)
    tid = a["trace_id"]
    r = gw.apply_rollback(tid, agent_id="wb1")
    assert r["ok"] is True and r["restored"] is True and r["pending"] is False
    assert r["stage"] == "restored"
    assert len(stub["deploy"]) == 1
    dep = stub["deploy"][0]
    assert dep["flow_id"] == "f_apply" and dep["force"] is True
    assert [n["id"] for n in dep["flow"]["nodes"]] == ["n1", "n2"]
    assert dep["flow"]["nodes"][1]["name"] == "开灯"      # 补丁前的原值


def test_rollback_refuses_empty_snapshot(gw, stub, tmp_path, monkeypatch):
    monkeypatch.setattr(gwmod, "snapshot_flow",
                        lambda *a, **k: str(_write_empty(tmp_path)))
    a = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                      mode="C", auto_approve=True)
    r = gw.apply_rollback(a["trace_id"], auto_approve=True)
    assert r["ok"] is False and "空 flow" in r["error"]
    assert stub["deploy"] == []


def test_rollback_of_mode_b_has_no_rollback_point(gw, stub):
    b = gw.apply_flow("", {"domain": "light", "service": "turn_on", "data": {}}, mode="B")
    r = gw.apply_rollback(b["trace_id"], auto_approve=True)
    assert r["ok"] is False and "无法还原" in r["error"]


# ───────────── #701 fail-closed 透传（apply 编排层）─────────────

def test_mode_c_patch_nomatch_surfaces_failclosed(gw, stub, monkeypatch):
    """#701：node_patches 零匹配时 modify_flow 返回 ok:False/stage:patch，
    apply_flow 须如实透传（ok:False / applied:False），绝不谎报成功。"""
    monkeypatch.setattr(gw, "modify_flow",
                        lambda *a, **k: {"ok": False, "stage": "patch",
                                         "error": "node_patches 未匹配到任何节点",
                                         "unmatched": [{"match": {"id": "nope"}}],
                                         "available_nodes": []})
    r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "nope"}, "set": {}}]},
                      mode="C", auto_approve=True)
    assert r["ok"] is False and r["applied"] is False
    assert r["stage"] == "modify_flow"
    assert "未匹配" in r["error"]
    # 失败路径一个字节都不应写回
    assert stub["deploy"] == [] and stub["modify"] == []


def test_apply_recorded_in_trace(gw, stub):
    """审计完整性：apply 默认即写回，trace 记录 applied 事件，供 autoflow_get_trace 独立复核。"""
    r = gw.apply_flow("f_apply", {"dsl": "场景: v2", "reason": "r"}, mode="A")
    tr = gwmod._read_apply_trace(r["trace_id"])
    assert tr is not None
    assert tr["events"][0]["applied"] is True
    assert tr["events"][0]["pending"] is False


# ───────────── 自愈闭环：滑动窗口失败预算（selfheal_budget）─────────────

def _write_selfheal_budget(gw, tmp_path, val):
    """把自愈重试次数写进 feature_flags.json（模拟 WebUI 落盘），重定向 data_dir 避免污染仓库。"""
    monkeypatch_dir = str(tmp_path / "gwdata")
    gw.cfg.data_dir = monkeypatch_dir
    p = gw.cfg.feature_flags_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"selfheal_budget": val}, f)


def test_selfheal_budget_exhausted_then_blocked(gw, stub, tmp_path, monkeypatch):
    """连续 N 次写回失败 → 第 N+1 次被拒 stage=selfheal_budget_exhausted，且不再调 modify_flow。"""
    _write_selfheal_budget(gw, tmp_path, 3)
    modify_calls = []
    def _fail(*a, **k):
        modify_calls.append(1)
        return {"ok": False, "stage": "node_gate", "error": "未注册节点类型"}
    monkeypatch.setattr(gw, "modify_flow", _fail)
    for _ in range(3):
        r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                          mode="C", agent_id="wb1")
        assert r["ok"] is False and r["applied"] is False
    assert len(modify_calls) == 3
    r4 = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                       mode="C", agent_id="wb1")
    assert r4["ok"] is False and r4["applied"] is False
    assert r4["stage"] == "selfheal_budget_exhausted"
    assert r4.get("retry_budget") == 3
    assert r4.get("failed_attempts_in_window") == 3
    assert len(modify_calls) == 3          # 第 4 次被拒，未写回


def test_selfheal_budget_reset_on_success(gw, stub, tmp_path, monkeypatch):
    """中间一次成功 → 计数清零，后续失败不被立即耗尽（有界但非永久封锁）。"""
    _write_selfheal_budget(gw, tmp_path, 2)
    seq = {"n": 0}
    def _seq(*a, **k):
        seq["n"] += 1
        if seq["n"] == 2:
            return {"ok": True, "flow_id": "f_apply", "changed_nodes": 1}
        return {"ok": False, "stage": "node_gate", "error": "x"}
    monkeypatch.setattr(gw, "modify_flow", _seq)
    assert gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                         mode="C", agent_id="wb1")["ok"] is False   # 失败1
    assert gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                         mode="C", agent_id="wb1")["ok"] is True    # 成功→清零
    assert gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                         mode="C", agent_id="wb1")["ok"] is False   # 失败2
    assert gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                         mode="C", agent_id="wb1")["ok"] is False   # 失败3
    r5 = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                       mode="C", agent_id="wb1")
    assert r5["stage"] == "selfheal_budget_exhausted"   # 累计3次失败 >= 2 → 第5次被拒


def test_selfheal_budget_zero_disables_retry(gw, stub, tmp_path, monkeypatch):
    """selfheal_budget=0：一次失败即停（等同纯人审时代行为但无闸）。"""
    _write_selfheal_budget(gw, tmp_path, 0)
    modify_calls = []
    def _fail(*a, **k):
        modify_calls.append(1)
        return {"ok": False, "stage": "node_gate", "error": "x"}
    monkeypatch.setattr(gw, "modify_flow", _fail)
    r = gw.apply_flow("f_apply", {"node_patches": [{"match": {"id": "n2"}, "set": {}}]},
                      mode="C", agent_id="wb1")
    assert r["stage"] == "selfheal_budget_exhausted"
    assert len(modify_calls) == 0          # 0=禁用自主重试，连一次都不试


def _write_empty(tmp_path):
    p = tmp_path / "snap_empty.json"
    p.write_text(json.dumps({"flow": {"id": "f_apply", "nodes": []}}), encoding="utf-8")
    return p
