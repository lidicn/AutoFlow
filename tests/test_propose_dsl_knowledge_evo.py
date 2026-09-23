#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实网关接线守卫：propose_dsl 失败路径必须 (1) 记录本次失败 且 (2) 注入 prior_art。

本文件直接构造真实 Gateway 并调用 propose_dsl（而非 test_knowledge_evo 里的 _MiniGateway
复刻），确保 §3.4 缝在实际网关代码里真正接上 —— 一旦 _record_and_attach_prior_art 接线被
误删/回退，本测试立即变红（守卫有效，非假绿）。

覆盖三个确定性失败返回点：empty_dsl / compile / entity_check。
不依赖真实 NR/HA（AUTOFLLOW_ENV=staging + 临时 data_dir）。
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

os.environ.setdefault("AUTOFLLOW_ENV", "staging")
_TMP = tempfile.mkdtemp(prefix="af_evo_wire_")
os.environ["AUTOFLLOW_DATA_DIR"] = _TMP

from autoflow_gateway import gateway as G
from autoflow_gateway.config import reset_config

reset_config()
GW = G.Gateway()
GW.state.add_mapping("书房主灯", "light.study_main")
GW.state.add_mapping("light.study_main", "light.study_main")


def _evo_store_path():
    return os.path.join(_TMP, "error_knowledge", "error_knowledge.json")


def _errors():
    sp = _evo_store_path()
    if not os.path.exists(sp):
        return []
    return json.load(open(sp, encoding="utf-8")).get("errors", [])


def _assert_wired(res, expect_stage):
    assert res["ok"] is False and res["stage"] == expect_stage, res
    assert "prior_art" in res, res                       # (2) 注入 prior_art
    assert any(e.get("agent_id") == "agent_evo_wire"
               and e.get("stage") == expect_stage
               for e in _errors()), "失败未被记录落盘"     # (1) 记录落盘


def test_propose_dsl_empty_dsl_wires():
    _assert_wired(GW.propose_dsl("", "agent_evo_wire"), "empty_dsl")


def test_propose_dsl_compile_error_wires():
    _assert_wired(GW.propose_dsl("这不是合法dsl @@##", "agent_evo_wire"), "compile")


def test_propose_dsl_entity_check_wires():
    dsl = ("场景: 不存在实体测试\n"
           "触发: light.nonexistent_xyz 任意\n"
           "动作: light.turn_on(书房主灯)\n")
    _assert_wired(GW.propose_dsl(dsl, "agent_evo_wire"), "entity_check")


def test_propose_dsl_success_path_no_throw():
    """成功路径 P2 闭环接线必须 fail-safe：调用不抛、返回 dict。"""
    dsl = ("场景: 成功路径接线\n"
           "触发: light.study_main 任意\n"
           "动作: light.turn_on(书房主灯)\n")
    try:
        res = GW.propose_dsl(dsl, "agent_evo_wire")
    except Exception as e:  # 接线若未 fail-safe 会在此抛出
        raise AssertionError(f"propose_dsl 成功闭环接线未 fail-safe: {e}")
    assert isinstance(res, dict)
