#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.3.0 任务 3.2 / DCD 议题七 A：deploy_proposal / deploy_raw 吃 ref（闭环零重传）。

验证：
  - deploy_proposal(ref=...) 凭 propose_dsl 返回的 ref 取回关联提案（与 pid 二选一）；
  - deploy_raw(ref=...) 凭 ref 取回暂存 flow，免去 agent 重传整份 flow；
  - 两者对「ref 不存在/已过期」fail-closed 直接报错，绝不触发 NR 快照等副作用；
  - deploy_proposal(ref=...) 的草稿若未关联提案，明确报错（而非误部署）。

零依赖运行：python tests/test_deploy_ref.py
"""
import os
import sys
import tempfile

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_tmp = tempfile.mkdtemp(prefix="af_deploy_ref_test_")
os.environ["AUTOFLLOW_DATA_DIR"] = _tmp

from autoflow_gateway import gateway as G
from autoflow_gateway import vhass as VH
from autoflow_gateway.config import reset_config
from autoflow_gateway.draft_store import stage_draft, get_draft

reset_config()
GW = G.Gateway()
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("客厅主灯", "light.living_room_main")
for _eid in ("light.study_main", "light.living_room_main",
             "binary_sensor.study_door", "device_tracker.me"):
    GW.state.add_mapping(_eid, _eid)


def _vhass_with(*rows):
    store = VH.VHassStore()
    seed = VH.build_seed_from_entities(rows)
    store.areas = seed["areas"]
    store.entities = {}
    for e in seed["entities"]:
        store.entities[e["entity_id"]] = VH.VHassStore._normalize(e)
    return store


DSL_OK = """场景: 书房入户播报3
触发: binary_sensor.study_door 有人
动作: light.turn_on(书房主灯, brightness=80)
调用子流程: demo_notify(text=欢迎进入书房, room=书房, level=一般)
"""


# ── 失败闭环（fail-closed，无需 NR） ──────────────────────────────────────────

def test_deploy_proposal_bad_ref():
    # ref 不存在/已过期 → 直接报错，绝不触发 NR 快照（解析置于快照之前）
    r = GW.deploy_proposal(pid=None, ref="af:deadbeef")
    assert r["ok"] is False, r
    assert r["stage"] == "ref", r
    assert "不存在或已过期" in r["error"], r


def test_deploy_proposal_ref_without_proposal_id():
    # 草稿未关联提案（如纯 raw 暂存）→ 明确报错，而非误部署
    ref = stage_draft({"flow": {"label": "x", "nodes": []}})  # 无 proposal_id
    assert get_draft(ref) is not None
    r = GW.deploy_proposal(pid=None, ref=ref)
    assert r["ok"] is False, r
    assert r["stage"] == "ref", r
    assert "未关联提案" in r["error"], r


def test_deploy_proposal_needs_pid_or_ref():
    # 既不给 pid 也不给 ref → 明确报错
    r = GW.deploy_proposal(pid=None, ref=None)
    assert r["ok"] is False and r["stage"] == "ref", r


def test_deploy_raw_bad_ref():
    # ref 不存在/已过期 → 直接报错（置于 _slog/校验之前），无需 NR
    r = GW.deploy_raw(flow_json=None, ref="af:deadbeef")
    assert r["ok"] is False, r
    assert r["stage"] == "ref", r
    assert "不存在或已过期" in r["error"], r


# ── 正向：ref 解析后把正确 flow 喂给部署 ─────────────────────────────────────

class _FakeNR:
    def __init__(self):
        self.captured = None

    def take_instance_snapshot(self, name):
        return None

    def list_flows(self):
        return []

    def create_or_update_flow(self, flow_id, flow, *args, **kw):
        self.captured = flow
        return {"id": flow.get("id") or "gen-id", "rev": "1"}

    def get_flow(self, fid):
        return {"nodes": []}


class _FakeDefense:
    def check_write(self, **kw):
        return None


def test_deploy_raw_by_ref_resolves_flow():
    # propose_dsl 落档并暂存 → 返回 ref；deploy_raw(ref=) 凭 ref 取回 flow 部署，
    # 全程不重传整份 flow。NR/defense 用假对象，避免依赖真实 Node-RED。
    store = _vhass_with(
        ("light.study_main", "书房主灯", "书房", "off", {}),
        ("binary_sensor.study_door", "书房门", "书房", "off", {}),
    )
    res = GW.propose_dsl(DSL_OK, "agent_test",
                         [{"entity_id": "light.study_main", "state": "on"}],
                         vhass_store=store)
    assert res["ok"] and res["ref"].startswith("af:"), res
    ref = res["ref"]
    expected_label = res["flow"]["label"]

    fake_nr = _FakeNR()
    GW.nr = fake_nr
    GW.defense = _FakeDefense()
    # 这两步可能触达 NR，单测里直接置为 no-op，聚焦验证 ref→flow 解析链路。
    GW._ensure_history_subflow_for = lambda *a, **k: None
    GW._inject_ha_server = lambda flow: ([], [])

    r = GW.deploy_raw(flow_json=None, ref=ref, run_gate=False,
                      block_on_lint_error=False, block_on_schema_error=False)
    assert r["ok"] is True, r
    assert fake_nr.captured is not None, "deploy_raw 未把 flow 部署出去"
    assert fake_nr.captured["label"] == expected_label, (
        f"部署的 flow 与 ref 暂存的不一致: {fake_nr.captured.get('label')} != {expected_label}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa
            fail += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if fail else 0)
