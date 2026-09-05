"""C25-fix: DeviceGuardStore 零覆盖测试（用 tmp_path 隔离 data_dir，不污染真实 data/）。

对齐 src/autoflow_gateway/device_guard.py 的 DeviceGuardStore API：
  - DeviceGuardStore(cfg) 构造时落盘 device_guard.json 到 cfg.data_dir
  - list() -> 规则列表
  - upsert(rule) -> 返回落库完整规则（含自动 id）
  - delete(rid) -> 命中删除返回 True，否则 False
  - match_tier(entity_id, domain, area) -> 命中最高保护级 0/1，未命中 None
"""
import pytest

from autoflow_gateway.device_guard import DeviceGuardStore
from autoflow_gateway.config import get_config


def _cfg(tmp_path):
    cfg = get_config()
    cfg.data_dir = str(tmp_path)
    return cfg


def test_construct_then_empty_list(tmp_path):
    dg = DeviceGuardStore(_cfg(tmp_path))
    assert dg.list() == []


def test_upsert_and_readback(tmp_path):
    dg = DeviceGuardStore(_cfg(tmp_path))
    rule = {"match": {"type": "entity", "value": "light.kitchen"}, "tier": 0}
    rec = dg.upsert(rule)
    assert rec["id"].startswith("dg_")
    assert dg.list()[0]["id"] == rec["id"]


def test_delete_removes_rule(tmp_path):
    dg = DeviceGuardStore(_cfg(tmp_path))
    rec = dg.upsert({"match": {"type": "entity", "value": "x"}, "tier": 1})
    assert dg.delete(rec["id"]) is True
    assert dg.list() == []


def test_match_tier_hit_and_miss(tmp_path):
    dg = DeviceGuardStore(_cfg(tmp_path))
    dg.upsert({"match": {"type": "entity", "value": "light.a"}, "tier": 0})
    assert dg.match_tier("light.a") == 0
    assert dg.match_tier("nope.x") is None
