#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TEST-TICKET-005 V-CONTRACT-1/2 收口守卫（声明 vs 实际发射一致性）。

T005 S1 实测：4 个 history 子流程的「实际发射 msg.payload 顶层键」（compute.js 实跑 +
GET NR1990 live 对照）为：
  history_state_at : found, entity, at_iso, value, attribute, unit, nearest_ts, source
  history_occurred : occurred, entity, start_iso, end_iso, count, state, events, first_ts, last_ts
  history_duration : total_seconds, total_human, entity, start_iso, end_iso, state, ratio
  history_aggregate: value, unit, entity, start_iso, end_iso, metric, attribute, samples, error

守卫双重目标：
  1) V-CONTRACT-1/2 收口：history_state_at 必须声明 `source`、history_aggregate 必须声明
     `samples`/`error`，消除因「声明漏字段」导致的过度拦截（可用性回退）。
  2) 防未来反向漂移（over-declared）：声明集合必须是「实际发射键」的子集——若出现声明了
     实际不发射的字段，下游 switch 会把它当 reliable → 假绿盲区（V-NEW-3 信任面风险）。
     声明 ⊆ 实际 即锁死该方向。

不污染 prod / 不写回外部目录；纯仓库内静态断言。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.dont_write_bytecode = True

from autoflow_gateway import subflows as sf

# T005 S1 实测的实际发射键（compute.js 实跑 + live 对照），作为「真值」上界。
ACTUAL_EMIT = {
    "history_state_at": {"found", "entity", "at_iso", "value", "attribute",
                          "unit", "nearest_ts", "source"},
    "history_occurred": {"occurred", "entity", "start_iso", "end_iso", "count",
                          "state", "events", "first_ts", "last_ts"},
    "history_duration": {"total_seconds", "total_human", "entity", "start_iso",
                          "end_iso", "state", "ratio"},
    "history_aggregate": {"value", "unit", "entity", "start_iso", "end_iso",
                           "metric", "attribute", "samples", "error"},
}


def _declared(name):
    spec = sf.SUBFLOWS.get(name)
    assert spec is not None, f"子流程 {name} 未注册"
    return set(spec.outputs or [])


def test_vcontract1_history_state_at_declares_source():
    """V-CONTRACT-1：history_state_at 实际发射 source，声明必须包含之（消除过度拦截）。"""
    assert "source" in _declared("history_state_at"), \
        "history_state_at 声明漏 source → 依赖 payload.source 的流被过度拦截（V-CONTRACT-1）"


def test_vcontract2_history_aggregate_declares_samples_error():
    """V-CONTRACT-2：history_aggregate 实际发射 samples/error，声明必须包含之。"""
    decl = _declared("history_aggregate")
    assert "samples" in decl, \
        "history_aggregate 声明漏 samples → 依赖 payload.samples 的流被过度拦截（V-CONTRACT-2）"
    assert "error" in decl, \
        "history_aggregate 声明漏 error → 依赖 payload.error 的流被过度拦截（V-CONTRACT-2）"


def test_no_over_declared_drift():
    """反向守卫：声明集合 ⊆ 实际发射键，防 V-NEW-3 信任面引入 over-declared 假绿。"""
    for name, actual in ACTUAL_EMIT.items():
        decl = _declared(name)
        over = decl - actual
        assert not over, \
            f"{name} 声明了实际不发射的字段 {sorted(over)} → 下游误判 reliable → 假绿风险"
